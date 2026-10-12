"""The heavy-process lease (design 5.6, 9.4 step 4): one ``pytest`` or reindexing at a time on the machine.

A session runs its checks as ``{heavy_cmd} -- <command>``. That client asks the runner socket
(`src.schedules.socket_service`) for the lease with ``heavy.acquire``, runs the command itself
without a shell, and releases the lease with ``heavy.release`` or simply by disconnecting. The
service only grants and releases; it never starts a command.

The lease is a lock file, ``<state_dir>/schedules/heavy.lease`` beside the run lease
(`src.schedules.lease`), that the runner holds for as long as the granting connection stays open,
so a client that dies releases it. A grant is refused while another holder keeps the lock, and
while a foreign heavy process runs (`HEAVY`): one other than the runner itself and the requesting
client with its ancestors, whose command lines name the command the client is about to start. The
scheduler's tick waits while the lease is held (`held`).
"""
from __future__ import annotations

import os
from pathlib import Path
import re
import subprocess
from typing import Callable

from . import lease
from .socket_service import Connection, Registry, RequestRefused

LEASE_NAME = "heavy.lease"
RESOURCE = "the heavy-process lease"
PS = "/bin/ps"
PS_TIMEOUT = 30  # seconds
HEAVY = (re.compile(r"\bpytest\b"), re.compile(r"\bsrc[./]reindex\b"))


def lease_path(directory: str | Path | None = None) -> Path:
    return lease.schedules_dir(directory) / LEASE_NAME


def held(directory: str | Path | None = None) -> bool:
    """Whether a check holds the heavy-process lease now. Creates nothing."""
    return lease.locked(lease_path(directory))


def process_table(ps: str = PS) -> dict[int, tuple[int, str]] | None:
    """``{pid: (parent pid, command line)}`` of every process; None when ``ps`` fails."""
    try:
        listed = subprocess.run([ps, "-A", "-o", "pid=,ppid=,command="], capture_output=True, text=True,
                                stdin=subprocess.DEVNULL, timeout=PS_TIMEOUT, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    table = {}
    for line in listed.splitlines():
        parts = line.split(None, 2)
        if len(parts) >= 2 and parts[0].isdigit() and parts[1].isdigit():
            table[int(parts[0])] = (int(parts[1]), parts[2] if len(parts) == 3 else "")
    return table


def lineage(table: dict[int, tuple[int, str]], pid: int) -> set[int]:
    """``pid`` and its ancestors in ``table``."""
    found: set[int] = set()
    while pid > 0 and pid not in found:
        found.add(pid)
        if pid not in table:
            break
        pid = table[pid][0]
    return found


def heavy_processes(ps: str = PS, *, requester: int | None = None) -> list[str]:
    """Running ``pytest`` and reindexing processes as ``"<pid> <command>"``: never this process, nor
    ``requester`` and its ancestors, whose command lines name the command the client will start."""
    table = process_table(ps)
    if table is None:
        return []
    excluded = {os.getpid()}
    if requester is not None:
        excluded |= lineage(table, requester)
    return [f"{pid} {command.strip()[:80]}" for pid, (_, command) in table.items()
            if pid not in excluded and any(pattern.search(command) for pattern in HEAVY)]


class HeavyLease:
    """The handlers of ``heavy.acquire`` and ``heavy.release``.

    ``processes(requester_pid)`` lists the foreign heavy processes (default `heavy_processes`).
    """

    def __init__(self, directory: str | Path | None = None, *,
                 processes: Callable[[int | None], list] | None = None, wait: float = lease.HOLD_WAIT) -> None:
        self.path = lease_path(directory)
        self.processes = processes or (lambda requester: heavy_processes(requester=requester))
        self.wait = wait  # a tick's probe of `held` takes the lock for an instant

    def acquire(self, args: dict, connection: Connection) -> dict:
        if connection.holds(RESOURCE):
            raise RequestRefused(f"this connection already holds {RESOURCE}")
        stack = lease.acquire(self.path, wait=self.wait)
        if stack is None:
            raise RequestRefused(f"{RESOURCE} is held by another check")
        try:
            running = self.processes(connection.peer_pid)
            if running:
                raise RequestRefused(f"heavy processes are running: {', '.join(map(str, running))}")
            connection.keep(RESOURCE, stack)
        except BaseException:
            stack.close()
            raise
        return {"lease": "granted"}

    def release(self, args: dict, connection: Connection) -> dict:
        return {"released": connection.release(RESOURCE)}


def register(registry: Registry, directory: str | Path | None = None, **options) -> HeavyLease:
    """Register ``heavy.acquire`` and ``heavy.release``; neither takes an argument."""
    service = HeavyLease(directory, **options)
    registry.register("heavy.acquire", service.acquire)
    registry.register("heavy.release", service.release)
    return service
