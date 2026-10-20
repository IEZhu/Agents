"""Small, explicit owner configuration shared with the Claude mod."""
from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile

MAX_CONFIG_BYTES = 65536
DEFAULTS = {"schema_version": 1, "enabled": False, "min_context_tokens": 100000,
            "min_turns": 5, "cooldown_seconds": 900, "cursor_bindings": []}


def config_path(value: str | Path | None = None) -> Path:
    raw = os.environ.get("AGENTS_CONTEXT_COMPACTION_CONFIG")
    if raw is None:
        raw = value
    if raw is not None:
        path = Path(raw)
        if not str(raw) or not path.is_absolute() or str(raw).startswith(("//", "\\\\")):
            raise ValueError("compaction config path must be absolute")
        return path
    return Path.home() / ".config/agents-core/context-compaction.json"


def refuse_links(path: Path) -> None:
    for part in (path, *path.parents):
        if part.is_symlink():
            raise ValueError("compaction paths must not contain symbolic links")


def validate(document: object) -> dict:
    if not isinstance(document, dict) or set(document) - set(DEFAULTS):
        raise ValueError("expected a compaction settings object with known fields")
    result = {**DEFAULTS, **document}
    if type(result["schema_version"]) is not int or result["schema_version"] != 1:
        raise ValueError("unsupported compaction schema_version")
    if type(result["enabled"]) is not bool:
        raise ValueError("enabled must be a boolean")
    for key, lower, upper in (("min_context_tokens", 10000, 10000000),
                              ("min_turns", 1, 1000), ("cooldown_seconds", 0, 86400)):
        value = result[key]
        if type(value) is not int or not lower <= value <= upper:
            raise ValueError(f"{key} must be an integer from {lower} to {upper}")
    bindings = result["cursor_bindings"]
    if not isinstance(bindings, list) or len(bindings) > 100:
        raise ValueError("cursor_bindings must be a list with at most 100 entries")
    seen = set()
    labels = set()
    for item in bindings:
        if not isinstance(item, dict) or set(item) != {"conversation_id", "window_title", "chat_title"}:
            raise ValueError("each Cursor binding needs conversation_id, window_title and chat_title")
        for value in item.values():
            if not isinstance(value, str) or not value.strip() or len(value) > 512 or any(ord(c) < 32 for c in value):
                raise ValueError("Cursor binding values must be bounded nonempty single-line strings")
        label = (item["window_title"], item["chat_title"])
        if item["conversation_id"] in seen or label in labels:
            raise ValueError("Cursor bindings must identify distinct conversations and UI targets")
        seen.add(item["conversation_id"])
        labels.add(label)
    return result


def load(path: str | Path | None = None) -> tuple[dict, str | None]:
    """Missing, unreadable, oversized or invalid settings are always disabled."""
    try:
        target = config_path(path)
        refuse_links(target)
        with target.open("rb") as stream:
            raw = stream.read(MAX_CONFIG_BYTES + 1)
        if len(raw) > MAX_CONFIG_BYTES:
            raise ValueError("compaction settings exceed 64 KiB")
        return validate(json.loads(raw)), None
    except FileNotFoundError:
        return validate({}), "not_configured"
    except (OSError, ValueError, TypeError, RecursionError):
        return validate({}), "invalid_config"


def write(path: str | Path, document: dict) -> None:
    target = config_path(path)
    validated = validate(document)
    content = (json.dumps(validated, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    if len(content) > MAX_CONFIG_BYTES:
        raise ValueError("compaction settings exceed 64 KiB")
    refuse_links(target)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temporary = tempfile.mkstemp(prefix=target.name + ".", dir=target.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        refuse_links(target)
        os.replace(temporary, target)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
