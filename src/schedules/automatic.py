"""The automatic mode (#350; design 5.7, 11 and 14, phase 1b): scheduled real runs under the
owner's account, and what `autodev ready` reads from this machine.

A scheduled real run (``run`` on a slot, and the ``resume`` of a continuation) starts only while
``autodev ready`` passes (`runner.readiness`, reference docs/autodev-runner.md#automatic-mode):

* the real-run preconditions of #347 (`preconditions.check`, read from GitHub with the broker's
  token): the broker credentials of Q15, the protection of ``main`` with at least one required
  approving review, ``can_approve_pull_request_reviews`` false, and ``CLAUDE_ROUTINE_TOKEN`` not a
  repository secret (whether the ``issue-agent`` environment holds it is not read);
* the sandbox probe records (`probe_records`) of every stage kind a Claude session runs, a Codex
  ``select`` stage included while its stored availability says Codex cannot run (a run then falls
  back to a Claude select session, `stages.stage_profile`);
* the owner's current acceptance of the residual risks (`acceptance_problems`).

Every check reads stored state or GitHub; none starts a probe session or a model turn.

**Probe records.** A report in the run records counts when its probe session ran
(``ran_session``) or when it failed: the static checks alone of a select session neither replace
a probe session nor hide a failure. The owner's ``probe`` command records one (``probes[0]``),
and every guarded stage session of a run adds one under the mode it ran in. The rule (plan
acceptance A7): for each stage kind and each mode of `PROBE_MODES`, the newest counting report
must have passed, at any age (one dated in the future or without a time is stale). No record has
to be recent: every run probes before every session with Bash, in every mode, and a session
starts only after its own probe passed (plan 5.7, the owner's decision of 2026-10-14).

**The acceptance.** ``<state>/schedules/acceptance.json`` (0600 in the private directory): when
it was given (``accepted``, UTC), the rows of plan section 11 it covers (`RISK_ROWS`), the version
of the risk list (``risk_list_sha256``, SHA-256 of `risk_list_text`), the risks as they read,
where it was given (``source``: ``cli`` or ``page``) and the tripwire count when it was given
(``tripwire_count``, `decoys.trip_count`). ``autodev enable --mode run --accept-shared-account``
records it under the writers' lock once every other condition passes (`record_acceptance`);
``disable`` and ``enable --mode dry-run`` withdraw it (`revoke_acceptance`), so a later edit of
``autodev.json`` back to run mode does not revive it. It is current while its rows and version are
today's, it is not withdrawn, it is not dated in the future, ``tripwire.json`` can be read and
its count is the one stored, and no tripwire entry is dated at or after it: a runner update that
changes `RESIDUAL_RISKS`, a tripwire or a failed sandbox probe (the owner's own ``probe`` runs
included: the schedule is off then anyway, and turning run mode on again records a new
acceptance) asks for a new acceptance. `RESIDUAL_RISKS` holds the rows' text as the plan states
it; tests/test_schedules_automatic.py pins it to docs/autodev-plan.md.

**The recorded result.** ``autodev ready`` stores its outcome in ``<state>/schedules/ready.json``
(`record_ready`: when, whether it passed, what was missing). The settings page shows it and
reads no credential itself: it offers run mode only while that result passed at most
`READY_FRESH` seconds ago and the acceptance is current (`page_problems`), and its ``enable``
checks the same; the tick checks everything again before a scheduled run.
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from pathlib import Path
from typing import Any

from src.daemon.state import private_dir, read_json, write_json

from . import decoys, records, stages
from .lease import schedules_dir
from .tick import fixed, parse_time

ACCEPTANCE_NAME = "acceptance.json"
ACCEPTANCE_VERSION = 2  # 2: with the tripwire count and the withdrawal (#350 review)
ACCEPT_FLAG = "--accept-shared-account"
SOURCES = ("cli", "page")
REVOKED_BY = ("disable", "dry-run")
# Plan section 11 (design pre-mortem): the rows whose residual risk the owner accepts, each as
# (row, how it broke, the residual risk), word for word.
RESIDUAL_RISKS: tuple[tuple[int, str, str], ...] = (
    (1, "A model under prompt injection read the owner's token or keys on the machine",
     "a bug or an unknown bypass in the Claude Code sandbox itself or in the nested Codex sandbox"),
    (2, 'An "owner\'s" decision was actually made by an agent',
     "labels, issues and edits made by any of the owner's agents under the owner's account count as the owner's"),
    (4, "The agent merged or pushed to `main`",
     "a bug in the broker, the only holder of credentials; a leaked deploy key can push a workflow that runs with "
     "repository access; merging through it is closed by the required approval since 2026-10-12 (5.7)"),
)
RISK_ROWS = tuple(row for row, _, _ in RESIDUAL_RISKS)
ACCEPTANCE_ITEM = "the owner's acceptance of the residual risks (plan section 11, rows 1, 2 and 4)"
ENABLE_COMMAND = f"python -m src.schedules autodev enable --mode run {ACCEPT_FLAG}"
READY_COMMAND = "python -m src.schedules autodev ready"
# The modes a probe is recorded under (`runner.probe`, `sequence.Sequence._probe_mode`); the probe
# is the same in each, and the plan asks for a pass in every one (5.7, acceptance A7).
PROBE_MODES = ("dry-run", "run-now", "scheduled", "resume")
PROBE_COMMAND = "python -m src.schedules probe --mode {mode} --kind {kind} --confirm-live-probe"
PASSED, FAILED, STALE, MISSING = "passed", "failed", "stale", "missing"
READY_NAME = "ready.json"
READY_VERSION = 1
READY_FRESH = 15 * 60  # seconds a passing `autodev ready` lets the settings page turn run mode on


def _utc(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).isoformat(timespec="seconds")


def _now(now: datetime | None) -> datetime:
    return now if now is not None else datetime.now(timezone.utc)


# --- the risk list ---------------------------------------------------------------------------------

def risks() -> list[dict]:
    """The accepted rows: ``{"row", "risk", "residual_risk"}``."""
    return [{"row": row, "risk": risk, "residual_risk": residual} for row, risk, residual in RESIDUAL_RISKS]


def risk_list_text() -> str:
    """The text the acceptance covers, one line per row."""
    return "\n".join(f"{row}. {risk}. Residual risk: {residual}." for row, risk, residual in RESIDUAL_RISKS)


def risk_list_version() -> str:
    """The version of the risk list: SHA-256 of `risk_list_text`."""
    return hashlib.sha256(risk_list_text().encode("utf-8")).hexdigest()


# --- the acceptance --------------------------------------------------------------------------------

def acceptance_path(directory: str | Path | None = None) -> Path:
    return schedules_dir(directory) / ACCEPTANCE_NAME


def read_acceptance(directory: str | Path | None = None) -> dict | None:
    """The stored acceptance, or None when there is none or it is not one (unreadable, another shape)."""
    try:
        stored = read_json(acceptance_path(directory), None)
    except (OSError, ValueError):
        return None
    if not isinstance(stored, dict) or parse_time(stored.get("accepted")) is None \
            or not isinstance(stored.get("risk_list_sha256"), str) or not isinstance(stored.get("rows"), list):
        return None
    return stored


def record_acceptance(directory: str | Path | None, now: datetime, *, source: str,
                      tripwire_count: int | None = None) -> dict:
    """Store the owner's acceptance of today's risk list, given ``now`` (`ACCEPT_FLAG` on the CLI,
    ``accept_shared_account`` on the settings page), with the tripwire count it was given at
    (``tripwire_count``, read now when None; `runner.enable` passes the one it read before its
    checks), and return it. `runner.enable` calls it under the writers' lock."""
    if source not in SOURCES:
        raise ValueError(f"not an acceptance source: {source!r}")
    count = decoys.trip_count(directory) if tripwire_count is None else tripwire_count
    if not isinstance(count, int) or isinstance(count, bool) or count < 0:
        raise ValueError("tripwire.json cannot be read: no acceptance is recorded over it")
    entry = {"version": ACCEPTANCE_VERSION, "accepted": _utc(now), "rows": list(RISK_ROWS),
             "risk_list_sha256": risk_list_version(), "risks": risks(), "source": source, "tripwire_count": count}
    private_dir(schedules_dir(directory))
    write_json(acceptance_path(directory), entry)
    return entry


def revoke_acceptance(directory: str | Path | None, now: datetime, *, by: str) -> bool:
    """Withdraw the stored acceptance (``revoked``, ``revoked_by``): ``disable`` and ``enable
    --mode dry-run`` do, under the writers' lock, so it is bound to the act of enabling run mode.
    True when there was one to withdraw."""
    if by not in REVOKED_BY:
        raise ValueError(f"not a withdrawal: {by!r}")
    stored = read_acceptance(directory)
    if stored is None or stored.get("revoked"):
        return False
    write_json(acceptance_path(directory), {**stored, "revoked": _utc(now), "revoked_by": by})
    return True


def acceptance_problems(directory: str | Path | None = None, now: datetime | None = None) -> list[str]:
    """Why the stored acceptance is not current, named (empty when it is): none recorded, given for
    other rows or another version of the risk list (or in an older format), withdrawn, dated after
    ``now``, ``tripwire.json`` unreadable, or a trip since it (another count, or an entry dated at
    or after it)."""
    stored = read_acceptance(directory)
    again = f"accept again with `{ENABLE_COMMAND}`"
    if stored is None:
        return [f"{ACCEPTANCE_ITEM}: not recorded; give it with `{ENABLE_COMMAND}`"]
    if stored["rows"] != list(RISK_ROWS) or stored["risk_list_sha256"] != risk_list_version() \
            or stored.get("version") != ACCEPTANCE_VERSION:
        return [f"{ACCEPTANCE_ITEM}: given for another version of the risk list; {again}"]
    if stored.get("revoked"):
        return [f"{ACCEPTANCE_ITEM}: withdrawn when the schedule was turned off or set to dry-run "
                f"({stored['revoked']}); {again}"]
    accepted = parse_time(stored["accepted"])
    if accepted is None or fixed(accepted) > fixed(_now(now)):
        return [f"{ACCEPTANCE_ITEM}: dated in the future ({stored['accepted']}; was the clock ahead?); {again}"]
    count = decoys.trip_count(directory)
    if count is None:
        return [f"{ACCEPTANCE_ITEM}: tripwire.json cannot be read, so a tripwire since it cannot be ruled out; "
                f"check it and the run records, then {again}"]
    tripped = decoys.last_trip(directory) or {}
    when = parse_time(tripped.get("time"))
    if count != stored.get("tripwire_count") or (when is not None and fixed(when) >= fixed(accepted)):
        at = f" ({tripped['time']})" if when is not None else ""
        return [f"{ACCEPTANCE_ITEM}: a tripwire turned the schedule off after it{at}; check the run records, "
                f"then {again}"]
    return []


def acceptance_summary(directory: str | Path | None = None, now: datetime | None = None) -> dict:
    """What `autodev status` shows of the acceptance: whether it is current, when and where it was
    given, and why it is not current."""
    stored = read_acceptance(directory) or {}
    problems = acceptance_problems(directory, now)
    return {"current": not problems, "accepted": stored.get("accepted"), "source": stored.get("source"),
            "problems": problems}


# --- the probe records -----------------------------------------------------------------------------

def codex_stage_kinds(config: Mapping) -> list[str]:
    """The stage kinds configured to run with Codex and not committing (`stages.stage_profile`
    runs ``implement`` and ``submit`` with Claude)."""
    configured = config.get("stages") or {}
    return [kind for kind in stages.KINDS if kind not in stages.WRITING_KINDS
            and (configured.get(kind) or {}).get("client") == "codex"]


def claude_stage_kinds(config: Mapping, codex_fallback: Iterable[str] = ()) -> list[str]:
    """The stage kinds a Claude session runs, whose sandbox probes `probe_records` asks for: every
    kind not run with Codex, and the Codex kinds of ``codex_fallback`` (their stored availability
    says Codex cannot run, so a run starts a Claude session instead)."""
    fallback = set(codex_fallback)
    codex = [kind for kind in codex_stage_kinds(config) if kind not in fallback]
    return [kind for kind in stages.KINDS if kind not in codex]


def _probe_item(kind: str, mode: str, status: str, run_id: str | None) -> str:
    command = PROBE_COMMAND.format(mode=mode, kind=kind)
    if status == FAILED:
        return f"sandbox probe {kind} in {mode} mode: failed in run {run_id}; run `{command}` once it is fixed"
    if status == STALE:
        return (f"sandbox probe {kind} in {mode} mode: the passing record (run {run_id}) is dated in the future or "
                f"has no time; run `{command}`")
    return f"sandbox probe {kind} in {mode} mode: no record; run `{command}`"


def probe_records(directory: str | Path | None, kinds: Iterable[str], now: datetime, *,
                  modes: Iterable[str] = PROBE_MODES) -> tuple[list[str], dict]:
    """The missing probe records, named, and every (kind, mode) with its status (`PASSED`,
    `FAILED`, `STALE` or `MISSING`), the run it came from and when it finished. See the module
    docstring. The records are read newest first until every pair has its report."""
    kinds, modes = list(kinds), list(modes)
    wanted = [(kind, mode) for kind in kinds for mode in modes]
    found: dict[tuple[str, str], tuple[Mapping, Any]] = {}
    for record in records.iter_records(directory) if wanted else ():
        reports = record.get("probes") if isinstance(record.get("probes"), list) else []
        for report in reversed(reports):  # a record lists its reports in the order they ran
            if not isinstance(report, dict):
                continue
            if not (report.get("ran_session") is True or report.get("ok") is not True):
                continue  # the static checks alone of a select session (see the module docstring)
            key = (report.get("kind"), report.get("mode"))
            if key in wanted and key not in found:
                found[key] = (report, record.get("run_id"))
        if len(found) == len(wanted):
            break  # before an older record is read
    problems: list[str] = []
    details: dict = {}
    for kind in kinds:
        for mode in modes:
            report, run_id = found.get((kind, mode), ({}, None))
            finished = parse_time(report.get("finished")) if report else None
            if not report:
                status = MISSING
            elif report.get("ok") is not True:
                status = FAILED
            elif finished is None or fixed(finished) > fixed(now):
                status = STALE
            else:
                status = PASSED
            details.setdefault(kind, {})[mode] = {"status": status, "run_id": run_id,
                                                  "finished": report.get("finished") if report else None}
            if status != PASSED:
                problems.append(_probe_item(kind, mode, status, run_id))
    return problems, details


# --- the recorded `autodev ready` --------------------------------------------------------------------

def ready_path(directory: str | Path | None = None) -> Path:
    return schedules_dir(directory) / READY_NAME


def record_ready(directory: str | Path | None, state: "Readiness", now: datetime) -> dict:
    """Store what ``autodev ready`` found (when, whether it passed, what was missing; 0600 in the
    private directory) for the settings page, which reads no credential itself, and return it."""
    entry = {"version": READY_VERSION, "checked": _utc(now), "ready": state.ready, "missing": state.missing}
    private_dir(schedules_dir(directory))
    write_json(ready_path(directory), entry)
    return entry


def read_ready(directory: str | Path | None = None) -> dict | None:
    """The recorded ``autodev ready``, or None when there is none or it is not one."""
    try:
        stored = read_json(ready_path(directory), None)
    except (OSError, ValueError):
        return None
    if not isinstance(stored, dict) or parse_time(stored.get("checked")) is None \
            or not isinstance(stored.get("ready"), bool) or not isinstance(stored.get("missing"), list):
        return None
    return {**stored, "missing": [str(item) for item in stored["missing"]]}


def page_problems(directory: str | Path | None, now: datetime) -> list[str]:
    """Why the settings page may not turn run mode on now, named (empty when it may): the recorded
    ``autodev ready`` must have passed at most `READY_FRESH` seconds before ``now`` (not after it),
    and the acceptance must be current. Reads stored state only: no credential, no GitHub read."""
    recorded = read_ready(directory)
    minutes = f"{READY_FRESH // 60} minutes"
    problems: list[str] = []
    if recorded is None:
        problems.append(f"`{READY_COMMAND}` has no recorded result; run it in a terminal")
    else:
        checked = parse_time(recorded["checked"])
        age = (fixed(now) - fixed(checked)).total_seconds() if checked is not None else -1
        if not recorded["ready"]:
            problems.append(f"the last `{READY_COMMAND}` ({recorded['checked']}) did not pass")
            problems += recorded["missing"]
        elif not 0 <= age <= READY_FRESH:
            problems.append(f"the last passing `{READY_COMMAND}` ({recorded['checked']}) is not from the last "
                            f"{minutes}; run it again")
    problems += [item for item in acceptance_problems(directory, now) if item not in problems]
    return problems


# --- the outcome -----------------------------------------------------------------------------------

@dataclass
class Readiness:
    """What `runner.readiness` found: the missing conditions (GitHub, probes) and the acceptance's
    problems, each named; ready when both are empty."""

    conditions: list[str]
    acceptance: list[str]
    probes: dict = field(default_factory=dict)
    codex: dict = field(default_factory=dict)
    accepted: dict | None = None

    @property
    def missing(self) -> list[str]:
        return [*self.conditions, *self.acceptance]

    @property
    def ready(self) -> bool:
        return not self.missing

    def to_dict(self) -> dict:
        accepted = self.accepted or {}
        return {"ready": self.ready, "missing": self.missing, "conditions": list(self.conditions),
                "probes": self.probes, "codex": self.codex,
                "acceptance": {"current": not self.acceptance, "problems": list(self.acceptance),
                               "accepted": accepted.get("accepted"), "source": accepted.get("source"),
                               "rows": list(RISK_ROWS), "risk_list_sha256": risk_list_version()},
                "risks": risks()}
