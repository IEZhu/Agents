"""The heavy-process lease (design 5.6, 9.4 step 4): one ``pytest`` or reindexing at a time on the machine.

A session runs its checks as ``{heavy_cmd} -- <command>``. That client asks the runner socket
(`src.schedules.socket_service`) for the lease with ``heavy.acquire``, runs the command itself
without a shell, and releases the lease with ``heavy.release`` or simply by disconnecting. The
service only grants and releases; it never starts a command.

The lease is a lock file, ``<state_dir>/schedules/heavy.lease`` beside the run lease
(`src.schedules.lease`), that the runner holds for as long as the granting connection stays open,
so a client that dies releases it. A grant is refused while another holder keeps the lock, while
a foreign heavy process runs (`HEAVY`), and when the process table cannot be read. A foreign
process is one other than the runner itself and the requesting client with its ancestors, whose
command lines name the command the client is about to start; when the runner names the stage
session's root PID, only the ancestors up to that root are excluded, so a heavy process that
started the session, or runs beside it, still counts. The scheduler's tick waits while the lease
is held (`held`) and while its own probe (`heavy_processes`) sees a heavy process; that probe
counts none when ``ps`` fails, so the tick fails open there.

The lease is cooperative coordination between well-behaved sessions, not a guarantee: nothing
stops a session from running ``pytest`` without ``heavy --``, or from exhausting the user's process
limit so that ``ps`` fails. Enforcing one heavy process against a misbehaving session needs
process and resource limits on the session's tree, which no part sets yet; the run sequence (#347)
bounds a session only by its stage timeout and ends its process group afterwards.
"""
from __future__ import annotations

import os
from pathlib import Path
import re
from typing import Callable

from . import lease
from .socket_service import (Registry, RequestContext, RequestRefused, lineage,  # noqa: F401 (re-exported)
                             process_table)

LEASE_NAME = "heavy.lease"
RESOURCE = "the heavy-process lease"
PS = "/bin/ps"
PS_TIMEOUT = 10.0  # seconds
ACQUIRE_DEADLINE = 15.0  # seconds for heavy.acquire: the lock's short wait and one ps
HEAVY = (re.compile(r"\bpytest\b"), re.compile(r"\bsrc[./]reindex\b"))


def lease_path(directory: str | Path | None = None) -> Path:
    return lease.schedules_dir(directory) / LEASE_NAME


def held(directory: str | Path | None = None) -> bool:
    """Whether a check holds the heavy-process lease now. Creates nothing."""
    return lease.locked(lease_path(directory))


def foreign_heavy(ps: str = PS, *, requester: int | None = None, session_root: int | None = None,
                  timeout: float = PS_TIMEOUT) -> list[str] | None:
    """Running ``pytest`` and reindexing processes as ``"<pid> <command>"``, or None when ``ps``
    fails. Never this process, nor ``requester`` and its ancestors, whose command lines name the
    command the client will start; with ``session_root``, those ancestors only up to that root."""
    table = process_table(ps, timeout=timeout)
    if table is None:
        return None
    excluded = {os.getpid()}
    if requester is not None:
        excluded |= lineage(table, requester, stop=session_root)
    return [f"{pid} {command.strip()[:80]}" for pid, (_, command) in table.items()
            if pid not in excluded and any(pattern.search(command) for pattern in HEAVY)]


def heavy_processes(ps: str = PS, *, requester: int | None = None, session_root: int | None = None,
                    timeout: float = PS_TIMEOUT) -> list[str]:
    """`foreign_heavy` for the tick's probe, which fails open: no process when ``ps`` fails."""
    return foreign_heavy(ps, requester=requester, session_root=session_root, timeout=timeout) or []


class HeavyLease:
    """The handlers of ``heavy.acquire`` and ``heavy.release``.

    ``processes(requester=, session_root=, timeout=)`` lists the foreign heavy processes, or
    returns None when it cannot tell (default `foreign_heavy`).
    """

    def __init__(self, directory: str | Path | None = None, *,
                 processes: Callable[..., list | None] | None = None, wait: float = lease.HOLD_WAIT) -> None:
        self.path = lease_path(directory)
        self.processes = processes or foreign_heavy
        self.wait = wait  # a tick's probe of `held` takes the lock for an instant

    def acquire(self, args: dict, request: RequestContext) -> dict:
        connection = request.connection
        # The lock goes onto the connection as it is taken, so the server's close releases it even
        # while the probe below still runs, and a closed connection takes it no more.
        if not connection.take(RESOURCE, lambda: lease.acquire(self.path, wait=0),
                               wait=min(self.wait, request.remaining())):
            raise RequestRefused(f"{RESOURCE} is held by another check")
        try:
            running = self.processes(requester=request.peer_pid, session_root=request.session_root,
                                     timeout=request.remaining())
            if running is None:
                raise RequestRefused("the process table cannot be read, so the lease is not granted")
            if running:
                raise RequestRefused(f"heavy processes are running: {', '.join(map(str, running))}")
        except BaseException:
            connection.release(RESOURCE)
            raise
        return {"lease": "granted"}

    def release(self, args: dict, request: RequestContext) -> dict:
        return {"released": request.connection.release(RESOURCE)}


def register(registry: Registry, directory: str | Path | None = None, **options) -> HeavyLease:
    """Register ``heavy.acquire`` and ``heavy.release``; neither takes an argument."""
    service = HeavyLease(directory, **options)
    registry.register("heavy.acquire", service.acquire, deadline=ACQUIRE_DEADLINE)
    registry.register("heavy.release", service.release)
    return service
