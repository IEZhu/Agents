"""The select stage's candidate issues, read from GitHub with GET requests only (#347, design 9.3).

flows/autodev-select.md names what the selection session reads from ``{inputs_file}``;
`fetch_candidates` builds it through the read-only client (`src.schedules.github.GitHubClient`)
and `src.schedules.stages.select_inputs` and `write_select_inputs` write it into the run's state
copy. The pool is every open issue labelled ``autodev:ready`` or ``autodev:next`` (two list
requests: ``labels`` is an AND filter), pull requests excluded, at most `MAX_CANDIDATES` of them
(the oldest label first is not known here, so the newest issues are kept; the count of dropped
ones goes into the run record's ``candidates``, not into the inputs). For each candidate:

* the metadata of the list answer: ``number``, ``title``, ``labels`` (names), ``assignees``
  (logins), ``created_at``, ``updated_at``, ``author`` (``login``, ``id``) and ``app`` (the slug of
  ``performed_via_github_app``, or null);
* ``GET /repos/R/issues/N/events``: ``label_times`` (the latest ``labeled`` event of
  ``autodev:ready`` and of ``autodev:next``) and, with the comments, ``owner_activity`` (the owner
  account's latest event, comment or comment edit; an edit of the issue body is not an event and
  is not seen);
* ``GET /repos/R/issues/N/comments``: the comments (``id``, ``author``, ``app``, ``created_at``,
  ``body``) and ``agent_state``, the issue agent's state comment (``<!-- issue-agent:state {JSON}``,
  flows/issue-agent.md) only from the owner account (numeric ID and the ``User`` type), as
  ``{"phase", "lock_at"}``, else null;
* ``open_prs``: open pull requests (the gate snapshot's ``pulls``) whose head branch, title or body
  names the issue; ``closed_autodev_prs``: self-development pull requests of the issue closed
  without merging;
* ``branches``: the repository's branches ``claude/issue-<N>-*`` and ``claude/autodev-<N>-*``
  (``GET /repos/R/branches``, read once, only when there is a candidate);
* ``dependencies``: each ``#N`` the body names after "Builds on", "Depends on", "Blocked by" or
  "after", with its state (``GET /repos/R/issues/N``; ``unknown`` when the read is refused). The
  parent epic's work order is left out: the API that names an issue's parent was not verified, so
  the session reads the order from the bodies it has, or drops the candidate for the missing field.

Bodies and comments are untrusted data: they go into files the session reads, never into the
prompt. Every answer shape here is the REST one as recalled from GitHub's reference; the tests use
synthetic answers in that shape.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import datetime
import json
import re
from typing import Any

from .gate import OWNER_ID, is_self_development, issue_of
from .github import GitHubClient, GitHubError

READY_LABEL = "autodev:ready"
NEXT_LABEL = "autodev:next"
POOL_LABELS = (READY_LABEL, NEXT_LABEL)
MAX_CANDIDATES = 50
MAX_DEPENDENCIES = 10
AGENT_STATE = re.compile(r"<!--\s*issue-agent:state\s*(\{.*?\})\s*-->", re.S)
DEPENDENCY = re.compile(r"(?i)\b(builds on|depends on|blocked by|after)\s+#([1-9][0-9]{0,8})\b")


def _user(user: Any) -> dict:
    user = user if isinstance(user, Mapping) else {}
    return {"login": user.get("login"), "id": user.get("id")}


def _app(item: Mapping) -> str | None:
    app = item.get("performed_via_github_app")
    if not isinstance(app, Mapping):
        return None
    return str(app.get("slug") or app.get("name") or "app")


def _owner(user: Any) -> bool:
    return isinstance(user, Mapping) and type(user.get("id")) is int and user["id"] == OWNER_ID \
        and user.get("type", "User") == "User"


def _latest(times: Iterable[Any]) -> str | None:
    found = [text for text in times if isinstance(text, str) and _time(text) is not None]
    return max(found, key=lambda text: _time(text)) if found else None  # type: ignore[arg-type,return-value]


def _time(text: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo is not None else None


def _labels(item: Mapping) -> list[str]:
    return sorted({label.get("name") for label in item.get("labels") or []
                   if isinstance(label, Mapping) and isinstance(label.get("name"), str)})


def label_times(events: Iterable[Mapping]) -> dict:
    """When ``autodev:ready`` and ``autodev:next`` were last added (``labeled`` events)."""
    found: dict[str, str | None] = {}
    for label in POOL_LABELS:
        found[label] = _latest(event.get("created_at") for event in events
                               if event.get("event") == "labeled" and (event.get("label") or {}).get("name") == label)
    return found


def owner_activity(events: Iterable[Mapping], comments: Iterable[Mapping]) -> str | None:
    """The owner account's latest issue event, comment or comment edit (an ISO time), or None."""
    times = [event.get("created_at") for event in events if _owner(event.get("actor"))]
    for comment in comments:
        if _owner(comment.get("user")):
            times += [comment.get("created_at"), comment.get("updated_at")]
    return _latest(times)


def agent_state(comments: Iterable[Mapping]) -> dict | None:
    """The newest issue-agent state comment of the owner account as ``{"phase", "lock_at"}``."""
    found = []
    for comment in comments:
        if not _owner(comment.get("user")):
            continue
        match = AGENT_STATE.search(str(comment.get("body") or ""))
        if not match:
            continue
        try:
            state = json.loads(match[1])
        except ValueError:
            continue
        if isinstance(state, dict):
            found.append((str(comment.get("updated_at") or comment.get("created_at") or ""), state))
    if not found:
        return None
    state = max(found, key=lambda item: item[0])[1]
    return {"phase": state.get("phase") if isinstance(state.get("phase"), str) else None,
            "lock_at": state.get("lock_at") if isinstance(state.get("lock_at"), str) else None}


def _names(number: int, text: str) -> bool:
    return bool(re.search(rf"(?<![0-9A-Za-z_])#{number}(?![0-9])|/issues/{number}(?![0-9])", text))


def open_prs(number: int, pulls: Iterable[Mapping]) -> list[dict]:
    """Open pull requests whose head branch, title or body names issue ``number``."""
    found = []
    for pull in pulls:
        if pull.get("state") != "open":
            continue
        ref = str((pull.get("head") or {}).get("ref") or "")
        text = f"{pull.get('title') or ''}\n{pull.get('body') or ''}"
        if re.match(rf"claude/(?:autodev|issue)-{number}(?:-|$)", ref) or _names(number, text):
            found.append({"number": pull.get("number"), "branch": ref, "labels": _labels(pull)})
    return found


def closed_autodev_prs(number: int, pulls: Iterable[Mapping], repository: str) -> list[dict]:
    """Self-development pull requests of issue ``number`` closed without merging."""
    return [{"number": pull.get("number"), "closed_at": pull.get("closed_at")} for pull in pulls
            if pull.get("state") == "closed" and not pull.get("merged_at") and is_self_development(pull, repository)
            and issue_of(pull) == number]


def branches_of(number: int, names: Iterable[str]) -> list[str]:
    """The branches ``claude/issue-<N>-*`` and ``claude/autodev-<N>-*``."""
    return sorted(name for name in names if re.match(rf"claude/(?:issue|autodev)-{number}-", name))


def dependency_numbers(body: str) -> list[int]:
    """The ``#N`` that "Builds on", "Depends on", "Blocked by" or "after" name, in order, once each."""
    found: list[int] = []
    for match in DEPENDENCY.finditer(body or ""):
        number = int(match[2])
        if number not in found:
            found.append(number)
    return found[:MAX_DEPENDENCIES]


def _comment(comment: Mapping) -> dict:
    return {"id": comment.get("id"), "author": _user(comment.get("user")), "app": _app(comment),
            "created_at": comment.get("created_at"), "body": str(comment.get("body") or "")}


def fetch_candidates(client: GitHubClient, *, pulls: Iterable[Mapping] = ()) -> dict:
    """``{"issues": [...], "dropped": n}``: each candidate with every field of
    `src.schedules.stages.SELECT_ISSUE_FIELDS` plus ``body`` and ``comments`` (see the module
    docstring); ``pulls`` are the gate snapshot's open and recently closed pull requests."""
    base = f"/repos/{client.repository}"
    pool: dict[int, Mapping] = {}
    for label in POOL_LABELS:
        for issue in client.get_all(f"{base}/issues", {"labels": label, "state": "open", "per_page": 100}):
            if isinstance(issue, Mapping) and "pull_request" not in issue and type(issue.get("number")) is int:
                pool.setdefault(issue["number"], issue)
    numbers = sorted(pool, reverse=True)
    kept, dropped = numbers[:MAX_CANDIDATES], max(0, len(numbers) - MAX_CANDIDATES)
    if not kept:
        return {"issues": [], "dropped": dropped}
    pulls = [pull for pull in pulls if isinstance(pull, Mapping)]
    branch_names = [str(branch.get("name")) for branch in client.get_all(f"{base}/branches", {"per_page": 100})
                    if isinstance(branch, Mapping)]
    states: dict[int, str] = {}
    issues = []
    for number in kept:
        issue = pool[number]
        events = [event for event in client.get_all(f"{base}/issues/{number}/events", {"per_page": 100})
                  if isinstance(event, Mapping)]
        comments = [comment for comment in client.get_all(f"{base}/issues/{number}/comments", {"per_page": 100})
                    if isinstance(comment, Mapping)]
        body = str(issue.get("body") or "")
        dependencies = []
        for dependency in dependency_numbers(body):
            if dependency not in states:
                states[dependency] = _state_of(client, base, dependency, pool)
            dependencies.append({"number": dependency, "state": states[dependency]})
        issues.append({
            "number": number, "title": str(issue.get("title") or ""), "labels": _labels(issue),
            "assignees": sorted(str(user.get("login")) for user in issue.get("assignees") or []
                                if isinstance(user, Mapping)),
            "created_at": issue.get("created_at"), "updated_at": issue.get("updated_at"),
            "author": _user(issue.get("user")), "app": _app(issue), "label_times": label_times(events),
            "owner_activity": owner_activity(events, comments), "agent_state": agent_state(comments),
            "open_prs": open_prs(number, pulls), "branches": branches_of(number, branch_names),
            "dependencies": dependencies, "closed_autodev_prs": closed_autodev_prs(number, pulls, client.repository),
            "body": body, "comments": [_comment(comment) for comment in comments]})
    return {"issues": issues, "dropped": dropped}


def _state_of(client: GitHubClient, base: str, number: int, pool: Mapping[int, Mapping]) -> str:
    if number in pool:
        return str(pool[number].get("state") or "open")
    try:
        answer = client.get(f"{base}/issues/{number}")
    except GitHubError:
        return "unknown"
    return str(answer.get("state")) if isinstance(answer, Mapping) and answer.get("state") else "unknown"


def selected(data: Mapping, issue: int) -> Mapping | None:
    """The fetched candidate ``issue``, or None."""
    return next((item for item in data.get("issues") or [] if item.get("number") == issue), None)


def criteria(candidate: Mapping, refs: Iterable[str], repository: str, *, limit: int, max_chars: int) -> tuple[str, ...]:
    """The acceptance criteria of the selected issue for the worker reviews (design 5.2): its body
    when the owner account opened it, and each owner comment that ``refs`` names
    (``…/issues/<N>#issuecomment-<id>``) that came through no app and holds no autodev marker;
    at most ``limit`` items of at most ``max_chars`` characters each."""
    number = candidate.get("number")
    wanted = set()
    for ref in refs:
        match = re.fullmatch(rf"https://github\.com/{re.escape(repository)}/issues/{number}#issuecomment-([1-9][0-9]*)",
                             str(ref), re.IGNORECASE)
        if match:
            wanted.add(int(match[1]))
    found = []
    author = candidate.get("author") or {}
    if author.get("id") == OWNER_ID and str(candidate.get("body") or "").strip():
        found.append(str(candidate["body"]))
    for comment in candidate.get("comments") or []:
        body = str(comment.get("body") or "")
        if comment.get("id") in wanted and (comment.get("author") or {}).get("id") == OWNER_ID \
                and comment.get("app") is None and "<!-- autodev" not in body and body.strip():
            found.append(body)
    return tuple(text[:max_chars] for text in found[:limit])


def owner_criteria(client: GitHubClient, issue: int, *, limit: int, max_chars: int) -> tuple[str, ...]:
    """The criteria of a resumed run's issue, read again (``GET`` the issue and its comments): the
    body when the owner account opened it, and every owner comment without an app or marker."""
    base = f"/repos/{client.repository}"
    answer = client.get(f"{base}/issues/{issue}")
    comments = client.get_all(f"{base}/issues/{issue}/comments", {"per_page": 100})
    candidate = {"number": issue, "author": _user((answer or {}).get("user")), "body": (answer or {}).get("body"),
                 "comments": [_comment(comment) for comment in comments if isinstance(comment, Mapping)]}
    refs = [f"https://github.com/{client.repository}/issues/{issue}#issuecomment-{comment['id']}"
            for comment in candidate["comments"] if isinstance(comment.get("id"), int)]
    return criteria(candidate, refs, client.repository, limit=limit, max_chars=max_chars)


def issue_labels(client: GitHubClient, issue: int) -> list[str]:
    """The labels of ``issue`` (the change check reads ``autodev:next`` from them)."""
    answer = client.get(f"/repos/{client.repository}/issues/{issue}")
    return _labels(answer) if isinstance(answer, Mapping) else []

