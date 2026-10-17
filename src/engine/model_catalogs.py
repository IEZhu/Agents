"""Read-only model lists of the AI clients that launch delegated workers.

Codex keeps its model list in ``$CODEX_HOME/models_cache.json``; Cursor keeps its list in
its VS Code state database. Both are the clients' own caches, read for model names only and
never written. Any failure reads as an empty list, so a caller falls back to pinned models.
"""
from __future__ import annotations

from contextlib import closing
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import sys
import threading
import time
from typing import Callable

logger = logging.getLogger(__name__)

CACHE_SECONDS = 600
MAX_CATALOG_BYTES = 16 * 1024 * 1024
CURSOR_STATE_KEY = ("src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl"
                    ".persistentStorage.applicationUser")
_cache: dict[str, tuple[float, list[str]]] = {}
_lock = threading.Lock()


def version_key(slug: str, pattern: re.Pattern) -> tuple[int, ...] | None:
    """The numeric version in ``slug`` (the pattern's first group), or None when it does not match."""
    match = pattern.fullmatch(slug)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


def newest(slugs: list[str], pattern: re.Pattern) -> str | None:
    """The matching slug with the highest version: ``gpt-6.1-sol`` beats ``gpt-6-sol``."""
    ranked = [(key, slug) for slug in slugs if (key := version_key(slug, pattern)) is not None]
    return max(ranked)[1] if ranked else None


def clear_cache() -> None:
    with _lock:
        _cache.clear()


def _cached(key: str, loader: Callable[[], list[str]]) -> list[str]:
    with _lock:
        hit = _cache.get(key)
        if hit is not None and time.monotonic() - hit[0] < CACHE_SECONDS:
            return list(hit[1])
    try:
        models = loader()
    except (OSError, ValueError, TypeError, KeyError, AttributeError, sqlite3.Error) as error:
        logger.debug("Model catalog %s is unreadable: %r", key, error)
        models = []
    with _lock:
        _cache[key] = (time.monotonic(), models)
    return list(models)


def _bounded_text(path: Path) -> str:
    if path.stat().st_size > MAX_CATALOG_BYTES:
        raise ValueError("catalog is too large")
    return path.read_text(encoding="utf-8")


def codex_home() -> Path:
    # The rule of src/client_paths.client_home("codex"), not imported: the autodev runner
    # imports this module, and its import closure must stay within its control paths.
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()


def codex_models(path: Path | str | None = None) -> list[str]:
    """Slugs Codex lists in its model picker (``visibility: "list"``)."""
    path = Path(path) if path is not None else codex_home() / "models_cache.json"

    def load() -> list[str]:
        data = json.loads(_bounded_text(path))
        return [model["slug"] for model in data["models"]
                if isinstance(model, dict) and isinstance(model.get("slug"), str)
                and model.get("visibility") == "list"]
    return _cached(f"codex:{path}", load)


def cursor_state_path() -> Path:
    """Cursor's global state database on this platform."""
    if sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    elif sys.platform == "win32":
        base = Path(os.environ.get("APPDATA") or Path.home() / "AppData/Roaming")
    else:
        base = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
    return base / "Cursor/User/globalStorage/state.vscdb"


def cursor_models(path: Path | str | None = None) -> list[str]:
    """Model names Cursor offers (``availableDefaultModels2[].serverModelName``)."""
    path = Path(path) if path is not None else cursor_state_path()

    def load() -> list[str]:
        if not path.is_file():
            return []
        # mode=ro never creates or writes the database; Cursor may hold it open meanwhile.
        with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=1.0)) as db:
            row = db.execute("SELECT value FROM ItemTable WHERE key = ?", (CURSOR_STATE_KEY,)).fetchone()
        if row is None:
            return []
        value = row[0].decode("utf-8") if isinstance(row[0], bytes) else row[0]
        if not isinstance(value, str) or len(value) > MAX_CATALOG_BYTES:
            return []
        models = json.loads(value)["availableDefaultModels2"]
        return [model["serverModelName"] for model in models
                if isinstance(model, dict) and isinstance(model.get("serverModelName"), str)]
    return _cached(f"cursor:{path}", load)
