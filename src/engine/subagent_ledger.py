"""Private ledger of tiered worker outcomes: routing facts only, never task text.

One JSON line per prepared package with an execution and per validation of such a contract,
in the installation's ``data/subagent-tiers/ledger.jsonl``. Task IDs are stored hashed. A
write failure is logged and never fails the tool call. ``summarize`` shows per client, tier
and model how often light workers were accepted without escalation.
"""
from __future__ import annotations

from collections import Counter
import datetime as dt
import hashlib
import json
import logging
import os
from pathlib import Path
import threading

logger = logging.getLogger(__name__)

LEDGER_MAX_BYTES = 5 * 1024 * 1024
_lock = threading.Lock()


def ledger_path() -> Path:
    from src.engine import config  # lazy: the report script reads a given path without it

    return Path(config.INSTALL_DATA_DIR) / "subagent-tiers" / "ledger.jsonl"


def _task(task_id: str) -> str:
    return hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:16]


def _execution(execution) -> dict:
    return {"client": execution.client, "complexity": execution.complexity, "tier": execution.tier,
            "model": execution.model, "reasoning_effort": execution.reasoning_effort}


def append(entry: dict) -> None:
    """Append one line, rotating to ``ledger.jsonl.1`` past the size cap."""
    path = ledger_path()
    line = json.dumps({"at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), **entry},
                      ensure_ascii=False, sort_keys=True) + "\n"
    try:
        with _lock:
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if path.exists() and path.stat().st_size + len(line.encode("utf-8")) > LEDGER_MAX_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(descriptor, "a", encoding="utf-8") as handle:
                handle.write(line)
    except OSError as error:
        logger.warning("Subagent tier ledger write failed for %s: %s", path, error)


def record_prepared(package) -> None:
    execution = package.execution
    if execution is None:
        return
    append({"event": "prepared", "task": _task(package.task_id), "attempt_id": package.attempt_id,
            **_execution(execution), "requested_tier": execution.requested_tier,
            "source": execution.source, "floor_reasons": list(execution.floor_reasons)})


def record_validation(outcome) -> None:
    if outcome is None:
        return
    escalation = outcome.escalation
    append({"event": "validated", "task": _task(outcome.task_id), "attempt_id": outcome.attempt_id,
            **_execution(outcome.execution), "valid": outcome.valid, "status": outcome.status,
            "failed_checks": outcome.failed_checks,
            "escalation": escalation.tier if escalation else None,
            "escalation_reason": escalation.reason if escalation else None})


def read_entries(path: Path | str | None = None) -> list[dict]:
    """Entries of the rotated copy, then the current file; malformed lines are skipped."""
    path = Path(path) if path is not None else ledger_path()
    entries = []
    for source in (path.with_name(path.name + ".1"), path):
        try:
            text = source.read_text(encoding="utf-8")
        except OSError:
            continue
        for line in text.splitlines():
            try:
                value = json.loads(line)
            except ValueError:
                continue
            if isinstance(value, dict):
                entries.append(value)
    return entries


def summarize(entries: list[dict]) -> list[dict]:
    """Counts per (client, tier, model), in first-seen order."""
    rows: dict[tuple, dict] = {}
    for entry in entries:
        key = (entry.get("client") or "unknown", entry.get("tier"), entry.get("model") or "inherit")
        row = rows.setdefault(key, {"client": key[0], "tier": key[1], "model": key[2], "prepared": 0,
                                    "validated": 0, "accepted": 0, "invalid": 0, "escalations": Counter()})
        if entry.get("event") == "prepared":
            row["prepared"] += 1
        elif entry.get("event") == "validated":
            row["validated"] += 1
            if entry.get("valid") is False:
                row["invalid"] += 1
            if entry.get("escalation"):
                row["escalations"][entry["escalation"]] += 1
            elif entry.get("valid") and entry.get("status") == "completed":
                row["accepted"] += 1
    return [{**row, "escalations": dict(row["escalations"])} for row in rows.values()]


def format_table(rows: list[dict]) -> str:
    header = ("client", "tier", "model", "prepared", "validated", "accepted", "invalid", "escalations")
    table = [header] + [(row["client"], row["tier"], row["model"], str(row["prepared"]), str(row["validated"]),
                         str(row["accepted"]), str(row["invalid"]),
                         ", ".join(f"{tier}={count}" for tier, count in row["escalations"].items()) or "-")
                        for row in rows]
    widths = [max(len(line[column]) for line in table) for column in range(len(header))]
    return "\n".join("  ".join(cell.ljust(width) for cell, width in zip(line, widths)).rstrip()
                     for line in table)
