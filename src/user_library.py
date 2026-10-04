"""Root files and change notifications of the personal flow library (``flows/.user``).

Three files at the library root prepare it for sync between machines; the library
itself never reads them as flows:

* ``.agents-library.json`` marks the directory as an Agents-Core library and
  records its format, so a sync can recognize a library remote and refuse a
  newer format;
* ``.gitignore`` keeps machine-local and temporary files out of a repository that
  holds the library;
* ``.gitattributes`` turns off line-ending conversion, because a flow's revision
  is the SHA-256 of its bytes.

Writers create the files that are missing and never overwrite them. After each
write that changed something they call the listeners registered here with the
changed paths, relative to the library root; until a sync runner subscribes,
nothing listens. The ``.gitignore`` is a default, not a guarantee: a user may edit
it, so a sync must also exclude machine-local files and groups itself.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import tempfile
import threading
from typing import Callable, Iterable

FORMAT = 1
MARKER = ".agents-library.json"
REPO_META = ".repo.json"         # a repository group's normalized origin; may be shared
REPO_LOCAL = ".repo.local.json"  # this machine's clone path for that group; never shared
GITIGNORE = (
    "# Written by Agents-Core: machine-local and temporary files stay on this machine.\n"
    ".lock\n"
    ".tmp-*\n"
    "__pycache__/\n"
    "*.pyc\n"
    f"**/{REPO_LOCAL}\n"
)
GITATTRIBUTES = (
    "# Written by Agents-Core: keep bytes unchanged; a flow's revision is their SHA-256.\n"
    "* -text\n"
)

Listener = Callable[[Path, tuple[str, ...]], None]

logger = logging.getLogger(__name__)
_listeners: list[Listener] = []
_listeners_guard = threading.Lock()


def subscribe(listener: Listener) -> Callable[[], None]:
    """Call ``listener(root, paths)`` after every successful change; returns the unsubscribe function."""
    with _listeners_guard:
        _listeners.append(listener)

    def unsubscribe() -> None:
        with _listeners_guard:
            if listener in _listeners:
                _listeners.remove(listener)

    return unsubscribe


def notify(root: Path, paths: Iterable[str]) -> None:
    """Pass the paths a write changed (relative to ``root``) to every listener.

    Writers call it also when they fail after changing something, so a listener
    hears of every change and of nothing else. ``root`` is resolved, so one library
    has one name whichever path reached it. The write has already happened, so a
    failing listener is logged and never fails it.
    """
    changed = tuple(dict.fromkeys(paths))
    if not changed:
        return
    root = Path(root).expanduser().resolve()
    with _listeners_guard:
        listeners = list(_listeners)
    for listener in listeners:
        try:
            listener(root, changed)
        except Exception:
            logger.exception("A library change listener failed")


def ensure_root_files(root: Path) -> list[str]:
    """Create the root files that are missing and return their names. Call under the library lock.

    The write that calls it has already succeeded, so a file that cannot be
    created is logged and left to the next write.
    """
    root = Path(root)
    created = []
    for name, content in ((MARKER, _marker), (".gitignore", GITIGNORE), (".gitattributes", GITATTRIBUTES)):
        path = root / name
        if path.exists() or path.is_symlink():
            continue  # a user's edit or a symlink stays as it is
        try:
            atomic_write(path, (content() if callable(content) else content).encode("utf-8"))
        except OSError:
            logger.warning("Could not create the library file %s", path, exc_info=True)
            continue
        created.append(name)
    return created


def _marker() -> str:
    from src.version import agents_core_version
    return json.dumps({"format": FORMAT,
                       "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                       "created_by": f"Agents-Core {agents_core_version()}"}, indent=2) + "\n"


def atomic_write(path: Path, data: bytes) -> None:
    """Write ``data`` through a temporary file in the same directory, synced, then renamed into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=".tmp-", delete=False) as stream:
        try:
            stream.write(data)
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
