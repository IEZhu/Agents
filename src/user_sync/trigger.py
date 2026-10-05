"""A debounced sync after library writes in a stdio server (#168).

A stdio MCP server has no sync loop of its own; the OS scheduler starts a run only
every few minutes (`src.user_sync.schedule`). So that a save reaches the remote
within seconds while the session is open, `start` listens to the library's change
notifications (`src.user_library.subscribe`) and runs one cycle in a background
daemon thread ``delay`` seconds after the first change of a burst:

* changes during the wait join the pending run and do not extend the wait, so a
  save waits at most ``delay`` plus a cycle that is already running;
* a change during a running cycle schedules exactly one more run when it ends,
  ``delay`` after that change or at once if the cycle took longer;
* ``is_ready()`` (sync is set up and the sync lock is free) is asked right before
  each run, and a false answer skips the run: sync is off, or another runner is
  syncing.

The listener only arms a timer, so it never blocks the write, nor the MCP request
behind the write. A failing run is logged, never raised into the writer. A process
that exits during a cycle leaves nothing that blocks the next run; the engine
clears a stale ``index.lock`` (#165).
"""
from __future__ import annotations

import functools
import logging
from pathlib import Path
import threading
import time
from typing import Callable, Protocol

from src import user_library

DELAY = 10.0  # seconds from the first change of a burst to its run

logger = logging.getLogger(__name__)


class Timer(Protocol):
    def start(self) -> None: ...
    def cancel(self) -> None: ...


def daemon_timer(wait: float, function: Callable[[], None]) -> threading.Timer:
    """A ``threading.Timer`` in a daemon thread: a pending or running cycle never holds up the server's exit."""
    timer = threading.Timer(wait, function)
    timer.daemon = True
    timer.name = "user-sync-trigger"
    return timer


class Trigger:
    """Runs a sync cycle after changes in one library, as the module describes; made by `start`."""

    def __init__(self, run_cycle: Callable[[], object], *, library_root: str | Path,
                 is_ready: Callable[[], bool], delay: float = DELAY,
                 timer: Callable[[float, Callable[[], None]], Timer] = daemon_timer,
                 clock: Callable[[], float] = time.monotonic):
        if delay < 0:
            raise ValueError("delay must not be negative")
        self.root = Path(library_root).expanduser().resolve()  # `user_library.notify` passes it resolved
        self._run_cycle, self._is_ready, self._delay = run_cycle, is_ready, delay
        self._make_timer, self._clock = timer, clock
        self._guard = threading.Lock()
        self._pending: Timer | None = None  # the armed timer of the next run
        self._generation = 0                # tells the armed timer from cancelled ones
        self._running = False
        self._again_at: float | None = None  # due time of the run after the running cycle
        self._stopped = False
        self._unsubscribe: Callable[[], None] = lambda: None

    def _changed(self, root: Path, paths: tuple[str, ...]) -> None:
        if root != self.root:
            return
        with self._guard:
            if self._stopped or self._pending is not None:
                return  # the pending run includes this change; its wait is not extended
            if self._running:
                if self._again_at is None:
                    self._again_at = self._clock() + self._delay
                return
            timer = self._arm(self._delay)
        self._start(timer)

    def _arm(self, wait: float) -> Timer:
        """The timer of the next run. Called under the guard; `_start` it after releasing the guard."""
        self._generation += 1
        self._pending = self._make_timer(wait, functools.partial(self._fire, self._generation))
        return self._pending

    def _start(self, timer: Timer) -> None:
        try:
            timer.start()
        except Exception:  # no thread to be had: forget the run, so that the next change arms one again
            with self._guard:
                if self._pending is timer:
                    self._pending = None
            logger.exception("Could not start the sync run after a library change")

    def _fire(self, generation: int) -> None:
        with self._guard:
            if self._stopped or generation != self._generation or self._pending is None:
                return
            self._pending, self._running = None, True
        try:
            if self._is_ready():
                self._run_cycle()
            else:
                logger.debug("Sync is not ready; the run after a library change is skipped")
        except Exception:
            logger.exception("The sync run after a library change failed")
        finally:
            follow_up = None
            with self._guard:
                self._running = False
                due, self._again_at = self._again_at, None
                if due is not None and not self._stopped:
                    follow_up = self._arm(max(0.0, due - self._clock()))
            if follow_up is not None:
                self._start(follow_up)

    def stop(self) -> None:
        """Stop listening and cancel a pending run. A running cycle finishes, with no run after it."""
        self._unsubscribe()
        with self._guard:
            self._stopped, self._again_at = True, None
            pending, self._pending = self._pending, None
        if pending is not None:
            pending.cancel()


def start(run_cycle: Callable[[], object], *, library_root: str | Path, is_ready: Callable[[], bool],
          delay: float = DELAY, timer: Callable[[float, Callable[[], None]], Timer] = daemon_timer,
          clock: Callable[[], float] = time.monotonic) -> Trigger:
    """Run ``run_cycle()`` after changes in the library at ``library_root``; ``stop()`` the result to end it.

    ``timer(wait, function)`` returns an unstarted timer with ``start`` and
    ``cancel``, like ``threading.Timer``; ``clock`` is monotonic seconds. Tests
    replace both.
    """
    trigger = Trigger(run_cycle, library_root=library_root, is_ready=is_ready, delay=delay,
                      timer=timer, clock=clock)
    trigger._unsubscribe = user_library.subscribe(trigger._changed)
    return trigger
