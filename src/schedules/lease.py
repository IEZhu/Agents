"""The run lease: a lock file a running autodev run holds from start to finish.

The daemon's auto-update (`src.daemon.autoupdate`) takes the same lease for its whole
transaction, so an update and a run exclude each other whichever starts first: an update never
replaces the runner's code under a run, and a run never starts during an update. A run also
rechecks the update markers once it holds the lease (`src.schedules.runner`). The scheduler's tick
starts no second run while the lease is held.

The lease is an OS lock (`src.file_lock`): it ends with the process that holds it, even when the
process is killed, and descriptors opened by Python are not inherited by child processes.
`held` probes it by taking it for an instant, so `hold` retries for ``HOLD_WAIT`` seconds before
it reports the lease busy: a probe never makes a run or an update fail.
"""
from __future__ import annotations

from contextlib import ExitStack, contextmanager
from pathlib import Path
import time
from typing import Iterator

from src.daemon.state import private_dir, state_dir
from src.file_lock import file_lock

SCHEDULES = "schedules"
LEASE_NAME = "run.lease"
HOLD_WAIT = 1.0  # seconds; far longer than a probe by `held`
RETRY_SECONDS = 0.05


class LeaseBusy(RuntimeError):
    """Another run holds the run lease."""


def schedules_dir(directory: str | Path | None = None) -> Path:
    """``<state_dir>/schedules``; ``directory`` is the service state directory (default `state_dir`)."""
    return Path(directory or state_dir()) / SCHEDULES


def lease_path(directory: str | Path | None = None) -> Path:
    return schedules_dir(directory) / LEASE_NAME


@contextmanager
def hold(directory: str | Path | None = None, *, wait: float = HOLD_WAIT) -> Iterator[Path]:
    """Hold the run lease for the body; ``LeaseBusy`` when another holder keeps it for ``wait`` seconds."""
    path = lease_path(directory)
    private_dir(path.parent)
    deadline = time.monotonic() + wait
    while True:
        stack = ExitStack()
        try:
            stack.enter_context(file_lock(path, blocking=False))
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                raise LeaseBusy("an autodev run or a daemon update holds the run lease") from None
        time.sleep(RETRY_SECONDS)
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
