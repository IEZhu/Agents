"""The History tab of the web UI (#189): read each registered repository's history.

Every workspace of the registry keeps its answers in ``history.md`` and, once that file grows,
in monthly archives ``history/YYYY-MM.md`` (``src/memory/history.py``). These reads serve them
to the page: only files of a registered workspace, only ``history.md`` and ``history/`` archive
names, never a path from the request. The text is shown in full; the page is local and behind
the UI session. Entries of other machines (user library sync) live in the library, not here.

- ``GET /ui/api/history/repos``: the workspaces with a history, newest first: name, root,
  whether the directory is still there, and per file its entries, size and newest time.
- ``GET /ui/api/history?workspace=&file=&before=&limit=``: a file's entries, newest first,
  ``limit`` at a time. ``before`` counts from the oldest entry, so the answers appended meanwhile
  never shift a portion: the first portion (without ``before``) gives ``total``, and the next one
  asks for ``before=total - <entries shown>``. The first portion also carries ``index``, every
  entry's id, time, agent and persona action, which the page's Contents lists. ``file`` defaults
  to the newest file. ``entry=<id>`` (with ``time=``, as ids repeat) opens the file that holds
  that entry and reaches it: ``focus`` is its position, newest first.
- ``GET /ui/api/history/source?workspace=&file=``: the file as it is.
- ``GET /ui/api/history/search?q=``: per workspace, the entries that hold every term, the
  repository's name counting as part of each entry.
- ``POST /ui/api/history/delete`` with ``{workspace, file, entry, time}``: removes exactly that
  entry, matched by its heading's hash and time, under the lock ``log_interaction`` appends under
  (``src.memory.history.delete_entry``); 409 ``entry_changed`` when it is not there as shown. With
  user library sync the entry then leaves what this machine shared (``src.user_sync.history``).
"""
from __future__ import annotations

import asyncio
import json
import os
import re
from contextlib import nullcontext as _nothing
from pathlib import Path
from typing import Optional

from starlette.requests import Request
from starlette.responses import JSONResponse

from src.memory.history import ENTRY_ID, EntryChanged, _archive_names, _reading, delete_entry, parsed_file
from src.memory.history_stats import FileSummaries, attribution
from .state import read_json
from .workspaces import WorkspaceError

PREFIX = "/ui/api/history"
CURRENT = "current"
PORTION = 20
MAX_PORTION = 200
MAX_BODY = 8 * 1024
_MONTH = re.compile(r"[0-9]{4}-(?:0[1-9]|1[0-2])")


def is_history_path(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


class HistoryError(Exception):
    """A refused request: ``code`` names it, ``status`` is the HTTP status."""

    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


def _file_path(root: Path, name: str) -> Path:
    """The file ``name`` (``current`` or ``YYYY-MM``) of a workspace; anything else is refused."""
    if name == CURRENT:
        path = root / "history.md"
    elif isinstance(name, str) and _MONTH.fullmatch(name):
        path = root / "history" / f"{name}.md"
    else:
        raise HistoryError("file_invalid", "file must be current or a YYYY-MM archive")
    if not path.resolve().is_relative_to(root.resolve()):
        raise HistoryError("file_invalid", "the file is outside the workspace")
    return path


def _files(root: Path) -> list[str]:
    """``current`` when ``history.md`` exists, then the archives of a real month, newest first."""
    names = [name[:7] for name in reversed(_archive_names(str(root / "history"))) if _MONTH.fullmatch(name[:7])]
    return ([CURRENT] if (root / "history.md").is_file() else []) + names


def _entry(entry) -> dict:
    agent, action = attribution(entry.action)
    return {"id": entry.id, "timestamp": entry.timestamp, "agent": agent, "persona_action": action,
            "intent": entry.intent, "action": entry.action, "outcome": entry.outcome, "files": entry.files,
            "tags": entry.tags, "metadata": entry.metadata, "machine": entry.machine}


def _newest_first(entries) -> list:
    """Newest first by time; entries with the same time keep the later one in the file first."""
    return sorted(reversed(entries), key=lambda entry: entry.timestamp, reverse=True)


class HistoryUI:
    def __init__(self, service, summaries: Optional[FileSummaries] = None):
        self.service = service
        usage = getattr(service, "usage", None)
        self.summaries = summaries or getattr(usage, "files", None) or FileSummaries()

    async def handle(self, request: Request, path: str):
        if path == PREFIX + "/delete":
            if request.method != "POST":
                return self._json({"error": "method_not_allowed"}, 405, headers={"Allow": "POST"})
            raw = await request.body()
            try:
                body = json.loads(raw or b"{}") if len(raw) <= MAX_BODY else None
            except ValueError:
                body = None
            if not isinstance(body, dict):
                return self._json({"status": "error", "error": "invalid_request: the body must be a small JSON object"}, 400)
            return await self._run(lambda: self.delete(body.get("workspace", ""), body.get("file"), body.get("entry"),
                                                       body.get("time")))
        if request.method != "GET":
            return self._json({"error": "method_not_allowed"}, 405, headers={"Allow": "GET"})
        query = request.query_params
        routes = {PREFIX + "/repos": lambda: self.repos(),
                  PREFIX: lambda: self.read(query.get("workspace", ""), query.get("file"), query.get("before"),
                                            query.get("limit"), query.get("entry"), query.get("time")),
                  PREFIX + "/source": lambda: self.source(query.get("workspace", ""), query.get("file", CURRENT)),
                  PREFIX + "/search": lambda: self.search(query.get("q", ""))}
        work = routes.get(path)
        if work is None:
            return self._json({"error": "not_found"}, 404)
        return await self._run(work)

    async def _run(self, work):
        try:
            return self._json(await asyncio.to_thread(work))
        except HistoryError as error:
            return self._json({"status": "error", "error": f"{error.code}: {error}"}, error.status)
        except WorkspaceError as error:
            return self._json({"status": "error", "error": f"{error}: the workspace is not registered or is gone"}, 400)
        except OSError as error:
            return self._json({"status": "error", "error": f"storage_error: {error.strerror or error}"}, 500)

    @staticmethod
    def _json(value, status=200, **kwargs):
        headers = {"Cache-Control": "no-store", **kwargs.pop("headers", {})}
        return JSONResponse(value, status, headers=headers, **kwargs)

    def _workspaces(self) -> list[tuple[str, Path]]:
        try:
            records = read_json(self.service.registry.path, {})
        except ValueError:
            raise HistoryError("registry_unreadable", "the workspace registry cannot be read", 500) from None
        if not isinstance(records, dict):
            return []
        return [(identity, Path(root)) for identity, root in records.items()
                if isinstance(identity, str) and isinstance(root, str)]

    def _root(self, workspace: str) -> Path:
        return Path(self.service.registry.resolve(workspace))

    # --- the routes ------------------------------------------------------------------

    def repos(self) -> dict:
        out = []
        for identity, root in self._workspaces():
            try:  # what the reads accept: registered once, still a directory at its saved path
                root = self._root(identity)
            except WorkspaceError:
                out.append({"workspace": identity, "name": root.name, "root": str(root), "available": False,
                            "files": [], "entries": 0, "bytes": 0, "newest": None})
                continue
            files = []
            for name in _files(root):
                try:
                    path = _file_path(root, name)
                    if name == CURRENT:
                        with _reading(str(path)):
                            answers = self.summaries.answers(str(path))
                    else:
                        answers = self.summaries.answers(str(path))
                    size = path.stat().st_size
                except (HistoryError, OSError):
                    files.append({"file": name, "entries": None, "bytes": None, "newest": None, "unreadable": True})
                    continue
                files.append({"file": name, "entries": len(answers), "bytes": size,
                              "newest": max((answer.time for answer in answers), default=None)})
            if not files:
                continue
            out.append({"workspace": identity, "name": root.name, "root": str(root), "available": True,
                        "files": files, "entries": sum(f["entries"] or 0 for f in files),
                        "bytes": sum(f["bytes"] or 0 for f in files),
                        "newest": max((f["newest"] for f in files if f["newest"]), default=None)})
        out.sort(key=lambda repo: repo["name"].lower())
        out.sort(key=lambda repo: repo["newest"] or "", reverse=True)
        return {"repos": out}

    def _entries(self, root: Path, name: str) -> list:
        path = _file_path(root, name)
        if not path.is_file():
            raise HistoryError("file_not_found", f"{name} does not exist in this workspace", 404)
        if name == CURRENT:
            with _reading(str(path)):
                parsed = parsed_file(str(path))
        else:
            parsed = parsed_file(str(path))
        if parsed is None:
            raise HistoryError("file_not_found", f"{name} is not a regular file", 404)
        return _newest_first(parsed[1])

    def read(self, workspace: str, file: Optional[str], before, limit, entry: Optional[str],
             time: Optional[str] = None) -> dict:
        root = self._root(workspace)
        limit = min(_number(limit, PORTION, "limit") or PORTION, MAX_PORTION)
        files = _files(root)
        focus = None
        if entry:
            for name in ([file] if file else files):
                try:
                    entries = self._entries(root, name)
                except HistoryError:
                    continue
                focus = next((index for index, item in enumerate(entries)
                              if item.id == entry and (not time or item.timestamp == time)), None)
                if focus is not None:
                    file = name
                    break
            if focus is None:
                raise HistoryError("entry_not_found", "no such entry in this workspace", 404)
            first, end = True, max(limit, focus + 1)  # the newest entries down to it
        else:
            file = file or (files[0] if files else CURRENT)
            entries = self._entries(root, file)
            total = len(entries)
            first = before in (None, "")
            # ``before`` counts from the oldest entry: the newest-first slice ends ``before`` entries
            # above the oldest one, whatever was appended since the first portion.
            start = 0 if first else max(0, total - min(_number(before, total, "before"), total))
            end = start + limit
        result = {"workspace": workspace, "name": root.name, "root": str(root), "file": file, "files": files,
                  "total": len(entries), "bytes": _file_path(root, file).stat().st_size,
                  "entries": [_entry(item) for item in entries[(0 if entry else start):end]]}
        if first:
            result["index"] = [{"id": item.id, "timestamp": item.timestamp, **dict(zip(
                ("agent", "persona_action"), attribution(item.action)))} for item in entries]
        if focus is not None:
            result["focus"] = focus
        return result

    def source(self, workspace: str, file: str) -> dict:
        root = self._root(workspace)
        path = _file_path(root, file)
        if not path.is_file():
            raise HistoryError("file_not_found", f"{file} does not exist in this workspace", 404)
        # history.md under the shared history lock, as every reader: a rotation never sees it open.
        with _reading(str(root / "history.md")) if file == CURRENT else _nothing():
            with open(path, encoding="utf-8", errors="replace", newline="") as stream:
                return {"workspace": workspace, "file": file, "text": stream.read()}

    def delete(self, workspace: str, file, entry, time) -> dict:
        root = self._root(workspace)
        if not isinstance(entry, str) or not ENTRY_ID.fullmatch(entry) or not isinstance(time, str) or not time:
            raise HistoryError("invalid_request", "entry (an entry's id) and its time are required")
        _file_path(root, file)  # current or a YYYY-MM archive of this workspace
        try:
            delete_entry(str(root / "history.md"), None if file == CURRENT else file, entry, time,
                         archive_dir=str(root / "history"))
        except EntryChanged as error:
            raise HistoryError("entry_changed", str(error), 409) from None
        return {"status": "deleted", "workspace": workspace, "file": file, "entry": entry, "time": time}

    def search(self, query: str) -> dict:
        terms = [term for term in query.lower().split() if term]
        if not terms:
            return {"q": query, "repos": []}
        out = []
        for identity, root in self._workspaces():
            try:
                root = self._root(identity)
            except WorkspaceError:
                continue
            matches = 0
            for name in _files(root):
                try:
                    entries = self._entries(root, name)
                except (HistoryError, OSError):
                    continue
                for item in entries:
                    # As on the other tabs, the name counts too: "agents lawyer" finds lawyer's
                    # answers in the repository "agents".
                    text = "\n".join((root.name, item.intent, item.action, item.outcome, " ".join(item.files),
                                      " ".join(item.tags))).lower()
                    if all(term in text for term in terms):
                        matches += 1
            if matches:
                out.append({"workspace": identity, "matches": matches})
        return {"q": query, "repos": out}


def _number(value, default: int, name: str) -> int:
    if value in (None, ""):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        raise HistoryError("invalid_request", f"{name} must be a whole number") from None
    if number < 0:
        raise HistoryError("invalid_request", f"{name} must not be negative")
    return number
