"""The Memory tab of the web UI (#297): read, edit, create, delete, copy and move file-based memory.

Claude Code keeps durable memory per project in ``<config dir>/projects/<project>/memory/``: one
Markdown file per item, with YAML frontmatter (``name``, ``description``, ``metadata.type``,
``metadata.modified``), and the index ``MEMORY.md``, one ``- [Title](file.md) — hook`` line per item,
which a session loads at its start. ``<project>`` is the project's path with every character but
letters and digits replaced by ``-``. One machine can run Claude Code from several configuration
directories (``client_dirs.py``), each with memory of its own, so the tab shows them side by side and
copies or moves an item between them. The top-level ``<config dir>/memory/``, where the installer puts
the routing reminder, is listed as outside any project. Codex's ``$CODEX_HOME/memories/`` is listed
when it holds files; Cursor keeps no memory in files.

Every place comes from that discovery, never from a request: a request names a configuration
directory the discovery found (``store``), a directory that exists under its ``projects/``
(``project``; empty for the top-level memory) and a plain file name other than ``MEMORY.md``, which
has routes of its own. Symlinks and Windows junctions are refused, and the resolved path must stay
inside the memory directory.

Claude Code writes memory without a lock. A write carries the SHA-256 of the version the page showed
and is refused (409 ``file_changed``) when the file differs right before the atomic rename, so only a
session writing in that very instant can still win or lose; a create never replaces a file that
appeared meanwhile. A delete renames the file aside before it checks the version, so a file written
under the name afterwards survives, but on POSIX a session still writing through a handle it opened
earlier writes into the file being deleted. A deleted item goes to ``memory-trash/`` in the service's
state directory, from where it can be restored by hand. Files are read without following a symlink
that replaced them after they were listed (``O_NOFOLLOW``; on Windows the check right before opening).

An item's time (``changed``) is its ``metadata.modified`` and, without a readable one, the file's.

- ``GET /ui/api/memory/projects``: the configuration directories, one row per project merged across
  them (its places with item counts and newest item), and notes on the clients without memory.
- ``GET /ui/api/memory?store=&project=``: one memory directory: its items with text, frontmatter
  fields and SHA-256, the index ``MEMORY.md``, the items it does not link and the links to missing
  files, the other places of this project and the directories an item can be copied to.
- ``GET /ui/api/memory/file?store=&project=&file=``: one file's text and SHA-256.
- ``PUT /ui/api/memory/file`` with ``{store, project, file, text, sha256, index?}``: writes the file;
  ``sha256: null`` creates it (409 ``file_exists`` when it exists) and ``index: true`` adds its line
  to ``MEMORY.md``.
- ``POST /ui/api/memory/delete`` with ``{store, project, file, sha256}``: moves the file to the trash
  and removes the index lines that link to it.
- ``POST /ui/api/memory/copy`` with ``{store, project, file, sha256, to_store, move?, replace_sha256?}``:
  copies the item into the same project of another Claude configuration directory, with its index
  line; 409 ``target_exists`` (carrying the target's text and SHA-256) unless ``replace_sha256`` names
  the target as it is. ``move`` deletes the source afterwards, as Delete does.
- ``GET /ui/api/memory/search?q=``: per project, the items that hold every term.
"""
from __future__ import annotations

import asyncio
import errno
import hashlib
import json
import os
import re
import secrets
import stat
import tempfile
import threading
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import yaml
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.environments import is_link
from src.utils.prompt_loader import split_frontmatter
from .client_dirs import claude_config_dirs, codex_homes
from .state import atomic_private, private_dir, read_json

PREFIX = "/ui/api/memory"
INDEX = "MEMORY.md"
TRASH = "memory-trash"
MAX_EDIT = 256 * 1024  # larger files are shown, not edited
MAX_SHOW = 1024 * 1024  # larger files are listed without their text
HEAD = 64 * 1024  # what is read of a larger file for its frontmatter
MAX_BODY = 4 * MAX_EDIT  # a JSON body escapes the text
FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")  # 255 characters, the usual file name limit
PROJECT = re.compile(r"[A-Za-z0-9._-]{1,255}")
LINK = re.compile(r"^\s*[-*+]\s+\[(?P<title>[^\]]*)\]\((?P<file>[^)\s]+)\)")
SANITIZE = re.compile(r"[^A-Za-z0-9]")
DEFAULT_MODE = 0o644
CACHE_LIMIT = 10_000  # remembered item times; a list that outgrows it starts over
# A file system without hard links (FAT, some network shares) answers os.link with these; Windows
# with ERROR_INVALID_FUNCTION or ERROR_NOT_SUPPORTED.
NO_LINKS = {errno.EPERM, errno.ENOTSUP, errno.EOPNOTSUPP, errno.ENOSYS}
NO_LINKS_WINDOWS = {1, 50}

WRITES = {PREFIX + "/file": "PUT", PREFIX + "/delete": "POST", PREFIX + "/copy": "POST"}
READS = (PREFIX, PREFIX + "/projects", PREFIX + "/file", PREFIX + "/search")


def is_memory_path(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


class MemoryRefused(Exception):
    """A refused request: ``code`` names it, ``status`` is the HTTP status, ``extra`` goes to the page."""

    def __init__(self, code: str, message: str, status: int = 400, **extra):
        super().__init__(message)
        self.code, self.status, self.extra = code, status, extra


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
        if self.client == "codex":
            return self.path / "memories"
        return self.path / "memory" if project == "" else self.path / "projects" / project / "memory"


def _label(path: Path, home: Path) -> str:
    try:
        return "~/" + path.relative_to(home).as_posix()
    except ValueError:
        return str(path)


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="seconds")


def _item_name(store: Store, name: str) -> bool:
    """A plain file name; Claude Code's items are Markdown files other than its index."""
    return (isinstance(name, str) and FILE.fullmatch(name) is not None and name not in (".", "..")
            and (store.client != "claude" or (name.endswith(".md") and not _is_index(store, name))))


def _index(store: Store, memory: Path) -> Path | None:
    """Claude Code's ``MEMORY.md`` of a memory directory when it is a plain file; Codex keeps no index."""
    index = memory / INDEX
    return index if store.client == "claude" and not is_link(index) and index.is_file() else None


def _is_index(store: Store, file) -> bool:
    """Claude Code's index name in any case: Windows and macOS file systems usually ignore case, so
    ``memory.md`` would reach ``MEMORY.md``."""
    return store.client == "claude" and isinstance(file, str) and file.lower() == INDEX.lower()


def _inside(store: Store, path: Path) -> None:
    """Refuses a path that resolves outside its store, as one through a link swapped in after the
    per-step checks would; operations on the path still follow a swap made after this check."""
    resolved = path.resolve()
    if resolved != store.path and not resolved.is_relative_to(store.path):
        raise MemoryRefused("symlink_refused", f"{path.name} resolves outside {store.label}")


def _frontmatter(text: str) -> tuple[dict, bool]:
    """The frontmatter mapping and whether it parsed; a file without one has ``{}`` and parses."""
    block, _ = split_frontmatter(text)
    if block is None:
        return {}, True
    try:
        value = yaml.safe_load(block)
    except yaml.YAMLError:
        return {}, False
    return (value, True) if isinstance(value, dict) else ({}, False)


def _metadata(meta: dict) -> dict:
    return meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}


def _field(value) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


def _when(value) -> float | None:
    """A frontmatter time as a POSIX timestamp, or None; a time without a zone counts as UTC."""
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


def links(text: str) -> list[dict]:
    """The index lines that link a file of the same directory, in order: ``{file, title, line}``."""
    found = []
    for number, line in enumerate(text.splitlines(), 1):
        match = LINK.match(line)
        if match and "/" not in match.group("file") and ":" not in match.group("file"):
            found.append({"file": match.group("file"), "title": match.group("title"), "line": number})
    return found


def _without_links(text: str, file: str) -> str:
    return "".join(line for line in text.splitlines(keepends=True)
                   if not ((match := LINK.match(line)) and match.group("file") == file))


def _with_line(text: str, line: str) -> str:
    if text and not text.endswith("\n"):
        text += "\n"
    return text + line.rstrip("\r\n") + "\n"


def _index_line(file: str, text: str) -> str:
    meta, _ = _frontmatter(text)
    title = _field(meta.get("name")) or (file[:-3] if file.endswith(".md") else file)
    description = _field(meta.get("description"))
    return f"- [{title}]({file})" + (f" — {description}" if description else "")


def _links_unsupported(error: OSError) -> bool:
    return error.errno in NO_LINKS or getattr(error, "winerror", None) in NO_LINKS_WINDOWS


def _open(path: Path):
    """``path`` opened for reading without following a symlink, also one that replaced the file since
    it was listed; a symlink, a junction and anything that is not a regular file are refused.

    ``O_NONBLOCK`` keeps the open itself from waiting. A FIFO under an item's name would otherwise
    hold ``O_RDONLY`` until some process opened it for writing, and a save reads the file it is about
    to replace while it holds the page's lock, so one such entry would stall every later write."""
    if is_link(path):
        raise MemoryRefused("symlink_refused", f"{path.name} is a symlink")
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
                     | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as error:
        if error.errno == errno.ELOOP:  # O_NOFOLLOW met a symlink
            raise MemoryRefused("symlink_refused", f"{path.name} is a symlink") from None
        raise
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise MemoryRefused("not_a_file", f"{path.name} is not a regular file")
    except BaseException:
        os.close(fd)
        raise
    return os.fdopen(fd, "rb")


def _read(path: Path) -> bytes:
    with _open(path) as stream:
        return stream.read()


def _create_exclusively(path: Path, data: bytes, mode: int) -> None:
    """Creates ``path`` on a file system that has no hard links, without replacing anything.

    ``O_EXCL`` keeps the create contract that a file which appeared since the check is never lost; a
    rename would overwrite it. Without hard links nothing installs a finished file under a new name
    at once, so the content goes into the final name and a reader can see the item while it is still
    short, which the hard-link path avoids. A failure gives the name back through _undo_create,
    which takes it only while the bytes under it are the ones this call meant to write; a name that
    holds anything else, its own partial write included, is left for its owner, who sees that item
    next to the error it came with. The identity of the file read here goes with those bytes rather
    than instead of them: on its own it is still this call's own after another process has written
    into the file, while the bytes on their own would match a different file carrying the same
    content. The mode goes through the descriptor, which a symlink put in the way
    after the create cannot redirect.
    """
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), mode)
    except FileExistsError:
        raise MemoryRefused("file_exists", f"{path.name} already exists", 409) from None
    try:
        created = os.fstat(fd)  # the identity the undo compares beside the bytes, where it reads
    except OSError:
        created = None
    try:
        stream = os.fdopen(fd, "wb")
    except BaseException:
        try:
            os.close(fd)
        except OSError:  # fdopen may have closed it on its way out
            pass
        _undo_create(path, data, created)
        raise
    try:
        with stream:
            if hasattr(os, "fchmod"):  # Windows has none; there the mode of os.open stands
                os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except BaseException:
        _undo_create(path, data, created)
        raise


def _replace(path: Path, data: bytes, expected: str | None) -> None:
    """Writes ``path`` through a temporary file and a rename, keeping its mode.

    ``expected`` is the SHA-256 the file must still have, checked right before the rename. The check
    and the rename are two steps, so a session writing between them loses its version. ``None`` means
    the file must not exist: it is then installed with a hard link, which fails when any file took the
    name meanwhile, and only a file system without hard links falls back to an exclusive create.
    """
    try:
        mode = os.lstat(path).st_mode & 0o777
    except FileNotFoundError:
        mode = DEFAULT_MODE
    # Not named after the file: a name near the 255-character limit must still fit.
    fd, temporary = tempfile.mkstemp(prefix=".memory-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        _check(path, expected)
        if expected is not None:
            os.replace(temporary, path)
            return
        try:
            os.link(temporary, path)
        except FileExistsError:
            raise MemoryRefused("file_exists", f"{path.name} already exists", 409) from None
        except OSError as error:
            if not _links_unsupported(error):
                raise
            _create_exclusively(path, data, mode)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _utf8(raw: bytes, name: str) -> str:
    """The text of a file about to be rewritten; text that is not UTF-8 would lose its bytes."""
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        raise MemoryRefused("not_utf8", f"{name} is not valid UTF-8: shown, not edited") from None


def _editable(raw: bytes) -> bool:
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return len(raw) <= MAX_EDIT


def _rewritable(raw: bytes, name: str) -> str:
    """The text of a file about to be rewritten; a file shown but not editable is refused."""
    if len(raw) > MAX_EDIT:
        raise MemoryRefused("file_too_large", f"{name} is over {MAX_EDIT // 1024} KB: shown, not edited")
    return _utf8(raw, name)


def _fields(meta: dict) -> dict:
    metadata = _metadata(meta)
    return {"name": _field(meta.get("name")), "description": _field(meta.get("description")),
            "type": _field(metadata.get("type", meta.get("type"))),
            "modified": _field(metadata.get("modified", meta.get("modified")))}


def _warning(error: Exception) -> str:
    """A refusal or storage error that came after the change itself was done, for the answer."""
    if isinstance(error, MemoryRefused):
        return f"{error.code}: {error}"
    return f"storage_error: {getattr(error, 'strerror', None) or error}"


def _undo_create(path: Path, data: bytes, created: os.stat_result | None = None) -> None:
    """Removes a file this request just created, unless something changed it since.

    The bytes under the name must still be the ones this request wrote. ``created``, the identity
    of the file it created, has to match as well wherever the caller holds it: equal bytes alone
    would take down a file another session installed under the name whose content happens to be
    the same, which is likeliest when that content is empty. Neither test replaces the other. The
    bytes catch a file written into in place, where the identity is still this request's own, and
    the identity catches a different file that carries the same bytes. An unset ``st_ino``, which
    some file systems without hard links report for every file alike, identifies nothing: there
    the bytes would decide alone, which is the case the identity is here to cover, so nothing is
    removed instead.

    What neither test closes is the gap between the last check and the unlink: POSIX has no
    operation that removes a name only while it still holds a given file, so a replacement landing
    inside that gap is removed. It stays open deliberately, because the alternative is to never
    undo, which leaves every failed create's own item under its name instead.
    """
    try:
        if _read(path) != data:
            return
        if created is not None:
            if not created.st_ino:
                return  # a file system that numbers no file cannot tell two of them apart
            current = os.lstat(path)
            if (current.st_dev, current.st_ino) != (created.st_dev, created.st_ino):
                return
        path.unlink()
    except (OSError, MemoryRefused):
        pass  # the request fails anyway with the error that made it undo; this file stays


def _check(path: Path, expected: str | None) -> None:
    try:
        current = _read(path)
    except FileNotFoundError:
        current = None
    if expected is None and current is not None:
        raise MemoryRefused("file_exists", f"{path.name} already exists", 409, sha256=_sha(current))
    if expected is not None and (current is None or _sha(current) != expected):
        raise MemoryRefused("file_changed", f"{path.name} changed since it was shown", 409)


class MemoryUI:
    def __init__(self, service, *, home=None, environ=None):
        self.service = service
        self.home, self.environ = home, environ
        self._lock = threading.Lock()  # one change at a time from this page
        self._times = {}  # path -> (mtime_ns, size, its time): frontmatter is read again only after a change

    # --- requests ---------------------------------------------------------------------

    async def handle(self, request: Request, path: str):
        """Answers one ``/ui/api/memory…`` request; FlowsUI has checked the session and the headers."""
        if path not in READS and path not in WRITES:
            return self._json({"error": "not_found"}, 404)
        if request.method == WRITES.get(path):
            raw = await request.body()
            try:
                body = json.loads(raw or b"{}") if len(raw) <= MAX_BODY else None
            except ValueError:
                body = None
            if not isinstance(body, dict):
                return self._json({"status": "error", "error": "invalid_request: the body must be a JSON object"}, 400)
            work = {PREFIX + "/file": self.save, PREFIX + "/delete": self.delete, PREFIX + "/copy": self.copy}[path]
            return await self._run(lambda: work(body))
        if request.method != "GET" or path not in READS:
            allowed = ", ".join(method for method in ("GET" if path in READS else None, WRITES.get(path)) if method)
            return self._json({"error": "method_not_allowed"}, 405, headers={"Allow": allowed})
        query = request.query_params
        reads = {PREFIX + "/projects": lambda: self.projects(),
                 PREFIX: lambda: self.read(query.get("store", ""), query.get("project", "")),
                 PREFIX + "/file": lambda: self.file(query.get("store", ""), query.get("project", ""),
                                                     query.get("file", "")),
                 PREFIX + "/search": lambda: self.search(query.get("q", ""))}
        return await self._run(reads[path])

    async def _run(self, work):
        try:
            return self._json(await asyncio.to_thread(work))
        except MemoryRefused as error:
            return self._json({"status": "error", "error": f"{error.code}: {error}", **error.extra}, error.status)
        except OSError as error:
            return self._json({"status": "error", "error": f"storage_error: {error.strerror or error}"}, 500)
        except ValueError as error:  # a state directory or trash path that turned into a symlink
            return self._json({"status": "error", "error": f"storage_error: {error}"}, 500)

    @staticmethod
    def _json(value, status=200, **kwargs):
        headers = {"Cache-Control": "no-store", **kwargs.pop("headers", {})}
        return JSONResponse(value, status, headers=headers, **kwargs)

    # --- places -----------------------------------------------------------------------

    def stores(self) -> list[Store]:
        """The configuration directories with memory, Claude Code's first, from ``client_dirs``."""
        home = Path(self.home or Path.home()).resolve()
        directory = getattr(self.service, "directory", None)
        found = [Store(path, "claude", _label(path, home))
                 for path in claude_config_dirs(directory, home=self.home, environ=self.environ)]
        found += [Store(path, "codex", _label(path, home))
                  for path in codex_homes(directory, home=self.home, environ=self.environ)]
        return found

    def _store(self, store: str) -> Store:
        for candidate in self.stores():
            if candidate.id == store:
                return candidate
        raise MemoryRefused("store_unknown", "no such configuration directory with memory")

    @staticmethod
    def _dir(store: Store, project: str, create: bool = False) -> Path:
        """The memory directory of ``project`` in ``store``; ``create`` makes a project's when missing."""
        if not isinstance(project, str) or (project and (not PROJECT.fullmatch(project) or project in (".", ".."))):
            raise MemoryRefused("project_invalid", "project must name a directory under projects/")
        if store.client == "codex" and project:
            raise MemoryRefused("project_invalid", "Codex keeps its memory outside projects")
        memory = store.memory_dir(project)
        steps = [memory.parent.parent, memory.parent, memory] if project else [memory]
        for step in steps:
            if is_link(step):
                raise MemoryRefused("symlink_refused", f"{step.name} is a symlink")
        if project and not create and not memory.parent.is_dir():
            raise MemoryRefused("project_not_found", "no such project in this configuration directory", 404)
        if create:
            # One step at a time: mkdir never creates through a link, and a link that took a step's
            # name since the check above makes it fail.
            for step in steps:
                try:
                    os.mkdir(step)
                except FileExistsError:
                    pass
                if is_link(step) or not step.is_dir():
                    raise MemoryRefused("symlink_refused", f"{step.name} is a symlink")
        _inside(store, memory)
        return memory

    def _file(self, store: Store, project: str, file: str, create_dir: bool = False) -> Path:
        if not _item_name(store, file):
            raise MemoryRefused("file_invalid", "file must be a plain file name"
                                + (" ending in .md" if store.client == "claude" else ""))
        memory = self._dir(store, project, create=create_dir)
        path = memory / file
        if is_link(path):
            raise MemoryRefused("symlink_refused", f"{file} is a symlink")
        if path.resolve().parent != memory.resolve():
            raise MemoryRefused("file_invalid", f"{file} is outside the memory directory")
        _inside(store, path)
        return path

    @staticmethod
    def _names(store: Store, memory: Path) -> list[str]:
        """The item files of a memory directory, sorted; Claude Code's ``MEMORY.md`` is not an item."""
        try:
            entries = list(os.scandir(memory))
        except (FileNotFoundError, NotADirectoryError):
            return []
        return sorted(entry.name for entry in entries
                      if _item_name(store, entry.name) and entry.is_file(follow_symlinks=False))

    def _places(self, store: Store):
        """``(project, memory dir)`` of every place in ``store`` that holds an item or an index."""
        candidates = [""]
        projects = store.path / "projects"
        if store.client == "claude" and not is_link(projects):
            try:
                candidates += sorted(entry.name for entry in os.scandir(projects)
                                     if entry.is_dir(follow_symlinks=False) and PROJECT.fullmatch(entry.name))
            except (FileNotFoundError, NotADirectoryError):
                pass
        for project in candidates:
            memory = store.memory_dir(project)
            if (project and is_link(memory.parent)) or is_link(memory) or not memory.is_dir():
                continue
            if self._names(store, memory) or _index(store, memory):
                yield project, memory

    def _workspaces(self) -> dict[str, tuple[str, str]]:
        """Registered workspaces by Claude Code's project directory name: ``{name: (id, root)}``."""
        registry = getattr(self.service, "registry", None)
        try:
            records = read_json(registry.path, {}) if registry is not None else {}
        except (OSError, ValueError):
            records = {}
        if not isinstance(records, dict):
            return {}
        return {SANITIZE.sub("-", root): (identity, root) for identity, root in records.items()
                if isinstance(identity, str) and isinstance(root, str)}

    @staticmethod
    def _key(store: Store, project: str) -> str:
        return store.client + ":" + project

    def _row(self, store: Store, project: str, workspaces) -> dict:
        workspace, root = workspaces.get(project, (None, None)) if store.client == "claude" else (None, None)
        if store.client == "codex":
            name = "Codex memories"
        elif project == "":
            name = "Outside projects"
        else:
            name = Path(root).name if root else project
        return {"key": self._key(store, project), "client": store.client, "project": project, "name": name,
                "root": root, "workspace": workspace, "places": [], "items": 0, "newest": None}

    def _remember(self, path: Path, stat: os.stat_result, meta: dict) -> float:
        """An item's time: its ``metadata.modified``, else the file's; kept until the file changes."""
        moment = _when(_metadata(meta).get("modified", meta.get("modified")))
        moment = stat.st_mtime if moment is None else moment
        if len(self._times) >= CACHE_LIMIT:
            self._times.clear()
        self._times[str(path)] = (stat.st_mtime_ns, stat.st_size, moment)
        return moment

    def _changed(self, path: Path) -> float:
        stat = os.lstat(path)
        cached = self._times.get(str(path))
        if cached and cached[:2] == (stat.st_mtime_ns, stat.st_size):
            return cached[2]
        with _open(path) as stream:
            head = stream.read(HEAD if stat.st_size > MAX_SHOW else -1)
        meta, _ = _frontmatter(head.decode("utf-8", errors="replace"))
        return self._remember(path, stat, meta)

    # --- reads ------------------------------------------------------------------------

    def projects(self) -> dict:
        """``GET /ui/api/memory/projects``: one row per project, merged across directories, newest first."""
        stores, workspaces, rows = self.stores(), self._workspaces(), {}
        for store in stores:
            for project, memory in self._places(store):
                names = self._names(store, memory)
                times = []
                for name in names:
                    try:
                        times.append(self._changed(memory / name))
                    except (OSError, MemoryRefused):  # gone or turned into a link since it was listed
                        pass
                newest = _iso(max(times)) if times else None
                row = rows.setdefault(self._key(store, project), self._row(store, project, workspaces))
                row["places"].append({"store": store.id, "label": store.label, "items": len(names), "newest": newest})
                row["items"] += len(names)
                row["newest"] = max(filter(None, (row["newest"], newest)), default=None)
        out = sorted(rows.values(), key=lambda row: row["name"].lower())
        out.sort(key=lambda row: row["newest"] or "", reverse=True)
        notes = []
        if not any(row["client"] == "codex" for row in out):
            notes.append("Codex: no memory files in $CODEX_HOME/memories (its memories feature may be off).")
        notes.append("Cursor keeps no memory in files.")
        return {"stores": [{"store": store.id, "label": store.label, "client": store.client} for store in stores],
                "projects": out, "notes": notes}

    def _item(self, path: Path) -> dict:
        with _open(path) as stream:
            stat = os.fstat(stream.fileno())
            item = {"file": path.name, "bytes": stat.st_size, "mtime": _iso(stat.st_mtime), "changed": None,
                    "editable": stat.st_size <= MAX_EDIT, "text": None, "sha256": None, "name": None,
                    "description": None, "type": None, "modified": None, "frontmatter": True}
            if stat.st_size > MAX_SHOW:  # listed and deletable, not shown; its frontmatter still counts
                head = stream.read(HEAD)
                digest = hashlib.sha256(head)
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(chunk)
                meta, _ = _frontmatter(head.decode("utf-8", errors="replace"))
                item.update(sha256=digest.hexdigest(), changed=_iso(self._remember(path, stat, meta)), **_fields(meta))
                return item
            raw = stream.read()
        text = raw.decode("utf-8", errors="replace")
        meta, parsed = _frontmatter(text)
        # Strictly decoded: a file that holds U+FFFD as text is still valid UTF-8 and editable.
        item.update(text=text, sha256=_sha(raw), frontmatter=parsed, changed=_iso(self._remember(path, stat, meta)),
                    editable=_editable(raw), **_fields(meta))
        return item

    def read(self, store: str, project: str) -> dict:
        """``GET /ui/api/memory``: one memory directory with its items, index and copy targets."""
        place = self._store(store)
        memory = self._dir(place, project)
        items = []
        for name in self._names(place, memory):
            try:
                items.append(self._item(memory / name))
            except (OSError, MemoryRefused):  # gone or turned into a link since it was listed
                items.append({"file": name, "unreadable": True})
        index = {"exists": False, "sha256": None, "text": None, "links": [], "editable": True}
        if _index(place, memory):
            raw = _read(memory / INDEX)
            text = raw.decode("utf-8", errors="replace")
            index = {"exists": True, "sha256": _sha(raw), "text": text, "links": links(text), "editable": _editable(raw)}
        linked = {link["file"] for link in index["links"]}
        present = {item["file"] for item in items}
        others = [candidate for candidate in self.stores() if candidate != place]
        places = [{"store": place.id, "label": place.label}]
        for candidate in others:
            if candidate.client == place.client:
                try:
                    memory_dir = self._dir(candidate, project)
                    holds = bool(self._names(candidate, memory_dir) or _index(candidate, memory_dir))
                except MemoryRefused:
                    holds = False
                if holds:
                    places.append({"store": candidate.id, "label": candidate.label})
        row = self._row(place, project, self._workspaces())
        return {"store": place.id, "label": place.label, "client": place.client, "project": project,
                "key": row["key"], "name": row["name"], "root": row["root"], "dir": str(memory),
                "items": items, "index": index,
                "not_indexed": [item["file"] for item in items if item["file"] not in linked],
                "broken": [link for link in index["links"] if link["file"] not in present],
                "places": places,
                "targets": [{"store": candidate.id, "label": candidate.label}
                            for candidate in others if candidate.client == "claude" and place.client == "claude"]}

    def file(self, store: str, project: str, file: str) -> dict:
        """``GET /ui/api/memory/file``: one file's text and SHA-256."""
        place = self._store(store)
        path = self._dir(place, project) / INDEX if _is_index(place, file) else self._file(place, project, file)
        if is_link(path) or not path.is_file():
            raise MemoryRefused("file_not_found", f"{file} does not exist here", 404)
        raw = _read(path)
        return {"store": place.id, "project": project, "file": file, "text": raw.decode("utf-8", errors="replace"),
                "sha256": _sha(raw), "editable": _editable(raw)}

    def search(self, query: str) -> dict:
        """``GET /ui/api/memory/search``: per project, the items whose name or text holds every term."""
        terms = [term for term in query.lower().split() if term]
        if not terms:
            return {"q": query, "projects": []}
        counts, workspaces = {}, self._workspaces()
        for store in self.stores():
            for project, memory in self._places(store):
                row = self._row(store, project, workspaces)
                prefix = "\n".join(filter(None, (row["name"], row["root"], project)))
                for name in self._names(store, memory):
                    try:
                        if os.lstat(memory / name).st_size > MAX_SHOW:
                            continue
                        text = _read(memory / name).decode("utf-8", errors="replace")
                    except (OSError, MemoryRefused):
                        continue
                    if all(term in (prefix + "\n" + name + "\n" + text).lower() for term in terms):
                        counts[row["key"]] = counts.get(row["key"], 0) + 1
        return {"q": query, "projects": [{"key": key, "matches": count} for key, count in counts.items()]}

    # --- writes -----------------------------------------------------------------------

    @staticmethod
    def _text(body: dict) -> bytes:
        text = body.get("text")
        if not isinstance(text, str):
            raise MemoryRefused("invalid_request", "text must be a string")
        data = text.encode("utf-8")
        if len(data) > MAX_EDIT:
            raise MemoryRefused("file_too_large", f"a memory file is limited to {MAX_EDIT // 1024} KB")
        return data

    @staticmethod
    def _expected(body: dict, key: str = "sha256", required: bool = True) -> str | None:
        value = body.get(key)
        if value is None and not required:
            return None
        if not isinstance(value, str) or not re.fullmatch(r"[0-9a-f]{64}", value):
            raise MemoryRefused("invalid_request", f"{key} must be the SHA-256 of the version shown")
        return value

    def save(self, body: dict) -> dict:
        """``PUT /ui/api/memory/file``: writes the version shown, or creates a file when ``sha256`` is null."""
        place = self._store(body.get("store", ""))
        project, file = body.get("project", ""), body.get("file", "")
        data = self._text(body)
        creating = body.get("sha256") is None
        expected = self._expected(body, required=not creating)
        with self._lock:
            path = self._dir(place, project) / INDEX if _is_index(place, file) else self._file(place, project, file)
            if not path.parent.is_dir():
                raise MemoryRefused("project_not_found", "this memory directory does not exist", 404)
            if not creating and not is_link(path) and path.is_file():
                if os.lstat(path).st_size > MAX_EDIT:
                    raise MemoryRefused("file_too_large", f"{path.name} is over {MAX_EDIT // 1024} KB: shown, not edited")
                _rewritable(_read(path), path.name)
            index = creating and body.get("index") is True and place.client == "claude" and not _is_index(place, file)
            if index and is_link(path.parent / INDEX):
                raise MemoryRefused("symlink_refused", f"{INDEX} is a symlink")
            _replace(path, data, expected)
            indexed = False
            if index:
                try:
                    indexed = self._index_add(path.parent, file, _index_line(file, data.decode("utf-8")))
                except BaseException:
                    _undo_create(path, data)  # a refused request leaves no unindexed item behind
                    raise
        return {"status": "created" if creating else "saved", "store": place.id, "project": project, "file": file,
                "sha256": _sha(data), "indexed": indexed}

    def _index_add(self, memory: Path, file: str, line: str) -> bool:
        """Appends ``line`` to the index unless it already links ``file``; True when it was added."""
        index = memory / INDEX
        if is_link(index):
            raise MemoryRefused("symlink_refused", f"{INDEX} is a symlink")
        try:
            raw = _read(index)
        except FileNotFoundError:
            raw = None
        text = _rewritable(raw, INDEX) if raw is not None else ""
        if any(link["file"] == file for link in links(text)):
            return False
        _replace(index, _with_line(text, line).encode("utf-8"), _sha(raw) if raw is not None else None)
        return True

    def _index_remove(self, memory: Path, file: str) -> bool:
        index = memory / INDEX
        if is_link(index) or not index.is_file():
            return False
        raw = _read(index)
        text = _rewritable(raw, INDEX)
        changed = _without_links(text, file)
        if changed == text:
            return False
        _replace(index, changed.encode("utf-8"), _sha(raw))
        return True

    def _trash(self, place: Store, project: str, path: Path, data: bytes) -> Path:
        """Keeps a deleted file in the service's state directory and returns where."""
        root = private_dir(Path(self.service.directory) / TRASH)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
        target = root / stamp / SANITIZE.sub("-", place.label).strip("-") / (project or "_top") / path.name
        atomic_private(target, data)
        return target

    def _remove(self, place: Store, project: str, path: Path, expected: str) -> dict:
        """Deletes ``path`` when it is still the version shown, keeping a copy in the trash.

        The file is renamed aside before its version is checked: a version Claude Code writes after
        that rename lands under the original name and is never unlinked. The name aside does not
        embed the file's, so a name near the 255-character limit still fits.
        """
        aside = path.with_name(f".deleting-{secrets.token_hex(8)}")
        try:
            os.rename(path, aside)
        except FileNotFoundError:
            raise MemoryRefused("file_changed", f"{path.name} no longer exists", 409) from None
        try:
            data = _read(aside)
            if _sha(data) != expected:
                raise MemoryRefused("file_changed", f"{path.name} changed since it was shown", 409)
            kept = self._trash(place, project, path, data)
        except BaseException:
            self._put_back(place, project, aside, path)
            raise
        aside.unlink()
        result = {"trash": str(kept), "unindexed": False}
        try:
            result["unindexed"] = self._index_remove(path.parent, path.name)
        except (MemoryRefused, OSError) as error:  # the delete is done: say why MEMORY.md still links the file
            result["index_warning"] = _warning(error)
        return result

    def _put_back(self, place: Store, project: str, aside: Path, path: Path) -> None:
        """Returns a file renamed aside to its name, unless a newer file took the name meanwhile: then
        the one aside goes to the trash rather than over the newer one."""
        try:
            os.link(aside, path)
        except FileExistsError:
            self._trash(place, project, path, _read(aside))
        except OSError as error:
            if not _links_unsupported(error):
                raise  # the file stays aside, under its hidden name
            if os.path.lexists(path):
                self._trash(place, project, path, _read(aside))
            else:
                os.replace(aside, path)
                return
        aside.unlink()

    def delete(self, body: dict) -> dict:
        """``POST /ui/api/memory/delete``: moves an item to the trash and removes its index lines."""
        place = self._store(body.get("store", ""))
        project, file = body.get("project", ""), body.get("file", "")
        expected = self._expected(body)
        with self._lock:
            path = self._file(place, project, file)
            result = self._remove(place, project, path, expected)
        return {"status": "deleted", "store": place.id, "project": project, "file": file, **result}

    def copy(self, body: dict) -> dict:
        """``POST /ui/api/memory/copy``: copies or moves an item to another Claude configuration directory."""
        source = self._store(body.get("store", ""))
        target = self._store(body.get("to_store", ""))
        project, file, move = body.get("project", ""), body.get("file", ""), body.get("move") is True
        if source.client != "claude" or target.client != "claude":
            raise MemoryRefused("invalid_request", "items are copied between Claude Code configuration directories")
        if source == target:
            raise MemoryRefused("invalid_request", "the target is the item's own configuration directory")
        expected = self._expected(body)
        replace = self._expected(body, "replace_sha256", required=False)
        with self._lock:
            path = self._file(source, project, file)
            try:
                data = _read(path)
            except FileNotFoundError:
                raise MemoryRefused("file_changed", f"{file} no longer exists", 409) from None
            if _sha(data) != expected:
                raise MemoryRefused("file_changed", f"{file} changed since it was shown", 409)
            destination = self._file(target, project, file, create_dir=True)
            try:
                existing = _read(destination)
            except FileNotFoundError:
                existing = None
            if existing is not None and replace is None:
                raise MemoryRefused("target_exists", f"{target.label} already has {file}", 409,
                                    target={"store": target.id, "label": target.label, "sha256": _sha(existing),
                                            "text": existing.decode("utf-8", errors="replace")})
            if existing is None and replace is not None:
                raise MemoryRefused("target_changed", f"{file} is no longer in {target.label}", 409)
            if existing is not None:  # Replace rewrites the target; the copy itself keeps its bytes
                _rewritable(existing, f"{file} in {target.label}")
            # The source's index line is read before anything is written: a failure here changes nothing.
            source_index = _index(source, path.parent)
            line = None
            if source_index:
                index_text = _read(source_index).decode("utf-8", errors="replace")
                line = next((raw for raw in index_text.splitlines()
                             if (match := LINK.match(raw)) and match.group("file") == file), None)
            _replace(destination, data, replace)
            text = data.decode("utf-8", errors="replace")
            result = {"status": "copied", "store": source.id, "project": project, "file": file,
                      "to_store": target.id, "indexed": False, "replaced": existing is not None}
            try:
                result["indexed"] = self._index_add(destination.parent, file, line or _index_line(file, text))
            except (MemoryRefused, OSError) as error:
                if existing is None:  # a new item that cannot join the index is not left behind
                    _undo_create(destination, data)
                    raise
                result["index_warning"] = _warning(error)  # the replacement is done
            except BaseException:
                if existing is None:
                    _undo_create(destination, data)
                raise
            if move:
                try:
                    removed = self._remove(source, project, path, expected)
                except (MemoryRefused, OSError) as error:  # the copy is done; the source stays where it was
                    result["move_warning"] = _warning(error)
                else:
                    if "index_warning" in removed:  # about the source's MEMORY.md, not the target's
                        removed["source_index_warning"] = removed.pop("index_warning")
                    result.update(removed, status="moved")
        return result
