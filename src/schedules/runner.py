"""The autodev run and the scheduler tick: what the CLI commands do (design 10.1, 10.2).

``launch`` starts ``<python> -P -m src.schedules run autodev --run-id …`` under supervision
(`src.schedules.supervise`) and completes its record when the run timed out or crashed. Once the
run's process tree is gone, it also removes a runner socket the run left behind (a SIGKILL ends
`serve` without its cleanup) when the record names one under ``endpoint``
(`socket_service.Endpoint.to_record`), and records what it did as ``endpoint_cleanup``.
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
import signal
import subprocess
import sys
from typing import Callable

from src import executables
from src.daemon.state import private_dir, read_json, write_json

from . import config as autodev_config
from . import drift, heavy, launchagent, lease, records, repos, socket_service
from .gate import gate
from .github import GitHubClient, GitHubError, NetworkError, TokenUnavailable, fetch_snapshot, token_provider
from .heavy import HEAVY, heavy_processes  # noqa: F401 (HEAVY: moved to heavy, still importable here)
from .supervise import SupervisorStopped, run_supervised
from .tick import decide, iso, local_now, next_slot

REAL_MODES = ("run", "resume")
MISSING_FOR_REAL_MODES = ["#342 broker", "#347 run sequence", "#350 automatic mode"]
DRY_RUN_TIMEOUT_MINUTES = 30
TICK_STATE = "tick.json"
UPDATE_MARKERS = ("maintenance.json", "transaction.json")
_ENVIRONMENT = ("HOME", "USER", "LOGNAME", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")


@dataclass
class Context:
    """Everything a command touches, so tests can replace each part."""
    installation: Path
    directory: Path
    python: str = sys.executable
    clock: Callable[[], datetime] = local_now  # in the local zoneinfo zone (daylight saving)
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

def update_markers(directory: Path) -> list[str]:
    """The daemon's update markers present in the state directory."""
    return [name for name in UPDATE_MARKERS if (Path(directory) / name).exists()]


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

def default_drift(installation: Path, config: dict) -> dict:
    """Fetch ``main`` into the trusted repository and compare the installation with its ``pinned``."""
    trusted = repos.TrustedRepository.from_config(config, installation).ensure()
    commit = trusted.fetch()
    return {"pinned": commit, "runner_commit": drift.commit_of(installation),
            "changed": drift.check(installation, commit, repository=trusted.path)}


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
            # Checked again under the lease: an update that began after the tick's check holds
            # the lease itself, and a marker left by any other update or `recover` shows here.
            if markers := update_markers(ctx.directory):
                record.update(infra(f"update: a daemon update or maintenance is in progress ({', '.join(markers)})"))
            else:
                record.update(_dry_run(ctx, config, now, pr))
    except lease.LeaseBusy as error:
        record.update(infra(f"lease: {error}"))
    record["finished"] = iso(ctx.clock())
    written = records.write_record(run_id, record, ctx.directory)
    return (0 if written["status"] == "completed" else 1), written


def _dry_run(ctx: Context, config: dict, now: datetime, pr: int | None) -> dict:
    try:
        checked = ctx.drift_check(ctx.installation) if ctx.drift_check else default_drift(ctx.installation, config)
    except drift.DriftCheckFailed as error:
        return infra(f"cli: {error}")
    except repos.FetchError as error:
        return infra(f"network: {error}")
    except (repos.RepositoryError, ValueError, OSError) as error:
        return infra(f"cli: trusted repository: {error}")
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


def remove_endpoint(record: dict, run_dir: Path) -> None:
    """Remove the socket, clients and private directory a killed run left behind, when its
    record names them under ``endpoint``; note the outcome as ``endpoint_cleanup``."""
    endpoint = record.get("endpoint")
    if not isinstance(endpoint, dict):
        return
    outcome = socket_service.remove_leftovers(endpoint, run_directory=run_dir)
    if outcome["removed"] or outcome["kept"]:
        record["endpoint_cleanup"] = outcome


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
    except SupervisorStopped as stopped:  # the run's tree is already swept; record it, then stop
        record = records.read_record(run_id, ctx.directory) or {}
        remove_endpoint(record, run_dir)
        record.update(infra(f"stopped: the supervisor received {signal.Signals(stopped.signal).name}; "
                            "the run's process tree was ended"))
        record.setdefault("finished", iso(ctx.clock()))
        records.write_record(run_id, record, ctx.directory)
        raise
    except OSError as error:
        outcome, failure = None, infra(f"cli: the run could not start: {type(error).__name__}: {error}")
    else:
        failure = None
    record = records.read_record(run_id, ctx.directory) or {}
    remove_endpoint(record, run_dir)  # the run and everything it started are gone by now
    if failure:
        record.update(failure)
    elif outcome.timed_out:
        record.update(infra(f"timeout: the run exceeded {timeout} seconds; its process tree was ended"),
                      signalled=outcome.signalled)
    else:
        if outcome.signalled:  # processes the run left behind when it exited
            record["swept"] = outcome.signalled
        if record.get("status") not in ("completed", "failed: infra"):
            record.update(infra(f"cli: the run exited with code {outcome.returncode} without a result"))
    if outcome is not None:
        record["caffeinated"] = outcome.caffeinated
        if outcome.failed_scans:  # ps failed: the sweep reached only the run's process group
            record["failed_process_scans"] = outcome.failed_scans
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
        update_pending=lambda: bool(update_markers(ctx.directory)),
        heavy_lease_held=lambda: heavy.held(ctx.directory),
        heavy_processes=ctx.heavy_processes or heavy_processes,
        on_ac_power=ctx.on_ac_power or on_ac_power)
    state = dict(decision.state)
    if decision.action != "idle" or decision.missed or decision.reset:
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
        "heavy_lease_held": heavy.held(ctx.directory),
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
