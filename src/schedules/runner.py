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

``codex_probe`` is the owner's live check of Codex (#344, docs/autodev-codex.md): it builds a
Codex worker or stage launch under its outer profile, runs the model-free probes and the nested
check, and decides between Codex and the Claude worker; for a stage it then runs the #343 probe
plan through ``codex exec`` (a model turn) only when every model-free check passed.

``probe`` is the owner's live check of the session sandbox (#343, docs/autodev-sandbox.md): it
builds one session exactly as a run would (`src.schedules.sandbox`), checks its login, plants the
decoys and runs `src.schedules.probes.guard`, which starts a real probe session (a model turn), so
it refuses without the explicit confirmation flag. No stage session starts in this part: the run
sequence (#347) builds every Claude session with `sandbox.build`, calls `probes.guard` and
`probes.require` before it, records the report with `probes.record_report`, and starts it only
through `probes.start_session`, in every mode (dry-run, run-now, scheduled and resume).
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from datetime import datetime
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
from typing import Callable

from src import executables
from src.daemon.state import private_dir, read_json, write_json

from . import config as autodev_config
from . import codex_probes, codex_proxy, codex_sandbox, decoys, drift, heavy, launchagent, lease, probes, records, repos
from . import sandbox, scan, socket_service
from .gate import gate
from .github import (GitHubClient, GitHubError, NetworkError, TokenUnavailable, fetch_snapshot, keychain_token,
                     token_provider)
from .heavy import HEAVY, heavy_processes  # noqa: F401 (HEAVY: moved to heavy, still importable here)
from .supervise import SupervisorStopped, run_supervised
from .tick import decide, iso, local_now, next_slot

REAL_MODES = ("run", "resume")
MISSING_FOR_REAL_MODES = ["#347 run sequence", "#350 automatic mode"]
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
    # The sandbox probe (#343): the claude executable (default: tools.json, then PATH), the home
    # directory the decoys go into, the platform, the session directories (`ProbeSession`), the
    # runner of `claude auth status`, `security` and `launchctl list`, the managed-settings check,
    # where the run's short temporary root goes, and the runner's own name resolution.
    claude: str | None = None
    home: Path = field(default_factory=Path.home)
    platform: str = sys.platform
    probe_session: Callable[..., "ProbeSession"] | None = None
    command_runner: Callable[..., subprocess.CompletedProcess] = subprocess.run
    managed_sources: Callable[[], list] = sandbox.managed_sources
    socket_root: Path | None = None
    probe_timeout: float = probes.PROBE_TIMEOUT
    temp_base: Path = sandbox.TMP_BASE
    resolver: Callable[[str], bool] = probes.resolves
    # The Codex check (#344): the codex executable (default: tools.json, then PATH), how a probe
    # command runs (`codex_probes.execute`) and how the runner resolves the external address.
    codex: str | None = None
    codex_execute: Callable[..., probes.Captured] = codex_probes.execute
    codex_resolver: Callable[[str], str | None] = codex_probes.resolve_ipv4

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


# --- the sandbox probe (#343) -------------------------------------------------------------------------

PROBE_CONFIRMATION = "--confirm-live-probe"


@dataclass
class ProbeSession:
    """The directories of one probe session (`sandbox.session_layout`) and how to remove them."""
    layout: dict
    venv: Path
    cleanup: Callable[[], None]


def default_probe_session(ctx: Context, config: dict, run_id: str) -> ProbeSession:
    """The directories a run's session gets: a one-off repository at ``pinned``, the exchange,
    state copy and ``gh`` directories beside it, and the read-only environment for ``pinned``."""
    trusted = repos.TrustedRepository.from_config(config, ctx.installation).ensure()
    pinned = trusted.fetch()
    layout = sandbox.session_layout(sandbox.sessions_root(trusted.path), run_id, "probe")
    sandbox.prepare_layout(layout)

    def cleanup() -> None:
        shutil.rmtree(Path(layout["base"]).parent, ignore_errors=True)

    try:
        trusted.create_session_repository(layout["repository"])
        venv = trusted.environment(pinned, trusted.path.parent)
    except BaseException:
        cleanup()
        raise
    return ProbeSession(layout, venv, cleanup)


def claude_executable(ctx: Context) -> str | None:
    if ctx.claude:
        return ctx.claude
    tools = read_json(ctx.schedules / "tools.json", None)
    entry = tools.get("claude") if isinstance(tools, dict) else None
    if isinstance(entry, dict) and isinstance(entry.get("path"), str):
        return entry["path"]
    found = shutil.which("claude")
    return os.path.realpath(found) if found else None


def probe_targets(ctx: Context, config: dict, mode: str, config_dir: Path | None = None) -> probes.Targets:
    """What the probes must find closed: the runner's records, the installation, ``autodev.json``,
    the scheduler plist, the trusted repository, the daemon's port and the real keychain items
    (the broker's, gh's, Claude Code's default one, the one Claude Code names after the session's
    configuration directory, and the OAuth item of the keychain login mode)."""
    try:
        service = read_json(ctx.directory / "service.json", {})
    except (OSError, ValueError):
        service = {}
    port = service.get("port") if isinstance(service, dict) else None
    items = [str(config["github"]["token"]).removeprefix("keychain:"), *probes.REAL_KEYCHAIN_ITEMS[1:]]
    if config_dir is not None:
        items.append(sandbox.claude_keychain_item(config_dir))
    if mode.startswith(sandbox.KEYCHAIN_PREFIX):
        items.append(mode.removeprefix(sandbox.KEYCHAIN_PREFIX))
    return probes.Targets(
        installation=ctx.installation, runs=records.runs_dir(ctx.directory), schedules=ctx.schedules,
        config_file=autodev_config.config_path(ctx.directory),
        trusted=autodev_config.runner_location(config["clone"], ctx.installation),
        plist=ctx.scheduler().plist,
        daemon_port=port if isinstance(port, int) and not isinstance(port, bool) and 0 < port < 65536 else None,
        keychain_items=tuple(dict.fromkeys(items)),
        extra_sockets=(probes.DOCKER_SOCKET,) if probes.DOCKER_SOCKET.exists() else ())


def _home_path(value: str, home: Path) -> Path:
    """A configured ``~/`` path against the context's home directory (never the process's)."""
    text = str(value)
    return home / text[2:] if text.startswith("~/") else Path(text)


def broker_secrets(ctx: Context, config: dict) -> list[scan.KnownSecret]:
    """The broker's token and deploy key as known secrets of the probe scan, when they exist (a
    dry run may have neither); they are read in memory only and never written or printed."""
    found = []
    service = str(config["github"]["token"]).removeprefix("keychain:")
    try:
        token = keychain_token(service, runner=ctx.command_runner)
    except TokenUnavailable:
        token = None
    if token and len(token) >= scan.KNOWN_MIN_LENGTH:
        found.append(scan.KnownSecret("broker token", token))
    key = _home_path(config["github"]["deploy_key"], ctx.home)
    try:
        if not os.path.islink(key) and key.is_file() and key.stat().st_size <= 64 * 1024:
            text = key.read_bytes().decode("latin-1").strip()
            if len(text) >= scan.KNOWN_MIN_LENGTH:
                found.append(scan.KnownSecret("deploy key", text))
    except OSError:
        pass
    return found


def probe(ctx: Context, mode: str, kind: str, confirmed: bool) -> tuple[int, dict]:
    """The owner's live sandbox check: one guarded probe session of ``kind``, recorded as a run."""
    if not confirmed:
        return 1, {"ok": False, "error": "confirmation_required",
                   "message": "the probe starts a real claude -p session (a model turn) that tries to read "
                              "decoys and write outside its sandbox; run again with " + PROBE_CONFIRMATION}
    config = autodev_config.load(ctx.directory)
    now = ctx.clock()
    run_id = records.new_run_id(now)
    record = {"trigger": {"kind": "probe"}, "mode": mode, "session_kind": kind, "started": iso(now),
              "status": "running"}
    markers: list[str] = []
    try:
        with lease.hold(ctx.directory):
            records.write_record(run_id, record, ctx.directory)
            record.update(_probe(ctx, config, run_id, mode, kind, markers))
    except lease.LeaseBusy as error:
        record.update(infra(f"lease: {error}"))
    record["finished"] = iso(ctx.clock())
    written = records.write_record(run_id, record, ctx.directory, known=markers)
    return (0 if written.get("status") == "completed" else 1), written


def _probe(ctx: Context, config: dict, run_id: str, mode: str, kind: str, markers: list[str]) -> dict:
    stage = config["stages"][kind]
    if stage["client"] != "claude":
        return {"status": "refused", "reason": f"the {kind} stage uses {stage['client']}; only Claude sessions are sandboxed"}
    claude = claude_executable(ctx)
    if not claude:
        return infra("cli: the claude executable was not found (tools.json, PATH)")
    known: list[scan.KnownSecret] = []
    try:
        login = sandbox.login_mode(config)
        oauth = None
        if login != sandbox.LOGIN_CONFIG_DIR:
            oauth = sandbox.keychain_secret(login.removeprefix(sandbox.KEYCHAIN_PREFIX), runner=ctx.command_runner)
            markers.append(oauth)
            known.append(scan.KnownSecret("oauth token", oauth))
        known += broker_secrets(ctx, config)
        markers.extend(secret.value for secret in known if isinstance(secret.value, str))
        path = run_environment(ctx)["PATH"]
        config_dir = sandbox.config_dir(ctx.directory)
        sandbox.require_login(claude, config_dir, path=path, home=ctx.home, oauth_token=oauth,
                              runner=ctx.command_runner)
        planted = decoys.ensure(ctx.directory, ctx.home, keychain=config["sandbox"]["decoy_keychain"],
                                runner=ctx.command_runner)
        markers.extend(decoy.marker for decoy in planted)
        session = (ctx.probe_session or default_probe_session)(ctx, config, run_id)
    except sandbox.LoginRequired as error:
        return {"status": "refused", "reason": str(error), "login_command": error.command}
    except (sandbox.SandboxError, decoys.DecoyError) as error:
        return {"status": "refused", "reason": f"sandbox: {error}"}
    except repos.FetchError as error:
        return infra(f"network: {error}")
    except (repos.RepositoryError, ValueError, OSError) as error:
        return infra(f"cli: probe session: {type(error).__name__}: {error}")
    temp_root = None
    try:
        layout = session.layout
        has_bash = "Bash" in sandbox.tools_for(kind)
        run_dir = records.run_dir(run_id, ctx.directory)
        temp_root = sandbox.make_temp_root(ctx.temp_base)
        writable = [layout["repository"], layout["exchange"], temp_root]
        with _maybe_serve(has_bash, run_dir, writable, ctx.socket_root) as endpoint:
            interpreters = [socket_service.client_interpreter()]
            if (base := sandbox.venv_base_interpreter(session.venv)) is not None:
                interpreters.append(str(base))
            roots = sandbox.interpreter_roots(*interpreters)
            if endpoint is not None:
                roots = (endpoint.socket.parent / socket_service.CLIENTS_DIR, *roots)
            paths = sandbox.SessionPaths(
                home=ctx.home, repository=Path(layout["repository"]), exchange=Path(layout["exchange"]),
                state_copy=Path(layout["state_copy"]), venv=session.venv, config_dir=config_dir,
                gh_config=Path(layout["gh_config"]), settings_file=Path(layout["settings_file"]),
                tmp_root=temp_root, state_dir=Path(ctx.directory),
                socket=endpoint.socket if endpoint is not None else None, read_roots=roots)
            settings = stage.get("claude") or {}
            spec = sandbox.build(claude, kind, paths, path=path, mode=login, oauth_token=oauth,
                                 model=settings.get("model"), effort=settings.get("effort"),
                                 run_variables={"AUTODEV_RUN_ID": run_id})
            report = probes.guard(spec, directory=ctx.directory,
                                  targets=probe_targets(ctx, config, login, config_dir),
                                  persistent=planted, mode=mode, run_id=run_id, known=known, force_session=True,
                                  platform=ctx.platform, timeout=ctx.probe_timeout, socket_root=ctx.socket_root,
                                  command_runner=ctx.command_runner, managed=ctx.managed_sources,
                                  allowed_plugins=config["sandbox"]["allowed_plugins"], resolver=ctx.resolver)
    except (sandbox.SandboxError, socket_service.UnsafeSocketDirectory) as error:
        return {"status": "refused", "reason": f"sandbox: {error}"}
    except (ValueError, OSError) as error:
        return infra(f"cli: probe session: {type(error).__name__}: {error}")
    finally:
        session.cleanup()
        if temp_root is not None:
            sandbox.remove_temp_root(temp_root, ctx.temp_base)
    result = {"status": "completed" if report.ok else "refused", "probes": [report.to_record()]}
    if not report.ok:
        result["reason"] = "sandbox probes failed" + (" and the decoy tripwire turned the schedule off"
                                                      if report.tripped else "")
    return result


def _maybe_serve(serve: bool, run_dir: Path, writable: list, temp_root: Path | None):
    """The runner socket for a session with Bash (its clients and socket are the allowed ones)."""
    if not serve:
        return contextlib.nullcontext()
    return socket_service.serve(run_dir, socket_service.Registry(), session_writable=writable, temp_root=temp_root)


# --- the Codex check (#344) ----------------------------------------------------------------------------

CODEX_PROBE_KINDS = codex_sandbox.KINDS


def codex_executable(ctx: Context) -> str | None:
    if ctx.codex:
        return ctx.codex
    tools = read_json(ctx.schedules / "tools.json", None)
    entry = tools.get("codex") if isinstance(tools, dict) else None
    if isinstance(entry, dict) and isinstance(entry.get("path"), str):
        return entry["path"]
    return shutil.which("codex")


def codex_probe(ctx: Context, kind: str, confirmed: bool) -> tuple[int, dict]:
    """The owner's live Codex check: one worker or stage launch, its probes and the decision,
    recorded as a run; the stored stage availability is updated for #347 and #348."""
    if not confirmed:
        return 1, {"ok": False, "error": "confirmation_required",
                   "message": "the check runs codex under its outer profile, tries to read decoys and write outside "
                              "it, and for a stage starts a real codex exec session (a model turn) when the model-free "
                              "checks pass; run again with " + PROBE_CONFIRMATION}
    if kind not in CODEX_PROBE_KINDS:
        return 1, {"ok": False, "error": "invalid_kind", "message": f"--kind must be one of {', '.join(CODEX_PROBE_KINDS)}"}
    config = autodev_config.load(ctx.directory)
    now = ctx.clock()
    run_id = records.new_run_id(now)
    record = {"trigger": {"kind": "codex-probe"}, "codex_kind": kind, "started": iso(now), "status": "running"}
    markers: list[str] = []
    try:
        with lease.hold(ctx.directory):
            records.write_record(run_id, record, ctx.directory)
            record.update(_codex_probe(ctx, config, run_id, kind, markers))
    except lease.LeaseBusy as error:
        record.update(infra(f"lease: {error}"))
    record["finished"] = iso(ctx.clock())
    written = records.write_record(run_id, record, ctx.directory, known=markers)
    return (0 if written.get("status") == "completed" else 1), written


def _codex_work(run_dir: Path) -> dict[str, Path]:
    """A synthetic tree, materials and schema for the worker check (#345 assembles the real ones)."""
    work = private_dir(run_dir / "codex-work")
    tree, materials = private_dir(work / "tree"), private_dir(work / "materials")
    (tree / "README.md").write_text("autodev Codex check\n")
    (materials / "diff.txt").write_text("no change\n")
    schema = work / "schema.json"
    schema.write_text('{"type": "object", "properties": {"verdict": {"type": "string"}}}\n')
    return {"tree": tree, "materials": materials, "schema": schema}


def _codex_probe(ctx: Context, config: dict, run_id: str, kind: str, markers: list[str]) -> dict:
    try:
        mode = codex_sandbox.login_mode(config)
        codex = codex_executable(ctx)
        path = run_environment(ctx)["PATH"]
        home_dir = codex_sandbox.codex_home(ctx.directory)
        planted = decoys.ensure(ctx.directory, ctx.home, keychain=config["sandbox"]["decoy_keychain"],
                                runner=ctx.command_runner)
        markers.extend(decoy.marker for decoy in planted)
        known = broker_secrets(ctx, config)
        markers.extend(secret.value for secret in known if isinstance(secret.value, str))
    except (codex_sandbox.CodexSandboxError, decoys.DecoyError) as error:
        return {"status": "refused", "reason": f"codex: {error}"}
    run_dir = records.run_dir(run_id, ctx.directory)
    temp_root = session = None
    try:
        temp_root = sandbox.make_temp_root(ctx.temp_base)
        common = dict(kind=kind, home=ctx.home, codex_home=home_dir, tmp_root=temp_root,
                      run_home=codex_sandbox.prepare_run_home(temp_root), out_dir=private_dir(run_dir / "codex-out"),
                      profile=run_dir / "codex-profile" / "codex.sb")
        if kind == codex_sandbox.WORKER:
            paths = codex_sandbox.CodexPaths(**common, **_codex_work(run_dir))
        else:
            session = (ctx.probe_session or default_probe_session)(ctx, config, run_id)
            paths = codex_sandbox.CodexPaths(**common, repository=Path(session.layout["repository"]),
                                             exchange=Path(session.layout["exchange"]),
                                             read_roots=(Path(session.layout["state_copy"]), Path(session.venv)))
        targets = codex_probes.CodexTargets(
            state_dir=Path(ctx.directory), secrets_dir=ctx.home / ".config" / "agents-autodev",
            runner=probe_targets(ctx, config, sandbox.LOGIN_CONFIG_DIR) if kind == codex_sandbox.STAGE else None)
        settings = (config["delegates"].get("code-review-pass") or {}).get("codex") or {}
        if kind == codex_sandbox.STAGE:
            settings = (config["stages"]["implement"].get("codex") or settings)
        with codex_proxy.ConnectProxy(codex_sandbox.allowed_hosts(mode)) as proxy:
            options = dict(path=path, proxy=proxy, mode=mode, targets=targets, persistent=planted,
                           directory=ctx.directory, run_id=run_id, model=settings.get("model"),
                           effort=settings.get("effort"), known=known, platform=ctx.platform,
                           execute_=ctx.codex_execute, command_runner=ctx.command_runner,
                           resolver=ctx.codex_resolver, socket_root=ctx.socket_root)
            if kind == codex_sandbox.STAGE:
                decision = codex_probes.stage_availability(codex, paths, timeout=ctx.probe_timeout, **options)
            else:
                decision = codex_probes.decide(codex, paths, **options)
            report = decision.report
            if report is not None and "workspace_write_blocks_network" not in report.installed \
                    and ctx.platform == "darwin" and codex and report.installed.get("version"):
                report.installed.update(codex_probes.installed_checks(os.path.realpath(codex), path=path,
                                                                      execute_=ctx.codex_execute))
    except (codex_sandbox.CodexSandboxError, sandbox.SandboxError) as error:
        return {"status": "refused", "reason": f"codex: {error}"}
    except repos.FetchError as error:
        return infra(f"network: {error}")
    except (repos.RepositoryError, ValueError, OSError) as error:
        return infra(f"cli: codex check: {type(error).__name__}: {error}")
    finally:
        if session is not None:
            session.cleanup()
        if temp_root is not None:
            sandbox.remove_temp_root(temp_root, ctx.temp_base)
        shutil.rmtree(run_dir / "codex-work", ignore_errors=True)
    result = {"status": "completed" if decision.codex else "refused", **decision.to_record()}
    if decision.report is not None and decision.report.tripped:
        result["tripped"] = True
    if not decision.codex:
        result["reason"] = f"Codex is unavailable ({decision.reason}); the step uses the Claude worker"
    return result
