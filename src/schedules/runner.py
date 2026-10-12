"""The autodev run and the scheduler tick: what the CLI commands do (design 10.1, 10.2).

``launch`` starts ``<python> -P -m src.schedules run autodev --run-id …`` under supervision
(`src.schedules.supervise`) and completes its record when the run timed out or crashed.
``run_autodev`` is that child: it holds the run lease, checks the runner for drift, reads a
GitHub snapshot and asks the gate. Infrastructure failures (a CLI, the network, the lease, drift,
a timeout) end as ``failed: infra`` with a reason, set no label, and the next slot retries.

Only ``dry-run`` exists here. Every real mode (``run``, ``resume``) refuses and names the parts it
needs; nothing in this module writes to GitHub, and the gate's cleanup actions are only reported.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Callable

from src import executables
from src.daemon.state import private_dir, read_json, write_json

from . import config as autodev_config
from . import drift, launchagent, lease, records
from .gate import gate
from .github import GitHubClient, GitHubError, NetworkError, TokenUnavailable, fetch_snapshot, token_provider
from .supervise import run_supervised
from .tick import decide, iso, next_slot

REAL_MODES = ("run", "resume")
MISSING_FOR_REAL_MODES = ["#342 broker", "#347 run sequence", "#350 automatic mode"]
DRY_RUN_TIMEOUT_MINUTES = 30
TICK_STATE = "tick.json"
UPDATE_MARKERS = ("maintenance.json", "transaction.json")
_ENVIRONMENT = ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")
HEAVY = (re.compile(r"\bpytest\b"), re.compile(r"\bsrc[./]reindex\b"))


def _local_now() -> datetime:
    return datetime.now().astimezone()


@dataclass
class Context:
    """Everything a command touches, so tests can replace each part."""
    installation: Path
    directory: Path
    python: str = sys.executable
    clock: Callable[[], datetime] = _local_now
    launchctl: launchagent.Runner = launchagent.run_command
    agents_dir: Path | None = None
    client_factory: Callable[[dict], GitHubClient] | None = None
    drift_check: Callable[[Path], dict] | None = None
    supervise: Callable[..., object] = run_supervised
    heavy_processes: Callable[[], list] | None = None
    on_ac_power: Callable[[], bool | None] | None = None
    tool_search: str | None = None

    @property
    def schedules(self) -> Path:
        return lease.schedules_dir(self.directory)

    def scheduler(self) -> launchagent.Scheduler:
        return launchagent.Scheduler(self.installation, self.directory, self.python, runner=self.launchctl,
                                     agents_dir=self.agents_dir)


def refusal(mode: str) -> dict:
    return {"ok": False, "error": "mode_unavailable", "mode": mode,
            "message": f"mode {mode} is not available yet; this part runs dry-run only",
            "missing": list(MISSING_FOR_REAL_MODES)}


def infra(reason: str) -> dict:
    return {"status": "failed: infra", "reason": reason}


# --- probes ---------------------------------------------------------------------------------------

def heavy_processes(ps: str = "/bin/ps") -> list[str]:
    """Other processes that would compete with a run: ``pytest`` and reindexing."""
    try:
        listed = subprocess.run([ps, "-A", "-o", "pid=,command="], capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, timeout=30, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    found = []
    for line in listed.splitlines():
        pid, _, command = line.strip().partition(" ")
        if pid.isdigit() and int(pid) != os.getpid() and any(pattern.search(command) for pattern in HEAVY):
            found.append(f"{pid} {command.strip()[:80]}")
    return found


def on_ac_power(pmset: str = "/usr/bin/pmset") -> bool | None:
    """True on AC power, False on battery, None when unknown (not macOS, no answer)."""
    if sys.platform != "darwin":
        return None
    try:
        output = subprocess.run([pmset, "-g", "batt"], capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, timeout=30, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    if "AC Power" in output:
        return True
    if "Battery Power" in output:
        return False
    return None


# --- the run ----------------------------------------------------------------------------------------

def default_drift(installation: Path) -> dict:
    commit = drift.pinned(installation)
    return {"pinned": commit, "runner_commit": drift.commit_of(installation),
            "changed": drift.check(installation, commit)}


def default_client(config: dict) -> GitHubClient:
    return GitHubClient(config["repository"], token_provider(config))


def run_autodev(ctx: Context, mode: str, *, pr: int | None = None, run_id: str | None = None) -> tuple[int, dict]:
    """The supervised child: one dry-run up to the gate decision, recorded."""
    if mode in REAL_MODES:
        return 1, refusal(mode)
    config = autodev_config.load(ctx.directory)
    now = ctx.clock()
    run_id = run_id or records.new_run_id(now)
    record = records.read_record(run_id, ctx.directory) or {"trigger": {"kind": "manual"}, "started": iso(now)}
    record.update(mode=mode, repository=config["repository"], installation=str(ctx.installation), pr=pr,
                  status="running", cleanup_executed=False)
    try:
        with lease.hold(ctx.directory):
            records.write_record(run_id, record, ctx.directory)
            record.update(_dry_run(ctx, config, now, pr))
    except lease.LeaseBusy as error:
        record.update(infra(f"lease: {error}"))
    record["finished"] = iso(ctx.clock())
    written = records.write_record(run_id, record, ctx.directory)
    return (0 if written["status"] == "completed" else 1), written


def _dry_run(ctx: Context, config: dict, now: datetime, pr: int | None) -> dict:
    try:
        checked = (ctx.drift_check or default_drift)(ctx.installation)
    except drift.DriftCheckFailed as error:
        return infra(f"cli: {error}")
    result = {"pinned": checked.get("pinned"), "runner_commit": checked.get("runner_commit")}
    if checked.get("changed"):
        return {**result, **infra("runner drift: " + ", ".join(checked["changed"][:20]))}
    client = (ctx.client_factory or default_client)(config)
    try:
        snapshot = fetch_snapshot(client, now)
    except TokenUnavailable as error:
        return {**result, **infra(f"cli: {error}")}
    except NetworkError as error:
        return {**result, **infra(f"network: {error}")}
    except GitHubError as error:
        return {**result, **infra(f"network: {error}"), "github_requests": client.requests}
    decision = gate(snapshot, records.trusted_comment_ids(ctx.directory),
                    max_bot_rounds=config["limits"]["max_bot_rounds"], now=now, pr=pr)
    return {**result, "status": "completed", "gate": decision.to_dict(), "github_requests": client.requests,
            "selection": "not started: the selection session arrives with the run sequence (#347)",
            "note": "dry-run: the gate's cleanup actions were not executed"}


def run_environment(ctx: Context) -> dict[str, str]:
    """The explicit environment of a supervised run."""
    tools = read_json(ctx.schedules / "tools.json", None)
    path = launchagent.explicit_path(tools) if isinstance(tools, dict) else executables.service_path()
    environment = {name: os.environ[name] for name in _ENVIRONMENT if name in os.environ}
    environment.update(PATH=path, PYTHONPATH=str(ctx.installation), AGENTS_SERVICE_DIR=str(ctx.directory),
                       PYTHONDONTWRITEBYTECODE="1")
    return environment


def launch(ctx: Context, mode: str, trigger: dict, *, pr: int | None = None) -> tuple[int, dict]:
    """Start one supervised run and return its completed record."""
    if mode in REAL_MODES:
        return 1, refusal(mode)
    now = ctx.clock()
    run_id = records.new_run_id(now)
    run_dir = records.run_dir(run_id, ctx.directory)
    records.write_record(run_id, {"status": "starting", "mode": mode, "trigger": trigger, "started": iso(now)},
                         ctx.directory)
    argv = [ctx.python, "-P", "-m", "src.schedules", "run", "autodev", "--mode", mode, "--run-id", run_id]
    if pr is not None:
        argv += ["--pr", str(pr)]
    timeout = DRY_RUN_TIMEOUT_MINUTES * 60
    try:
        outcome = ctx.supervise(argv, timeout=timeout, cwd=ctx.installation, env=run_environment(ctx),
                                stdout_path=run_dir / "stdout.log", stderr_path=run_dir / "stderr.log")
    except OSError as error:
        outcome, failure = None, infra(f"cli: the run could not start: {type(error).__name__}: {error}")
    else:
        failure = None
    record = records.read_record(run_id, ctx.directory) or {}
    if failure:
        record.update(failure)
    elif outcome.timed_out:
        record.update(infra(f"timeout: the run exceeded {timeout} seconds; its process tree was ended"),
                      signalled=outcome.signalled)
    elif record.get("status") not in ("completed", "failed: infra"):
        record.update(infra(f"cli: the run exited with code {outcome.returncode} without a result"))
    if outcome is not None:
        record["caffeinated"] = outcome.caffeinated
    record.setdefault("finished", iso(ctx.clock()))
    written = records.write_record(run_id, record, ctx.directory)
    return (0 if written.get("status") == "completed" else 1), written


# --- the tick and the autodev commands -----------------------------------------------------------

def _tick_state(ctx: Context) -> dict:
    try:
        state = read_json(ctx.schedules / TICK_STATE, {})
    except (OSError, ValueError):
        state = {}
    return state if isinstance(state, dict) else {}


def _save_tick_state(ctx: Context, state: dict) -> None:
    private_dir(ctx.schedules)
    write_json(ctx.schedules / TICK_STATE, state)


def tick(ctx: Context) -> tuple[int, dict]:
    config = autodev_config.load(ctx.directory)
    now = ctx.clock()
    decision = decide(
        now, config, _tick_state(ctx),
        lease_free=lambda: not lease.held(ctx.directory),
        update_pending=lambda: any((ctx.directory / name).exists() for name in UPDATE_MARKERS),
        heavy_processes=ctx.heavy_processes or heavy_processes,
        on_ac_power=ctx.on_ac_power or on_ac_power)
    state = dict(decision.state)
    if decision.action != "idle" or decision.missed:
        state["last_decision"] = {"time": iso(now), **decision.to_dict()}
    _save_tick_state(ctx, state)  # before the run, so a crash never repeats a handled slot
    result = {"ok": True, "tick": decision.to_dict()}
    if decision.action == "run":
        mode, pr = config["mode"], None
        if decision.trigger["kind"] == "continuation" and mode == "run":
            mode, pr = "resume", decision.trigger.get("pr")
        _, run = launch(ctx, mode, decision.trigger, pr=pr)
        result["run"] = run
    return 0, result


def enable(ctx: Context, mode: str) -> tuple[int, dict]:
    if mode in REAL_MODES:
        return 1, refusal(mode)
    config = autodev_config.load(ctx.directory)
    was_enabled = config["enabled"]
    config.update(enabled=True, mode=mode)
    autodev_config.save(config, ctx.directory)
    now = ctx.clock()
    if not was_enabled:
        _save_tick_state(ctx, {**_tick_state(ctx), "enabled_at": iso(now)})
    installed = ctx.scheduler().plist.is_file()
    result = {"ok": True, "enabled": True, "mode": mode, "scheduler_installed": installed,
              "next_slot": iso(next_slot(config["times"], now))}
    if not installed:
        result["note"] = "run `python -m src.schedules autodev install` so that ticks start"
    return 0, result


def disable(ctx: Context) -> tuple[int, dict]:
    config = autodev_config.load(ctx.directory)
    config["enabled"] = False
    autodev_config.save(config, ctx.directory)
    return 0, {"ok": True, "enabled": False}


def _summary(record: dict) -> dict:
    gate_decision = record.get("gate") or {}
    return {key: value for key, value in {
        "run_id": record.get("run_id"), "mode": record.get("mode"), "trigger": record.get("trigger"),
        "status": record.get("status"), "reason": record.get("reason"), "decision": gate_decision.get("decision"),
        "decision_reason": gate_decision.get("reason"), "started": record.get("started"),
        "finished": record.get("finished")}.items() if value is not None}


def status(ctx: Context) -> tuple[int, dict]:
    config = autodev_config.load(ctx.directory)
    now = ctx.clock()
    return 0, {
        "ok": True,
        "config": {key: config[key] for key in ("enabled", "mode", "repository", "times", "catch_up_minutes",
                                                "allowed_hours", "require_ac_power")},
        "config_file": str(autodev_config.config_path(ctx.directory)),
        "scheduler": ctx.scheduler().status(),
        "lease_held": lease.held(ctx.directory),
        "next_slot": iso(next_slot(config["times"], now)),
        "tick": _tick_state(ctx),
        "recent_runs": [_summary(record) for record in records.all_records(ctx.directory)[:5]],
        "real_modes": {"available": False, "missing": list(MISSING_FOR_REAL_MODES)},
    }


def ready(ctx: Context) -> tuple[int, dict]:
    """A stub until #350: the automatic mode is never ready yet."""
    return 1, {"ok": True, "ready": False, "missing": list(MISSING_FOR_REAL_MODES),
               "note": "the automatic-mode checks, including the sandbox probes, arrive with #350"}


def run_now(ctx: Context, mode: str, pr: int | None) -> tuple[int, dict]:
    return launch(ctx, mode, {"kind": "manual", "at": iso(ctx.clock())}, pr=pr)
