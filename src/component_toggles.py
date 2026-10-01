"""Installation-wide on/off switches for rules, skills and implants.

State lives in the git-ignored ``flows/.user/components.json`` (``AGENTS_USER_FLOWS_DIR``
overrides the directory, as for personal flows)::

    {"disabled": {"rules": ["no-fabrication"], "skills": [], "implants": []}}

Only disabled IDs are stored, so everything is enabled by default. Rules use their
``name``; skills and implants use the file name without ``.mdc``. The file is read
fresh on every call, so a change made in the web UI reaches the next bundle that any
process of the installation builds. Writes are atomic under one lock. A missing,
unreadable or malformed file means "everything enabled": a switch must never turn
an activation into an error.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
from pathlib import Path

from src.engine.config import FLOWS_DIR
from src.file_lock import file_lock

KINDS = ("rules", "skills", "implants")
_ID = re.compile(r"[A-Za-z0-9_-]+")


class ToggleError(ValueError):
    """The request names an unknown kind or an invalid component ID."""


def _path() -> Path:
    configured = os.environ.get("AGENTS_USER_FLOWS_DIR")
    root = Path(configured).expanduser() if configured else Path(FLOWS_DIR) / ".user"
    return root / "components.json"


def _read() -> dict[str, set[str]]:
    result = {kind: set() for kind in KINDS}
    try:
        with _path().open(encoding="utf-8") as stream:
            data = json.load(stream)
        disabled = data["disabled"] if isinstance(data, dict) else {}
        for kind in KINDS:
            values = disabled.get(kind, []) if isinstance(disabled, dict) else []
            if isinstance(values, list):
                result[kind] = {v for v in values if isinstance(v, str) and _ID.fullmatch(v)}
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return result


def disabled(kind: str) -> frozenset[str]:
    """IDs currently switched off for ``kind``; empty when nothing is stored."""
    if kind not in KINDS:
        raise ToggleError(f"unknown component kind: {kind!r}")
    return frozenset(_read()[kind])


def is_enabled(kind: str, component_id: str) -> bool:
    return component_id not in disabled(kind)


def set_enabled(kind: str, component_id: str, enabled: bool) -> None:
    """Switch one component on or off. Raises ``ToggleError`` for a bad kind or ID."""
    if kind not in KINDS:
        raise ToggleError(f"unknown component kind: {kind!r}")
    if not isinstance(component_id, str) or not _ID.fullmatch(component_id):
        raise ToggleError(f"invalid component ID: {component_id!r}")
    path = _path()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with file_lock(path.parent / ".lock"):
        state = _read()
        if enabled:
            state[kind].discard(component_id)
        else:
            state[kind].add(component_id)
        payload = json.dumps(
            {"disabled": {k: sorted(state[k]) for k in KINDS}}, indent=2,
        ).encode("utf-8") + b"\n"
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".tmp-", delete=False) as stream:
            try:
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            except BaseException:
                stream.close()
                os.unlink(stream.name)
                raise
        try:
            os.replace(stream.name, path)
        except BaseException:
            Path(stream.name).unlink(missing_ok=True)
            raise
