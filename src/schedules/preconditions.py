"""What a real run needs before it starts (design 5.7, #347): checked with reads only.

A supervised real run (``run-now --mode run`` or ``resume``) starts only when every item holds;
the run sequence calls `check` after the drift check and before the gate, the cleanup or any
session, and a non-empty answer ends the run ``refused`` with the list (`refusal`): no label, no
session. A dry-run never calls it. The items, each named in the answer, never with a value:

* **The broker's credentials** (Q15): the deploy key, the pinned github.com host keys and the
  fine-grained API token (`broker.credential_problems`, which `broker.Broker` shares). Without the
  token the API items below cannot be read, so they are left out of the answer.
* **Protection of main** (phase 0, adopted 2026-10-12): ``GET /repos/R/rules/branches/main`` lists
  the rule types ``deletion`` and ``non_fast_forward`` and a ``pull_request`` rule whose
  ``parameters.required_approving_review_count`` is at least 1.
* **No workflow approves a pull request**: ``GET /repos/R/actions/permissions/workflow`` says
  ``can_approve_pull_request_reviews`` is false, so a workflow's ``GITHUB_TOKEN`` cannot supply the
  required approval.
* **CLAUDE_ROUTINE_TOKEN is no repository secret** (moved into the ``issue-agent`` environment,
  2026-10-12): ``GET /repos/R/actions/secrets`` (names only) does not list it.

A read GitHub refuses (403 or 404, also any other error status) is the item ``cannot verify <x>
(HTTP <n>)``: a real run never starts on a condition it could not read (the broker token needs
``Administration: read`` and ``Secrets: read`` for the last two, recalled, not verified). The
answer shapes (a list of rule objects with ``type`` and ``parameters``; an object with
``total_count`` and ``secrets``) follow GitHub's REST reference as recalled; the tests use
synthetic answers in that shape.
"""
from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import broker
from .github import GitHubClient, GitHubError, NetworkError, TokenUnavailable

REQUIRED_RULES = ("deletion", "non_fast_forward")
ROUTINE_SECRET = "CLAUDE_ROUTINE_TOKEN"
TOKEN_ITEM = "the API token"
REVIEW_ITEM = "main protection: a pull_request rule with at least one required approving review"
WORKFLOW_ITEM = "workflows may not approve pull requests (can_approve_pull_request_reviews false)"
SECRET_ITEM = f"{ROUTINE_SECRET} only as an environment secret, not a repository secret"
WHAT = {"rules": "main protection", "workflow": "the workflow approval permission",
        "secrets": "the repository secrets"}
SECRETS_PAGE = 100


def rule_item(rule: str) -> str:
    return f"main protection: the {rule} rule"


class _Unverified(Exception):
    def __init__(self, item: str):
        super().__init__(item)
        self.item = item


def _read(what: str, call: Callable[[], Any]) -> Any:
    """The answer of one read, or `_Unverified` naming why it could not be read."""
    try:
        return call()
    except GitHubError as error:
        # A 2xx here is an answer the client could not use (not JSON, not a list, too many pages).
        why = "unexpected answer" if 200 <= error.status < 300 else f"HTTP {error.status}"
        raise _Unverified(f"cannot verify {WHAT[what]} ({why})") from None
    except NetworkError:
        raise _Unverified(f"cannot verify {WHAT[what]} (GitHub unreachable)") from None
    except TokenUnavailable:
        raise _Unverified(f"cannot verify {WHAT[what]} (no API token)") from None


def _rules(client: GitHubClient, base: str) -> list[str]:
    rules = _read("rules", lambda: client.get_all(f"{base}/rules/branches/main", {"per_page": 100}))
    if not all(isinstance(rule, dict) for rule in rules):
        raise _Unverified(f"cannot verify {WHAT['rules']} (unexpected answer)")
    types = {rule.get("type") for rule in rules}
    missing = [rule_item(rule) for rule in REQUIRED_RULES if rule not in types]
    counts = [(rule.get("parameters") or {}).get("required_approving_review_count") for rule in rules
              if rule.get("type") == "pull_request" and isinstance(rule.get("parameters") or {}, dict)]
    if not any(isinstance(count, int) and not isinstance(count, bool) and count >= 1 for count in counts):
        missing.append(REVIEW_ITEM)
    return missing


def _workflow(client: GitHubClient, base: str) -> list[str]:
    answer = _read("workflow", lambda: client.get(f"{base}/actions/permissions/workflow"))
    if not isinstance(answer, dict) or not isinstance(answer.get("can_approve_pull_request_reviews"), bool):
        raise _Unverified(f"cannot verify {WHAT['workflow']} (unexpected answer)")
    return [WORKFLOW_ITEM] if answer["can_approve_pull_request_reviews"] else []


def _secrets(client: GitHubClient, base: str) -> list[str]:
    names: list[str] = []
    for page in range(1, client.max_pages + 1):
        answer = _read("secrets", lambda: client.get(f"{base}/actions/secrets", {"per_page": SECRETS_PAGE,
                                                                                 "page": page}))
        listed = answer.get("secrets") if isinstance(answer, dict) else None
        total = answer.get("total_count") if isinstance(answer, dict) else None
        if not isinstance(listed, list) or not isinstance(total, int) or isinstance(total, bool):
            raise _Unverified(f"cannot verify {WHAT['secrets']} (unexpected answer)")
        names += [str(item.get("name")) for item in listed if isinstance(item, dict)]
        if len(names) >= total or len(listed) < SECRETS_PAGE:
            break
    else:
        raise _Unverified(f"cannot verify {WHAT['secrets']} (more than {client.max_pages} pages)")
    return [SECRET_ITEM] if ROUTINE_SECRET in names else []


def check(client: GitHubClient, credentials: broker.Credentials | None, *, repository: str,
          key_reader: Callable[[Path], bytes] = Path.read_bytes) -> list[str]:
    """Every missing item of a real run, named (see the module docstring); empty when it may start."""
    missing = broker.credential_problems(credentials, key_reader)
    if TOKEN_ITEM in missing:
        return missing
    base = f"/repos/{repository}"
    for part in (_rules, _workflow, _secrets):
        try:
            missing += part(client, base)
        except _Unverified as unverified:
            missing.append(unverified.item)
    return missing


def refusal(missing: list[str]) -> dict:
    """The fields a real run's record gets when `check` named anything: ``refused`` with the list."""
    return {"status": "refused", "reason": "preconditions: " + "; ".join(missing), "missing": list(missing)}
