"""Run supervision (design 10.1): a run in its own process group, kept awake, ended as a whole.

`run_supervised` starts the run with ``start_new_session`` (a new session and process group) and
a per-run marker in its environment (``AGENTS_SUPERVISED_RUN=<random>``), and keeps the Mac awake
with ``caffeinate -i -w <pid>`` for as long as the run lives. However the run ends (a normal exit,
a timeout, or SIGTERM, SIGHUP or SIGINT to the supervisor), the supervisor sweeps its tree:
SIGTERM, then, after a grace period, SIGKILL, to the run's process group and to every process of
the run. A process belongs to the run when

* it descends from the run by parent PID,
* it is in the run's process group,
* its environment carries the run's marker (``ps -E``), which also finds a descendant that left
  the group with ``setsid`` and lost its parent (a double fork), or
* a periodic scan while the run lived saw it in the run, and it is still the same process (the
  same start time).

The root process stays unreaped until the sweep ends (`os.waitid` with ``WNOWAIT``), so its PID
and its process group ID cannot be reused meanwhile, and every signal goes only to PIDs found in a
scan taken right before it, never to a PID remembered from an earlier scan without the same start
time. macOS hides the environment of its platform binaries (``/bin/*``) from ``ps -E``: such a
process that both leaves the group and loses its parent before a periodic scan sees it, or a
process that starts with an emptied environment, is not found. POSIX only; the scheduler is
macOS's (#210 brings the others).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Iterable, Mapping, NamedTuple, Sequence

CAFFEINATE = "/usr/bin/caffeinate"
PS = "/bin/ps"
MARKER = "AGENTS_SUPERVISED_RUN"
GRACE_SECONDS = 10.0
POLL_SECONDS = 0.1
GRACE_POLL_SECONDS = 0.25
RESCAN_SECONDS = 5.0
STOP_SIGNALS = (signal.SIGTERM, signal.SIGHUP, signal.SIGINT)


class Proc(NamedTuple):
    """One row of the process table."""
    pid: int
    ppid: int
    pgid: int
    started: str = ""  # ps's lstart: with the PID, the identity of a process
    zombie: bool = False
    markers: frozenset = frozenset()  # values of MARKER in its environment


class SupervisorStopped(SystemExit):
    """The supervisor received a stop signal; the run's tree was swept before this propagates."""

    def __init__(self, number: int):
        super().__init__(128 + number)
        self.signal = number


@dataclass
class Supervised:
    returncode: int | None
    timed_out: bool = False
    signalled: list[int] = field(default_factory=list)  # PIDs of the run sent a signal by the sweep
    caffeinated: bool = False


def caffeinate_argv(pid: int) -> list[str]:
    """Prevent idle sleep (``-i``) until process ``pid`` exits (``-w``)."""
    return [CAFFEINATE, "-i", "-w", str(pid)]


def _ps_argv(ps: str) -> list[str]:
    environment = ["-E"] if sys.platform == "darwin" else []
    return [ps, "-A", "-ww", *environment, "-o", "pid=,ppid=,pgid=,stat=,lstart=,command="]


def process_table(ps: str = PS) -> list[Proc]:
    """Every process this user can see; markers only where ``ps -E`` shows the environment."""
    try:
        listed = subprocess.run(_ps_argv(ps), capture_output=True, text=True, errors="replace",
                                stdin=subprocess.DEVNULL, timeout=30, check=False,
                                env={"LC_ALL": "C", "PATH": "/usr/bin:/bin"}).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    prefix = MARKER + "="
    rows = []
    for line in listed.splitlines():
        parts = line.split()
        if len(parts) < 9 or not all(part.isdigit() for part in parts[:3]):
            continue
        markers = frozenset(part[len(prefix):] for part in parts[9:] if part.startswith(prefix))
        rows.append(Proc(int(parts[0]), int(parts[1]), int(parts[2]), " ".join(parts[4:9]),
                         parts[3].startswith("Z"), markers))
    return rows


def descendants(root: int, table: Iterable[Proc], marker: str | None = None) -> set[int]:
    """Live processes of the run: below ``root`` by parent PID, in ``root``'s process group, or
    carrying ``marker``; never ``root`` itself or this process."""
    table = [Proc(*row) for row in table]
    children: dict[int, list[int]] = {}
    for row in table:
        children.setdefault(row.ppid, []).append(row.pid)
    found, pending = set(), [root]
    while pending:
        for child in children.get(pending.pop(), []):
            if child not in found:
                found.add(child)
                pending.append(child)
    found |= {row.pid for row in table if row.pgid == root or (marker and marker in row.markers)}
    found -= {row.pid for row in table if row.zombie}
    found.discard(root)
    found.discard(os.getpid())
    return found


def _members(root: int, marker: str | None, rows: Sequence[Proc], seen: Mapping[int, str]) -> dict[int, str]:
    """``{pid: start time}`` of the run's live processes in ``rows``, including those seen
    earlier that are still the same process."""
    by_pid = {row.pid: row for row in rows}
    found = descendants(root, rows, marker)
    found |= {pid for pid, started in seen.items()
              if pid in by_pid and by_pid[pid].started == started and not by_pid[pid].zombie}
    found -= {root, os.getpid()}
    return {pid: by_pid[pid].started if pid in by_pid else "" for pid in found}


def _signal(pids: Iterable[int], group: int, number: int) -> None:
    try:
        os.killpg(group, number)
    except (ProcessLookupError, PermissionError):
        pass
    for pid in pids:
        try:
            os.kill(pid, number)
        except (ProcessLookupError, PermissionError):
            pass


def exited(pid: int) -> bool:
    """Whether child ``pid`` has exited, without reaping it: its PID and group stay reserved."""
    try:
        return os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is not None
    except ChildProcessError:
        return True


def terminate_tree(process: subprocess.Popen, *, marker: str | None = None, grace: float = GRACE_SECONDS,
                   table: Callable[[], Sequence[Proc]] = process_table,
                   seen: Mapping[int, str] | None = None) -> list[int]:
    """SIGTERM, then SIGKILL after ``grace`` seconds, to the run's group and all its processes;
    then reap the root. Returns the PIDs signalled (the root only while it was still running).

    The root is reaped only at the end, so an exception on the way (a stop signal) leaves it
    unreaped for a second sweep."""
    root, seen = process.pid, dict(seen or {})
    signalled = set() if exited(root) else {root}
    first = _members(root, marker, table(), seen)
    seen.update(first)
    _signal(first, root, signal.SIGTERM)
    signalled |= set(first)
    deadline = time.monotonic() + grace
    while time.monotonic() < deadline:
        time.sleep(GRACE_POLL_SECONDS)
        if exited(root) and not _members(root, marker, table(), seen):
            break
    final = _members(root, marker, table(), seen)  # a fresh scan: only live members of the run
    _signal(final, root, signal.SIGKILL)
    signalled |= set(final)
    if not exited(root):
        os.kill(root, signal.SIGKILL)  # unreaped, so still the run's root
    process.wait()
    return sorted(signalled)


def _install_stop_handlers() -> dict | None:
    """Turn the first stop signal into `SupervisorStopped`; later ones wait for the sweep."""
    if threading.current_thread() is not threading.main_thread():
        return None
    stopping = []

    def stop(number, frame):
        if not stopping:
            stopping.append(number)
            raise SupervisorStopped(number)
    previous = {}
    for number in STOP_SIGNALS:
        previous[number] = signal.getsignal(number)
        signal.signal(number, stop)
    return previous


def _restore_handlers(previous: dict | None) -> None:
    for number, handler in (previous or {}).items():
        signal.signal(number, handler if handler is not None else signal.SIG_DFL)


def run_supervised(argv: Sequence[str], *, timeout: float, cwd: str | Path, env: Mapping[str, str],
                   stdout_path: str | Path, stderr_path: str | Path, caffeinate: bool = True,
                   grace: float = GRACE_SECONDS, rescan: float = RESCAN_SECONDS,
                   table: Callable[[], Sequence[Proc]] = process_table) -> Supervised:
    """Run ``argv`` to completion or until ``timeout`` seconds; no process of the run outlives it."""
    if os.name != "posix":
        raise OSError("run supervision needs POSIX process groups")
    marker = secrets.token_hex(16)
    previous = _install_stop_handlers()
    keeper = None
    try:
        with open(stdout_path, "ab") as stdout, open(stderr_path, "ab") as stderr:
            process = subprocess.Popen(list(argv), cwd=str(cwd), env={**env, MARKER: marker},
                                       stdin=subprocess.DEVNULL, stdout=stdout, stderr=stderr,
                                       start_new_session=True)
        seen: dict[int, str] = {}
        try:
            if caffeinate and sys.platform == "darwin" and os.path.exists(CAFFEINATE):
                try:
                    keeper = subprocess.Popen(caffeinate_argv(process.pid), stdin=subprocess.DEVNULL,
                                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                except OSError:
                    keeper = None
            result = Supervised(None, caffeinated=keeper is not None)
            deadline = time.monotonic() + timeout
            next_scan = time.monotonic() + rescan
            while not exited(process.pid):
                if time.monotonic() >= deadline:
                    result.timed_out = True
                    break
                if time.monotonic() >= next_scan:  # remember members whose parent may exit later
                    seen.update(_members(process.pid, marker, table(), seen))
                    next_scan = time.monotonic() + rescan
                time.sleep(POLL_SECONDS)
            result.signalled = terminate_tree(process, marker=marker, grace=grace, table=table, seen=seen)
            result.returncode = process.returncode
        except BaseException:  # the supervisor itself is stopped: the run does not outlive it
            if process.returncode is None:
                terminate_tree(process, marker=marker, grace=grace, table=table, seen=seen)
            raise
    finally:
        _restore_handlers(previous)
        if keeper is not None:
            try:
                keeper.wait(timeout=5)  # caffeinate -w exits with the run
            except subprocess.TimeoutExpired:
                keeper.kill()
                keeper.wait()
    return result
