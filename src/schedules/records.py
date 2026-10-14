"""Run records: ``<state_dir>/schedules/runs/<run-id>/record.json``, with secrets masked.

A record is the runner's memory of one run: its mode and trigger, the gate decision, the status
(``completed``, ``refused`` or ``failed: infra`` with a reason) and the IDs of the comments the
broker wrote (``comments``: ``[{"id": N, "kind": "state" | "selection", ...}]``). Only those IDs
make a state or selection comment trusted (design 5.2). Every string written passes `mask`.

Writers on several threads of one runner process (the run sequence, a worker thread that adds its
``workers`` entry, a Codex decision that adds to ``clients``) change one record: `update_record`
reads, changes and writes it under a per-run lock, and `write_record` takes the same lock, so no
writer drops another's fields as long as each one that adds to the record goes through
`update_record` instead of writing a copy it kept in memory (#345).
"""
from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
import re
import secrets
import threading
from typing import Any, Iterable

from src.daemon.state import private_dir, read_json, write_json

from .lease import schedules_dir

RECORD_NAME = "record.json"
_RUN_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{6}$")
MASK = "***"
SECRET_PATTERNS = (
    re.compile(r"\b(gh[opsur]_)[A-Za-z0-9]{16,}"),
    re.compile(r"\b(github_pat_)[A-Za-z0-9_]{16,}"),
    re.compile(r"\b(sk-ant-)[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b(sk-or-)[A-Za-z0-9_-]{8,}"),
    re.compile(r"\b(AKIA)[0-9A-Z]{16}\b"),
    re.compile(r"(-----BEGIN [A-Z ]*PRIVATE KEY-----)[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{16,}"),
)


def runs_dir(directory: str | Path | None = None) -> Path:
    return schedules_dir(directory) / "runs"


def new_run_id(now: datetime | None = None) -> str:
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    return f"{now:%Y%m%dT%H%M%SZ}-{secrets.token_hex(3)}"


def valid_run_id(run_id: str) -> bool:
    return isinstance(run_id, str) and bool(_RUN_ID.match(run_id))


def mask(value: Any, known: Iterable[str] = ()) -> Any:
    """``value`` with token-like strings, and each ``known`` secret, replaced by ``***``."""
    known = [secret for secret in known if secret]
    if isinstance(value, str):
        for secret in known:
            value = value.replace(secret, MASK)
        for pattern in SECRET_PATTERNS:
            value = pattern.sub(lambda match: match[1] + MASK, value)
        return value
    if isinstance(value, dict):
        return {mask(str(key), known): mask(item, known) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [mask(item, known) for item in value]
    return value


def run_dir(run_id: str, directory: str | Path | None = None) -> Path:
    if not valid_run_id(run_id):
        raise ValueError(f"not a run ID: {run_id!r}")
    path = runs_dir(directory) / run_id
    private_dir(runs_dir(directory))
    return private_dir(path)


_LOCKS: dict[str, threading.RLock] = {}
_LOCKS_GUARD = threading.Lock()


def _record_lock(path: Path) -> threading.RLock:
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(str(path.resolve()), threading.RLock())


def write_record(run_id: str, record: dict, directory: str | Path | None = None,
                 known: Iterable[str] = ()) -> dict:
    """Write the masked record and return what was written."""
    masked = mask({**record, "run_id": run_id}, known)
    path = run_dir(run_id, directory) / RECORD_NAME
    with _record_lock(path):
        write_json(path, masked)
    return masked


def update_record(run_id: str, change: Callable[[dict], dict | None], directory: str | Path | None = None,
                  known: Iterable[str] = ()) -> dict:
    """Read the record (empty when there is none), apply ``change`` and write the result, all under
    the run's lock; ``change`` returns the new record or changes the one it gets in place (and
    returns None). Returns what was written."""
    path = run_dir(run_id, directory) / RECORD_NAME
    with _record_lock(path):
        record = read_record(run_id, directory) or {}
        changed = change(record)
        return write_record(run_id, record if changed is None else changed, directory, known)


def read_record(run_id: str, directory: str | Path | None = None) -> dict | None:
    if not valid_run_id(run_id):
        return None
    try:
        record = read_json(runs_dir(directory) / run_id / RECORD_NAME, None)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def all_records(directory: str | Path | None = None, *, limit: int | None = None) -> list[dict]:
    """Every readable record, newest first; with ``limit``, the ``limit`` newest, reading no older one."""
    base = runs_dir(directory)
    if not base.is_dir():
        return []
    found = []
    for name in sorted((path.name for path in base.iterdir() if valid_run_id(path.name)), reverse=True):
        if limit is not None and len(found) >= limit:
            break
        record = read_record(name, directory)
        if record:
            found.append(record)
    return found


def trusted_comment_ids(directory: str | Path | None = None) -> set[int]:
    """IDs of the comments that runs recorded as their own (state and selection comments)."""
    ids = set()
    for record in all_records(directory):
        for comment in record.get("comments") or []:
            if isinstance(comment, dict) and isinstance(comment.get("id"), int) and \
                    not isinstance(comment.get("id"), bool):
                ids.add(comment["id"])
    return ids
