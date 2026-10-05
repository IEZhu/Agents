"""User library sync in the macOS daemon (#167): the sync task and ``/admin/user-sync/*``.

The engine (``src.user_sync``) syncs the personal flow library through one private git
repository. In the daemon one task drives it:

* A write in this process (web UI, MCP tools) reaches the change hook of ``src.user_library``.
  The first change of a burst schedules a cycle ``debounce`` seconds later; later changes do not
  move it, and a change during a cycle schedules one more.
* Every ``scan_every`` seconds a scan looks for writes by other processes (stdio servers, manual
  edits). It takes a stat-only fingerprint of the library and asks the engine's ``status()``,
  which reads and hashes every file, only when the fingerprint changed since the library was last
  seen in sync. The engine's scope rules decide what counts as a change.
* Every ``fetch_minutes`` (a setting, 1 to 60, default 5) a cycle fetches. After a network
  failure the next cycle waits for the engine's ``retry_at``.
* "Sync now" (``/admin/user-sync/run``) runs a forced cycle.

Nothing automatic runs before the service is ready, while sync is off, waiting for access or
paused, or while an update transaction (``maintenance.json`` or ``transaction.json``) runs.
Cycles and the admin operations that change the repository run one at a time, in the task.

Engine calls run in daemon threads and count in ``io_pending``, so ``auto-update`` and a drain
wait for them. They do not use the service's executor: its shutdown and the interpreter's exit
both wait for executor threads, and a cycle can wait up to a minute for each network call. When
the service drains, one last cycle commits and pushes what is left, within ``drain_budget``
seconds of the last admitted request. A call still running then is abandoned: it no longer counts
in ``io_pending`` and the task ends without it; the engine's sync lock and its stale-lock cleanup
make a killed cycle harmless.

This module imports nothing outside the standard library at load time, so the controller's
``user-sync`` command can use its helpers (``operation``, ``direct``) without the service.
"""
from __future__ import annotations

import asyncio
from collections import deque
from datetime import datetime, timezone
import hashlib
import json
import logging
import os
from pathlib import Path
import stat
import threading
import time

from src import user_library

logger = logging.getLogger(__name__)

DEBOUNCE_SECONDS = 10
SCAN_SECONDS = 30
DRAIN_SECONDS = 10
RECHECK_SECONDS = 5           # how often a cycle held back by an update transaction looks again
DEFAULT_FETCH_MINUTES = 5
FETCH_MINUTES = (1, 60)
POLL_SECONDS = .1             # how often a wait for an engine call checks for a drain
MAX_BODY = 64 * 1024
UPDATE_FILES = ("maintenance.json", "transaction.json")
COMMANDS = ("status", "run", "setup", "check", "preview", "start", "pause", "resume", "disconnect")
# Run by the task, one at a time with the cycles; the rest answer at once.
QUEUED = frozenset({"run", "setup", "check", "preview", "start", "disconnect"})
CYCLES = frozenset({"run", "start"})
# States in which a changed library is worth a cycle.
UNSYNCED = ("pending", "attention")
# Seconds the command line waits for the service. Network commands can take minutes when a remote
# hangs: each git network call may take 60 s, and a push is tried three times.
REQUEST_TIMEOUTS = {"status": 30, "pause": 30, "resume": 30}
LONG_REQUEST_TIMEOUT = 900


class Abandoned(Exception):
    """The drain deadline passed before the engine call ended; it keeps running in its thread."""


class Draining(Exception):
    """The service drains; the operation did not start."""


# --- commands, shared with the controller ----------------------------------------------------

_TEXT = (str,)
_OPTIONAL_TEXT = (str, type(None))
_OPTIONAL_FLAG = (bool, type(None))
_ARGUMENTS = {
    "run": {"confirm": _OPTIONAL_TEXT},
    "start": {"confirm": _TEXT},
    "setup": {"remote": _TEXT, "name": _TEXT, "email": _TEXT, "label": _OPTIONAL_TEXT,
              "branch": _OPTIONAL_TEXT, "ask_new_repositories": _OPTIONAL_FLAG,
              "trust_host_key": _OPTIONAL_TEXT, "confirm_private": _OPTIONAL_FLAG},
}
_REQUIRED = {"start": ("confirm",), "setup": ("remote", "name", "email")}


def operation(command: str, arguments):
    """``call(syncer)`` for one user-sync command; ValueError for an unknown command or bad arguments.

    ``run`` is "Sync now": it ignores the retry delay after a network failure. ``setup`` takes the
    engine's setup arguments; the engine validates their values.
    """
    if command not in COMMANDS:
        raise ValueError(f"unknown command {command!r}")
    if not isinstance(arguments, dict):
        raise ValueError("the arguments must be a JSON object")
    allowed = _ARGUMENTS.get(command, {})
    unknown = sorted(set(arguments) - set(allowed))
    if unknown:
        raise ValueError(f"{command} does not take {', '.join(map(str, unknown))}")
    for key, value in arguments.items():
        if not isinstance(value, allowed[key]):
            raise ValueError(f"{key} must be {' or '.join(kind.__name__ for kind in allowed[key])}")
    missing = [key for key in _REQUIRED.get(command, ()) if not arguments.get(key)]
    if missing:
        raise ValueError(f"{command} needs {', '.join(missing)}")
    given = {key: value for key, value in arguments.items() if value is not None}
    if command == "run":
        return lambda syncer: syncer.run(force=True, confirm=given.get("confirm"))
    if command == "start":
        return lambda syncer: syncer.start(given["confirm"])
    if command == "setup":
        return lambda syncer: syncer.setup(**given)
    return lambda syncer: getattr(syncer, command)()


def failure(error: BaseException) -> tuple[int, dict]:
    """``(HTTP status, JSON)`` for an engine failure; never a traceback."""
    try:
        from src.user_sync.engine import SyncError
        from src.user_sync.gitcmd import GitError
        from src.user_sync.keys import SSHKeyError
    except Exception:  # the engine itself cannot load; report the original error
        SyncError = GitError = SSHKeyError = ()
    if isinstance(error, SyncError):
        return 409, {"status": error.state, "reason": error.reason, "message": error.message}
    if isinstance(error, GitError):
        return 409, {"status": "attention", "reason": "git_error", "message": str(error)}
    if isinstance(error, SSHKeyError):
        return 409, {"status": "attention", "reason": "ssh_key", "message": str(error)}
    logger.error("User sync operation failed", exc_info=error)
    # An OSError's text names the file, and a file of the private state may be the key.
    detail = error.strerror if isinstance(error, OSError) and error.strerror else str(error)
    return 500, {"status": "error", "reason": "internal", "message": f"{type(error).__name__}: {detail}"[:300]}


def summary_of(status: dict) -> dict:
    """The short status for /health and the controller: no key, remote, identity or path."""
    conflicts = status.get("conflicts", 0)
    return {"state": status.get("state") or status.get("status"), "reason": status.get("reason"),
            "last_success": status.get("last_success"),
            "conflicts": len(conflicts) if isinstance(conflicts, list) else conflicts or 0,
            "pending": status.get("pending") or 0}


def service_library(installation) -> Path | None:
    """The library the service and stdio servers use: ``AGENTS_USER_FLOWS_DIR`` or the ``.env``.

    None means the engine's default, ``flows/.user`` of the installation.
    """
    configured = os.environ.get("AGENTS_USER_FLOWS_DIR")
    if not configured and installation:
        try:
            from dotenv import dotenv_values
            configured = dotenv_values(Path(installation) / ".env").get("AGENTS_USER_FLOWS_DIR")
        except Exception:  # an unreadable .env leaves the default
            configured = None
    return Path(configured).expanduser() if configured else None


def direct(directory, installation, command: str, arguments: dict) -> tuple[int, dict]:
    """One user-sync command with the engine in this process, for when the service is down.

    The state directory is the service's (``<directory>/user-sync``), as the service uses it. The
    engine's sync lock keeps this run from overlapping a running service: one gets ``lock_held``.
    """
    try:
        call = operation(command, arguments)
    except ValueError as error:
        return 400, {"error": "invalid_request", "message": str(error)}
    try:
        from src.user_sync.engine import Syncer
        syncer = Syncer(library=service_library(installation), state_dir=Path(directory) / "user-sync")
        return 200, call(syncer)
    except Exception as error:
        return failure(error)


def direct_summary(directory, installation) -> dict:
    """The short status of a stopped service's sync, read with the engine in this process."""
    code, status = direct(directory, installation, "status", {})
    if code != 200:
        return {"state": "unknown", "reason": status.get("reason"), "last_success": None,
                "conflicts": 0, "pending": 0}
    return summary_of(status)


# --- helpers ----------------------------------------------------------------------------------


def library_fingerprint(root) -> str:
    """A digest of every path's type, size, modification time and inode below ``root``, ``.git`` aside.

    It reads no file content. A file's bytes rarely change without these, and the engine's
    ``status()``, which reads every file, decides what the change means.
    """
    root = Path(root)
    digest = hashlib.blake2b(digest_size=16)
    stack = [(root, True)]
    while stack:
        directory, top = stack.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda entry: entry.name)
        except OSError:
            digest.update(b"?" + os.fsencode(directory) + b"\n")
            continue
        for entry in entries:
            if top and entry.name == ".git":
                continue
            try:
                info = entry.stat(follow_symlinks=False)
            except OSError:
                continue
            digest.update(b"%s\0%o\0%d\0%d\0%d\n" % (os.fsencode(entry.path), info.st_mode, info.st_size,
                                                     info.st_mtime_ns, info.st_ino))
            if stat.S_ISDIR(info.st_mode):
                stack.append((Path(entry.path), False))
    return digest.hexdigest()


def _active(settings) -> bool:
    """Set up, started and not paused: the only state in which anything runs by itself."""
    return settings is not None and bool(getattr(settings, "started", None)) \
        and not getattr(settings, "paused", False)


def _monotonic_at(text) -> float | None:
    """The monotonic time of an ISO time (the engine's ``retry_at``), one second late.

    A time that passed maps to the past, not to now: every scan recomputes it from the same
    status, and a clamp would move it forward on each one.
    """
    try:
        moment = datetime.fromisoformat(text)
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return time.monotonic() + (moment - datetime.now(timezone.utc)).total_seconds() + 1.0


def _seen(future) -> None:
    """Mark an outcome as retrieved: one that nobody waits for any more is not an error to log."""
    if not future.cancelled():
        future.exception()


# --- the task ---------------------------------------------------------------------------------


class UserSync:
    """The daemon's sync task and the operations behind ``/admin/user-sync/*``.

    Everything except the engine calls runs on the service's event loop, so the scheduling state
    needs no lock. ``syncer`` replaces the engine in tests; by default the engine syncs the
    service's library with ``<state directory>/user-sync``. ``minute`` is the length of one
    ``fetch_minutes`` step in seconds.
    """

    def __init__(self, service, syncer=None, *, debounce=DEBOUNCE_SECONDS, scan_every=SCAN_SECONDS,
                 drain_budget=DRAIN_SECONDS, recheck=RECHECK_SECONDS, minute=60.0):
        self._syncer_injected = syncer is not None
        self.service = service
        self._syncer = syncer
        self._syncer_guard = threading.Lock()
        self.debounce, self.scan_every, self.drain_budget = debounce, scan_every, drain_budget
        self.recheck, self.minute = recheck, minute
        self.wake_event = asyncio.Event()
        self.jobs = deque()            # (command, call, future) waiting for the task
        self.calls = set()             # engine calls running in threads; part of io_pending
        self.task = None
        self.loop = None
        self.library = None
        self.unsubscribe = None
        self.armed = False             # automatic sync began: the service was ready
        self.active = True             # set up, started and not paused, as last seen
        self.due = None                # a heard or found change wants a cycle at this time
        self.retry_due = None          # the engine accepts a cycle again after a network failure
        self.next_fetch = self.next_scan = 0.0
        self.hold_until = None         # an update transaction runs; look again at this time
        self.clean = None              # the library's fingerprint when it was last seen in sync
        self.syncing = False
        self.last_status = None
        self.last_cycle = None
        self.drain_requested = False
        self.flush_wanted = False      # the drain's last cycle did not end yet
        self.drain_deadline = None

    # --- the service's view (event loop) ---------------------------------------------

    def start(self):
        """Create the task with the service; it does nothing automatic until the service is ready."""
        self.task = asyncio.create_task(self.run(), name="agents-user-sync")
        return self.task

    def wake(self) -> None:
        self.wake_event.set()

    def drain(self) -> None:
        """The service drains (``/admin/drain`` or shutdown): one last cycle, counted in io_pending."""
        if self.drain_requested:
            return
        self.drain_requested = True
        self.drain_deadline = None
        self.flush_wanted = self.armed and self.task is not None and not self.task.done()
        self.wake()

    def resume(self) -> None:
        """The drain ended without a stop (``/admin/resume``); a later drain flushes again."""
        self.drain_requested = False
        self.drain_deadline = None
        self.flush_wanted = False
        self.wake()

    @property
    def pending(self) -> int:
        """Engine calls and the drain's last cycle, for io_pending; abandoned calls hold no drain."""
        if self.drain_deadline is not None and time.monotonic() >= self.drain_deadline:
            return 0
        return len(self.calls) + (1 if self.flush_wanted else 0)

    def summary(self) -> dict | None:
        """The short status for /health, from the last status the task read; None before that."""
        if self.syncing:
            short = summary_of(self.last_status or {})
            short.update(state="syncing", reason=None)
            return short
        return None if self.last_status is None else summary_of(self.last_status)

    def loop_info(self) -> dict:
        now = time.monotonic()
        running = self.armed and self.active
        return {"state": "draining" if self.service.state == "draining"
                else "running" if self.armed else "waiting_for_ready",
                "active": running, "syncing": self.syncing,
                "next_fetch_seconds": round(max(0.0, self.next_fetch - now), 1) if running else None,
                "last_cycle": self.last_cycle}

    async def finish(self) -> None:
        """After stop: the task ends by the drain deadline at the latest, else it is cancelled."""
        task = self.task
        if task is None:
            return
        self.wake()
        done, _ = await asyncio.wait({task}, timeout=self.drain_budget + 5)
        if not done:
            logger.warning("User sync task did not end after the drain; cancelling it")
            task.cancel()
        outcome = (await asyncio.gather(task, return_exceptions=True))[0]
        if isinstance(outcome, Exception):
            logger.error("User sync task failed", exc_info=outcome)

    # --- admin endpoints ---------------------------------------------------------------

    async def handle(self, request, command: str):
        """``/admin/user-sync/<command>``; the service checked the bearer token already."""
        from starlette.responses import JSONResponse

        def reply(value, code=200, **headers):
            return JSONResponse(value, code, headers={"Cache-Control": "no-store", **headers})

        if command not in COMMANDS:
            return reply({"error": "not_found"}, 404)
        method = "GET" if command == "status" else "POST"
        if request.method != method:
            return reply({"error": "method_not_allowed"}, 405, Allow=method)
        arguments = {}
        if method == "POST":
            raw = b""
            async for chunk in request.stream():
                raw += chunk
                if len(raw) > MAX_BODY:
                    return reply({"error": "invalid_request", "message": "the request is too large"}, 413)
            try:
                arguments = json.loads(raw) if raw.strip() else {}
            except ValueError:
                return reply({"error": "invalid_request", "message": "the body must be JSON"}, 400)
        try:
            call = operation(command, arguments)
        except ValueError as error:
            return reply({"error": "invalid_request", "message": str(error)}, 400)
        draining = {"error": "draining", "message": "the service is stopping; try again when it is back"}
        try:
            if command == "status":
                status = await self._engine(self._status_work)
                self.last_status = status
                return reply({**status, "loop": self.loop_info()})
            if command in QUEUED:
                if self.service.state == "draining":
                    return reply(draining, 503)
                if self.task is None or self.task.done():
                    return reply({"error": "unavailable", "message": "the sync task is not running"}, 503)
                # A client that goes away does not cancel a submitted operation.
                outcome = await asyncio.shield(self.submit(command, call))
            else:  # pause and resume only write the settings: they never wait for a cycle
                outcome = await self._engine(self._job_work, call, False)
                self._note_job(command, outcome)
                self.wake()
        except (Abandoned, Draining):
            return reply(draining, 503)
        except Exception as error:
            code, value = failure(error)
            return reply(value, code)
        return reply(outcome["result"], outcome["code"])

    def submit(self, command: str, call):
        """Queue an operation for the task; the future resolves to ``{"code", "result", ...}``."""
        future = asyncio.get_running_loop().create_future()
        future.add_done_callback(_seen)
        self.jobs.append((command, call, future))
        self.wake()
        return future

    # --- the loop ----------------------------------------------------------------------

    async def run(self) -> None:
        self.loop = asyncio.get_running_loop()
        try:
            while not self.service.stop.is_set():
                self.wake_event.clear()
                until = None
                try:
                    if self.service.state == "draining":
                        await self._drain()
                        continue
                    if self.jobs:
                        await self._run_job(*self.jobs.popleft())
                        continue
                    until = await self._automatic()
                except Abandoned:
                    continue  # the drain deadline passed; _drain decides what follows
                except Exception:
                    logger.exception("User sync loop failed; it tries again")
                    until = time.monotonic() + self.scan_every
                await self._sleep(until)
        finally:
            if self.unsubscribe is not None:
                self.unsubscribe()
                self.unsubscribe = None
            self._reject_queued()

    async def _automatic(self) -> float | None:
        """Run what is due; returns when to look again (None: when woken)."""
        if self.service.state != "ready":
            return None
        if not self.armed:
            await self._arm()
        now = time.monotonic()
        kind = self._due_kind(now)
        if kind is None:
            return self._next_time()
        if kind == "scan":
            self._note_scan(await self._engine(self._scan_work, self.clean, True))
        else:
            await self._cycle(kind)
        return time.monotonic()

    async def _arm(self) -> None:
        syncer = await self._engine(self._engine_syncer)
        if self._syncer_injected is False:  # the real engine: a scheduled job would compete with the loop
            from .control import stop_scheduled_sync
            await self._engine(stop_scheduled_sync)
        self.library = Path(syncer.library)
        self.unsubscribe = user_library.subscribe(self._heard)
        self.armed = True
        self.next_fetch = self.next_scan = time.monotonic()  # catch up with the remote at once

    def _due_kind(self, now: float) -> str | None:
        if self.hold_until is not None and now < self.hold_until:
            return None
        if self.active:
            if self.due is not None and now >= self.due:
                return "change"
            if self.retry_due is not None and now >= self.retry_due:
                return "retry"
            if now >= self.next_fetch:
                return "fetch"
        return "scan" if now >= self.next_scan else None

    def _next_time(self) -> float:
        times = [self.next_scan]
        if self.active:
            times += [moment for moment in (self.due, self.retry_due, self.next_fetch) if moment is not None]
        until = min(times)
        return until if self.hold_until is None else max(until, self.hold_until)

    async def _cycle(self, trigger: str, *, barrier: bool = True) -> None:
        self.due = None  # the cycle reads the library after every change heard so far
        self.syncing = True
        try:
            probe = await self._engine(self._cycle_work, barrier)
        finally:
            self.syncing = False
        self._note_cycle(probe, trigger)

    async def _run_job(self, command: str, call, future) -> None:
        if command in CYCLES:
            self.due = None
            self.syncing = True
        try:
            outcome = await self._engine(self._job_work, call, True)
        except Abandoned:
            if not future.done():
                future.set_exception(Abandoned())
            raise
        except Exception as error:  # a thread could not start
            code, result = failure(error)
            outcome = {"code": code, "result": result}
        finally:
            self.syncing = False
        try:
            self._note_job(command, outcome)
        finally:  # the request waits for this future
            if not future.done():
                future.set_result(outcome)

    async def _drain(self) -> None:
        """One last cycle when something is left to push, then wait for a resume or the stop."""
        self._reject_queued()
        try:
            # Requests admitted before the drain finish first: their saves belong in the last cycle.
            while self.service.state == "draining" and self._deadline() is None:
                await asyncio.sleep(.05)
            deadline = self._deadline()
            if self.flush_wanted and deadline is not None and time.monotonic() < deadline \
                    and await self._final_wanted():
                # The last cycle runs during an update transaction as well: the service stops next.
                await self._cycle("drain", barrier=False)
        except Abandoned:
            pass
        finally:
            self.flush_wanted = False
        while self.service.state == "draining" and not self.service.stop.is_set():
            self.wake_event.clear()
            await self._sleep(None)
        if not self.service.stop.is_set():  # resumed, or the warmup ended a drain it overlapped
            self.drain_requested = False
            self.drain_deadline = None

    async def _final_wanted(self) -> bool:
        """Whether the last cycle has work: a change heard, or one the scan finds."""
        if self.due is not None:
            return True
        probe = await self._engine(self._scan_work, self.clean, False)
        self._note_scan(probe)
        status = probe.get("status")
        return bool(probe.get("active")) and status is not None and status.get("state") in UNSYNCED

    def _deadline(self) -> float | None:
        """When a drain abandons engine calls: ``drain_budget`` after the last admitted request."""
        if self.service.state != "draining":
            return None
        if self.drain_deadline is None:
            io = self.service.io.inflight if self.service.io is not None else 0
            if self.service.stop.is_set() or not (self.service.inflight or io):
                self.drain_deadline = time.monotonic() + self.drain_budget
        return self.drain_deadline

    async def _sleep(self, until: float | None) -> None:
        timeout = self.scan_every if until is None else min(self.scan_every, until - time.monotonic())
        if timeout <= 0 or self.wake_event.is_set():
            return
        try:
            await asyncio.wait_for(self.wake_event.wait(), timeout)
        except asyncio.TimeoutError:
            pass

    def _reject_queued(self) -> None:
        while self.jobs:
            _, _, future = self.jobs.popleft()
            if not future.done():
                future.set_exception(Draining())

    # --- changes heard in this process -------------------------------------------------

    def _heard(self, root, paths) -> None:
        """The library's change hook; called in the writer's thread."""
        loop = self.loop
        if loop is None or Path(root) != self.library:
            return
        try:
            loop.call_soon_threadsafe(self._changed)
        except RuntimeError:  # the event loop is closed: the service is gone
            pass

    def _changed(self) -> None:
        if self.due is None:  # the first change of a burst decides when the cycle runs
            self.due = time.monotonic() + self.debounce
        self.wake()

    # --- engine calls --------------------------------------------------------------------

    async def _engine(self, function, *args):
        """``function(*args)`` in a daemon thread, counted in io_pending until the thread ends.

        Raises ``Abandoned`` once a drain's deadline passes, or ``drain_budget`` after the call
        started when it started later; the call goes on in its thread, and cancelling the waiter
        does not stop it either.
        """
        loop = asyncio.get_running_loop()
        future = loop.create_future()
        future.add_done_callback(_seen)
        token = object()

        def settle(ok, value):
            self.calls.discard(token)
            if not future.done():
                (future.set_result if ok else future.set_exception)(value)

        def work():
            try:
                value, ok = function(*args), True
            except BaseException as error:  # handed to the waiting coroutine
                value, ok = error, False
            try:
                loop.call_soon_threadsafe(settle, ok, value)
            except RuntimeError:  # the event loop is closed: the service is gone
                pass

        self.calls.add(token)
        try:
            threading.Thread(target=work, name="agents-user-sync", daemon=True).start()
        except BaseException:
            self.calls.discard(token)
            raise
        started = time.monotonic()
        while not future.done():
            deadline = self._deadline()
            if deadline is not None:
                # A call made after the deadline (a status during the stop) has a budget of its own.
                limit = deadline if started < deadline else started + self.drain_budget
                if time.monotonic() >= limit:
                    raise Abandoned()
            await asyncio.wait({future}, timeout=POLL_SECONDS)
        return future.result()

    def _engine_syncer(self):
        with self._syncer_guard:
            if self._syncer is None:
                from src.user_sync.engine import Syncer
                self._syncer = Syncer(state_dir=self.service.directory / "user-sync")
            return self._syncer

    def _in_update(self) -> bool:
        return any((self.service.directory / name).exists() for name in UPDATE_FILES)

    @staticmethod
    def _observe(syncer) -> tuple[str, dict]:
        """The fingerprint first: a write after it shows in this status or changes the next one."""
        fingerprint = library_fingerprint(syncer.library)
        return fingerprint, syncer.status()

    def _status_work(self) -> dict:
        return self._engine_syncer().status()

    def _scan_work(self, clean: str | None, barrier: bool) -> dict:
        syncer = self._engine_syncer()
        settings = syncer.settings()
        if not _active(settings):
            return {"active": False, "settings": settings, "status": syncer.status()}
        if barrier and self._in_update():
            return {"active": True, "blocked": True, "settings": settings}
        fingerprint = library_fingerprint(syncer.library)
        status = None if fingerprint == clean else syncer.status()
        return {"active": True, "settings": settings, "fingerprint": fingerprint, "status": status}

    def _cycle_work(self, barrier: bool) -> dict:
        syncer = self._engine_syncer()
        settings = syncer.settings()
        if not _active(settings):
            return {"active": False, "settings": settings, "status": syncer.status()}
        if barrier and self._in_update():
            return {"active": True, "blocked": True, "settings": settings}
        try:
            result = syncer.run()
        except Exception as error:  # the engine reports expected failures in its result
            result = failure(error)[1]
        fingerprint, status = self._observe(syncer)
        return {"active": True, "settings": settings, "result": result, "fingerprint": fingerprint,
                "status": status}

    def _job_work(self, call, observe: bool) -> dict:
        syncer = self._engine_syncer()
        try:
            code, result = 200, call(syncer)
        except Exception as error:
            code, result = failure(error)
        outcome = {"code": code, "result": result, "settings": None, "fingerprint": None, "status": None}
        try:
            outcome["settings"] = syncer.settings()
            if observe:
                outcome["fingerprint"], outcome["status"] = self._observe(syncer)
            elif code == 200 and isinstance(result, dict) and "state" in result:
                outcome["status"] = result  # pause and resume answer with the status
        except Exception:
            logger.warning("User sync status unavailable after an operation", exc_info=True)
        return outcome

    # --- bookkeeping (event loop) --------------------------------------------------------

    def _remember(self, status, fingerprint=None) -> None:
        """Keep the engine's status; a library seen in sync needs no status until it changes."""
        if not isinstance(status, dict):
            return
        self.last_status = status
        state = status.get("state")
        self.retry_due = _monotonic_at(status.get("retry_at")) if state == "offline" else None
        # Attention without a change of the library needs no cycle on every scan; offline waits
        # for retry_at, and "syncing" means another runner has the library.
        self.clean = None if fingerprint is None or state in ("pending", "syncing", "offline") else fingerprint

    def _inactive(self, probe: dict) -> None:
        self.active = False
        self.due = self.retry_due = self.clean = None
        if isinstance(probe.get("status"), dict):
            self.last_status = probe["status"]

    def _note_scan(self, probe: dict) -> None:
        now = time.monotonic()
        self.next_scan = now + self.scan_every
        if probe.get("blocked"):
            self.hold_until = now + self.recheck
            return
        self.hold_until = None
        if not probe.get("active"):
            self._inactive(probe)
            return
        if not self.active:  # set up, started or resumed elsewhere: catch up at once
            self.active = True
            self.next_fetch = now
        status = probe.get("status")
        if status is None:
            return  # unchanged since the library was last seen in sync
        self._remember(status, probe.get("fingerprint"))
        if status.get("state") in UNSYNCED and self.due is None:
            self.due = now

    def _note_cycle(self, probe: dict, trigger: str) -> None:
        now = time.monotonic()
        if probe.get("blocked"):
            self.hold_until = now + self.recheck
            if trigger == "change" and self.due is None:
                self.due = now
            return
        self.hold_until = None
        self.next_scan = now + self.scan_every
        if not probe.get("active"):
            self._inactive(probe)
            return
        self.active = True
        self.next_fetch = now + self._fetch_seconds(probe.get("settings"))
        result = probe.get("result") or {}
        self._record(trigger, result)
        self._remember(probe.get("status"), probe.get("fingerprint"))
        if result.get("status") == "lock_held" and self.due is None:
            self.due = now + self.debounce  # another runner has the library; look again soon

    def _note_job(self, command: str, outcome: dict) -> None:
        now = time.monotonic()
        settings = outcome.get("settings")
        was_active, self.active = self.active, _active(settings)
        self.hold_until = None
        self.next_scan = now
        if command in CYCLES and outcome.get("code") == 200:
            self._record("manual" if command == "run" else "start", outcome.get("result") or {})
            self.next_fetch = now + self._fetch_seconds(settings)
        elif self.active and not was_active:
            self.next_fetch = now
        if command == "resume" and self.active:
            self.due = now  # catch up with what changed while paused
        if not self.active:
            self.due = self.retry_due = None
        self.clean = None
        self._remember(outcome.get("status"), outcome.get("fingerprint"))

    def _record(self, trigger: str, result: dict) -> None:
        entry = {"time": datetime.now(timezone.utc).isoformat(timespec="seconds"), "trigger": trigger,
                 "status": result.get("status"), "reason": result.get("reason"),
                 "sent": len(result.get("sent") or ()), "received": len(result.get("received") or ()),
                 "conflicts": len(result.get("conflicts") or ()), "pushed": bool(result.get("pushed"))}
        previous = self.last_cycle or {}
        if (previous.get("status"), previous.get("reason")) != (entry["status"], entry["reason"]) \
                or entry["sent"] or entry["received"] or entry["conflicts"]:
            logger.info("User sync (%s): %s%s, sent %d, received %d, conflicts %d", trigger, entry["status"],
                        f" ({entry['reason']})" if entry["reason"] else "", entry["sent"], entry["received"],
                        entry["conflicts"])
        self.last_cycle = entry

    def _fetch_seconds(self, settings) -> float:
        minutes = getattr(settings, "fetch_minutes", DEFAULT_FETCH_MINUTES)
        if isinstance(minutes, bool) or not isinstance(minutes, int):
            minutes = DEFAULT_FETCH_MINUTES
        return min(FETCH_MINUTES[1], max(FETCH_MINUTES[0], minutes)) * self.minute
