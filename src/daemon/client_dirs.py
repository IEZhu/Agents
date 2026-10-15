"""The AI clients' configuration directories on this machine, for the web UI (#297).

Claude Code keeps its configuration in ``~/.claude`` or in the directory ``CLAUDE_CONFIG_DIR``
names, and one machine can use several. The daemon learns them from its own environment and from
the client configurations ``python -m src.daemon migrate`` recorded in ``client-configs.json``
(``audit.registered_configs``): a ``claude`` record's ``.claude.json`` lies in its configuration
directory, except ``~/.claude.json``, which belongs to ``~/.claude``; a ``claude-deny-desktop``
record's ``settings.json`` lies in its configuration directory. Codex's home is ``CODEX_HOME`` or
``~/.codex``, plus the home of each user-scope ``codex`` record. Only directories that exist count,
each once, in the order found.
"""
from __future__ import annotations

import logging
import os
from pathlib import Path

from src.client_paths import absolute_path, client_home
from .audit import registered_configs
from .state import read_json

logger = logging.getLogger(__name__)


def _records(directory) -> list[dict]:
    if directory is None:
        return []
    try:
        return registered_configs(read_json(Path(directory) / "client-configs.json", {}))
    except (OSError, ValueError) as error:
        logger.warning("client-configs.json cannot be read, so only the default directories count: %s", error)
        return []


def _existing(candidates) -> list[Path]:
    found, seen = [], set()
    for candidate in candidates:
        try:
            path = Path(candidate).resolve()
        except (OSError, RuntimeError):
            continue
        if path not in seen and path.is_dir():
            seen.add(path)
            found.append(path)
    return found


def claude_config_dirs(directory=None, *, home=None, environ=None) -> list[Path]:
    """Claude Code's configuration directories: the environment's, ``~/.claude`` and the recorded ones."""
    home = absolute_path(home or Path.home())
    env = os.environ if environ is None else environ
    candidates = [client_home("claude", home=home, environ=env), home / ".claude"]
    for record in _records(directory):
        path = Path(record["path"])
        if record["client"] == "claude":
            candidates.append(home / ".claude" if path.parent == home else path.parent)
        elif record["client"] == "claude-deny-desktop":
            candidates.append(path.parent)
    return _existing(candidates)


def codex_homes(directory=None, *, home=None, environ=None) -> list[Path]:
    """Codex's home directories: the environment's and those of the recorded user-scope configurations."""
    home = absolute_path(home or Path.home())
    env = os.environ if environ is None else environ
    candidates = [client_home("codex", home=home, environ=env)]
    candidates += [Path(record["path"]).parent for record in _records(directory)
                   if record["client"] == "codex" and record.get("workspace") is None]
    return _existing(candidates)
