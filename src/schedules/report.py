"""The run report and the owner's notification (design 9.5 and 10.1, #347).

`build_report` assembles the report of plan 9.5 from what the run left behind, for the run record:
``pinned``, the runner's commit, the flow revisions read (`run_state`), the ``bundle_revision``
of every worker package, the gate and selection decisions, for each stage session and each
delegated step its ``execution.requested`` and ``execution.applied`` (client, model, effort,
method; the applied ones from the argv actually passed, design 6.5) with the attempts and the
decisions on its findings, the checks, the PR link, the review status, the risk issues, the
exports of a control-path refusal, the blockers and the cost the CLIs reported. Security findings
appear as counts only. Its inputs:

* the run record (`records`): ``mode``, ``status``, ``reason``, ``pinned``, ``runner_commit``,
  ``gate``, ``selection`` (the validated select output, or a note), ``stages``
  (``{kind: {"status", "reason", "execution": {"requested", "applied"}, "cost_usd", ...}}``, as
  `stages.execution_of` gives the execution), ``checks`` (the implement output's), ``pr``,
  ``pr_url``, ``review_status``, ``next_attempt``, ``risk_issues`` (``[{"number", "finding"}]``),
  ``exports`` (`repos.TrustedRepository.export_commits`), ``blockers``;
* the run state (``flows``);
* the run directory: ``workers.json``, ``workers/<handle>/attempt-<n>.json`` (the requested and
  applied profile, the package revisions) and ``worker-decisions.json``, and the pre-mortem
  record for the security counts.

`notification` turns a report into a title and a message from a fixed vocabulary: the outcome,
the issue and PR numbers, the review status, the next command (``run-now --mode resume --pr N`` for
a PR that waits for the bots, or whose resume was refused, #350), the export paths and the run ID;
never a reason, a blocker, a finding or a secret. A run that turned the schedule off (the report's
``schedule_off``: a tripwire or a failed sandbox probe, #350) gets the title `SCHEDULE_OFF_TITLE`
and the cause's fixed phrase first. `notify` shows it with ``osascript``, passing
the title and the message as arguments of a constant AppleScript ``on run argv`` handler, never
inside the script text (design 10.1). `should_notify` says when: every real run, a dry-run only
when it did not complete (#347 open question Q-notify-dry, default adopted), and every run that
turned the schedule off.
`selection_view` is the selection part of the dry-run printout and the report.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

from . import broker, records, workers
from .stages import KINDS, score_table

OSASCRIPT = "/usr/bin/osascript"
# The constant script: the data arrive as the run handler's arguments (osascript(1): arguments after
# the script go to the run handler as a list of strings; with -e no file name is looked for).
SCRIPT = ("on run argv", "display notification (item 2 of argv) with title (item 1 of argv)", "end run")
NOTIFY_TIMEOUT = 30
TITLE_MAX = 100
MESSAGE_MAX = 600
DECISION_NAMES = ("fixed", "declined", "mitigated", "known_risk")
# A run that turned the schedule off (`decoys.tripped_by`, #350): the title and the first phrase of
# its notification, by the cause `decoys.trip` recorded.
SCHEDULE_OFF_TITLE = "Autodev schedule off"
SCHEDULE_OFF = {"tripwire": "a tripwire fired and turned the schedule off",
                "probe": "a sandbox probe failed and turned the schedule off"}
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_ATTEMPT = re.compile(r"attempt-([1-9][0-9]{0,2})\.json")


def _read(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# --- selection ----------------------------------------------------------------------------------

def selection_view(output: Mapping, repository: str | None = None) -> dict:
    """The selection as the dry-run prints it: the decision, the issue, the slice, the rationale,
    the top five scores and their Markdown table."""
    scores = list(output.get("scores") or [])[:5]
    return {"decision": output.get("decision"), "issue": output.get("issue"), "slice": output.get("slice"),
            "rationale": output.get("rationale"), "top_five": scores, "table": score_table(scores, repository)}


# --- steps ----------------------------------------------------------------------------------------

def _attempts(run_dir: Path, handle: str) -> list[dict]:
    directory = run_dir / workers.WORK_DIR / handle
    found = []
    try:
        names = os.listdir(directory)
    except OSError:
        return []
    for name in names:
        match = _ATTEMPT.fullmatch(name)
        data = _read(directory / name) if match else None
        if isinstance(data, dict):
            found.append((int(match[1]), data))  # type: ignore[index]
    return [data for _, data in sorted(found, key=lambda item: item[0])]


def _decisions_by_handle(run_dir: Path) -> dict[str, dict[str, int]]:
    """Per handle, how many findings it was decided on, by decision (the latest decision a key got
    for that handle, current or in its history)."""
    data = _read(run_dir / workers.DECISIONS_NAME)
    counts: dict[str, dict[str, int]] = {}
    for entry in (data.get("decisions") if isinstance(data, dict) else None) or []:
        if not isinstance(entry, dict):
            continue
        history = entry.get("history") if isinstance(entry.get("history"), list) else []
        seen = set()
        for decision in [entry, *reversed(history)]:
            handle = decision.get("handle") if isinstance(decision, dict) else None
            if not isinstance(handle, str) or handle in seen or decision.get("decision") not in DECISION_NAMES:
                continue
            seen.add(handle)
            per = counts.setdefault(handle, dict.fromkeys(DECISION_NAMES, 0))
            per[decision["decision"]] += 1
    return counts


def _step_execution(attempt: Mapping) -> dict:
    requested = attempt.get("requested") if isinstance(attempt.get("requested"), dict) else {}
    client = attempt.get("client") if isinstance(attempt.get("client"), dict) else {}
    return {"requested": {"client": requested.get("client", client.get("client_requested")),
                          "model": requested.get("model"), "effort": requested.get("effort"), "method": "delegate"},
            "applied": {"client": client.get("client_applied"), "model": attempt.get("model"),
                        "effort": attempt.get("effort"), "method": "delegate",
                        "fallback_reason": client.get("fallback_reason")}}


def _steps(run_dir: Path) -> tuple[list[dict], list[dict]]:
    """The steps (one per worker task) and the packages of every attempt."""
    state = _read(run_dir / workers.STATE_NAME)
    tasks = [task for task in ((state.get("tasks") if isinstance(state, dict) else None) or [])
             if isinstance(task, dict) and isinstance(task.get("handle"), str)]
    decisions = _decisions_by_handle(run_dir)
    steps, packages = [], []
    for task in tasks:
        handle = task["handle"]
        attempts = _attempts(run_dir, handle)
        for attempt in attempts:
            package = attempt.get("package") if isinstance(attempt.get("package"), dict) else {}
            packages.append({"handle": handle, "attempt_id": attempt.get("attempt_id"),
                             "bundle_revision": package.get("bundle_revision"),
                             "contract_revision": package.get("contract_revision")})
        chosen = next((attempt for attempt in attempts if attempt.get("verdict") == "accepted"),
                      attempts[-1] if attempts else {})
        steps.append({"handle": handle, "kind": task.get("kind"), "status": task.get("status"),
                      "head": task.get("head"), "attempts": len(attempts) or len(task.get("attempts") or []),
                      "execution": _step_execution(chosen),
                      "decisions": decisions.get(handle, dict.fromkeys(DECISION_NAMES, 0))})
    return steps, packages


# --- the report -----------------------------------------------------------------------------------

def _stages(record: Mapping) -> list[dict]:
    stages = record.get("stages") if isinstance(record.get("stages"), dict) else {}
    found = []
    for kind in KINDS:
        stage = stages.get(kind)
        if not isinstance(stage, dict):
            continue
        execution = stage.get("execution") if isinstance(stage.get("execution"), dict) else {}
        found.append({"kind": kind, "status": stage.get("status"), "reason": stage.get("reason"),
                      "execution": {"requested": execution.get("requested"), "applied": execution.get("applied")},
                      "cost_usd": stage.get("cost_usd")})
    return found


def _security(run_dir: Path | None) -> dict:
    if run_dir is None:
        return {"kept_local": 0, "open": 0}
    risks = broker.read_premortem(run_dir)
    return {"kept_local": sum(broker.security_signal(finding) for finding in risks.values()),
            "open": len(workers.open_security_risks(run_dir))}


def build_report(record: Mapping, state: Mapping | None = None, run_dir: str | os.PathLike[str] | None = None,
                 *, known: Iterable[str] = ()) -> dict:
    """The run report of plan 9.5 (see the module docstring), masked with ``known``."""
    directory = Path(run_dir) if run_dir is not None else None
    steps, packages = _steps(directory) if directory is not None else ([], [])
    gate = record.get("gate") if isinstance(record.get("gate"), dict) else {}
    selection = record.get("selection")
    stages = _stages(record)
    costs = [stage["cost_usd"] for stage in stages
             if isinstance(stage["cost_usd"], (int, float)) and not isinstance(stage["cost_usd"], bool)]
    checks = [{"name": check.get("name"), "status": check.get("status")}
              for check in record.get("checks") or [] if isinstance(check, dict)]
    risk_issues = [item for item in record.get("risk_issues") or [] if isinstance(item, dict)]
    report = {
        "run_id": record.get("run_id"), "mode": record.get("mode"), "status": record.get("status"),
        "pinned": record.get("pinned"), "runner_commit": record.get("runner_commit"),
        "flows": dict((state or {}).get("flows") or {}),
        "packages": packages,
        "gate": {"decision": gate.get("decision"), "reason": gate.get("reason")},
        "selection": ({"decision": selection.get("decision"), "issue": selection.get("issue"),
                       "top_five": [score.get("issue") for score in (selection.get("scores") or [])[:5]
                                    if isinstance(score, dict)]}
                      if isinstance(selection, dict) else selection),
        "issue": record.get("issue"), "stages": stages, "steps": steps, "checks": checks,
        "pr": record.get("pr"), "pr_url": record.get("pr_url"), "review_status": record.get("review_status"),
        "next_attempt": record.get("next_attempt"),
        "risk_issues": {"created": [item.get("number") for item in risk_issues if item.get("number")],
                        "count": len(risk_issues)},
        "exports": record.get("exports"), "blockers": list(record.get("blockers") or []),
        "cost_usd_total": round(sum(costs), 6) if costs else None,
        "security": _security(directory),
        "schedule_off": record.get("schedule_off") if record.get("schedule_off") in SCHEDULE_OFF else None,
    }
    return records.mask(report, list(known))


# --- the notification -----------------------------------------------------------------------------

def should_notify(record: Mapping) -> bool:
    """Every real run, a dry-run that did not complete (a failure or a tripwire), and any run that
    turned the schedule off (``schedule_off``, #350)."""
    return record.get("mode") != "dry-run" or record.get("status") != "completed" or bool(record.get("tripwire")) \
        or bool(record.get("schedule_off"))


def _clean(text: str, limit: int) -> str:
    flat = " ".join(_CONTROL.sub(" ", text).split()).lstrip("-").strip()
    return flat if len(flat) <= limit else flat[:limit - 1] + "…"


def notification(report: Mapping, *, known: Iterable[str] = ()) -> tuple[str, str]:
    """The title and the message for the owner, from a fixed vocabulary (see the module docstring)."""
    status = str(report.get("status") or "unknown").split(":")[0].strip() or "unknown"
    mode = "dry-run" if report.get("mode") == "dry-run" else "run"
    title = f"Autodev {mode} {status}"
    parts = []
    if report.get("schedule_off") in SCHEDULE_OFF:  # the run turned the schedule off (#350)
        title = SCHEDULE_OFF_TITLE
        parts.append(SCHEDULE_OFF[report["schedule_off"]])
    if isinstance(report.get("issue"), int):
        parts.append(f"issue #{report['issue']}")
    if isinstance(report.get("pr_url"), str) and report["pr_url"].startswith("https://github.com/"):
        parts.append(f"PR {report['pr_url']}")
    if report.get("review_status") in broker.REVIEW_STATUSES:
        parts.append(f"review {report['review_status']}")
    if isinstance(report.get("pr"), int) and (report.get("review_status") == "waiting_bots" or (
            report.get("mode") == "resume" and status == "refused")):  # a refused resume still waits (#350)
        parts.append(f"continue with: run-now --mode resume --pr {report['pr']}")
    if report.get("review_status") == "complete_with_followups":
        parts.append("the bot-round limit was reached; follow-ups are listed in the PR")
    exports = report.get("exports") if isinstance(report.get("exports"), dict) else {}
    if exports.get("patch") and exports.get("bundle"):
        parts.append(f"patch {exports['patch']}, bundle {exports['bundle']}")
    blockers = report.get("blockers") or []
    if blockers:
        parts.append(f"{len(blockers)} blocker(s) on the issue")
    parts.append(f"details in run {report.get('run_id')}")
    message = "; ".join(parts) + "."
    masked = records.mask([title, message], list(known))
    return _clean(masked[0], TITLE_MAX), _clean(masked[1], MESSAGE_MAX)


def notify_argv(title: str, message: str, osascript: str = OSASCRIPT) -> list[str]:
    """``osascript -e <SCRIPT line> ... <title> <message>``: the data only as arguments."""
    argv = [osascript]
    for line in SCRIPT:
        argv += ["-e", line]
    return argv + [_clean(title, TITLE_MAX) or "Autodev", _clean(message, MESSAGE_MAX) or "."]


def notify(title: str, message: str, *, runner: Callable[..., subprocess.CompletedProcess] = subprocess.run,
           osascript: str = OSASCRIPT, platform: str = sys.platform) -> bool:
    """Show a macOS notification; True when ``osascript`` exited with 0. Never raises."""
    if platform != "darwin":
        return False
    try:
        result = runner(notify_argv(title, message, osascript), capture_output=True, text=True,
                        stdin=subprocess.DEVNULL, timeout=NOTIFY_TIMEOUT, check=False,
                        env={"PATH": "/usr/bin:/bin", "LANG": "en_US.UTF-8"})
    except (OSError, subprocess.SubprocessError):
        return False
    return result.returncode == 0
