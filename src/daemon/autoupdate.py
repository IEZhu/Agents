"""Opt-in unattended updates of a shared installation from its tracked branch.

A second LaunchAgent, or on Windows a second Task Scheduler task
(`src.daemon.service.TaskScheduler.write_updater`), runs ``auto-update run``
every ``interval`` seconds. Each run is cheap when there is nothing to do: it
fetches the tracked branch and returns. When a fast-forward is available it waits for the service to be idle
and then runs the same controller transaction as ``update``: build the target
and its stores while the service serves, check again that it is idle, then
drain, stop, fast-forward, move the stores in, probation, ready, rollback on
failure. Clients use stateless HTTP, so they reach the restarted process on
their next request.

Once per embedding model generation (`src/model_migration.py`) a run also
switches the service to the default model, with no new commit needed: the
model downloads while the service still serves, then the same transaction
rebuilds the stores for it and verifies the restarted service.

Targets that the transaction would refuse (a diverged branch, a dirty tree,
changed dependency manifests) are rejected before the service is touched, so a
blocked update never costs a restart.
"""
from datetime import datetime, timezone
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import plistlib
import shutil
import subprocess

from src.file_lock import file_lock
from . import service
from .state import atomic_private, read_json, write_json
from .update import DEPENDENCIES, NO_WINDOW

DEFAULT_INTERVAL = 900
DEFAULT_IDLE_SECONDS = 120
GIT_TIMEOUT = 60

logger = logging.getLogger("agents-core.auto-update")


def settings(controller):
    configured = controller.config.get("auto_update") or {}
    return {"enabled": bool(configured.get("enabled")),
            "interval": int(configured.get("interval", DEFAULT_INTERVAL)),
            "idle_seconds": int(configured.get("idle_seconds", DEFAULT_IDLE_SECONDS))}


def label(controller):
    return controller.label + ".updater"


def plist_path(controller):
    return controller.plist.with_name(label(controller) + ".plist")


def write_plist(controller, interval):
    payload = {"Label": label(controller),
               "ProgramArguments": [controller.config["python"], "-m", "src.daemon", "--state",
                                    str(controller.directory), "auto-update", "run"],
               "WorkingDirectory": controller.config["installation"],
               "EnvironmentVariables": {"PATH": controller.config["path"], "PYTHONUNBUFFERED": "1"},
               "StartInterval": interval, "RunAtLoad": False,
               "ProcessType": "Background", "LowPriorityIO": True,
               "StandardOutPath": "/dev/null", "StandardErrorPath": "/dev/null"}
    atomic_private(plist_path(controller), plistlib.dumps(payload).decode())


def _save_settings(controller, **values):
    config = read_json(controller.directory / "service.json", {})
    config["auto_update"] = {**settings(controller), **values}
    write_json(controller.directory / "service.json", config)
    controller.config = config


def enable(controller, interval=DEFAULT_INTERVAL, idle_seconds=DEFAULT_IDLE_SECONDS):
    if not controller.config:
        raise RuntimeError("Service is not installed")
    if interval < 60 or idle_seconds < 0:
        raise ValueError("interval must be at least 60 seconds and idle_seconds not negative")
    with file_lock(controller.directory / "control.lock", blocking=False):
        if service.PLATFORM == "win32":
            # One /Create replaces the task; a failure leaves the previous task and config.
            service.TaskScheduler(controller).write_updater(interval)
            _save_settings(controller, enabled=True, interval=interval, idle_seconds=idle_seconds)
            return status(controller)
        # Config and plist change only once launchd runs the new job; any failure
        # leaves the previous config, plist and job as they were.
        domain, target = f"gui/{os.getuid()}", f"gui/{os.getuid()}/{label(controller)}"
        plist = plist_path(controller)
        previous_plist = plist.read_bytes() if plist.exists() else None
        was_loaded = controller.launchctl("print", target, check=False).returncode == 0
        if was_loaded:  # reload a changed interval
            controller.launchctl("bootout", target, check=False)
            if controller.launchctl("print", target, check=False).returncode == 0:
                raise RuntimeError("launchd could not unload the current updater; nothing changed")
        try:
            write_plist(controller, interval)
            controller.launchctl("bootstrap", domain, str(plist))
        except BaseException:
            if previous_plist is None:
                plist.unlink(missing_ok=True)
            else:
                atomic_private(plist, previous_plist.decode())
                if was_loaded:
                    controller.launchctl("bootstrap", domain, str(plist), check=False)
            raise
        _save_settings(controller, enabled=True, interval=interval, idle_seconds=idle_seconds)
    return status(controller)


def disable(controller):
    with file_lock(controller.directory / "control.lock", blocking=False):
        if service.PLATFORM == "win32":
            service.TaskScheduler(controller).remove_updater()
            if controller.config:
                _save_settings(controller, enabled=False)
            return {"enabled": False}
        target = f"gui/{os.getuid()}/{label(controller)}"
        result = controller.launchctl("bootout", target, check=False)
        if result.returncode and controller.launchctl("print", target, check=False).returncode == 0:
            raise RuntimeError("launchd could not stop the updater; it is still scheduled")
        plist_path(controller).unlink(missing_ok=True)
        if controller.config:
            _save_settings(controller, enabled=False)
    return {"enabled": False}


def status(controller):
    if service.PLATFORM == "win32":
        loaded = service.TaskScheduler(controller).updater_scheduled()
    else:
        loaded = controller.launchctl("print", f"gui/{os.getuid()}/{label(controller)}", check=False).returncode == 0
    return {**settings(controller), "scheduled": loaded,
            "last_run": read_json(controller.directory / "auto-update.json", {})}


def _git(controller, *args, check=True):
    return subprocess.run([controller.config["git"], *args], cwd=controller.config["installation"],
                          capture_output=True, text=True, timeout=GIT_TIMEOUT, check=check,
                          stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
                          env={**os.environ, "PATH": controller.config["path"], "GIT_TERMINAL_PROMPT": "0",
                               "GCM_INTERACTIVE": "false"})


def check_target(controller, remote=None, branch=None):
    """Fetch and classify the tracked branch without touching the service."""
    from src.engine.config import AUTO_UPDATE_BRANCH, AUTO_UPDATE_REMOTE
    from src.self_update import _is_safe_arg
    remote, branch = remote or AUTO_UPDATE_REMOTE, branch or AUTO_UPDATE_BRANCH
    if not (_is_safe_arg(remote) and _is_safe_arg(branch)):
        return {"state": "skipped", "reason": f"unsafe remote or branch name: {remote!r} {branch!r}"}
    current = _git(controller, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip()
    if current != branch:
        return {"state": "skipped", "reason": f"checked-out branch is {current or 'detached'}, not {branch}"}
    if _git(controller, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return {"state": "skipped", "reason": "tracked files have local changes"}
    fetched = _git(controller, "fetch", "--quiet", remote, branch, check=False)
    if fetched.returncode:
        return {"state": "skipped", "reason": "fetch failed: " + fetched.stderr.strip()[-300:]}
    head = _git(controller, "rev-parse", "HEAD").stdout.strip()
    target = _git(controller, "rev-parse", "FETCH_HEAD").stdout.strip()
    if head == target:
        return {"state": "up_to_date", "head": head}
    found = {"head": head, "target": target}
    if _git(controller, "merge-base", "--is-ancestor", head, target, check=False).returncode:
        return {**found, "state": "skipped", "reason": "not a fast-forward"}
    changed = _git(controller, "diff", "--name-only", head, target, "--", *DEPENDENCIES).stdout.split()
    if changed:
        return {**found, "state": "skipped", "reason": "dependency manifests changed: " + ", ".join(changed)}
    return {**found, "state": "available"}


def _record(controller, result):
    path = controller.directory / "auto-update.json"
    previous = read_json(path, {})
    stamped = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), **result}
    # A blocked target is re-checked every interval; log it once, not 96 times a day.
    if {k: v for k, v in previous.items() if k != "time"} != result:
        logger.info("%s", stamped)
    write_json(path, stamped)
    return stamped


def run(controller):
    """One scheduled attempt. Never raises for an expected refusal."""
    handler = RotatingFileHandler(controller.directory / "auto-update.log", maxBytes=1024**2, backupCount=3)
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    try:
        return _run(controller)
    finally:
        logger.removeHandler(handler)
        handler.close()


def other_readers(controller, daemon_pid):
    """PIDs other than the daemon holding the installation's session lease file.

    `offline_update` stops the service before it can learn that a stdio server
    holds the lease, then restarts it. Checking first keeps a connected stdio
    client from costing a restart on every interval. None when unknown; on
    Windows the held stdio slots instead (`held_slots`).
    """
    if service.PLATFORM == "win32":
        return held_slots(controller)
    lsof = shutil.which("lsof", path=controller.config["path"] + ":/usr/sbin")
    if not lsof:
        return None
    lease = Path(controller.config["installation"]) / "data/.sessions.lock"
    listed = subprocess.run([lsof, "-t", "--", str(lease)], capture_output=True, text=True, timeout=GIT_TIMEOUT,
                            stdin=subprocess.DEVNULL)
    return sorted({int(pid) for pid in listed.stdout.split()} - {daemon_pid, os.getpid()})


def held_slots(controller):
    """Windows: the stdio slots whose lease a running server holds (`src.startup.stdio_derived_state`).

    Windows does not say which processes hold a lock. Each stdio server holds one slot for
    its whole session, the service none: a slot that cannot be locked has a stdio reader.
    A reader without a slot, such as a `python -m src.user_sync` command, goes unseen: the
    update then stops the service, finds the lease held and restarts it.
    """
    base = Path(controller.config["installation"]) / "data" / "stdio"
    held = []
    for lease in sorted(base.glob("*/.lease")):
        if lease.is_symlink():
            continue
        try:
            with file_lock(lease, blocking=False):
                pass
        except BlockingIOError:
            held.append("stdio slot " + lease.parent.name)
    return held


def _enabled_on_disk(controller):
    # `disable` may have run while this run was fetching.
    return bool((read_json(controller.directory / "service.json", {}).get("auto_update") or {}).get("enabled"))


def _readers_refusal(controller, daemon_pid):
    """Defer while a stdio server holds the installation; unknown readers do not defer."""
    try:
        readers = other_readers(controller, daemon_pid)
    except (subprocess.SubprocessError, OSError):
        return None  # unknown: offline_update still refuses a held lease, after a restart
    if readers:
        return {"state": "deferred", "reason": f"stdio readers hold the installation: {readers}"}
    return None


def _precheck(controller):
    """Preconditions rechecked under the control lock; a refusal result or None."""
    if not _enabled_on_disk(controller):
        return {"state": "disabled"}
    health = controller.status()
    if health.get("state") != "ready":
        return {"state": "deferred", "reason": f"service is {health.get('state')}"}
    # Work may have arrived since the first check; draining now would 503 it.
    if health.get("inflight") or health.get("io_pending") or \
            health.get("idle_seconds", 0) < settings(controller)["idle_seconds"]:
        return {"state": "deferred", "reason": "service is busy"}
    # A stdio server may have started while the update was built.
    return _readers_refusal(controller, health.get("pid"))


def _run(controller):
    if not settings(controller)["enabled"]:
        return {"state": "disabled"}
    if any((controller.directory / name).exists() for name in ("maintenance.json", "transaction.json")):
        return _record(controller, {"state": "blocked", "reason": "maintenance or unfinished transaction; run recover"})
    try:
        found = check_target(controller)
    except (subprocess.SubprocessError, OSError) as error:
        return _record(controller, {"state": "skipped", "reason": f"git failed: {type(error).__name__}: {error}"})
    from src.model_migration import service_switch_pending
    if found["state"] == "up_to_date" and service_switch_pending(controller.config):
        # The update that brought this code left the service on its old model;
        # the same transaction switches it (`offline_update`).
        found = {**found, "state": "available", "target": found["head"], "model_switch": True}
    if found["state"] != "available":
        # Recorded so `status` shows the latest run; `_record` logs only changes.
        return _record(controller, found)
    health = controller.status()
    if health.get("state") != "ready":
        # A stopped service stays stopped; the next manual start picks nothing up.
        return _record(controller, {**found, "state": "deferred", "reason": f"service is {health.get('state')}"})
    idle = settings(controller)["idle_seconds"]
    if health.get("inflight") or health.get("io_pending") or health.get("idle_seconds", 0) < idle:
        return _record(controller, {**found, "state": "deferred", "reason": "service is busy"})
    if refusal := _readers_refusal(controller, health.get("pid")):
        return _record(controller, {**found, **refusal})
    if not _enabled_on_disk(controller):
        return {"state": "disabled"}
    from .update import TargetMoved, offline_update
    try:
        result = offline_update(controller, expected_target=found["target"], precheck=lambda: _precheck(controller))
    except TargetMoved as error:
        # Nothing was applied; the next interval checks the new commit from scratch.
        return _record(controller, {**found, "state": "deferred", "reason": str(error)})
    except BlockingIOError:
        return _record(controller, {**found, "state": "deferred", "reason": "another controller operation or a stdio reader holds the installation"})
    except Exception as error:
        return _record(controller, {**found, "state": "failed", "error": f"{type(error).__name__}: {error}"})
    if result.get("state") == "disabled":
        return {"state": "disabled"}
    if result.get("state") == "deferred":
        return _record(controller, {**found, **result})
    return _record(controller, {**found, "state": str(result.get("state")),
                                **{key: result[key] for key in ("model", "model_from") if key in result}})
