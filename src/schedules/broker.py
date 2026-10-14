"""The run-bound GitHub broker (#342; design 5.1, 5.5, 5.6, 5.7, 5.8 and 9.6).

A sandboxed stage session has no credentials and no network; the broker is its only path to
GitHub, and every operation is bound to one run (`Run`): its issue, the PR it created or the
resume binding named, the branch ``claude/autodev-<issue>-<slug>`` and the pinned ``main`` it
started from. The session reaches it through the runner socket (`src.schedules.socket_service`)
with named, typed ``gh.*`` request types only (`register`); there is no raw REST or GraphQL
passthrough. The broker sends GraphQL itself, with the fixed documents of `DOCUMENTS`.

Socket operations (all session-bound):

* ``gh.push`` pushes the run's branch with the deploy key, only when the head is the promoted
  ``refs/heads/claude/autodev-<issue>-<slug>`` of the trusted repository (a commit the runner
  imported from a bundle, `src.schedules.repos`), equals the last head a validated review covered
  and that review leaves no open blocker (`validated_review_head`, `review_gate`: the record the
  worker launcher of #345 writes, and its decision log), passes the change check of
  `src.schedules.paths` against the pinned commit, its diff and commit messages pass the scan
  of `src.schedules.scan`, and its commit messages name no other issue with a closing keyword.
* ``gh.pr_create`` once per run, from the pushed branch to ``main``; ``gh.pr_edit`` changes only
  the title and body of this run's PR. Closing keywords may name only this run's issue. There is
  no merge, close, reopen or branch deletion.
* ``gh.comment`` on this run's issue or PR, ``gh.comment_edit`` only of a comment this run's
  ``gh.comment`` logged. Every comment starts with `MARKER`; none may hold a line starting with
  ``/agent``, an ``@`` mention, an ``<!-- autodev:…`` sub-marker (state and selection comments are
  runner-internal) or text the scan finds secret-like. The exception is an exact command of
  `BOT_COMMANDS` on this run's PR. PR titles and bodies and risk issues follow the same mention
  and sub-marker rule. The issue body is never edited: no operation exists for it.
* ``gh.label`` adds ``autodev`` to this run's PR and adds or removes ``autodev:blocked`` on this
  run's issue or PR; removing ``autodev`` and the claim ``autodev:in-progress`` are
  runner-internal. No repository label is created or deleted.
* ``gh.threads`` lists this run's PR review threads and ``gh.resolve_thread`` resolves one of
  them, only when its first comment is from an allowlisted bot (`src.schedules.gate.TRUSTED_BOTS`,
  login and ID) or is the owner account's and starts with `MARKER`.
* ``gh.read`` reads one endpoint of `READS` under ``/repos/<repository>/``.

Runner-internal operations are methods, never registered: `Broker.cleanup` executes the gate's
actions (#338) on issues and PRs that earlier run records name, `Broker.select` writes the
selection comment and the claim label, `Broker.bind_resume` binds a resume run to its
self-development PR, and `Broker.create_risk_issues` opens at most `MAX_RISK_ISSUES` issues from
findings of the run's validated pre-mortem record (`read_premortem`) of level ``medium`` and
above that carry no security signal (`security_signal`; those stay local, design 5.8).

Credentials (Q15): pushes use the deploy key through `push_command` (``GIT_SSH_COMMAND`` with
``-F /dev/null``, ``IdentitiesOnly``, no agent and pinned host keys, ``SSH_AUTH_SOCK`` unset);
API calls use the fine-grained token from the keychain (`src.schedules.github`), never the ``gh``
token. A real run refuses to start without both (`CredentialsMissing`); in ``dry-run`` every
write is refused and only reads run. The token and the key are known secrets of the scan and
are masked in the broker log; they never appear in an answer or an exception.

Every write is logged in ``<run_dir>/broker.jsonl`` before it is sent (``intent``) and after
(``done`` with the created or changed object's ID, or ``failed``); refusals are logged too. The
log is read back when a broker opens on the same ``run_dir``, so the risk-issue cap and the
comments this run may edit hold across broker instances of one run.
"""
from __future__ import annotations

import html
import json
import os
import re
import shlex
import stat
import subprocess
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request

from . import github, gitsafe, paths, records, repos, scan
from .gate import (
    CLAIM_LABEL,
    OWNER_ID,
    PR_LABEL,
    is_self_development,
    issue_of,
    trusted_bot,
)
from .socket_service import (
    Field,
    Registry,
    RequestContext,
    RequestRefused,
    UnsafeSocketDirectory,
    check_directory,
)

MARKER = "<!-- autodev -->"
SELECTION_MARKER = "<!-- autodev:selection -->"
BLOCKED_LABEL = "autodev:blocked"
RISK_LABEL = "autodev:risk"
# Labels a session may set (design 9.6): ``autodev`` only added, on this run's PR; ``autodev:blocked``
# added or removed. Removing ``autodev`` and the claim label are runner-internal.
RUN_LABELS = frozenset({PR_LABEL, BLOCKED_LABEL})
# Exact requests to allowlisted bots (design 5.5), allowed unmarked and only on this run's PR.
BOT_COMMANDS = frozenset({"@coderabbitai review", "@coderabbitai full review"})
RISK_LEVELS = ("medium", "high", "critical")
MAX_RISK_ISSUES = 3
MODES = ("dry-run", "run", "resume")
LOG_NAME = "broker.jsonl"
REVIEW_RECORD = "validated-review.json"  # written by the worker launcher (#345)
PREMORTEM_RECORD = "premortem.json"  # written by the pre-mortem step (#345, #347)
DECISIONS_RECORD = "worker-decisions.json"  # the session's decisions on findings (#345)
# A finding of these levels, or one with a security signal, stays open when declined (design 9.4).
GATE_BLOCKING = ("critical", "high")
NOT_CLEAN_LEVELS = ("critical", "high", "medium")
KNOWN_HOSTS_NAME = "github_known_hosts"  # beside the deploy key
GRAPHQL_URL = github.API + "/graphql"
TIMEOUT = 30  # seconds of one API call
PUSH_TIMEOUT = 110  # seconds of git push, under the socket's 120-second deadline
WRITE_DEADLINE = 60.0
PUSH_DEADLINE = 120.0
MAX_OUTGOING_BYTES = 4_000_000  # diff and commit messages of one push, at most what the scan takes
MAX_THREAD_PAGES = 5
TITLE_MAX = 256
COMMENT_ID_MAX = 2 ** 53  # GitHub's numeric IDs exceed the socket's default integer range

_SHA = re.compile(r"[0-9a-f]{40}")
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+){0,15}")
_LABEL = re.compile(r"[a-z][a-z0-9:-]{0,49}")
_ACTION = re.compile(r"add|remove")
_THREAD_ID = re.compile(r"[A-Za-z0-9_-]{1,128}")
# GitHub's closing keywords followed by a reference: #N, GH-N, OWNER/REPO#N or an issue/PR URL.
# The keyword may follow any non-alphanumeric character, also ``_`` of Markdown emphasis.
_CLOSING = re.compile(
    r"(?i)(?<![a-z0-9])(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)\b\s*:?\s*\[?\s*"
    r"(?:(?:(?:https?://)?(?:www\.)?github\.com/)?([\w.-]+/[\w.-]+)(?:#|/issues/|/pull/)|#|gh-)(\d+)")
_AGENT_LINE = re.compile(r"(?m)^\s*/agent")
# ``<!-- autodev:state … -->``, ``<!-- autodev:selection -->`` and any other sub-marker: runner-internal.
_SUB_MARKER = re.compile(r"(?i)<!--\s*autodev\s*:")
# An @mention of a user, a team, a bot or an app. Only an ASCII e-mail local part before ``@`` makes
# it none (GitHub's own rule is ASCII); a backtick does not, an unclosed one is no code span.
_MENTION = re.compile(r"(?<![A-Za-z0-9._%+-])@[A-Za-z0-9]")
# The review bots' handles are refused anywhere, also inside an address-like word.
_BOT_HANDLE = re.compile(r"(?i)@(?:coderabbit|copilot)")
_INVISIBLE = re.compile(r"[\u00ad\u034f\u061c\u115f\u1160\u17b4\u17b5\u180b-\u180f\u200b-\u200f\u202a-\u202e"
                        r"\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff\uffa0]")
# Fixed keyword rule of the runner-side security classification (design 5.8): a finding that
# names any of these stays local.
SECURITY_KEYWORDS = re.compile(
    r"(?i)\b(?:secur\w*|secret\w*|token\w*|credential\w*|password\w*|passphrase|keychain|private[ _-]key|"
    r"deploy[ _-]key|auth\w*|sandbox\w*|inject\w*|exploit\w*|vulnerab\w*|cve-\d+|xss|csrf|ssrf|rce|"
    r"privilege\w*|permission\w*|escalat\w*|leak\w*|exfiltrat\w*|bypass\w*|unauthori[sz]\w*|"
    r"remote[ _-]code|code[ _-]execution|arbitrary[ _-](?:code|command|file|path)\w*|command[ _-]execution|"
    r"path[ _-]traversal|traversal|symlink\w*|symbolic[ _-]link\w*|toctou|race[ _-]condition\w*|ssh|"
    r"untrusted|trust[ _-]boundar\w*|denial[ _-]of[ _-]service|dos|ddos|saniti[sz]\w*|unsanitized|spoof\w*|"
    r"forg(?:e|ed|ery|eries|ing)|impersonat\w*|malicious\w*|attack\w*|tamper\w*|hijack\w*|phish\w*|"
    r"encrypt\w*|decrypt\w*|crypto\w*|cipher\w*|signing|signature\w*|unsafe|insecure|harden\w*|"
    r"access[ _-]control|deserializ\w*|pickle|eval|shell[ _-]injection|redos|cors|session[ _-]fixation)\b")

# Reads the session may request: name -> (path under /repos/<repository>, the argument it needs).
READS: dict[str, tuple[str, str]] = {
    "issue": ("/issues/{number}", "number"),
    "issue_comments": ("/issues/{number}/comments", "number"),
    "issue_events": ("/issues/{number}/events", "number"),
    "pull": ("/pulls/{number}", "number"),
    "pull_reviews": ("/pulls/{number}/reviews", "number"),
    "pull_comments": ("/pulls/{number}/comments", "number"),
    "pull_files": ("/pulls/{number}/files", "number"),
    "pull_commits": ("/pulls/{number}/commits", "number"),
    "commit_status": ("/commits/{sha}/status", "sha"),
    "check_runs": ("/commits/{sha}/check-runs", "sha"),
}
_READ_NAME = re.compile("|".join(sorted(READS)))
_NEXT_LINK = re.compile(r'<[^>]*>\s*;\s*rel="?next"?')

THREADS_QUERY = """\
query($owner: String!, $name: String!, $number: Int!, $after: String) {
  repository(owner: $owner, name: $name) {
    pullRequest(number: $number) {
      reviewThreads(first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes {
          id isResolved path line
          comments(first: 1) { nodes { databaseId body author { __typename login ... on Bot { databaseId } ... on User { databaseId } } } }
        }
      }
    }
  }
}"""
RESOLVE_MUTATION = """\
mutation($threadId: ID!) {
  resolveReviewThread(input: {threadId: $threadId}) { thread { id isResolved } }
}"""
DOCUMENTS = (THREADS_QUERY, RESOLVE_MUTATION)

ApiTransport = Callable[[str, str, Mapping[str, str], "bytes | None"], tuple[int, Mapping[str, str], bytes]]
PushRunner = Callable[..., subprocess.CompletedProcess]


class BrokerRefused(RequestRefused):
    """An operation the run's binding does not allow; the message names why, never a secret."""


class ReadOnly(BrokerRefused):
    """A write in ``dry-run``."""


class UnsafeRunDirectory(RuntimeError):
    """A run directory a session could write: the broker trusts the records it reads from there."""


class CredentialsMissing(RuntimeError):
    """A real run without the deploy key, the pinned host keys or the token; names what is missing."""

    def __init__(self, missing: list[str]):
        super().__init__("the broker needs " + ", ".join(missing) + " for a real run")
        self.missing = missing


# --- records the broker reads ---------------------------------------------------------------------

def validated_review_head(run_dir: str | Path) -> str | None:
    """The last head a validated review covered when it leaves no open blocker (`review_gate`);
    None otherwise. The push check and the pre-mortem's precondition (#345) read it."""
    return review_gate(run_dir)[0]


def review_gate(run_dir: str | Path) -> tuple[str | None, str]:
    """The head of ``<run_dir>/validated-review.json`` when it opens the push gate, and why (or
    why not); design 9.4 as amended by #345.

    The worker launcher writes ``{"validated": true, "status": "completed", "head", "handle",
    "kind", "number", "restricted", "clean", "blocked", "findings": [{"key", "level",
    "security"}]}`` after every accepted review or verification; ``findings`` lists the findings of
    level ``medium`` or higher. The gate opens only when ``blocked`` is false and the record is
    ``clean`` with no such finding, or when every listed finding is ``medium``, carries no security
    signal (``security`` false; an unmarked one counts as security) and was ``declined`` with a
    reason in ``<run_dir>/worker-decisions.json`` after this review: the latest decision on its key
    that names this review's handle (the current one or one of its history). A ``critical``,
    ``high`` or security finding keeps the gate closed even when declined:
    it stays open until a later review accepts the reason by not reporting it again. A missing or
    malformed field closes the gate.
    """
    data = _read_json(Path(run_dir) / REVIEW_RECORD)
    if not isinstance(data, dict) or data.get("validated") is not True or data.get("status") != "completed":
        return None, "no validated review"
    head = data.get("head")
    if not isinstance(head, str) or not _SHA.fullmatch(head):
        return None, "the review record names no head"
    if data.get("blocked") is not False:
        return None, "the review ended the stage blocked"
    findings = data.get("findings")
    if not isinstance(findings, list) or not all(isinstance(item, dict) for item in findings):
        return None, "the review record lists no findings"
    if not findings:
        return (head, "clean") if data.get("clean") is True else (None, "the review is not clean")
    decisions = _read_json(Path(run_dir) / DECISIONS_RECORD)
    entries = decisions.get("decisions") if isinstance(decisions, dict) else None
    by_key = {entry.get("key"): entry for entry in entries or [] if isinstance(entry, dict)}
    for item in findings:
        key, level = item.get("key"), item.get("level")
        if level in GATE_BLOCKING or item.get("security") is not False:
            signal = ", security" if item.get("security") is not False else ""
            return None, f"finding {str(key)[:16]} ({level}{signal}) stays open until a later review"
        if level not in NOT_CLEAN_LEVELS:
            return None, f"finding {str(key)[:16]} has an unknown level"
        if not _declined_for(by_key.get(key), data.get("handle")):
            return None, f"finding {str(key)[:16]} ({level}) is neither fixed by a later review nor declined"
    return head, "every remaining medium finding was declined"


def _declined_for(entry: Any, handle: Any) -> bool:
    """Whether the latest decision of a decision-log ``entry`` (the current one, then its history
    from the newest) that names ``handle`` is ``declined`` with a reason."""
    if not isinstance(entry, dict) or not isinstance(handle, str):
        return False
    history = entry.get("history") if isinstance(entry.get("history"), list) else []
    for decision in [entry, *reversed(history)]:
        if isinstance(decision, dict) and decision.get("handle") == handle:
            return decision.get("decision") == "declined" and isinstance(decision.get("reason"), str) \
                and bool(decision["reason"].strip())
    return False


@dataclass(frozen=True)
class RiskFinding:
    id: str
    level: str
    title: str
    body: str
    security: bool  # a security mark in the stage output


def read_premortem(run_dir: str | Path) -> dict[str, RiskFinding]:
    """The findings of the run's validated pre-mortem record, by ID; empty when there is none.

    ``<run_dir>/premortem.json`` holds ``{"validated": true, "findings": [{"id", "level",
    "title", "body", "security"?}]}``, written by the runner after the pre-mortem worker's result
    passed validation (#345, #347). Until then tests write the fixture.
    """
    data = _read_json(Path(run_dir) / PREMORTEM_RECORD)
    if not isinstance(data, dict) or data.get("validated") is not True:
        return {}
    found = {}
    for item in data.get("findings") or []:
        if not isinstance(item, dict):
            continue
        values = [item.get(key) for key in ("id", "level", "title", "body")]
        if all(isinstance(value, str) and value for value in values):
            # Fail closed: a finding counts as security-marked unless the record says false.
            found[values[0]] = RiskFinding(*values, security=item.get("security") is not False)
    return found


def normalize(text: str) -> str:
    """``text`` NFKC-normalised with invisible format characters (zero-width and the like) removed,
    so a compatibility form or a zero-width joiner cannot split a keyword or a mention."""
    return _INVISIBLE.sub("", unicodedata.normalize("NFKC", _INVISIBLE.sub("", text)))


def security_signal(finding: RiskFinding) -> bool:
    """Whether a finding must stay local (design 5.8): a security mark, a keyword match after
    `normalize`, or a title with a non-ASCII letter (a homoglyph could hide a keyword)."""
    title = normalize(finding.title)
    if finding.security or any(character.isalpha() and not character.isascii() for character in title):
        return True
    return bool(SECURITY_KEYWORDS.search(f"{title}\n{normalize(finding.body)}"))


def bound_numbers(earlier: Iterable[Mapping]) -> set[int]:
    """Issue and PR numbers earlier run records name (``issue``, ``pr``), which #347 writes."""
    numbers = set()
    for record in earlier:
        for key in ("issue", "pr"):
            value = record.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and value > 0:
                numbers.add(value)
    return numbers


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --- credentials and the push command ------------------------------------------------------------

@dataclass(frozen=True)
class Credentials:
    """Where the broker's secrets are; nothing is read until a broker opens."""
    deploy_key: Path
    known_hosts: Path
    token: github.TokenProvider = field(repr=False)

    @classmethod
    def from_config(cls, config: Mapping) -> Credentials:
        key = Path(os.path.expanduser(str(config["github"]["deploy_key"])))
        return cls(key, key.parent / KNOWN_HOSTS_NAME, github.token_provider(config))


def _regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(os.lstat(path).st_mode)
    except OSError:
        return False


def _whitespace(path: Path) -> bool:
    return any(character.isspace() for character in str(path))


def ssh_command(deploy_key: Path, known_hosts: Path) -> str:
    """``GIT_SSH_COMMAND`` of the push: no ssh configuration, only the deploy key, pinned host keys
    (no global known-hosts file), and no prompt. ssh splits ``UserKnownHostsFile`` at whitespace, so
    a path with whitespace is refused (`ValueError`)."""
    if _whitespace(deploy_key) or _whitespace(known_hosts):
        raise ValueError("the deploy key and the host keys need a path without whitespace")
    return " ".join(["ssh", "-F", "/dev/null", "-i", shlex.quote(str(deploy_key)), "-o", "IdentitiesOnly=yes",
                     "-o", "IdentityAgent=none", "-o", f"UserKnownHostsFile={shlex.quote(str(known_hosts))}",
                     "-o", "GlobalKnownHostsFile=/dev/null", "-o", "StrictHostKeyChecking=yes",
                     "-o", "BatchMode=yes"])


def push_url(repository: str) -> str:
    return f"git@github.com:{repository}.git"


def push_command(trusted: Path, repository: str, sha: str, issue: int, slug: str,
                 credentials: Credentials) -> tuple[list[str], dict[str, str]]:
    """The argv and environment of the push; runs nothing."""
    argv = gitsafe.command(trusted, repos.push_arguments(push_url(repository), sha, issue, slug), repos.CONFIG)
    env = gitsafe.environment(trusted)
    env.pop("SSH_AUTH_SOCK", None)
    env["GIT_SSH_COMMAND"] = ssh_command(credentials.deploy_key, credentials.known_hosts)
    return argv, env


class TrustedGit:
    """git on the trusted repository for a push; tests replace it with a fake."""

    def __init__(self, path: str | os.PathLike[str], runner: PushRunner = subprocess.run):
        self.path = Path(path)
        self._runner = runner

    def branch_head(self, branch: str) -> str | None:
        result = gitsafe.git_test(self.path, "rev-parse", "--verify", "--quiet", "--end-of-options",
                                  f"refs/heads/{branch}^{{commit}}", config=repos.CONFIG)
        sha = result.stdout.decode("ascii", "replace").strip()
        return sha if result.returncode == 0 and _SHA.fullmatch(sha) else None

    def check(self, base: str, head: str, labels: Iterable[str]) -> paths.ChangeCheck:
        classes = paths.load_path_classes(self.path, base)
        return paths.check_changes(self.path, base, head, labels, classes)

    def outgoing(self, base: str, head: str) -> tuple[str, str]:
        """The commit messages and the diff of ``base..head``: the scan reads both, the closing-keyword
        check only the messages (GitHub honours keywords in commit messages, not in diffs)."""
        messages = gitsafe.git_bounded(self.path, "log", "--format=%B", "--end-of-options", f"{base}..{head}",
                                       max_output=MAX_OUTGOING_BYTES, config=repos.CONFIG)
        diff = gitsafe.git_bounded(self.path, "diff", "--no-ext-diff", "--no-textconv", "--end-of-options",
                                   base, head, max_output=MAX_OUTGOING_BYTES, config=repos.CONFIG)
        return messages.decode("utf-8", "replace"), diff.decode("utf-8", "replace")

    def push(self, argv: list[str], env: dict[str, str], timeout: float) -> subprocess.CompletedProcess:
        return self._runner(argv, env=env, capture_output=True, stdin=subprocess.DEVNULL, timeout=timeout,
                            check=False)


# --- the API --------------------------------------------------------------------------------------

def urllib_transport(method: str, url: str, headers: Mapping[str, str],
                     body: bytes | None) -> tuple[int, Mapping[str, str], bytes]:
    """One HTTPS request without redirects; the token stays an unredirected header."""
    request = Request(url, data=body, method=method,
                      headers={name: value for name, value in headers.items() if name.lower() != "authorization"})
    for name, value in headers.items():
        if name.lower() == "authorization":
            request.add_unredirected_header(name.capitalize(), value)
    try:
        with github.urlopen(request, timeout=TIMEOUT) as response:  # noqa: S310 (the URL is checked by the caller)
            return response.status, dict(response.headers), response.read()
    except HTTPError as error:
        return error.code, dict(error.headers or {}), error.read() or b""
    except (URLError, OSError) as error:
        raise github.NetworkError(f"GitHub is unreachable: {type(error).__name__}") from None


class _Api:
    """REST under ``/repos/<repository>/`` and the fixed GraphQL documents; nothing else."""

    def __init__(self, repository: str, token: Callable[[], str], transport: ApiTransport):
        self.repository = repository
        self._token = token
        self._transport = transport

    def _headers(self) -> dict[str, str]:
        return {"Accept": "application/vnd.github+json", "Authorization": f"Bearer {self._token()}",
                "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "agents-core-autodev",
                "Content-Type": "application/json"}

    def rest(self, method: str, path: str, payload: Any = None, params: Mapping[str, Any] | None = None) -> Any:
        return self.rest_page(method, path, payload, params)[0]

    def rest_page(self, method: str, path: str, payload: Any = None,
                  params: Mapping[str, Any] | None = None) -> tuple[Any, bool]:
        """The answer and whether its ``Link`` header names a next page."""
        base = f"/repos/{self.repository}/"
        if not path.startswith(base) or ".." in path or "?" in path or "#" in path:
            raise BrokerRefused(f"refusing a request outside {self.repository}")
        url = github.API + path + (f"?{urlencode(params)}" if params else "")
        answer, headers = self._send(method, url, payload, f"{method} {path}")
        link = next((str(value) for name, value in headers.items() if name.lower() == "link"), "")
        return answer, bool(_NEXT_LINK.search(link))

    def graphql(self, document: str, variables: Mapping[str, Any]) -> Any:
        if not any(document is fixed for fixed in DOCUMENTS):
            raise BrokerRefused("only the broker's fixed GraphQL documents are sent")
        data, _ = self._send("POST", GRAPHQL_URL, {"query": document, "variables": dict(variables)}, "POST /graphql")
        if not isinstance(data, dict) or data.get("errors") or not isinstance(data.get("data"), dict):
            raise BrokerRefused("GraphQL answered with errors")
        return data["data"]

    def _send(self, method: str, url: str, payload: Any, what: str) -> tuple[Any, Mapping[str, str]]:
        body = None if payload is None else json.dumps(payload).encode()
        status, headers, answer = self._transport(method, url, self._headers(), body)
        if not 200 <= status < 300:
            raise github.GitHubError(f"{what}: HTTP {status}", status)
        if not answer:
            return None, headers or {}
        try:
            return json.loads(answer), headers or {}
        except ValueError:
            raise github.GitHubError(f"{what}: the answer is not JSON", status) from None


# --- the run binding and the log ------------------------------------------------------------------

@dataclass
class Run:
    """What one run's broker is bound to. ``base`` is the pinned ``main`` commit of the run;
    ``labels`` are the issue's labels for the change check; ``pr`` is set by ``gh.pr_create`` or
    `Broker.bind_resume`."""
    run_id: str
    repository: str
    issue: int
    base: str
    mode: str
    labels: frozenset[str] = frozenset()
    pr: int | None = None

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        if not isinstance(self.issue, int) or isinstance(self.issue, bool) or self.issue <= 0:
            raise ValueError("issue must be a positive integer")
        if not _SHA.fullmatch(str(self.base)):
            raise ValueError("base must be a 40-character lowercase hex SHA")
        self.labels = frozenset(self.labels)

    @property
    def read_only(self) -> bool:
        return self.mode == "dry-run"


class BrokerLog:
    """The broker log, ``<run_dir>/broker.jsonl``: one masked JSON object per line."""

    def __init__(self, path: Path | None, known: Iterable[str] = ()):
        self.path = path
        self.entries: list[dict] = self._load(path) if path is not None else []
        self._known = [secret for secret in known if secret]
        self._lock = threading.Lock()

    @staticmethod
    def _load(path: Path) -> list[dict]:
        """The entries an earlier broker of the same run wrote; unreadable lines are skipped."""
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        entries = []
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and isinstance(entry.get("op"), str):
                entries.append(entry)
        return entries

    def add(self, op: str, phase: str, **details: Any) -> dict:
        entry = records.mask({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "op": op,
                              "phase": phase, **details}, self._known)
        with self._lock:
            self.entries.append(entry)
            if self.path is not None:
                with open(self.path, "a", encoding="utf-8") as stream:
                    stream.write(json.dumps(entry, sort_keys=True) + "\n")
        return entry

    def done(self, op: str) -> list[dict]:
        return [entry for entry in self.entries if entry.get("op") == op and entry.get("phase") == "done"]


# --- checks on outgoing text ------------------------------------------------------------------------

def closing_references(text: str) -> list[tuple[str | None, int]]:
    """``(repository or None, number)`` of every closing keyword reference in ``text``."""
    return [(match[1], int(match[2])) for match in _CLOSING.finditer(text)]


def _unescape(text: str) -> str:
    """HTML entities decoded until nothing changes (``&amp;#64;`` -> ``&#64;`` -> ``@``), at most 8 rounds."""
    for _ in range(8):
        decoded = html.unescape(text)
        if decoded == text:
            break
        text = decoded
    return text


def check_session_text(text: str) -> None:
    """Refuse text a session sends out with an ``<!-- autodev:…`` sub-marker (state and selection
    comments are runner-internal) or an ``@`` mention (a bot or app command, or a ping); both are
    checked on the text as written, after `normalize`, and with HTML entities decoded."""
    for form in (text, normalize(text), normalize(_unescape(text))):  # &#64; renders as @
        if _SUB_MARKER.search(form):
            raise BrokerRefused("the text may not hold an <!-- autodev:... --> sub-marker; those are runner-internal")
        if _MENTION.search(form) or _BOT_HANDLE.search(form):
            raise BrokerRefused("the text may not hold an @mention; write the name without @")


def check_marker(body: str, *, on_pr: bool, internal: bool = False) -> None:
    """Refuse a comment without the marker, with a ``/agent`` line, or a bot command off the PR;
    a session's comment (not ``internal``) also passes `check_session_text`."""
    if body in BOT_COMMANDS and not internal:
        if not on_pr:
            raise BrokerRefused("a bot command is allowed only on this run's PR")
        return
    if _AGENT_LINE.search(body):
        raise BrokerRefused("a comment may not hold a line starting with /agent")
    if body != MARKER and not body.startswith(MARKER + "\n"):
        raise BrokerRefused(f"a comment must start with the line {MARKER}")
    if not internal:
        check_session_text(body)


def check_risk_issue(title: str, body: str, labels: Iterable[str], pr_url: str) -> None:
    """A risk issue carries exactly the ``autodev:risk`` label, the marker and the PR link."""
    if list(labels) != [RISK_LABEL]:
        raise BrokerRefused(f"a risk issue carries exactly the label {RISK_LABEL}")
    if not body.startswith(MARKER + "\n"):
        raise BrokerRefused(f"a risk issue body must start with the line {MARKER}")
    if not pr_url or pr_url not in body:
        raise BrokerRefused("a risk issue must link this run's PR")
    if not title.strip() or len(title) > TITLE_MAX:
        raise BrokerRefused("a risk issue needs a title of at most 256 characters")
    check_session_text(f"{title}\n{body}")


# --- the broker -----------------------------------------------------------------------------------

class Broker:
    """One run's broker; see the module docstring.

    ``credentials`` are required in a real mode and checked here (`CredentialsMissing`). The
    network (``transport``), git (``git``), the validated-review head (``reviewed_head``), the
    pre-mortem record (``premortem``) and the earlier records are injectable. ``run_dir`` must not
    lie inside or around any of ``session_writable`` (`UnsafeRunDirectory`): the broker trusts the
    review and pre-mortem records and its own log there. ``decoys`` are the sandbox's decoy markers
    (`src.schedules.decoys`, #343): the scan knows them in every mode, and a hit on one also calls
    ``on_decoy`` with the hits (the run sequence turns the schedule off there) before the refusal.
    ``known`` are further secrets of the run, such as the Codex API key (#344,
    `codex_probes.Decision.known`): the scan refuses them in every mode and the log masks them.
    """

    def __init__(self, run: Run, *, credentials: Credentials | None, git: TrustedGit,
                 run_dir: str | Path | None = None, session_writable: Iterable[str | Path] = (),
                 transport: ApiTransport = urllib_transport,
                 reviewed_head: Callable[[], str | None] | None = None,
                 premortem: Callable[[], Mapping[str, RiskFinding]] | None = None,
                 earlier_records: Callable[[], list[dict]] = records.all_records,
                 key_reader: Callable[[Path], bytes] = Path.read_bytes,
                 decoys: Iterable[scan.KnownSecret] = (),
                 on_decoy: Callable[[tuple[scan.Hit, ...]], None] | None = None,
                 known: Iterable[scan.KnownSecret] = ()):
        self.run = run
        self.credentials = credentials
        self.git = git
        self.run_dir = Path(run_dir) if run_dir is not None else None
        if self.run_dir is not None:
            try:
                check_directory(self.run_dir, session_writable)
            except UnsafeSocketDirectory as error:
                raise UnsafeRunDirectory(f"the run directory is unsafe: {error}") from None
        self._reviewed_head = reviewed_head or (lambda: validated_review_head(self.run_dir) if self.run_dir else None)
        self._premortem = premortem or (lambda: read_premortem(self.run_dir) if self.run_dir else {})
        self._earlier_records = earlier_records
        self._lock = threading.RLock()
        self._token: str | None = None
        self.branch: str | None = None  # set by the first push
        known = list(known)
        if not all(isinstance(secret, scan.KnownSecret) for secret in known):
            raise TypeError("known secrets must be KnownSecret instances")
        if not run.read_only:
            known = self._open(key_reader) + known
        decoys = tuple(decoys)
        self._decoy_labels = frozenset(secret.label for secret in decoys)
        self._on_decoy = on_decoy
        known += decoys
        self.scanner = scan.Scanner(known)
        self.log = BrokerLog(self.run_dir / LOG_NAME if self.run_dir else None,
                             [self._token or ""] + [secret.value for secret in known if isinstance(secret.value, str)])
        self.api = _Api(run.repository, self._api_token, transport)

    def _open(self, key_reader: Callable[[Path], bytes]) -> list[scan.KnownSecret]:
        """Check both credentials of a real run and return them as known secrets of the scan."""
        missing = []
        if self.credentials is None:
            raise CredentialsMissing(["the deploy key", "the pinned github.com host keys", "the API token"])
        if not _regular_file(self.credentials.deploy_key):
            missing.append("the deploy key")
        if not _regular_file(self.credentials.known_hosts):
            missing.append("the pinned github.com host keys")
        if _whitespace(self.credentials.deploy_key) or _whitespace(self.credentials.known_hosts):
            missing.append("a deploy key and host keys path without whitespace")
        try:
            self._token = self.credentials.token() or None
        except github.TokenUnavailable:
            self._token = None
        if self._token is None:
            missing.append("the API token")
        if missing:
            raise CredentialsMissing(missing)
        known = [scan.KnownSecret("broker token", self._token)]
        key = key_reader(self.credentials.deploy_key).decode("latin-1").strip()
        if len(key) >= scan.KNOWN_MIN_LENGTH:
            known.append(scan.KnownSecret("deploy key", key))
        return known

    def _api_token(self) -> str:
        if self._token is None:
            if self.credentials is None:
                raise BrokerRefused("no API token is configured")
            try:
                self._token = self.credentials.token()
            except github.TokenUnavailable as error:
                raise BrokerRefused(str(error)) from None
        return self._token

    # --- shared checks ---------------------------------------------------------------------------

    def _writable(self, op: str, request: RequestContext | None = None) -> None:
        if self.run.read_only:
            self.log.add(op, "refused", reason="dry-run")
            raise ReadOnly(f"{op}: the broker is read-only in dry-run")
        if request is not None and request.closing():
            raise BrokerRefused(f"{op}: the run is closing")

    def _scan(self, op: str, text: str) -> None:
        hits = self.scanner.scan(text)
        if hits:
            decoy_hits = tuple(hit for hit in hits if hit.kind == scan.HitKind.KNOWN and hit.name in self._decoy_labels)
            if decoy_hits and self._on_decoy is not None:
                self._on_decoy(decoy_hits)
            error = scan.SecretFound(hits)
            self.log.add(op, "refused", reason=str(error))
            raise BrokerRefused(f"{op}: {error}")

    def _refuse(self, op: str, message: str, **details: Any) -> BrokerRefused:
        """Log a refusal and return the error to raise."""
        self.log.add(op, "refused", reason=message, **details)
        return BrokerRefused(f"{op}: {message}")

    def _closing_only_own(self, op: str, text: str) -> None:
        for repository, number in closing_references(text):
            if number != self.run.issue or (repository and repository.lower() != self.run.repository.lower()):
                raise self._refuse(op, f"closing keywords may name only issue #{self.run.issue}")

    def _session_text(self, op: str, text: str) -> None:
        try:
            check_session_text(text)
        except BrokerRefused as error:
            raise self._refuse(op, str(error)) from None

    def _marker(self, op: str, body: str, *, on_pr: bool) -> None:
        try:
            check_marker(body, on_pr=on_pr)
        except BrokerRefused as error:
            raise self._refuse(op, str(error)) from None

    def _own_number(self, op: str, number: int) -> None:
        if number not in {self.run.issue, self.run.pr}:
            raise BrokerRefused(f"{op}: #{number} is neither this run's issue nor its PR")

    def _write(self, op: str, method: str, path: str, payload: Any = None, *, absent_ok: bool = False,
               request: RequestContext | None = None, **details: Any) -> Any:
        """One logged REST write: ``intent`` before, ``done`` with the object's ID or ``failed`` (with
        the HTTP status when GitHub answered). With ``absent_ok`` (a label removal), a 404 counts as
        already done. A session's write checks ``request.closing()`` right before it is sent."""
        if request is not None and request.closing():
            raise self._refuse(op, "the run is closing", path=path)
        self.log.add(op, "intent", method=method, path=path, **details)
        try:
            answer = self.api.rest(method, path, payload)
        except github.GitHubError as error:
            if absent_ok and error.status == 404:
                self.log.add(op, "done", path=path, id=None, note="already absent", **details)
                return None
            self.log.add(op, "failed", path=path, error=str(error), status=error.status, **details)
            raise BrokerRefused(f"{op}: {error}") from None
        except github.NetworkError as error:
            self.log.add(op, "failed", path=path, error=str(error))
            raise BrokerRefused(f"{op}: {error}") from None
        identifier = answer.get("id") if isinstance(answer, dict) else None
        self.log.add(op, "done", path=path, id=identifier,
                     number=answer.get("number") if isinstance(answer, dict) else None, **details)
        return answer

    def _base(self) -> str:
        return f"/repos/{self.run.repository}"

    # --- socket operations ------------------------------------------------------------------------

    def push(self, args: dict, request: RequestContext) -> dict:
        self._writable("push", request)
        with self._lock:
            head, slug = args["head"], args["slug"]
            try:
                branch = repos.branch_name(self.run.issue, slug)
            except ValueError as error:
                raise self._refuse("push", str(error), head=head) from None
            if self.branch is not None and branch != self.branch:
                raise self._refuse("push", f"this run pushes only {self.branch}", head=head)
            if self.git.branch_head(branch) != head:
                raise self._refuse("push", f"{head[:12]} is not the imported head of {branch} in the trusted repository",
                                   head=head)
            if self._reviewed_head() != head:
                raise self._refuse("push", f"no validated review covers head {head[:12]}", head=head)
            try:
                verdict = self.git.check(self.run.base, head, self.run.labels)
            except paths.PathsError as error:
                raise self._refuse("push", f"the change check failed: {error}", head=head) from None
            if not verdict.allowed:
                listed = ", ".join(f"{refusal.path or refusal.commit[:12]} ({refusal.reason})"
                                   for refusal in verdict.refusals[:10])
                self.log.add("push", "refused", head=head, reason=f"change check: {listed}")
                raise BrokerRefused(f"push: the change check refuses {listed}")
            try:
                messages, diff = self.git.outgoing(self.run.base, head)
            except gitsafe.GitError as error:
                raise self._refuse("push", f"the outgoing text cannot be read: {error}", head=head) from None
            self._scan("push", f"{messages}\n{diff}")
            # Only commit messages: GitHub honours closing keywords there, never in a diff (#342 review).
            self._closing_only_own("push", messages)
            if self.credentials is None:
                raise self._refuse("push", "no deploy key", head=head)
            try:
                argv, env = push_command(self.git.path, self.run.repository, head, self.run.issue, slug,
                                         self.credentials)
            except ValueError as error:
                raise self._refuse("push", str(error), head=head) from None
            if request.closing():
                raise BrokerRefused("push: the run is closing")
            self.log.add("push", "intent", branch=branch, head=head)
            try:
                result = self.git.push(argv, env, min(PUSH_TIMEOUT, max(1.0, request.remaining() - 5)))
            except (OSError, subprocess.SubprocessError) as error:
                self.log.add("push", "failed", branch=branch, head=head, error=type(error).__name__)
                raise BrokerRefused(f"push: git push could not run: {type(error).__name__}") from None
            if result.returncode != 0:
                self.log.add("push", "failed", branch=branch, head=head, error=f"exit code {result.returncode}")
                raise BrokerRefused(f"push: git push failed with exit code {result.returncode}")
            self.branch = branch
            self.log.add("push", "done", branch=branch, head=head, id=head)
            return {"branch": branch, "head": head}

    def pr_create(self, args: dict, request: RequestContext) -> dict:
        self._writable("pr_create", request)
        with self._lock:
            if self.run.pr is not None:
                raise BrokerRefused(f"pr_create: this run already has PR #{self.run.pr}")
            if self.branch is None:
                raise BrokerRefused("pr_create: this run has pushed no branch")
            title, body = args["title"], args["body"]
            self._check_pr_text("pr_create", title, body)
            payload = {"title": title, "body": body, "head": self.branch, "base": "main",
                       "draft": bool(args.get("draft", False)), "maintainer_can_modify": False}
            answer = self._write("pr_create", "POST", f"{self._base()}/pulls", payload, branch=self.branch,
                                 request=request)
            number = answer.get("number") if isinstance(answer, dict) else None
            if not isinstance(number, int):
                raise BrokerRefused("pr_create: GitHub returned no PR number")
            self.run.pr = number
            return {"number": number, "id": answer.get("id"), "url": answer.get("html_url")}

    def pr_edit(self, args: dict, request: RequestContext) -> dict:
        self._writable("pr_edit", request)
        with self._lock:
            if self.run.pr is None:
                raise BrokerRefused("pr_edit: this run has no PR")
            payload = {key: args[key] for key in ("title", "body") if key in args}
            if not payload:
                raise BrokerRefused("pr_edit: nothing to change")
            self._check_pr_text("pr_edit", payload.get("title", ""), payload.get("body", ""))
            answer = self._write("pr_edit", "PATCH", f"{self._base()}/pulls/{self.run.pr}", payload, pr=self.run.pr,
                                 request=request)
            return {"number": self.run.pr, "id": (answer or {}).get("id")}

    def _check_pr_text(self, op: str, title: str, body: str) -> None:
        if _AGENT_LINE.search(body) or _AGENT_LINE.search(title):
            raise self._refuse(op, "the PR text may not hold a line starting with /agent")
        self._session_text(op, f"{title}\n{body}")
        self._closing_only_own(op, f"{title}\n{body}")
        self._scan(op, f"{title}\n{body}")

    def comment(self, args: dict, request: RequestContext) -> dict:
        self._writable("comment", request)
        with self._lock:
            number, body = args["number"], args["body"]
            self._own_number("comment", number)
            self._marker("comment", body, on_pr=self.run.pr is not None and number == self.run.pr)
            self._closing_only_own("comment", body)
            self._scan("comment", body)
            answer = self._write("comment", "POST", f"{self._base()}/issues/{number}/comments", {"body": body},
                                 target=number, request=request)
            return {"id": answer.get("id"), "target": number}

    def comment_edit(self, args: dict, request: RequestContext) -> dict:
        self._writable("comment_edit", request)
        with self._lock:
            comment_id, body = args["comment_id"], args["body"]
            # Only what this run's gh.comment wrote: never a selection comment, an earlier run's
            # recorded (state) comment or one the runner wrote.
            logged = {entry.get("id") for entry in self.log.done("comment")}
            if comment_id not in logged:
                raise self._refuse("comment_edit", f"comment {comment_id} was not written by this run's gh.comment",
                                   id=comment_id)
            self._marker("comment_edit", body, on_pr=False)
            self._closing_only_own("comment_edit", body)
            self._scan("comment_edit", body)
            answer = self._write("comment_edit", "PATCH", f"{self._base()}/issues/comments/{comment_id}",
                                 {"body": body}, request=request)
            return {"id": (answer or {}).get("id", comment_id)}

    def label(self, args: dict, request: RequestContext) -> dict:
        self._writable("label", request)
        with self._lock:
            number, label, action = args["number"], args["label"], args["action"]
            if label not in RUN_LABELS:
                raise self._refuse("label", f"only {', '.join(sorted(RUN_LABELS))} may be set or removed",
                                   target=number, label=label)
            self._own_number("label", number)
            if label == PR_LABEL and (action != "add" or number != self.run.pr):
                raise self._refuse("label", f"{PR_LABEL} may only be added, and only to this run's PR",
                                   target=number, label=label)
            return self._set_label("label", number, label, action, request)

    def _set_label(self, op: str, number: int, label: str, action: str,
                   request: RequestContext | None = None) -> dict:
        if action == "add":
            self._write(op, "POST", f"{self._base()}/issues/{number}/labels", {"labels": [label]},
                        target=number, label=label, action=action, request=request)
        else:
            self._write(op, "DELETE", f"{self._base()}/issues/{number}/labels/{quote(label, safe='')}",
                        absent_ok=True, target=number, label=label, action=action, request=request)
        return {"target": number, "label": label, "action": action}

    def threads(self, args: dict, request: RequestContext) -> dict:
        return {"threads": [{"id": thread["id"], "resolved": bool(thread.get("isResolved")),
                             "resolvable": _resolvable(thread), "path": thread.get("path")}
                            for thread in self._threads(request)]}

    def _threads(self, request: RequestContext) -> list[dict]:
        if self.run.pr is None:
            raise BrokerRefused("threads: this run has no PR")
        owner, name = self.run.repository.split("/", 1)
        found, after = [], None
        for _ in range(MAX_THREAD_PAGES):
            self._time_left("threads", request)
            data = self._graphql(THREADS_QUERY, {"owner": owner, "name": name, "number": self.run.pr, "after": after})
            threads = (((data.get("repository") or {}).get("pullRequest") or {}).get("reviewThreads") or {})
            found.extend(node for node in threads.get("nodes") or [] if isinstance(node, dict) and node.get("id"))
            page = threads.get("pageInfo") or {}
            if not page.get("hasNextPage"):
                return found
            after = page.get("endCursor")
        raise BrokerRefused(f"threads: more than {MAX_THREAD_PAGES} pages")

    def resolve_thread(self, args: dict, request: RequestContext) -> dict:
        self._writable("resolve_thread", request)
        with self._lock:
            thread_id = args["thread_id"]
            thread = next((item for item in self._threads(request) if item["id"] == thread_id), None)
            if thread is None:
                raise self._refuse("resolve_thread", f"{thread_id} is not a review thread of this run's PR",
                                   id=thread_id)
            if not _resolvable(thread):
                raise self._refuse("resolve_thread", "the thread was started by neither an allowlisted bot nor the agent",
                                   id=thread_id)
            if request.closing():
                raise BrokerRefused("resolve_thread: the run is closing")
            self._time_left("resolve_thread", request)
            self.log.add("resolve_thread", "intent", pr=self.run.pr, id=thread_id)
            try:
                self._graphql(RESOLVE_MUTATION, {"threadId": thread_id})
            except BrokerRefused as error:
                self.log.add("resolve_thread", "failed", pr=self.run.pr, id=thread_id, error=str(error))
                raise BrokerRefused(f"resolve_thread: {error}") from None
            self.log.add("resolve_thread", "done", pr=self.run.pr, id=thread_id)
            return {"id": thread_id, "resolved": True}

    @staticmethod
    def _time_left(op: str, request: RequestContext) -> None:
        """Refuse to start an API call that could outlast the request's deadline (each call may take
        up to ``TIMEOUT`` seconds)."""
        if request.remaining() < TIMEOUT:
            raise BrokerRefused(f"{op}: too little time left before the request's deadline; ask again")

    def _graphql(self, document: str, variables: Mapping[str, Any]) -> dict:
        try:
            return self.api.graphql(document, variables)
        except (github.GitHubError, github.NetworkError) as error:
            raise BrokerRefused(str(error)) from None

    def read(self, args: dict, request: RequestContext) -> Any:
        template, needs = READS[args["endpoint"]]
        if needs not in args:
            raise BrokerRefused(f"read {args['endpoint']}: argument {needs} is required")
        path = self._base() + template.format(**{needs: args[needs]})
        params = {"per_page": 100, "page": args["page"]} if "page" in args else None
        try:
            data, more = self.api.rest_page("GET", path, params=params)
        except (github.GitHubError, github.NetworkError) as error:
            raise BrokerRefused(str(error)) from None
        return {"data": data, "next_page": more}

    def _read(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        try:
            return self.api.rest("GET", path, params=params)
        except (github.GitHubError, github.NetworkError) as error:
            raise BrokerRefused(str(error)) from None

    # --- runner-internal operations (never registered with the socket) ---------------------------

    def cleanup(self, actions: Iterable[Mapping]) -> list[dict]:
        """Execute the gate's cleanup actions (#338) on issues or PRs earlier run records name."""
        self._writable("cleanup")
        allowed = bound_numbers(self._earlier_records())
        done = []
        with self._lock:
            for action in actions:
                number, comment = action.get("issue"), action.get("comment")
                if action.get("op") != "remove_label" or action.get("label") != CLAIM_LABEL:
                    raise BrokerRefused(f"cleanup: not a gate action: {action.get('op')!r}")
                if not isinstance(number, int) or number not in allowed:
                    raise BrokerRefused(f"cleanup: #{number} is in no earlier run record")
                if comment is not None:
                    check_marker(comment, on_pr=False)
                    self._scan("cleanup", comment)
                    self._write("cleanup", "POST", f"{self._base()}/issues/{number}/comments", {"body": comment},
                                target=number)
                # A 404 means the owner already removed the label: done, and the next action runs.
                self._write("cleanup", "DELETE", f"{self._base()}/issues/{number}/labels/{quote(CLAIM_LABEL, safe='')}",
                            absent_ok=True, target=number, label=CLAIM_LABEL)
                done.append({"issue": number, "label": CLAIM_LABEL})
        return done

    def select(self, body: str) -> dict:
        """The selection comment and the claim label on this run's issue; returns the comment ID
        for the run record's ``comments``."""
        self._writable("select")
        if not body.startswith(f"{MARKER}\n{SELECTION_MARKER}"):
            raise BrokerRefused(f"select: the selection comment starts with {MARKER} and {SELECTION_MARKER}")
        with self._lock:
            check_marker(body, on_pr=False, internal=True)
            self._scan("select", body)
            answer = self._write("select", "POST", f"{self._base()}/issues/{self.run.issue}/comments", {"body": body},
                                 target=self.run.issue)
            self._set_label("select", self.run.issue, CLAIM_LABEL, "add")
            return {"id": answer.get("id"), "kind": "selection"}

    def bind_resume(self, pr: int) -> int:
        """Bind a ``resume`` run to self-development PR ``pr`` of this run's issue."""
        if self.run.mode not in ("resume", "dry-run"):
            raise BrokerRefused("bind_resume: only a resume run binds an existing PR")
        pull = self._read(f"{self._base()}/pulls/{pr}")
        if not isinstance(pull, dict) or pull.get("number") != pr or pull.get("state") != "open" or \
                not is_self_development(pull, self.run.repository) or issue_of(pull) != self.run.issue:
            raise BrokerRefused(f"bind_resume: #{pr} is no open self-development PR of issue #{self.run.issue}")
        with self._lock:
            self.run.pr = pr
            self.branch = (pull.get("head") or {}).get("ref")
            self.log.add("bind_resume", "done", id=pull.get("id"), pr=pr, branch=self.branch)
        return pr

    def create_risk_issues(self, finding_ids: Iterable[str]) -> list[dict]:
        """Risk issues (Q3) from findings of the validated pre-mortem record; see the module docstring."""
        self._writable("risk_issue")
        if self.run.pr is None:
            raise BrokerRefused("risk_issue: this run has no PR to link")
        findings = self._premortem()
        created = []
        with self._lock:
            for finding_id in finding_ids:
                # The log holds every earlier broker of this run too (it is read back on open).
                attempts = self._risk_attempts()
                if finding_id in attempts:
                    raise self._refuse("risk_issue", f"{finding_id!r} already has a risk issue, or one may have been "
                                       "created", finding=finding_id)
                if len(attempts) >= MAX_RISK_ISSUES:
                    raise self._refuse("risk_issue", f"at most {MAX_RISK_ISSUES} risk issues per run",
                                       finding=finding_id)
                finding = findings.get(finding_id)
                if finding is None:
                    raise BrokerRefused(f"risk_issue: {finding_id!r} is not in the run's validated pre-mortem record")
                if finding.level not in RISK_LEVELS:
                    raise BrokerRefused(f"risk_issue: {finding_id} is of level {finding.level!r}, below medium")
                if security_signal(finding):
                    self.log.add("risk_issue", "refused", finding=finding_id, reason="security signal: kept local")
                    raise BrokerRefused(f"risk_issue: {finding_id} carries a security signal and stays local")
                pr_url = f"https://github.com/{self.run.repository}/pull/{self.run.pr}"
                body = f"{MARKER}\n{finding.body}\n\nFrom the pre-mortem of {pr_url} (finding {finding.id}, level {finding.level})."
                created.append(self._risk_issue(finding.title, body, [RISK_LABEL], pr_url, finding.id))
        return created

    def _risk_attempts(self) -> set[str]:
        """Findings with a risk issue that may exist: every logged attempt except one GitHub refused
        with an HTTP status. A network error may follow a created issue, so it still counts."""
        attempts, refused = set(), set()
        for entry in self.log.entries:
            if entry.get("op") != "risk_issue" or not isinstance(entry.get("finding"), str):
                continue
            if entry.get("phase") in ("intent", "done"):
                attempts.add(entry["finding"])
            elif entry.get("phase") == "failed" and isinstance(entry.get("status"), int):
                refused.add(entry["finding"])
        done = {entry.get("finding") for entry in self.log.done("risk_issue")}
        return {finding for finding in attempts if finding in done or finding not in refused}

    def _risk_issue(self, title: str, body: str, labels: list[str], pr_url: str, finding_id: str) -> dict:
        check_risk_issue(title, body, labels, pr_url)
        check_marker(body, on_pr=False)
        self._closing_only_own("risk_issue", f"{title}\n{body}")
        self._scan("risk_issue", f"{title}\n{body}")
        answer = self._write("risk_issue", "POST", f"{self._base()}/issues", {"title": title, "body": body,
                                                                              "labels": labels}, finding=finding_id)
        return {"number": answer.get("number"), "id": answer.get("id"), "finding": finding_id}


def _resolvable(thread: Mapping) -> bool:
    """The first comment is an allowlisted bot's, or the owner account's with the agent marker."""
    comments = ((thread.get("comments") or {}).get("nodes") or [])
    if not comments or not isinstance(comments[0], dict):
        return False
    first = comments[0]
    author = first.get("author") or {}
    if not isinstance(author, dict):
        return False
    login, identifier = author.get("login"), author.get("databaseId")
    # GraphQL names a bot ``coderabbitai`` (``__typename`` Bot); REST and TRUSTED_BOTS say
    # ``coderabbitai[bot]``. The account ID is the same in both.
    if author.get("__typename") == "Bot" and isinstance(login, str) and type(identifier) is int:
        return trusted_bot({"login": f"{login}[bot]", "id": identifier})
    return (author.get("__typename") == "User" and identifier == OWNER_ID
            and str(first.get("body") or "").startswith(MARKER + "\n"))


def register(registry: Registry, broker: Broker) -> Broker:
    """Register the broker's named ``gh.*`` operations, each bound to the current stage session."""
    text = Field(str, text=True)
    title = Field(str, text=True, max_length=TITLE_MAX)
    number = Field(int, maximum=10 ** 9)
    session = {"session_bound": True}
    registry.register("gh.push", broker.push, {"slug": Field(str, pattern=_SLUG, max_length=repos._MAX_SLUG),
                                               "head": Field(str, pattern=_SHA)},
                      deadline=PUSH_DEADLINE, **session)
    registry.register("gh.pr_create", broker.pr_create,
                      {"title": title, "body": text, "draft": Field(bool, required=False)},
                      deadline=WRITE_DEADLINE, **session)
    registry.register("gh.pr_edit", broker.pr_edit,
                      {"title": Field(str, text=True, max_length=TITLE_MAX, required=False),
                       "body": Field(str, text=True, required=False)}, deadline=WRITE_DEADLINE, **session)
    registry.register("gh.comment", broker.comment, {"number": number, "body": text}, deadline=WRITE_DEADLINE,
                      **session)
    registry.register("gh.comment_edit", broker.comment_edit,
                      {"comment_id": Field(int, maximum=COMMENT_ID_MAX), "body": text}, deadline=WRITE_DEADLINE,
                      **session)
    registry.register("gh.label", broker.label, {"number": number, "label": Field(str, pattern=_LABEL),
                                                 "action": Field(str, pattern=_ACTION)},
                      deadline=WRITE_DEADLINE, **session)
    registry.register("gh.threads", broker.threads, deadline=WRITE_DEADLINE, **session)
    registry.register("gh.resolve_thread", broker.resolve_thread, {"thread_id": Field(str, pattern=_THREAD_ID)},
                      deadline=WRITE_DEADLINE, **session)
    registry.register("gh.read", broker.read,
                      {"endpoint": Field(str, pattern=_READ_NAME), "number": Field(int, required=False, maximum=10 ** 9),
                       "sha": Field(str, pattern=_SHA, required=False),
                       "page": Field(int, required=False, maximum=github.MAX_PAGES)},
                      deadline=WRITE_DEADLINE, **session)
    return broker
