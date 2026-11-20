"""Shared external review allowances as claimable cooperation resources.

An AI reviewer's review allowance belongs to the account or installation that
owns it, not to a pull request and not to a repository, so concurrent sessions
spend one budget. This module derives the claim key from what the platform
reports and turns an observed limit response into a published next free time.

No reviewer is special. A reviewer identity this code has never seen still
produces a key, and an unrecognised wording still produces a pause, so an
unknown reviewer can never fall through to requesting reviews freely.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Literal

RESOURCE_TYPE = "external_quota"
#: One session spends the allowance at a time, so the claim is a write.
ACCESS = "write"
#: Used when the reviewer states no wait and no rule knows its reset.
DEFAULT_BACKOFF_SECONDS = 3600.0
#: A reviewer's own number is untrusted text; a week is already far past any
#: observed reset, so an absurd or garbled wait is capped rather than believed.
MAX_WAIT_SECONDS = 7 * 86400.0
#: GitHub appends this to an app's login on reviews but not on check runs.
BOT_SUFFIX = "[bot]"
#: Platform slugs that spend one allowance. GitHub reports Copilot as
#: `copilot-pull-request-reviewer` on a review and as `copilot` elsewhere,
#: and both draw on the account's single Copilot budget, so a key built from
#: either surface has to be the same key.
REVIEWER_ALIASES = {"copilot-pull-request-reviewer": "copilot"}

_HOST = re.compile(r"^[a-z0-9]([a-z0-9.-]*[a-z0-9])?$")
_NAME = re.compile(r"^[a-z0-9]([a-z0-9._-]*[a-z0-9])?$")

WaitSource = Literal["stated", "rule", "default"]


def _text(value: object, what: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{what} must be non-blank text")
    return value.strip()


def host_identity(host: object) -> str:
    """Canonical platform host: no scheme, no port, no path, case folded."""
    text = _text(host, "host").casefold()
    text = text.split("://", 1)[-1].split("/", 1)[0].split("@")[-1].split(":", 1)[0]
    if not _HOST.match(text):
        raise ValueError("host must be a bare hostname, for example github.com")
    return text


def account_identity(account: object) -> str:
    """Canonical owner of the allowance: the account or installation login.

    Platform logins are compared case-insensitively, so two sessions spelling
    the same owner differently must still collide on one claim.
    """
    text = _text(account, "account").casefold()
    if not _NAME.match(text):
        raise ValueError("account must be a platform login or installation slug")
    return text


def reviewer_identity(reviewer: object) -> str:
    """Canonical reviewer identity: its login or app slug without ``[bot]``.

    One reviewer appears as ``coderabbitai[bot]`` on a review and
    ``coderabbitai`` on a check run. Both spend the same allowance, so both
    have to produce the same key, and so do the slugs in ``REVIEWER_ALIASES``.
    """
    text = _text(reviewer, "reviewer").casefold()
    if text.endswith(BOT_SUFFIX):
        text = text[: -len(BOT_SUFFIX)].strip()
    if not _NAME.match(text):
        raise ValueError("reviewer must be a platform login or app slug")
    return REVIEWER_ALIASES.get(text, text)


def quota_key(host: object, account: object, reviewer: object) -> str:
    """Build the ``external_quota`` resource key ``<host>/<account>:<reviewer>``.

    The repository is deliberately absent: the allowance is not per repository.
    """
    return f"{host_identity(host)}/{account_identity(account)}:{reviewer_identity(reviewer)}"


def bare_host(host: object) -> str:
    """The host of an existing key, which is bare already, so only case folds.

    ``host_identity`` strips a scheme, userinfo and a port from a platform
    input. A key has none of those by construction, so stripping them while
    parsing one would alias ``github.com:1234/iezhu:x`` and
    ``user@github.com/iezhu:x`` onto the real allowance instead of refusing a
    key that does not follow the documented form.
    """
    text = _text(host, "host").casefold()
    if not _HOST.match(text):
        raise ValueError("an allowance key's host is a bare hostname, for example github.com")
    return text


def parse_quota_key(key: object) -> tuple[str, str, str]:
    """Split a key into its canonical host, account and reviewer parts.

    The ``[bot]`` suffix still folds: it is how one reviewer identity differs
    between a review and a check run, not a separate spelling of the key.
    """
    host, slash, rest = _text(key, "resource key").partition("/")
    account, colon, reviewer = rest.partition(":")
    if not slash or not colon:
        raise ValueError("an external allowance key is <host>/<account>:<reviewer>")
    return bare_host(host), account_identity(account), reviewer_identity(reviewer)


def canonical_quota_key(key: object) -> str:
    """Re-canonicalize a supplied key so differing spellings claim one resource."""
    return quota_key(*parse_quota_key(key))


@dataclass(frozen=True)
class WaitRule:
    """One row of reviewer-specific wait knowledge; everything else is generic.

    ``unit_seconds`` multiplies the pattern's first capture group when the
    reviewer states a wait. ``fixed_seconds`` is the documented reset of a
    reviewer that states none. ``reviewer`` ``None`` matches any reviewer.
    """

    rule_id: str
    pattern: str
    reviewer: str | None = None
    unit_seconds: float | None = None
    fixed_seconds: float | None = None

    def __post_init__(self) -> None:
        if (self.unit_seconds is None) == (self.fixed_seconds is None):
            raise ValueError("a rule either extracts a wait or states a fixed one")

    def matches_reviewer(self, reviewer: str) -> bool:
        """Match the canonical identity exactly; a row without one matches all.

        ``REVIEWER_ALIASES`` has already folded the slugs that share a budget,
        so matching a prefix as well would only guess: a reviewer that merely
        shares a prefix is a different budget, and an unlisted slug is served
        better by the default backoff than by another reviewer's reset.
        """
        return self.reviewer is None or reviewer == self.reviewer

    def wait(self, evidence: str) -> tuple[float, str] | None:
        """Return the wait in seconds and the verbatim matched span, or None."""
        found = re.search(self.pattern, evidence, re.IGNORECASE)
        if not found:
            return None
        if self.fixed_seconds is not None:
            return self.fixed_seconds, found.group(0)
        stated = int(found.group(1))
        return stated * self.unit_seconds, found.group(0)


#: Ordered: reviewer-specific rows first, then wordings any reviewer may use.
#: Observed on this repository's pull requests in 2026-09, 2026-10 and 2026-11.
WAIT_RULES: tuple[WaitRule, ...] = (
    WaitRule("coderabbit_included_review", reviewer="coderabbitai",
             pattern=r"next included review available in\s*(\d{1,9})\s*minutes?", unit_seconds=60.0),
    WaitRule("copilot_account_quota", reviewer="copilot",
             pattern=r"reached their quota limit", fixed_seconds=86400.0),
    WaitRule("stated_seconds", unit_seconds=1.0,
             pattern=r"(?:retry after|retry in|try again in|available in|wait)\s*(\d{1,9})\s*seconds?"),
    WaitRule("stated_minutes", unit_seconds=60.0,
             pattern=r"(?:retry after|retry in|try again in|available in|wait|in)\s*(\d{1,9})\s*minutes?"),
    WaitRule("stated_hours", unit_seconds=3600.0,
             pattern=r"(?:retry after|retry in|try again in|available in|wait|in)\s*(\d{1,9})\s*hours?"),
)


#: A stated wait is read before a fixed reset: the fixed number is the
#: documented reset of a response that states none, so preferring it would
#: hold the allowance longer than the reviewer itself asked for.
_STATED_RULES = tuple(rule for rule in WAIT_RULES if rule.unit_seconds is not None)
_FIXED_RULES = tuple(rule for rule in WAIT_RULES if rule.fixed_seconds is not None)


@dataclass(frozen=True)
class Pause:
    """A reviewer's allowance pause, measured from the evidence's own time."""

    reviewer: str
    observed_at: float
    wait_seconds: float
    next_free_at: float
    source: WaitSource
    rule_id: str | None
    quote: str


def observed_pause(reviewer: object, evidence: object, *, observed_at: float,
                   default_seconds: float = DEFAULT_BACKOFF_SECONDS) -> Pause:
    """Turn an observed limit response into a next free time.

    Call this only with evidence already read as a quota, rate-limit or
    allowance response. States that merely look like one, such as a reviewer
    that skipped or self-paused a branch, are not allowance pauses and are
    handled by the flow instead.

    The wait is measured from ``observed_at``, the time the evidence itself
    carries, not from now: a pause read minutes later must not be extended.
    An unmatched wording returns ``default_seconds`` with ``source="default"``
    and the evidence quoted verbatim, never no pause at all. A stated wait is
    capped at ``MAX_WAIT_SECONDS``; the quote keeps what the reviewer said.
    """
    identity = reviewer_identity(reviewer)
    text = _text(evidence, "evidence")
    if not isinstance(observed_at, (int, float)) or isinstance(observed_at, bool) or observed_at < 0:
        raise ValueError("observed_at must be a nonnegative timestamp")
    if not 0 < default_seconds <= MAX_WAIT_SECONDS:
        raise ValueError("the default backoff must be positive and at most MAX_WAIT_SECONDS")
    for rules in (_STATED_RULES, _FIXED_RULES):
        for rule in rules:
            if not rule.matches_reviewer(identity):
                continue
            found = rule.wait(text)
            if found is None:
                continue
            seconds, quote = found
            seconds = min(float(seconds), MAX_WAIT_SECONDS)
            source: WaitSource = "stated" if rule.unit_seconds is not None else "rule"
            return Pause(identity, float(observed_at), seconds,
                         float(observed_at) + seconds, source, rule.rule_id, quote)
    return Pause(identity, float(observed_at), float(default_seconds),
                 float(observed_at) + float(default_seconds), "default", None, text)


def pause_observation(resource_key: object, pause: Pause) -> dict:
    """Checkpoint record publishing a paused allowance for other sessions."""
    return {"resource_key": canonical_quota_key(resource_key), "kind": "paused",
            "evidence": pause.quote, "observed_at": pause.observed_at,
            "next_free_at": pause.next_free_at, "wait_source": pause.source}


def consumed_observation(resource_key: object, evidence: object, *, observed_at: float) -> dict:
    """Checkpoint record for allowance a review has already spent.

    An automatic review cannot be queued, so it is published after the fact
    rather than claimed; a request the reviewer accepted is published the same
    way, so the next holder of the key does not ask again inside the window
    it spent. It carries no next free time: other sessions repeat a
    published time in their own refusals, and a run that already happened says
    nothing about when the next one may. A response that does state a wait is
    a pause, so publish it with ``pause_observation``.
    """
    if not isinstance(observed_at, (int, float)) or isinstance(observed_at, bool) or observed_at < 0:
        raise ValueError("observed_at must be a nonnegative timestamp")
    return {"resource_key": canonical_quota_key(resource_key), "kind": "consumed",
            "evidence": _text(evidence, "evidence"), "observed_at": float(observed_at),
            "next_free_at": None, "wait_source": None}


def resource(resource_key: object, *, access: str = ACCESS) -> dict:
    """The ``ResourceSpec`` to register in scope and to acquire before a request."""
    return {"resource_type": RESOURCE_TYPE, "resource_key": canonical_quota_key(resource_key),
            "access": access}
