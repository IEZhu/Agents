"""The run lease: a lock file a running autodev run holds from start to finish.

The daemon's auto-update (`src.daemon.autoupdate`) defers while the lease is held, so an update
never replaces the runner's code under a run, and the scheduler's tick starts no second run.
The lease is an OS lock (`src.file_lock`): it ends with the process that holds it, even when the
process is killed, and descriptors opened by Python are not inherited by child processes.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from pathlib import Path
from typing import Iterator

from src.daemon.state import private_dir, state_dir
from src.file_lock import file_lock

SCHEDULES = "schedules"
LEASE_NAME = "run.lease"


class LeaseBusy(RuntimeError):
    """Another run holds the run lease."""


def schedules_dir(directory: str | Path | None = None) -> Path:
    """``<state_dir>/schedules``; ``directory`` is the service state directory (default `state_dir`)."""
    return Path(directory or state_dir()) / SCHEDULES


def lease_path(directory: str | Path | None = None) -> Path:
    return schedules_dir(directory) / LEASE_NAME


@contextmanager
def hold(directory: str | Path | None = None) -> Iterator[Path]:
    """Hold the run lease for the body; ``LeaseBusy`` at once when another process holds it."""
    path = lease_path(directory)
    private_dir(path.parent)
    stack = ExitStack()
    try:
        stack.enter_context(file_lock(path, blocking=False))
    except BlockingIOError:
        raise LeaseBusy("another autodev run holds the run lease") from None
    with stack:
        yield path


def held(directory: str | Path | None = None) -> bool:
    """Whether a run holds the lease now. Creates nothing when no run ever took it."""
    path = lease_path(directory)
    if not path.is_file():
        return False
    try:
        with file_lock(path, blocking=False):
            return False
    except BlockingIOError:
        return True
