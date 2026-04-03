"""
Debug file logger for MCP tool calls.

Enabled by AGENTS_DEBUG=1 in .env. Each call writes a separate JSON file:
  logs/{YYYY-MM-DD}/{HH-MM-SS.fff}_{tool}_{direction}.json

When disabled — pure no-op, zero overhead.
"""

import json
import os
import re
from datetime import datetime, timezone

from src.engine.config import AGENTS_DEBUG, DEBUG_LOG_DIR


def debug_log(tool: str, direction: str, data: dict) -> None:
    """Write a debug snapshot to a timestamped JSON file.

    Args:
        tool: MCP tool name, e.g. "route_and_load"
        direction: "req" or "res"
        data: arbitrary dict with call details
    """
    if not AGENTS_DEBUG:
        return

    now = datetime.now(timezone.utc)
    date_dir = now.strftime("%Y-%m-%d")
    ts_prefix = now.strftime("%H-%M-%S") + f".{now.microsecond // 1000:03d}"
    safe_tool = re.sub(r'[^\w\-.]', '_', tool)
    safe_dir = re.sub(r'[^\w\-.]', '_', direction)
    filename = f"{ts_prefix}_{safe_tool}_{safe_dir}.json"

    target_dir = os.path.join(DEBUG_LOG_DIR, date_dir)
    os.makedirs(target_dir, exist_ok=True)

    payload = {
        "ts": now.isoformat(),
        "tool": tool,
        "dir": direction,
        "data": data,
    }

    filepath = os.path.join(target_dir, filename)
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2, default=str)
