"""Run records: ``<state_dir>/schedules/runs/<run-id>/record.json``, with secrets masked.

A record is the runner's memory of one run: its mode and trigger, the gate decision, the status
(``completed``, ``refused`` or ``failed: infra`` with a reason) and the IDs of the comments the
broker wrote (``comments``: ``[{"id": N, "kind": "state" | "selection", ...}]``). Only those IDs
make a state or selection comment trusted (design 5.2). Every string written passes `mask`.
"""
from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
import re
import secrets
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


def write_record(run_id: str, record: dict, directory: str | Path | None = None,
                 known: Iterable[str] = ()) -> dict:
    """Write the masked record and return what was written."""
    masked = mask({**record, "run_id": run_id}, known)
    write_json(run_dir(run_id, directory) / RECORD_NAME, masked)
    return masked


def read_record(run_id: str, directory: str | Path | None = None) -> dict | None:
    if not valid_run_id(run_id):
        return None
    try:
        record = read_json(runs_dir(directory) / run_id / RECORD_NAME, None)
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def all_records(directory: str | Path | None = None) -> list[dict]:
    """Every readable record, newest first."""
    base = runs_dir(directory)
    if not base.is_dir():
        return []
    names = sorted((path.name for path in base.iterdir() if valid_run_id(path.name)), reverse=True)
    return [record for record in (read_record(name, directory) for name in names) if record]


def trusted_comment_ids(directory: str | Path | None = None) -> set[int]:
    """IDs of the comments that runs recorded as their own (state and selection comments)."""
    ids = set()
    for record in all_records(directory):
        for comment in record.get("comments") or []:
            if isinstance(comment, dict) and isinstance(comment.get("id"), int) and \
                    not isinstance(comment.get("id"), bool):
                ids.add(comment["id"])
    return ids
