"""
Debug file logger for MCP tool calls.

Enabled by AGENTS_DEBUG=1 in .env. Each call writes a separate JSON file:
  logs/{YYYY-MM-DD}/{HH-MM-SS.fff}_{tool}_{direction}.json

When disabled — pure no-op, zero overhead.
"""

import json
import os
import re
from pathlib import Path
from datetime import datetime, timezone

from src.engine.config import AGENTS_DEBUG, get_debug_log_dir


def _write_debug(tool: str, direction: str, data: dict, directory=None) -> None:
    """Write a debug snapshot to a timestamped JSON file.

    Args:
        tool: MCP tool name, e.g. "route_and_load"
        direction: "req" or "res"
        data: arbitrary dict with call details
    """
    if not AGENTS_DEBUG:
        return

    try:
        now = datetime.now(timezone.utc)
        date_dir = now.strftime("%Y-%m-%d")
        ts_prefix = now.strftime("%H-%M-%S") + f".{now.microsecond // 1000:03d}"
        safe_tool = re.sub(r'[^\w\-.]', '_', tool)
        safe_dir = re.sub(r'[^\w\-.]', '_', direction)
        filename = f"{ts_prefix}_{__import__('uuid').uuid4().hex}_{safe_tool}_{safe_dir}.json"

        target_dir = Path(directory or get_debug_log_dir()).absolute() / date_dir
        if any(path.is_symlink() for path in (target_dir, *target_dir.parents)):
            return
        target_dir.mkdir(parents=True, exist_ok=True, mode=0o700)

        payload = {
            "ts": now.isoformat(),
            "tool": tool,
            "dir": direction,
            "data": data,
        }

        filepath = target_dir / filename
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        with os.fdopen(os.open(filepath, flags, 0o600), "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
    except Exception:
        pass


_queue = None
if os.environ.get("AGENTS_TRANSPORT") == "http":
    import queue
    import threading
    from src.daemon.state import state_dir
    _queue = queue.Queue(maxsize=256)
    def _write_queue():
        while True:
            item = _queue.get()
            try:
                _write_debug(*item)
            finally:
                _queue.task_done()
    threading.Thread(target=_write_queue, name="agents-debug", daemon=True).start()


def debug_log(tool: str, direction: str, data: dict, *, directory=None) -> None:
    if not AGENTS_DEBUG:
        return
    if _queue is not None:
        try:
            _queue.put_nowait((tool, direction, data, str(directory or state_dir() / "debug")))
        except queue.Full:
            pass  # Diagnostic loss must not block requests or allocate an unbounded queue.
    else:
        _write_debug(tool, direction, data, directory)
