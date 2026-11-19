"""Read-only access to the AI clients' native memory files (#297, #369).

Claude Code keeps durable memory per project in ``<config dir>/projects/<project>/memory/``: one
Markdown file per item, with YAML frontmatter, and the index ``MEMORY.md``, one
``- [Title](file.md) — hook`` line per item. ``<project>`` is the project's path with every character
but letters and digits replaced by ``-``. One machine can run Claude Code from several configuration
directories (``src/daemon/client_dirs.py``), each with memory of its own. The top-level
``<config dir>/memory/``, where the installer puts the routing reminder, holds memory outside any
project. Codex keeps its memories, when its memories feature is on, in ``$CODEX_HOME/memories/``
(older homes) or ``$CODEX_HOME/memories_v2/``, outside any project; Cursor keeps no memory in files.

Both the web UI's Memory tab (``src/daemon/memory_ui.py``) and the shared memory index
(``src/memory/shared.py``) read through this module. Every place comes from discovery, never from a
caller: symlinks and Windows junctions are refused, files are opened without following a symlink that
replaced them after they were listed (``O_NOFOLLOW``; on Windows the check right before opening), and
anything but a regular file is refused.
"""
from __future__ import annotations

import errno
import os
import re
import stat
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

import yaml

from src.environments import is_link
from src.utils.prompt_loader import split_frontmatter

INDEX = "MEMORY.md"
MAX_SHOW = 1024 * 1024  # larger files are listed without their text
HEAD = 64 * 1024  # what is read of a larger file for its frontmatter
FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")  # 255 characters, the usual file name limit
PROJECT = re.compile(r"[A-Za-z0-9._-]{1,255}")
SANITIZE = re.compile(r"[^A-Za-z0-9]")
# Codex's memory directories under its home: ``memories`` for homes that had it before
# ``memories_v2`` (the default of a fresh home) existed.
CODEX_MEMORY_DIRS = ("memories", "memories_v2")
# Codex documents no file names, so any visible name is an item; these characters never are.
_UNSAFE_NAME = set("/\\:\0") | {chr(code) for code in range(32)}
# How a file is opened inside its store: one component at a time where the platform has dir_fd
# (POSIX), else by checking the open handle's final path (Windows). Module flags, so tests can
# take either branch on any platform without touching the os module.
_DIR_FD = os.open in os.supports_dir_fd and hasattr(os, "O_DIRECTORY")
_WINDOWS = os.name == "nt"


class Refused(Exception):
    """A path this module will not read: ``code`` names why (``symlink_refused``, ``not_a_file``)."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class Store:
    """A configuration directory with memory: Claude Code's, or Codex's home."""
    path: Path
    client: str
    label: str

    @property
    def id(self) -> str:
        return str(self.path)

    def memory_dir(self, project: str) -> Path:
        """The directory the Memory tab edits: Codex's original ``memories``, a Claude project's ``memory``."""
        if self.client == "codex":
            return self.path / CODEX_MEMORY_DIRS[0]
        return self.path / "memory" if project == "" else self.path / "projects" / project / "memory"

    def memory_dirs(self, project: str) -> list[Path]:
        """Every directory that may hold ``project``'s memory: Codex has two, Claude Code one."""
        if self.client == "codex":
            return [self.path / name for name in CODEX_MEMORY_DIRS]
        return [self.memory_dir(project)]


def label(path: Path, home: Path) -> str:
    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return str(path)


def stores(directory=None, *, home=None, environ=None) -> list[Store]:
    """The configuration directories with memory, Claude Code's first.

    ``directory`` is the service's state directory, whose ``client-configs.json`` records more
    configuration directories (``client_dirs``); None counts only the environment's and the defaults.
    """
    from src.daemon.client_dirs import claude_config_dirs, codex_homes

    root = Path(home or Path.home()).resolve()
    found = [Store(path, "claude", label(path, root))
             for path in claude_config_dirs(directory, home=home, environ=environ)]
    found += [Store(path, "codex", label(path, root))
              for path in codex_homes(directory, home=home, environ=environ)]
    return found


def project_name(root) -> str:
    """Claude Code's directory name for the project at ``root``."""
    return SANITIZE.sub("-", str(root))


def is_item_name(store: Store, name: str) -> bool:
    """A plain file name. Claude Code's items are Markdown files other than its index, with the
    names Claude Code writes; Codex's are any visible name, since its docs name no format."""
    if not isinstance(name, str) or name in (".", "..") or not 0 < len(name) <= 255:
        return False
    if store.client == "codex":
        return not name.startswith(".") and not _UNSAFE_NAME.intersection(name)
    return FILE.fullmatch(name) is not None and name.endswith(".md") and not is_index_name(store, name)


def index_file(store: Store, memory: Path, link_check: Callable[[Path], bool] = is_link) -> Path | None:
    """Claude Code's ``MEMORY.md`` of a memory directory when it is a plain file; Codex keeps no index."""
    index = memory / INDEX
    return index if store.client == "claude" and not link_check(index) and index.is_file() else None


def is_index_name(store: Store, file) -> bool:
    """Claude Code's index name in any case: Windows and macOS file systems usually ignore case, so
    ``memory.md`` would reach ``MEMORY.md``."""
    return store.client == "claude" and isinstance(file, str) and file.lower() == INDEX.lower()


def inside(store: Store, path: Path) -> None:
    """Refuses a path that resolves outside its store, as one through a link swapped in after the
    per-step checks would; operations on the path still follow a swap made after this check."""
    resolved = path.resolve()
    if resolved != store.path and not resolved.is_relative_to(store.path):
        raise Refused("symlink_refused", f"{path.name} resolves outside {store.label}")


def frontmatter(text: str) -> tuple[dict, bool]:
    """The frontmatter mapping and whether it parsed; a file without one has ``{}`` and parses."""
    block, _ = split_frontmatter(text)
    if block is None:
        return {}, True
    try:
        value = yaml.safe_load(block)
    except (yaml.YAMLError, RecursionError, ValueError, OverflowError):  # also a date such as 2026-02-30
        return {}, False
    return (value, True) if isinstance(value, dict) else ({}, False)


def metadata_of(meta: dict) -> dict:
    return meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}


def field_text(value) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def timestamp_of(value) -> float | None:
    """A frontmatter time as a POSIX timestamp, or None; a time without a zone counts as UTC. A time
    before 1970 or after 9999, which ``datetime.fromtimestamp`` cannot always turn back, is None."""
    moment = _timestamp(value)
    return moment if moment is not None and 0 <= moment <= _LATEST else None


_LATEST = 253402214400.0  # 9999-12-31T00:00:00Z


def _timestamp(value) -> float | None:
    if isinstance(value, datetime):
        moment = value
    elif isinstance(value, date):
        moment = datetime(value.year, value.month, value.day)
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.strip())
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    try:
        return moment.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def fields(meta: dict) -> dict:
    """``name``, ``description``, ``type`` and ``modified`` of a frontmatter mapping; ``type`` and
    ``modified`` may sit at the top level, as Claude Code documents them, or under ``metadata``."""
    metadata = metadata_of(meta)
    return {"name": field_text(meta.get("name")), "description": field_text(meta.get("description")),
            "type": field_text(metadata.get("type", meta.get("type"))),
            "modified": field_text(metadata.get("modified", meta.get("modified")))}


def open_regular(path: Path, link_check: Callable[[Path], bool] = is_link):
    """``path`` opened for reading without following a symlink, also one that replaced the file since
    it was listed; a symlink, a junction and anything that is not a regular file are refused.

    ``O_NONBLOCK`` keeps the open itself from waiting: a FIFO under an item's name would otherwise
    hold ``O_RDONLY`` until some process opened it for writing. ``link_check`` lets a caller supply
    the link test it resolves at call time."""
    if link_check(path):
        raise Refused("symlink_refused", f"{path.name} is a symlink")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as error:
        if error.errno == errno.ELOOP:  # O_NOFOLLOW met a symlink
            raise Refused("symlink_refused", f"{path.name} is a symlink") from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused("not_a_file", f"{path.name} is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def open_in_store(store: Store, path: Path):
    """``path``, a file below ``store``, opened for reading only if the file that opens lies inside it.

    ``open_regular`` refuses a link in the last component only, and a directory above the file can be
    swapped for a link between a containment check and the open. On POSIX the file is therefore
    opened one component at a time from the store's directory, each with ``O_NOFOLLOW``; on Windows,
    which has no ``dir_fd``, the path the open handle really names must be the file's own path in the
    store. Both refuse a link anywhere below the store's root, also one into another project of the
    same store, whose note would otherwise be attributed to this project."""
    relative = Path(os.path.relpath(path, store.path))
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise Refused("symlink_refused", f"{path.name} lies outside {store.label}")
    if _DIR_FD:
        return _open_by_components(store, relative)
    stream = open_regular(path)
    final = _final_path(stream.fileno())
    if _WINDOWS and (final is None or not _same_path(final, store.path / relative)):
        stream.close()
        # A handle whose path cannot be read (some network shares) is not trusted either.
        raise Refused("symlink_refused", f"{path.name} is not verifiably the listed file in {store.label}")
    return stream


def _open_by_components(store: Store, relative: Path):
    flags = os.O_RDONLY | os.O_NOFOLLOW
    try:
        directory = os.open(store.path, flags | os.O_DIRECTORY)
    except OSError as error:
        if error.errno in (errno.ELOOP, errno.ENOTDIR):
            raise Refused("symlink_refused", f"{store.label} is a symlink") from None
        raise
    try:
        for part in relative.parts[:-1]:
            try:
                below = os.open(part, flags | os.O_DIRECTORY, dir_fd=directory)
            except OSError as error:
                if error.errno in (errno.ELOOP, errno.ENOTDIR):
                    raise Refused("symlink_refused", f"{part} is a symlink") from None
                raise
            os.close(directory)
            directory = below
        try:
            fd = os.open(relative.parts[-1], flags | getattr(os, "O_NONBLOCK", 0), dir_fd=directory)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise Refused("symlink_refused", f"{relative.name} is a symlink") from None
            raise
    finally:
        os.close(directory)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise Refused("not_a_file", f"{relative.name} is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def _final_path(fd: int) -> str | None:
    """The path an open file handle names after every link and junction (Windows), else None."""
    if not _WINDOWS:
        return None
    import ctypes
    import msvcrt
    from ctypes import wintypes
    function = ctypes.windll.kernel32.GetFinalPathNameByHandleW
    function.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    function.restype = wintypes.DWORD
    handle = msvcrt.get_osfhandle(fd)
    size = 1024
    while True:
        buffer = ctypes.create_unicode_buffer(size)
        length = function(handle, buffer, size, 0)
        if length == 0:
            return None
        if length < size:
            name = buffer.value
            if name.startswith("\\\\?\\UNC\\"):
                return "\\\\" + name[8:]
            return name[4:] if name.startswith("\\\\?\\") else name
        size = length + 1


def _same_path(path: str, expected: Path) -> bool:
    # ``expected`` joins the store as discovery resolved it (``client_dirs`` resolves every directory,
    # through the same final-path lookup as the handle) with the listed names, never resolved again
    # here: a root or directory swapped for a junction would resolve to the junction's target and pass.
    return os.path.normcase(os.path.normpath(path)) == os.path.normcase(os.path.normpath(str(expected)))


def refused_names(store: Store, memory: Path) -> list[str]:
    """Item-like names in a memory directory that are no regular file (a link, a junction, a FIFO, a
    directory): counted by a caller, never opened."""
    try:
        entries = list(os.scandir(memory))
    except (FileNotFoundError, NotADirectoryError):
        return []
    return sorted(entry.name for entry in entries
                  if is_item_name(store, entry.name) and not entry.is_file(follow_symlinks=False))


def item_names(store: Store, memory: Path) -> list[str]:
    """The item files of a memory directory, sorted; Claude Code's ``MEMORY.md`` is not an item."""
    try:
        entries = list(os.scandir(memory))
    except (FileNotFoundError, NotADirectoryError):
        return []
    return sorted(entry.name for entry in entries
                  if is_item_name(store, entry.name) and entry.is_file(follow_symlinks=False))


UNKNOWN_PROJECT = "?"  # the project of a place that could not be listed at all


def places(store: Store, *, names: Callable[[Store, Path], list[str]] = item_names,
           link_check: Callable[[Path], bool] = is_link, every_dir: bool = False,
           on_error: Callable[[str], None] | None = None) -> Iterator[tuple[str, Path]]:
    """``(project, memory dir)`` of every place in ``store`` that holds an item or an index.

    ``every_dir`` also yields Codex's ``memories_v2``; the Memory tab edits only ``memory_dir``.
    Without ``on_error`` a directory that cannot be listed raises; with it, the place's project
    (``UNKNOWN_PROJECT`` when ``projects/`` itself cannot be listed) is passed to it and the walk
    goes on with the other places."""
    candidates = [""]
    projects = store.path / "projects"
    if store.client == "claude" and not link_check(projects):
        try:
            candidates += sorted(entry.name for entry in os.scandir(projects)
                                 if entry.is_dir(follow_symlinks=False) and PROJECT.fullmatch(entry.name))
        except (FileNotFoundError, NotADirectoryError):
            pass
        except OSError:
            if on_error is None:
                raise
            on_error(UNKNOWN_PROJECT)
    for project in candidates:
        for memory in (store.memory_dirs(project) if every_dir else [store.memory_dir(project)]):
            try:
                if (project and link_check(memory.parent)) or link_check(memory) or not memory.is_dir():
                    continue
                found = names(store, memory) or index_file(store, memory, link_check)
            except OSError:
                if on_error is None:
                    raise
                on_error(project)
                continue
            if found:
                yield project, memory
