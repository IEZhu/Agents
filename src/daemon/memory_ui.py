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
(``project``; empty for the top-level memory) and a plain file name. Symlinks are refused.

Claude Code writes memory without a lock. A write carries the SHA-256 of the version the page showed
and is refused (409 ``file_changed``) when the file differs right before the atomic rename, so only a
session writing in that very instant can still win or lose. A deleted item goes to ``memory-trash/``
in the service's state directory, from where it can be restored by hand.

- ``GET /ui/api/memory/projects``: the configuration directories, one row per project merged across
  them (its places with item counts and newest change), and notes on the clients without memory.
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
import hashlib
import json
import os
import re
import secrets
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.utils.prompt_loader import split_frontmatter
from .client_dirs import claude_config_dirs, codex_homes
from .state import atomic_private, private_dir, read_json

PREFIX = "/ui/api/memory"
INDEX = "MEMORY.md"
TRASH = "memory-trash"
MAX_EDIT = 256 * 1024  # larger files are shown, not edited
MAX_SHOW = 1024 * 1024  # larger files are listed without their text
MAX_BODY = 4 * MAX_EDIT  # a JSON body escapes the text
FILE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,199}")
PROJECT = re.compile(r"[A-Za-z0-9._-]{1,255}")
LINK = re.compile(r"^\s*[-*+]\s+\[(?P<title>[^\]]*)\]\((?P<file>[^)\s]+)\)")
SANITIZE = re.compile(r"[^A-Za-z0-9]")
DEFAULT_MODE = 0o644


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


def _is_link(path: Path) -> bool:
    return path.is_symlink() or getattr(path, "is_junction", lambda: False)()


def _item_name(store: Store, name: str) -> bool:
    """A plain file name; Claude Code's items are Markdown files."""
    return (isinstance(name, str) and FILE.fullmatch(name) is not None and name not in (".", "..")
            and (store.client != "claude" or name.endswith(".md")))


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


def _field(value) -> str | None:
    if value is None or isinstance(value, (dict, list)):
        return None
    return value.isoformat() if hasattr(value, "isoformat") else str(value)


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


def _replace(path: Path, data: bytes, expected: str | None) -> None:
    """Writes ``path`` through a temporary file and a rename, keeping its mode.

    ``expected`` is the SHA-256 the file must still have, checked right before the rename; ``None``
    means the file must not exist.
    """
    try:
        mode = path.stat().st_mode & 0o777
    except FileNotFoundError:
        mode = DEFAULT_MODE
    fd, temporary = tempfile.mkstemp(prefix="." + path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        _check(path, expected)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _check(path: Path, expected: str | None) -> None:
    if _is_link(path):
        raise MemoryRefused("symlink_refused", f"{path.name} is a symlink")
    try:
        current = path.read_bytes()
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

    # --- requests ---------------------------------------------------------------------

    async def handle(self, request: Request, path: str):
        writes = {(PREFIX + "/file", "PUT"): self.save, (PREFIX + "/delete", "POST"): self.delete,
                  (PREFIX + "/copy", "POST"): self.copy}
        if (path, request.method) in writes:
            raw = await request.body()
            try:
                body = json.loads(raw or b"{}") if len(raw) <= MAX_BODY else None
            except ValueError:
                body = None
            if not isinstance(body, dict):
                return self._json({"status": "error", "error": "invalid_request: the body must be a JSON object"}, 400)
            return await self._run(lambda: writes[(path, request.method)](body))
        query = request.query_params
        reads = {PREFIX + "/projects": lambda: self.projects(),
                 PREFIX: lambda: self.read(query.get("store", ""), query.get("project", "")),
                 PREFIX + "/file": lambda: self.file(query.get("store", ""), query.get("project", ""),
                                                     query.get("file", "")),
                 PREFIX + "/search": lambda: self.search(query.get("q", ""))}
        if path in reads and request.method == "GET":
            return await self._run(reads[path])
        if path in reads or any(path == route for route, _ in writes):
            return self._json({"error": "method_not_allowed"}, 405)
        return self._json({"error": "not_found"}, 404)

    async def _run(self, work):
        try:
            return self._json(await asyncio.to_thread(work))
        except MemoryRefused as error:
            return self._json({"status": "error", "error": f"{error.code}: {error}", **error.extra}, error.status)
        except OSError as error:
            return self._json({"status": "error", "error": f"storage_error: {error.strerror or error}"}, 500)

    @staticmethod
    def _json(value, status=200):
        return JSONResponse(value, status, headers={"Cache-Control": "no-store"})

    # --- places -----------------------------------------------------------------------

    def stores(self) -> list[Store]:
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
        steps = [memory.parent, memory] if project else [memory]
        for step in steps:
            if _is_link(step):
                raise MemoryRefused("symlink_refused", f"{step.name} is a symlink")
        if project and not create and not memory.parent.is_dir():
            raise MemoryRefused("project_not_found", "no such project in this configuration directory", 404)
        if create:
            memory.mkdir(parents=True, exist_ok=True)
        return memory

    def _file(self, store: Store, project: str, file: str, create_dir: bool = False) -> Path:
        if not _item_name(store, file):
            raise MemoryRefused("file_invalid", "file must be a plain file name"
                                + (" ending in .md" if store.client == "claude" else ""))
        path = self._dir(store, project, create=create_dir) / file
        if _is_link(path):
            raise MemoryRefused("symlink_refused", f"{file} is a symlink")
        return path

    @staticmethod
    def _names(store: Store, memory: Path) -> list[str]:
        """The item files of a memory directory, sorted; ``MEMORY.md`` is the index, not an item."""
        try:
            entries = list(os.scandir(memory))
        except (FileNotFoundError, NotADirectoryError):
            return []
        return sorted(entry.name for entry in entries
                      if entry.name != INDEX and _item_name(store, entry.name)
                      and entry.is_file(follow_symlinks=False))

    def _places(self, store: Store):
        """``(project, memory dir)`` of every place in ``store`` that holds an item or an index."""
        candidates = [""]
        if store.client == "claude":
            try:
                candidates += sorted(entry.name for entry in os.scandir(store.path / "projects")
                                     if entry.is_dir(follow_symlinks=False) and PROJECT.fullmatch(entry.name))
            except (FileNotFoundError, NotADirectoryError):
                pass
        for project in candidates:
            memory = store.memory_dir(project)
            if _is_link(memory) or not memory.is_dir():
                continue
            if self._names(store, memory) or (memory / INDEX).is_file():
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

    # --- reads ------------------------------------------------------------------------

    def projects(self) -> dict:
        stores, workspaces, rows = self.stores(), self._workspaces(), {}
        for store in stores:
            for project, memory in self._places(store):
                names = self._names(store, memory)
                times = []
                for name in names + [INDEX]:
                    try:
                        times.append((memory / name).stat().st_mtime)
                    except OSError:
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

    def _item(self, store: Store, path: Path) -> dict:
        stat = path.stat()
        item = {"file": path.name, "bytes": stat.st_size, "mtime": _iso(stat.st_mtime),
                "editable": stat.st_size <= MAX_EDIT, "text": None, "sha256": None,
                "name": None, "description": None, "type": None, "modified": None, "frontmatter": True}
        if stat.st_size > MAX_SHOW:  # listed and deletable, not shown
            digest = hashlib.sha256()
            with open(path, "rb") as stream:
                for chunk in iter(lambda: stream.read(1 << 20), b""):
                    digest.update(chunk)
            item["sha256"] = digest.hexdigest()
            return item
        raw = path.read_bytes()
        text = raw.decode("utf-8", errors="replace")
        meta, parsed = _frontmatter(text)
        metadata = meta.get("metadata") if isinstance(meta.get("metadata"), dict) else {}
        item.update(text=text, sha256=_sha(raw), frontmatter=parsed, name=_field(meta.get("name")),
                    description=_field(meta.get("description")),
                    type=_field(metadata.get("type", meta.get("type"))),
                    modified=_field(metadata.get("modified", meta.get("modified"))))
        item["editable"] = item["editable"] and "\ufffd" not in text  # saving would lose the bytes
        return item

    def read(self, store: str, project: str) -> dict:
        place = self._store(store)
        memory = self._dir(place, project)
        items = []
        for name in self._names(place, memory):
            try:
                items.append(self._item(place, memory / name))
            except OSError:
                items.append({"file": name, "unreadable": True})
        index = {"exists": False, "sha256": None, "text": None, "links": []}
        if (memory / INDEX).is_file() and not _is_link(memory / INDEX):
            raw = (memory / INDEX).read_bytes()
            text = raw.decode("utf-8", errors="replace")
            index = {"exists": True, "sha256": _sha(raw), "text": text, "links": links(text)}
        linked = {link["file"] for link in index["links"]}
        present = {item["file"] for item in items}
        others = [candidate for candidate in self.stores() if candidate != place]
        places = [{"store": place.id, "label": place.label}]
        for candidate in others:
            if candidate.client == place.client:
                try:
                    holds = self._names(candidate, self._dir(candidate, project)) or (
                        candidate.memory_dir(project) / INDEX).is_file()
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
        place = self._store(store)
        path = self._file(place, project, file) if file != INDEX else self._dir(place, project) / INDEX
        if _is_link(path) or not path.is_file():
            raise MemoryRefused("file_not_found", f"{file} does not exist here", 404)
        raw = path.read_bytes()
        return {"store": place.id, "project": project, "file": file, "text": raw.decode("utf-8", errors="replace"),
                "sha256": _sha(raw), "editable": len(raw) <= MAX_EDIT}

    def search(self, query: str) -> dict:
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
                        if (memory / name).stat().st_size > MAX_SHOW:
                            continue
                        text = (memory / name).read_text(encoding="utf-8", errors="replace")
                    except OSError:
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
        place = self._store(body.get("store", ""))
        project, file = body.get("project", ""), body.get("file", "")
        data = self._text(body)
        creating = body.get("sha256") is None
        expected = self._expected(body, required=not creating)
        with self._lock:
            path = self._dir(place, project) / INDEX if file == INDEX else self._file(place, project, file)
            if not path.parent.is_dir():
                raise MemoryRefused("project_not_found", "this memory directory does not exist", 404)
            _replace(path, data, expected)
            indexed = False
            if creating and body.get("index") and file != INDEX:
                indexed = self._index_add(path.parent, file, _index_line(file, data.decode("utf-8")))
        return {"status": "created" if creating else "saved", "store": place.id, "project": project, "file": file,
                "sha256": _sha(data), "indexed": indexed}

    def _index_add(self, memory: Path, file: str, line: str) -> bool:
        """Appends ``line`` to the index unless it already links ``file``; True when it was added."""
        index = memory / INDEX
        if _is_link(index):
            raise MemoryRefused("symlink_refused", f"{INDEX} is a symlink")
        try:
            raw = index.read_bytes()
        except FileNotFoundError:
            raw = None
        text = raw.decode("utf-8", errors="replace") if raw is not None else ""
        if any(link["file"] == file for link in links(text)):
            return False
        _replace(index, _with_line(text, line).encode("utf-8"), _sha(raw) if raw is not None else None)
        return True

    def _index_remove(self, memory: Path, file: str) -> bool:
        index = memory / INDEX
        if _is_link(index) or not index.is_file():
            return False
        raw = index.read_bytes()
        text = raw.decode("utf-8", errors="replace")
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
        try:
            data = path.read_bytes()
        except FileNotFoundError:
            raise MemoryRefused("file_changed", f"{path.name} no longer exists", 409) from None
        if _sha(data) != expected:
            raise MemoryRefused("file_changed", f"{path.name} changed since it was shown", 409)
        kept = self._trash(place, project, path, data)
        path.unlink()
        unindexed = self._index_remove(path.parent, path.name)
        return {"trash": str(kept), "unindexed": unindexed}

    def delete(self, body: dict) -> dict:
        place = self._store(body.get("store", ""))
        project, file = body.get("project", ""), body.get("file", "")
        expected = self._expected(body)
        with self._lock:
            path = self._file(place, project, file)
            result = self._remove(place, project, path, expected)
        return {"status": "deleted", "store": place.id, "project": project, "file": file, **result}

    def copy(self, body: dict) -> dict:
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
                data = path.read_bytes()
            except FileNotFoundError:
                raise MemoryRefused("file_changed", f"{file} no longer exists", 409) from None
            if _sha(data) != expected:
                raise MemoryRefused("file_changed", f"{file} changed since it was shown", 409)
            destination = self._file(target, project, file, create_dir=True)
            try:
                existing = destination.read_bytes()
            except FileNotFoundError:
                existing = None
            if existing is not None and replace is None:
                raise MemoryRefused("target_exists", f"{target.label} already has {file}", 409,
                                    target={"store": target.id, "label": target.label, "sha256": _sha(existing),
                                            "text": existing.decode("utf-8", errors="replace")})
            if existing is None and replace is not None:
                raise MemoryRefused("target_changed", f"{file} is no longer in {target.label}", 409)
            _replace(destination, data, replace)
            text = data.decode("utf-8", errors="replace")
            source_index = path.parent / INDEX
            line = None
            if source_index.is_file() and not _is_link(source_index):
                index_text = source_index.read_text(encoding="utf-8", errors="replace")
                line = next((raw for raw in index_text.splitlines()
                             if (match := LINK.match(raw)) and match.group("file") == file), None)
            indexed = self._index_add(destination.parent, file, line or _index_line(file, text))
            result = {"status": "moved" if move else "copied", "store": source.id, "project": project, "file": file,
                      "to_store": target.id, "indexed": indexed, "replaced": existing is not None}
            if move:
                result.update(self._remove(source, project, path, expected))
        return result
