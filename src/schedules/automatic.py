"""The automatic mode (#350; design 5.7, 11 and 14, phase 1b): scheduled real runs under the
owner's account, and what `autodev ready` reads from this machine.

A scheduled real run (``run`` on a slot, and the ``resume`` of a continuation) starts only while
``autodev ready`` passes (`runner.readiness`, reference docs/autodev-runner.md#automatic-mode):

* the real-run preconditions of #347 (`preconditions.check`, read from GitHub with the broker's
  token): the broker credentials of Q15, the protection of ``main`` with at least one required
  approving review, ``can_approve_pull_request_reviews`` false, and ``CLAUDE_ROUTINE_TOKEN`` in the
  ``issue-agent`` environment only, not a repository secret;
* a passing, current record of every sandbox probe (`probe_records`) and, for a stage that runs
  with Codex, its stored stage availability (`codex_probes.configured_stage_availability`);
* the owner's current acceptance of the residual risks (`acceptance_problems`).

Every check reads stored state or GitHub; none starts a probe session or a model turn.

**Probe records.** For each mode of `PROBE_MODES` and each stage kind a Claude session runs
(`claude_stage_kinds`), the newest report in the run records counts: the owner's ``probe``
command records one (``probes[0]``), and every guarded stage session of a run adds one under the
mode it ran in. A report counts when its probe session ran (``ran_session``) or when it failed:
the static checks alone of a select session neither replace a probe session nor hide a failure.
It must have passed (``ok``) and be at most `PROBE_RECORD_MAX_AGE` old (one from the future is
stale too). A select stage that runs with Codex needs its stored Codex stage availability instead
(the ``probe`` command refuses a stage configured for Codex).

**The acceptance.** ``<state>/schedules/acceptance.json`` (0600 in the private directory): when
it was given (``accepted``, UTC), the rows of plan section 11 it covers (`RISK_ROWS`), the version
of the risk list (``risk_list_sha256``, SHA-256 of `risk_list_text`), the risks as they read and
where it was given (``source``: ``cli`` or ``page``). ``autodev enable --mode run
--accept-shared-account`` records it once every other condition passes (`record_acceptance`). It
is current while its rows and version are today's and no tripwire turned the schedule off at or
after it (``tripwire.json``, `decoys.last_trip`): a runner update that changes `RESIDUAL_RISKS`,
or a tripwire, asks for a new acceptance. `RESIDUAL_RISKS` holds the rows' text as the plan
states it; tests/test_schedules_automatic.py pins it to docs/autodev-plan.md.
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
ACCEPTANCE_VERSION = 1
ACCEPT_FLAG = "--accept-shared-account"
SOURCES = ("cli", "page")
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
# The modes a probe is recorded under (`runner.probe`, `sequence.Sequence._probe_mode`); the probe
# is the same in each, and the plan asks for a pass in every one (5.7, acceptance A7).
PROBE_MODES = ("dry-run", "run-now", "scheduled", "resume")
PROBE_RECORD_MAX_AGE = 7 * 24 * 3600  # seconds a passing probe record counts (as Codex's stored availability)
PROBE_COMMAND = "python -m src.schedules probe --mode {mode} --kind {kind} --confirm-live-probe"
CODEX_COMMAND = "python -m src.schedules codex-probe --kind stage --confirm-live-probe"
PASSED, FAILED, STALE, MISSING = "passed", "failed", "stale", "missing"


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


def record_acceptance(directory: str | Path | None, now: datetime, *, source: str) -> dict:
    """Store the owner's acceptance of today's risk list, given ``now`` (`ACCEPT_FLAG` on the CLI,
    ``accept_shared_account`` on the settings page), and return it."""
    if source not in SOURCES:
        raise ValueError(f"not an acceptance source: {source!r}")
    entry = {"version": ACCEPTANCE_VERSION,
             "accepted": now.astimezone(timezone.utc).isoformat(timespec="seconds"),
             "rows": list(RISK_ROWS), "risk_list_sha256": risk_list_version(), "risks": risks(), "source": source}
    private_dir(schedules_dir(directory))
    write_json(acceptance_path(directory), entry)
    return entry


def acceptance_problems(directory: str | Path | None = None) -> list[str]:
    """Why the stored acceptance is not current, named (empty when it is): none recorded, given for
    other rows or another version of the risk list, or a tripwire at or after it."""
    stored = read_acceptance(directory)
    again = f"accept again with `{ENABLE_COMMAND}`"
    if stored is None:
        return [f"{ACCEPTANCE_ITEM}: not recorded; give it with `{ENABLE_COMMAND}`"]
    if stored["rows"] != list(RISK_ROWS) or stored["risk_list_sha256"] != risk_list_version():
        return [f"{ACCEPTANCE_ITEM}: given for another version of the risk list; {again}"]
    tripped = decoys.last_trip(directory) or {}
    when, accepted = parse_time(tripped.get("time")), parse_time(stored["accepted"])
    if when is not None and accepted is not None and fixed(when) >= fixed(accepted):
        return [f"{ACCEPTANCE_ITEM}: a tripwire turned the schedule off after it ({tripped['time']}); "
                f"check the run records, then {again}"]
    return []


def acceptance_summary(directory: str | Path | None = None) -> dict:
    """What `autodev status` shows of the acceptance: whether it is current, when and where it was
    given, and why it is not current."""
    stored = read_acceptance(directory) or {}
    problems = acceptance_problems(directory)
    return {"current": not problems, "accepted": stored.get("accepted"), "source": stored.get("source"),
            "problems": problems}


# --- the probe records -----------------------------------------------------------------------------

def codex_stage_kinds(config: Mapping) -> list[str]:
    """The stage kinds that run with Codex: configured for Codex and not committing
    (`stages.stage_profile` runs ``implement`` and ``submit`` with Claude)."""
    configured = config.get("stages") or {}
    return [kind for kind in stages.KINDS if kind not in stages.WRITING_KINDS
            and (configured.get(kind) or {}).get("client") == "codex"]


def claude_stage_kinds(config: Mapping) -> list[str]:
    """The stage kinds a Claude session runs, whose sandbox probes `probe_records` asks for."""
    codex = codex_stage_kinds(config)
    return [kind for kind in stages.KINDS if kind not in codex]


def _probe_item(kind: str, mode: str, status: str, run_id: str | None) -> str:
    command = PROBE_COMMAND.format(mode=mode, kind=kind)
    if status == FAILED:
        return f"sandbox probe {kind} in {mode} mode: failed in run {run_id}; run `{command}` once it is fixed"
    if status == STALE:
        return (f"sandbox probe {kind} in {mode} mode: the passing record (run {run_id}) is older than "
                f"{PROBE_RECORD_MAX_AGE // 86400} days; run `{command}`")
    return f"sandbox probe {kind} in {mode} mode: no record; run `{command}`"


def probe_records(directory: str | Path | None, kinds: Iterable[str], now: datetime, *,
                  modes: Iterable[str] = PROBE_MODES, max_age: float = PROBE_RECORD_MAX_AGE) -> tuple[list[str], dict]:
    """The missing probe records, named, and every (kind, mode) with its status (`PASSED`,
    `FAILED`, `STALE` or `MISSING`), the run it came from and when it finished (see the module
    docstring). The records are read newest first until every pair has its report."""
    wanted = [(kind, mode) for kind in kinds for mode in modes]
    found: dict[tuple[str, str], tuple[Mapping, Any]] = {}
    for record in records.iter_records(directory) if wanted else ():
        reports = record.get("probes") if isinstance(record.get("probes"), list) else []
        for report in reversed(reports):  # a record lists its reports in the order they ran
            if not isinstance(report, dict):
                continue
            key = (report.get("kind"), report.get("mode"))
            counts = report.get("ran_session") is True or report.get("ok") is not True
            if counts and key in wanted and key not in found:
                found[key] = (report, record.get("run_id"))
        if len(found) == len(wanted):
            break  # before an older record is read
    problems: list[str] = []
    details: dict = {}
    for kind, mode in wanted:
        report, run_id = found.get((kind, mode), ({}, None))
        finished = parse_time(report.get("finished")) if report else None
        if not report:
            status = MISSING
        elif report.get("ok") is not True:
            status = FAILED
        elif finished is None or not 0 <= (fixed(now) - fixed(finished)).total_seconds() <= max_age:
            status = STALE
        else:
            status = PASSED
        details.setdefault(kind, {})[mode] = {"status": status, "run_id": run_id,
                                              "finished": report.get("finished") if report else None}
        if status != PASSED:
            problems.append(_probe_item(kind, mode, status, run_id))
    return problems, details


def codex_item(kind: str, reason: str) -> str:
    return f"Codex stage availability for {kind}: {reason}; run `{CODEX_COMMAND}`"


# --- the outcome -----------------------------------------------------------------------------------

@dataclass
class Readiness:
    """What `runner.readiness` found: the missing conditions (GitHub, probes, Codex) and the
    acceptance's problems, each named; ready when both are empty."""

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
