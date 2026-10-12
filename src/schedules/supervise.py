"""Run supervision (design 10.1): a run in its own process group, kept awake, ended as a whole.

`run_supervised` starts the run with ``start_new_session`` (a new session and process group),
keeps the Mac awake with ``caffeinate -i -w <pid>`` for as long as the run lives, and on timeout
ends every descendant: SIGTERM to the group and to each descendant found by parent PID (which also
catches one that left the group with ``setsid``), then, after a grace period, SIGKILL to the group
and to every descendant seen so far or since. POSIX only; the scheduler is macOS's (#210 brings
the others).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Callable, Mapping, Sequence

CAFFEINATE = "/usr/bin/caffeinate"
PS = "/bin/ps"
GRACE_SECONDS = 10.0
POLL_SECONDS = 0.1


@dataclass
class Supervised:
    returncode: int | None
    timed_out: bool = False
    signalled: list[int] = field(default_factory=list)  # PIDs sent a signal on timeout
    caffeinated: bool = False


def caffeinate_argv(pid: int) -> list[str]:
    """Prevent idle sleep (``-i``) until process ``pid`` exits (``-w``)."""
    return [CAFFEINATE, "-i", "-w", str(pid)]


def process_table(ps: str = PS) -> list[tuple[int, int, int]]:
    """``(pid, ppid, pgid)`` of every process this user can see."""
    listed = subprocess.run([ps, "-A", "-o", "pid=,ppid=,pgid="], capture_output=True, text=True,
                            stdin=subprocess.DEVNULL, timeout=30, check=False).stdout
    rows = []
    for line in listed.splitlines():
        parts = line.split()
        if len(parts) == 3 and all(part.isdigit() for part in parts):
            rows.append((int(parts[0]), int(parts[1]), int(parts[2])))
    return rows


def descendants(root: int, table: Sequence[tuple[int, int, int]]) -> set[int]:
    """Every process below ``root`` by parent PID, plus every member of ``root``'s process group."""
    children: dict[int, list[int]] = {}
    for pid, ppid, _ in table:
        children.setdefault(ppid, []).append(pid)
    found, pending = set(), [root]
    while pending:
        for child in children.get(pending.pop(), []):
            if child not in found:
                found.add(child)
                pending.append(child)
    found |= {pid for pid, _, pgid in table if pgid == root}
    found.discard(root)
    found.discard(os.getpid())
    return found


def _signal(pids: set[int], group: int, number: int) -> None:
    try:
        os.killpg(group, number)
    except (ProcessLookupError, PermissionError):
        pass
    for pid in pids:
        try:
            os.kill(pid, number)
        except (ProcessLookupError, PermissionError):
            pass


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def terminate_tree(process: subprocess.Popen, *, grace: float = GRACE_SECONDS,
                   table: Callable[[], Sequence[tuple[int, int, int]]] = process_table) -> list[int]:
    """SIGTERM, then SIGKILL after ``grace`` seconds, to the run's group and all its descendants."""
    seen = descendants(process.pid, table()) | {process.pid}
    _signal(seen - {process.pid}, process.pid, signal.SIGTERM)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        process.poll()
        if process.returncode is not None and not any(_alive(pid) for pid in seen - {process.pid}):
            break
        time.sleep(POLL_SECONDS)
    seen |= descendants(process.pid, table())
    _signal(seen - {process.pid}, process.pid, signal.SIGKILL)
    process.wait()
    return sorted(seen)


def run_supervised(argv: Sequence[str], *, timeout: float, cwd: str | Path, env: Mapping[str, str],
                   stdout_path: str | Path, stderr_path: str | Path, caffeinate: bool = True,
                   grace: float = GRACE_SECONDS) -> Supervised:
    """Run ``argv`` to completion or until ``timeout`` seconds; never leaves a descendant behind on timeout."""
    if os.name != "posix":
        raise OSError("run supervision needs POSIX process groups")
    with open(stdout_path, "ab") as stdout, open(stderr_path, "ab") as stderr:
        process = subprocess.Popen(list(argv), cwd=str(cwd), env=dict(env), stdin=subprocess.DEVNULL,
                                   stdout=stdout, stderr=stderr, start_new_session=True)
    keeper = None
    if caffeinate and sys.platform == "darwin" and os.path.exists(CAFFEINATE):
        try:
            keeper = subprocess.Popen(caffeinate_argv(process.pid), stdin=subprocess.DEVNULL,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        except OSError:
            keeper = None
    result = Supervised(None, caffeinated=keeper is not None)
    try:
        result.returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        result.timed_out = True
        result.signalled = terminate_tree(process, grace=grace)
        result.returncode = process.returncode
    except BaseException:  # the supervisor itself is stopped: the run does not outlive it
        terminate_tree(process, grace=grace)
        raise
    finally:
        if keeper is not None:
            try:
                keeper.wait(timeout=5)  # caffeinate -w exits with the run
            except subprocess.TimeoutExpired:
                keeper.kill()
                keeper.wait()
    return result
