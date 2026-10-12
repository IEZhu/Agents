"""The autodev gate (design 9.2): a pure function over a GitHub snapshot and the run records.

The gate starts no model and writes nothing. It returns a decision (``select``, ``resume`` or
``wait``) with a reason and the cleanup actions it needs; the runner's broker (#342) executes
those actions, and in ``dry-run`` nobody does.

The snapshot holds GitHub REST responses as they arrive (`src.schedules.github.fetch_snapshot`):

* ``claimed_issues``: open issues with ``autodev:in-progress`` (``GET /repos/R/issues``);
* ``issue_events``: per issue number, its events (``GET /repos/R/issues/N/events``);
* ``pulls``: open and recently closed pull requests (``GET /repos/R/pulls``);
* ``linked_issues``: per issue number, the issue (``GET /repos/R/issues/N``) that a closed
  self-development PR names by its branch and that is not among ``claimed_issues``, usually because
  the merge closed it; the claim label stays on a closed issue unless the gate removes it;
* ``pull_details``, ``issue_comments``, ``reviews``, ``review_comments``: per open
  self-development PR number, ``GET /repos/R/pulls/N``, ``/issues/N/comments``,
  ``/pulls/N/reviews`` and ``/pulls/N/comments``.

A self-development PR has the ``autodev`` label and a ``claude/autodev-<issue>-…`` branch of the
repository itself. Its state comment starts with ``<!-- autodev -->`` and then
``<!-- autodev:state {JSON} -->``, where the JSON holds ``head``, ``next_attempt``, ``rounds``,
``review_status`` and ``findings`` (``review:<id>`` and ``comment:<id>`` of bot findings already
seen). It counts only when its comment ID is in the runner's run records (design 5.2); copies,
however well formed, are ignored.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import re
from typing import Any, Iterable, Mapping

CLAIM_LABEL = "autodev:in-progress"
PR_LABEL = "autodev"
CLAIM_TTL = timedelta(hours=6)
WIP_LIMIT = 1
OWNER_ID = 5370211  # WonderMr (design 5.1)
# Allowlisted review bots by login and numeric account ID (design 5.2). Both must match.
TRUSTED_BOTS = {"coderabbitai[bot]": 136622811, "copilot-pull-request-reviewer[bot]": 175728472}
FINISHED = ("complete", "complete_with_followups")
_BRANCH = re.compile(r"^claude/autodev-(\d+)(?:-|$)")
_STATE = re.compile(r"\A<!-- autodev -->\r?\n<!-- autodev:state (\{.*?\}) -->", re.S)
STALE_CLAIM_COMMENT = ("<!-- autodev -->\nThe autodev claim on this issue is older than 6 hours and no pull "
                       "request exists, so the run that set it did not finish. The claim is removed.")


@dataclass(frozen=True)
class GateDecision:
    decision: str  # "select", "resume" or "wait"
    reason: str
    pr: int | None = None
    resume_branch: str | None = None
    actions: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {"decision": self.decision, "reason": self.reason, "pr": self.pr,
                "resume_branch": self.resume_branch, "actions": self.actions, "notes": self.notes}


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def _labels(item: Mapping) -> set[str]:
    return {label.get("name") for label in item.get("labels") or [] if isinstance(label, dict)}


def issue_of(pull: Mapping) -> int | None:
    """The issue a self-development branch names: ``claude/autodev-338-runner`` gives 338."""
    match = _BRANCH.match(str((pull.get("head") or {}).get("ref") or ""))
    return int(match[1]) if match else None


def is_self_development(pull: Mapping, repository: str) -> bool:
    head = pull.get("head") or {}
    head_repository = str(((head.get("repo") or {}).get("full_name")) or "")
    return (PR_LABEL in _labels(pull) and issue_of(pull) is not None
            and head_repository.lower() == repository.lower())


def trusted_bot(user: Any) -> bool:
    return isinstance(user, dict) and TRUSTED_BOTS.get(user.get("login")) == user.get("id")


def trusted_state(comments: Iterable[Mapping], trusted_ids: set[int]) -> tuple[dict | None, int]:
    """The newest state comment whose ID is in the run records, and how many marked ones were ignored."""
    found, ignored = [], 0
    for comment in comments:
        match = _STATE.match(str(comment.get("body") or ""))
        if not match:
            continue
        if comment.get("id") not in trusted_ids:
            ignored += 1
            continue
        try:
            state = json.loads(match[1])
        except ValueError:
            ignored += 1
            continue
        if isinstance(state, dict):
            found.append((str(comment.get("updated_at") or comment.get("created_at") or ""), state))
    return (max(found, key=lambda item: item[0])[1] if found else None), ignored


def bot_findings(reviews: Iterable[Mapping], review_comments: Iterable[Mapping]) -> set[str]:
    """IDs of findings from allowlisted bots: their reviews, their inline comments, and their replies
    in threads that the owner or an allowlisted bot started (design 5.2)."""
    findings = {f"review:{review.get('id')}" for review in reviews
                if trusted_bot(review.get("user")) and review.get("state") != "PENDING"}
    review_comments = list(review_comments)
    roots = {comment.get("id"): comment for comment in review_comments}
    for comment in review_comments:
        if not trusted_bot(comment.get("user")):
            continue
        parent = comment.get("in_reply_to_id")
        if parent is not None:
            root_user = (roots.get(parent) or {}).get("user") or {}
            if root_user.get("id") != OWNER_ID and not trusted_bot(root_user):
                continue
        findings.add(f"comment:{comment.get('id')}")
    return findings


def _open_pr(number: int, pull: Mapping, snapshot: Mapping, trusted_ids: set[int], now: datetime,
             max_bot_rounds: int, requested: bool) -> tuple[str, str, list[str]]:
    """``(decision, reason, notes)`` for one open self-development PR."""
    key = str(number)
    state, ignored = trusted_state(snapshot.get("issue_comments", {}).get(key, []), trusted_ids)
    notes = [f"PR #{number}: ignored {ignored} marked state comment(s) that are not in the run records"] if ignored else []
    if state is None:
        if requested:
            return "resume", f"PR #{number}: resume requested; it has no trusted state comment", notes
        return "wait", f"PR #{number}: no trusted state comment", notes
    rounds = state.get("rounds") if isinstance(state.get("rounds"), int) else 0
    if state.get("review_status") in FINISHED:
        return "wait", f"PR #{number}: the review is finished ({state['review_status']})", notes
    if rounds >= max_bot_rounds:
        return "wait", f"PR #{number}: the bot-round limit ({max_bot_rounds}) is exhausted", notes
    if requested:
        return "resume", f"PR #{number}: resume requested", notes
    attempt = _time(state.get("next_attempt"))
    if attempt is not None and attempt <= now:
        return "resume", f"PR #{number}: next_attempt {state['next_attempt']} has come", notes
    seen = {str(item) for item in state.get("findings") or []}
    new = bot_findings(snapshot.get("reviews", {}).get(key, []),
                       snapshot.get("review_comments", {}).get(key, [])) - seen
    if new:
        return "resume", f"PR #{number}: new findings from trusted bots: {', '.join(sorted(new))}", notes
    details = snapshot.get("pull_details", {}).get(key) or pull
    if details.get("mergeable") is False or details.get("mergeable_state") == "dirty":
        return "resume", f"PR #{number}: conflicts with the base branch", notes
    head = (details.get("head") or {}).get("sha")
    if head and state.get("head") and head != state.get("head"):
        return "resume", f"PR #{number}: head {head[:12]} has no review", notes
    return "wait", f"PR #{number}: waiting for the bots", notes


def _claim_age(number: int, snapshot: Mapping, now: datetime) -> timedelta | None:
    labeled = [_time(event.get("created_at")) for event in snapshot.get("issue_events", {}).get(str(number), [])
               if event.get("event") == "labeled" and (event.get("label") or {}).get("name") == CLAIM_LABEL]
    labeled = [moment for moment in labeled if moment is not None]
    return now - max(labeled) if labeled else None


def _remove_claim(issue: int, reason: str, comment: str | None = None) -> dict:
    return {"op": "remove_label", "issue": issue, "label": CLAIM_LABEL, "comment": comment, "reason": reason}


def gate(snapshot: Mapping, trusted_ids: Iterable[int], *, max_bot_rounds: int,
         now: datetime | None = None, pr: int | None = None) -> GateDecision:
    """The decision for one tick; ``pr`` is the ``resume`` input, which checks that PR right away."""
    now = now or _time(snapshot.get("fetched_at"))
    if now is None:
        raise ValueError("the gate needs the current time")
    trusted_ids = set(trusted_ids)
    repository = str(snapshot.get("repository") or "")
    pulls = [pull for pull in snapshot.get("pulls", []) if is_self_development(pull, repository)]
    by_number = {pull["number"]: pull for pull in pulls}

    if pr is not None:
        pull = by_number.get(pr)
        if pull is None:
            return GateDecision("wait", f"PR #{pr} is not a self-development pull request of {repository}")
        if pull.get("state") != "open":
            return GateDecision("wait", f"PR #{pr} is not open")
        decision, reason, notes = _open_pr(pr, pull, snapshot, trusted_ids, now, max_bot_rounds, True)
        branch = (pull.get("head") or {}).get("ref") if decision == "resume" else None
        return GateDecision(decision, reason, pr if decision == "resume" else None, branch, [], notes)

    actions, notes = [], []
    claimed = {issue["number"]: issue for issue in snapshot.get("claimed_issues", [])
               if "pull_request" not in issue and CLAIM_LABEL in _labels(issue)}
    pulls_of_issue: dict[int, list[Mapping]] = {}
    for pull in pulls:
        pulls_of_issue.setdefault(issue_of(pull), []).append(pull)

    # Closed self-development PRs end their issue's claim (9.2, step 3), also on an issue the merge
    # closed, which is no longer among the open claimed issues.
    closed_claims = {int(number): issue for number, issue in (snapshot.get("linked_issues") or {}).items()
                     if isinstance(issue, dict) and str(number).isdigit() and "pull_request" not in issue
                     and issue.get("number") == int(number) and CLAIM_LABEL in _labels(issue)}
    for pull in pulls:
        issue = issue_of(pull)
        if pull.get("state") == "closed" and issue in claimed.keys() | closed_claims.keys() and \
                not any(other.get("state") == "open" for other in pulls_of_issue[issue]):
            if pull.get("merged_at"):
                actions.append(_remove_claim(issue, f"PR #{pull['number']} was merged"))
            else:
                actions.append(_remove_claim(issue, f"PR #{pull['number']} was closed without merging"))
                notes.append(f"issue #{issue} is taken again only with autodev:next")
            claimed.pop(issue, None)
            closed_claims.pop(issue, None)

    # Claims without any PR (9.2, step 2).
    for number in sorted(claimed):
        if number in pulls_of_issue:
            continue
        age = _claim_age(number, snapshot, now)
        if age is None:
            return GateDecision("wait", f"issue #{number}: the age of its claim is unknown", None, None, actions, notes)
        if age < CLAIM_TTL:
            return GateDecision("wait", f"issue #{number}: claimed {int(age.total_seconds() // 60)} minutes ago "
                                        "and no PR exists yet", None, None, actions, notes)
        actions.append(_remove_claim(number, "the claim is older than 6 hours without a PR", STALE_CLAIM_COMMENT))

    # Open self-development PRs (9.2, steps 3 and 4).
    open_pulls = sorted((pull for pull in pulls if pull.get("state") == "open"), key=lambda pull: pull["number"])
    waits = []
    for pull in open_pulls:
        decision, reason, pr_notes = _open_pr(pull["number"], pull, snapshot, trusted_ids, now, max_bot_rounds, False)
        notes.extend(pr_notes)
        if decision == "resume":
            return GateDecision("resume", reason, pull["number"], (pull.get("head") or {}).get("ref"), actions, notes)
        waits.append(reason)
    if len(open_pulls) >= WIP_LIMIT:
        return GateDecision("wait", f"WIP limit {WIP_LIMIT}: " + "; ".join(waits), None, None, actions, notes)
    return GateDecision("select", "no claim or open self-development PR stands in the way", None, None, actions, notes)
