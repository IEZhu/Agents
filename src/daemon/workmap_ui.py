"""The Work map tab of the web UI: read the work maps of the registered workspaces.

Every route only reads. A map lives in ``<workspace>/data/workmap/`` (``src/workmap/store.py``);
these routes serve the maps of workspaces in the daemon's registry, addressed by the registry's
workspace id and never by a path from the request, behind the UI session and the page header.

- ``GET /ui/api/workmap/maps``: per workspace, its most recently written maps (the store's
  ``LIST_LIMIT``), newest first: task id, goal, open or closed, revision, counts by status and
  the prepared (running) nodes; map files the store refuses or that are too large to draw are
  counted, not listed.
- ``GET /ui/api/workmap/map?workspace=&task_id=``: one map as a graph, nodes in dependency order
  with status, level and result fields, the states the store derives (settled, ready, stale,
  needs attention), the time of each node's current status and its attempts, edges by type, the
  nodes the last commit touched and the recorded verdicts. Without ``workspace`` the registered
  workspaces are searched, the map written last of that task id answers, and ``candidates`` says
  how many workspaces hold one.
- ``GET /ui/api/workmap/node?workspace=&task_id=&node=``: one node's record, with its status and
  level history, relations and the artifacts its stored result names.
- ``GET /ui/api/workmap/result?workspace=&task_id=&node=``: the node's stored result text, which
  the page shows as text only on an explicit click.

The page polls, so each map file is folded once per change (``web.FoldCache``), and the reads run
on two threads of their own: a slow read never holds the daemon's shared executor. No answer names
a path: refusals that would quote one say what failed instead.
"""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import threading
import time

from starlette.requests import Request
from starlette.responses import JSONResponse

from src.safe_store import StoreError
from src.workmap import web
from src.workmap.store import WorkmapError, WorkmapStore, _task_id
from .state import read_json
from .workspaces import WorkspaceError

PREFIX = "/ui/api/workmap"
# A stored result may be large; the page shows it in a text box, so the route caps what it sends.
RESULT_MAX_CHARS = 2_000_000
_STATUS = {"unknown_map": 404, "unknown_node": 404, "no_result": 404, "invalid_request": 400,
           "key_collision": 409, "map_too_large": 413, "workspace_invalid": 400, "workspace_required": 400}
# Refusals whose message names no path; any other store refusal is answered without its message.
_PLAIN = {"unknown_map", "unknown_node", "no_result", "invalid_request", "key_collision", "map_too_large"}
_executor: ThreadPoolExecutor | None = None
_executor_lock = threading.Lock()


def _reads() -> ThreadPoolExecutor:
    """The work-map reads' own two threads, made on first use and shared by the process."""
    global _executor
    with _executor_lock:
        if _executor is None:
            _executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="agents-workmap")
        return _executor


def is_workmap_path(path: str) -> bool:
    return path == PREFIX or path.startswith(PREFIX + "/")


class WorkmapUIError(Exception):
    def __init__(self, code: str, message: str, status: int = 400):
        super().__init__(message)
        self.code, self.status = code, status


class WorkmapUI:
    def __init__(self, service):
        self.service = service
        self.cache = web.FoldCache()

    async def handle(self, request: Request, path: str):
        if request.method != "GET":
            return self._json({"error": "method_not_allowed"}, 405, headers={"Allow": "GET"})
        query = request.query_params
        routes = {PREFIX + "/maps": self.maps,
                  PREFIX + "/map": lambda: self.map(query.get("workspace", ""), query.get("task_id", "")),
                  PREFIX + "/node": lambda: self.node(query.get("workspace", ""), query.get("task_id", ""),
                                                     query.get("node", "")),
                  PREFIX + "/result": lambda: self.result(query.get("workspace", ""), query.get("task_id", ""),
                                                          query.get("node", ""))}
        work = routes.get(path)
        if work is None:
            return self._json({"error": "not_found"}, 404)
        try:
            answer = await asyncio.get_running_loop().run_in_executor(_reads(), work)
        except WorkmapUIError as error:
            return self._json({"status": "error", "error": f"{error.code}: {error}"}, error.status)
        except StoreError as error:  # WorkmapError is StoreError
            message = str(error) if error.code in _PLAIN else "the repository's work map files cannot be read"
            return self._json({"status": "error", "error": f"{error.code}: {message}"}, _STATUS.get(error.code, 500))
        except WorkspaceError as error:
            return self._json({"status": "error", "error": f"{error}: the workspace is not registered or is gone"},
                              400)
        except OSError as error:
            return self._json({"status": "error", "error": f"storage_error: {error.strerror or 'read failed'}"}, 500)
        try:
            return self._json(answer)
        except (ValueError, UnicodeError):  # a value JSON cannot carry: one log never takes a route down
            return self._json({"status": "error", "error": "unencodable: the map holds a value that cannot be sent"},
                              500)

    @staticmethod
    def _json(value, status=200, **kwargs):
        headers = {"Cache-Control": "no-store", **kwargs.pop("headers", {})}
        return JSONResponse(value, status, headers=headers, **kwargs)

    # --- workspaces ------------------------------------------------------------------

    def _workspaces(self) -> list[tuple[str, Path]]:
        """The registered workspaces that still resolve, as the other tabs accept them."""
        try:
            records = read_json(self.service.registry.path, {})
        except ValueError:
            raise WorkmapUIError("registry_unreadable", "the workspace registry cannot be read", 500) from None
        found = []
        for identity in (records if isinstance(records, dict) else {}):
            try:
                found.append((identity, Path(self.service.registry.resolve(identity))))
            except WorkspaceError:
                continue
        return found

    def _store(self, workspace: str) -> WorkmapStore:
        return WorkmapStore(self.service.registry.resolve(workspace))

    # --- routes ----------------------------------------------------------------------

    def maps(self) -> dict:
        out, now = [], time.time()
        for identity, root in self._workspaces():
            try:
                listed = web.list_maps(WorkmapStore(root), self.cache, now)
            except (StoreError, OSError):
                out.append({"workspace": identity, "name": root.name, "unreadable": True, "total": 0, "maps": []})
                continue
            if not listed["total"]:
                continue
            out.append({"workspace": identity, "name": root.name, **listed})
        out.sort(key=lambda repo: repo["name"].lower())
        out.sort(key=lambda repo: max((item["updated"] or "" for item in repo["maps"]), default=""), reverse=True)
        return {"workspaces": out}

    def _find(self, task_id: str) -> tuple[str, int]:
        """The workspace whose map of ``task_id`` was written last, for a link that names none, and
        how many workspaces hold a map of that task id. A time in the future counts as none."""
        task_id = _task_id(task_id)  # checked before the search: an error never repeats an unchecked value
        newest, found, candidates, now = "", None, 0, time.time()
        for identity, root in self._workspaces():
            try:
                history = self.cache.history(WorkmapStore(root), task_id)
            except (StoreError, OSError):
                continue
            if history.workmap is None:
                continue
            candidates += 1
            stamp = web.summary(history.workmap, now)["updated"] or ""
            if found is None or stamp > newest:
                newest, found = stamp, identity
        if found is None:
            raise WorkmapError("unknown_map", f"no registered workspace has a work map for task {task_id}")
        return found, candidates

    def map(self, workspace: str, task_id: str) -> dict:
        extra = {}
        if not workspace:
            workspace, extra["candidates"] = self._find(task_id)
        root = self.service.registry.resolve(workspace)
        return {"workspace": workspace, "name": Path(root).name,
                **web.load_graph(WorkmapStore(root), task_id, self.cache), **extra}

    def node(self, workspace: str, task_id: str, node_id: str) -> dict:
        return {"workspace": workspace, **web.node_record(self._store(workspace), task_id, node_id, self.cache)}

    def result(self, workspace: str, task_id: str, node_id: str) -> dict:
        text = web.result_text(self._store(workspace), task_id, node_id, self.cache)
        if text is None:
            raise WorkmapError("no_result", f"node {node_id} has no stored result")
        truncated = len(text) > RESULT_MAX_CHARS
        return {"workspace": workspace, "task_id": task_id, "node": node_id, "chars": len(text),
                "truncated": truncated, "text": text[:RESULT_MAX_CHARS]}
