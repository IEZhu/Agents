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

An autodev run (`src.schedules`) holds a run lease from start to finish; while it is held the
service counts as busy, so an update never replaces the runner's code under a run. The update
takes the same lease, non-blocking, for its whole transaction instead of only probing it, so a
run cannot start between the update's check and its maintenance marker either.

Targets that the transaction would refuse (a diverged branch, a dirty tree,
dependency changes it cannot deliver, `src.daemon.update_dependencies`) are
rejected before the service is touched, so a blocked update never costs a
restart. A target that changes the Python dependencies is ``available`` with an
``environment`` note: the transaction builds its environment while the service
serves and switches ``.venv`` to it in maintenance (`src.environments`, #181).

The branch is the installation's update channel (`src.update_channel`, #381): the
``.env`` key ``AGENTS_AUTO_UPDATE_BRANCH``, which this scheduled run reads although its
environment has no other ``.env`` value. When the checkout is on the previous setting
(``channel set`` recorded it), the run switches branches through the same transaction;
a checkout on any other branch is local development and is skipped, and so is a switch
to a branch that another worktree has checked out.
"""
from datetime import datetime, timezone
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import plistlib
import subprocess

from src import environments, executables
from src.file_lock import file_lock
from src.schedules import lease as run_lease
from . import service
from .state import atomic_private, read_json, write_json
from .update import NO_WINDOW, runtime_source_refusal

DEFAULT_INTERVAL = 900
DEFAULT_IDLE_SECONDS = 120
GIT_TIMEOUT = 60

logger = logging.getLogger("agents-core.auto-update")


def settings(controller):
    configured = controller.config.get("auto_update") or {}
    return {"enabled": bool(configured.get("enabled")),
            "interval": int(configured.get("interval", DEFAULT_INTERVAL)),
            "idle_seconds": int(configured.get("idle_seconds", DEFAULT_IDLE_SECONDS))}


def channel(controller):
    """The update channel this command uses (`src.update_channel.read`)."""
    from src import update_channel
    return update_channel.read(controller.config["installation"])


def label(controller):
    return controller.label + ".updater"


def plist_path(controller):
    return controller.plist.with_name(label(controller) + ".plist")


def write_plist(controller, interval):
    payload = {"Label": label(controller),
               "ProgramArguments": [controller.config["python"], "-m", "src.daemon", "--state",
                                    str(controller.directory), "auto-update", "run"],
               "WorkingDirectory": controller.config["installation"],
               "EnvironmentVariables": {"PATH": executables.service_path(), "PYTHONUNBUFFERED": "1"},
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
        else:
            _bootstrap_updater(controller, interval)
        _save_settings(controller, enabled=True, interval=interval, idle_seconds=idle_seconds)
    # After the lock: the remote check can take GIT_TIMEOUT, which stop and disable need not wait for.
    return _enabled(controller)


def _bootstrap_updater(controller, interval):
    """Config and plist change only once launchd runs the new job; any failure leaves the previous
    config, plist and job as they were."""
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


def _enabled(controller):
    """``status`` after ``enable``, with a ``warning`` when the updater cannot reach its remote.

    The updater's git runs without the user's global git configuration (#279): a fork that needs a
    credential helper, proxy, CA file or SSH command set only there would fail every fetch.
    """
    from src.self_update import _is_safe_arg
    result = status(controller)
    found = channel(controller)
    remote, branch = found.remote, found.branch
    if not (_is_safe_arg(remote) and _is_safe_arg(branch)):
        return result  # every run reports it (check_target)
    try:
        listed = _git(controller, "ls-remote", "--quiet", "--exit-code", remote,
                      "refs/heads/" + branch, check=False)
        problem = (listed.stderr.strip()[-300:] or f"exit code {listed.returncode}") if listed.returncode else None
    except (OSError, subprocess.SubprocessError) as error:
        problem = str(error)
    if problem:
        result["warning"] = (f"the updater cannot reach {remote}/{branch} without your global "
                             f"git configuration ({problem}); put what it needs (credential helper, proxy, CA file, "
                             "SSH command) in the installation's repository configuration: "
                             "docs/shared-mcp-daemon.md#tools-and-path")
    return result


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
    result = {**settings(controller), "scheduled": loaded,
              "last_run": read_json(controller.directory / "auto-update.json", {})}
    if controller.config.get("installation"):
        from src import update_channel
        try:
            result["channel"] = update_channel.summary(controller.config["installation"])
        except (OSError, ValueError) as error:
            result["channel"] = {"error": f"{type(error).__name__}: {error}"}
    return result


def _git(controller, *args, check=True):
    return subprocess.run([executables.path("git"), *args], cwd=controller.config["installation"],
                          capture_output=True, text=True, timeout=GIT_TIMEOUT, check=check,
                          stdin=subprocess.DEVNULL, creationflags=NO_WINDOW,
                          env={**executables.git_environment(controller.config["installation"],
                                                             executables.service_environment()),
                               "GIT_TERMINAL_PROMPT": "0",
                               "GCM_INTERACTIVE": "false"})


def check_target(controller, remote=None, branch=None):
    """Fetch and classify the channel's branch without touching the service.

    A checkout on the previous setting (`src.update_channel.Policy.may_switch`) is a
    branch switch: ``available`` with ``switch``, also when the target does not move the
    commit; a switch to code that lacks non-merge commits of HEAD
    (`src.update_channel.drops_commits`) needs the recorded confirmation.
    """
    from src.self_update import _is_safe_arg
    from src.update_channel import drops_commits, in_use_reason, plausible_branch, worktree_on
    if reason := runtime_source_refusal(controller.config):
        return {"state": "skipped", "reason": reason}
    configured = channel(controller)
    remote, branch = remote or configured.remote, branch or configured.branch
    if not (_is_safe_arg(remote) and _is_safe_arg(branch)):
        return {"state": "skipped", "reason": f"unsafe remote or branch name: {remote!r} {branch!r}"}
    current = _git(controller, "symbolic-ref", "--quiet", "--short", "HEAD", check=False).stdout.strip()
    switching = current != branch
    policy = configured.policy if branch == configured.branch else None
    if switching and (policy is None or not policy.may_switch(current, branch)):
        reason = policy.reason(current, branch) if policy is not None else \
            f"checked-out branch is {current or 'detached'}, not {branch}"
        return {"state": "skipped", "reason": reason}
    if switching:
        named = _git(controller, "check-ref-format", "--branch", branch, check=False)
        if not plausible_branch(branch) or named.returncode or named.stdout.strip() != branch:
            return {"state": "skipped", "reason": f"invalid branch name: {branch!r}"}
        elsewhere = worktree_on(lambda *args: _git(controller, *args, check=False), branch)
        if elsewhere:
            return {"state": "skipped", "reason": in_use_reason(branch, elsewhere)}
    if _git(controller, "status", "--porcelain", "--untracked-files=no").stdout.strip():
        return {"state": "skipped", "reason": "tracked files have local changes"}
    fetched = _git(controller, "fetch", "--quiet", remote, branch, check=False)
    if fetched.returncode:
        return {"state": "skipped", "reason": "fetch failed: " + fetched.stderr.strip()[-300:]}
    # FETCH_HEAD is read once, right after this fetch; the target sha is used from here on.
    target = _git(controller, "rev-parse", "--verify", "FETCH_HEAD^{commit}").stdout.strip()
    head = _git(controller, "rev-parse", "HEAD").stdout.strip()
    if head == target and not switching:
        return {"state": "up_to_date", "head": head}
    found = {"head": head, "target": target}
    if switching:
        dropped = drops_commits(lambda *args: _git(controller, *args, check=False), head, target)
        if dropped is None:
            return {**found, "state": "skipped", "reason": f"cannot compare HEAD with {remote}/{branch}"}
        found["switch"] = {"from": current, "to": branch, "downgrade": dropped}
        if dropped and not policy.downgrade_confirmed(branch, head):
            return {**found, "state": "skipped",
                    "reason": f"{remote}/{branch} lacks commits of HEAD on {current}: switching would go back "
                              f"to older code; confirm with `channel set {branch} --allow-downgrade`, or keep "
                              f"{current} with `channel set {current}`"}
        tip = _git(controller, "rev-parse", "--verify", "--quiet", "refs/heads/" + branch, check=False).stdout.strip()
        if tip and _git(controller, "merge-base", "--is-ancestor", tip, target, check=False).returncode:
            return {**found, "state": "skipped", "reason": f"local branch {branch} has commits {remote}/{branch} lacks"}
    elif _git(controller, "merge-base", "--is-ancestor", head, target, check=False).returncode:
        return {**found, "state": "skipped", "reason": "not a fast-forward"}
    try:
        needed = environments.needed(controller.config["installation"],
                                     lambda *args, **kwargs: _git(controller, *args, **kwargs),
                                     head, target, controller.config.get("python"))
    except environments.Refused as refusal:
        return {**found, "state": "skipped", "reason": str(refusal)}
    if needed is not None:
        return {**found, "state": "available", "environment": needed.reason}
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
    try:
        lsof = executables.path("lsof")
    except FileNotFoundError:
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


RUN_LEASE_BUSY = {"state": "deferred", "reason": "service is busy: an autodev run holds its run lease"}


def _run_lease_refusal(controller):
    """Defer while an autodev run holds its lease in this state directory (`src.schedules.lease`)."""
    if run_lease.held(controller.directory):
        return dict(RUN_LEASE_BUSY)
    return None


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
    if health.get("inflight") or health.get("io_pending") or health.get("local_inference_pending") or \
            health.get("idle_seconds", 0) < settings(controller)["idle_seconds"]:
        return {"state": "deferred", "reason": "service is busy"}
    # The run lease needs no recheck: `_run` holds it for the whole transaction.
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
    if health.get("inflight") or health.get("io_pending") or health.get("local_inference_pending") or health.get("idle_seconds", 0) < idle:
        return _record(controller, {**found, "state": "deferred", "reason": "service is busy"})
    if refusal := _run_lease_refusal(controller):
        return _record(controller, {**found, **refusal})
    if refusal := _readers_refusal(controller, health.get("pid")):
        return _record(controller, {**found, **refusal})
    if not _enabled_on_disk(controller):
        return {"state": "disabled"}
    from .update import TargetMoved, offline_update
    try:
        with run_lease.hold(controller.directory):
            result = offline_update(controller, expected_target=found["target"], precheck=lambda: _precheck(controller))
    except run_lease.LeaseBusy:
        return _record(controller, {**found, **RUN_LEASE_BUSY})
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
                                **{key: result[key] for key in ("model", "model_from", "reason") if key in result}})
