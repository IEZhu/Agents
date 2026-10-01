"""Local flow editor served by the daemon at /ui.

The browser never sees the service bearer token. ``python -m src.daemon flows-ui``
asks the daemon (with the bearer token) for a one-use code, opens
``/ui#code=...`` and the page trades the code for an HttpOnly, SameSite=Strict
session cookie that authorizes only ``/ui/api/*``. Every UI request must use the
loopback Host; mutations also need a matching Origin and the ``X-Agents-UI``
header, which a cross-site form cannot send.
"""
import asyncio
import html
import json
import secrets
import time
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from src.flows import FlowCatalog, FlowError
from src.user_flows import FlowLibrary
from src.version import agents_core_version
from .state import read_json
from .workspaces import WorkspaceError

CODE_TTL = 120
SESSION_IDLE = 30 * 60
SESSION_MAX = 8 * 3600
COOKIE = "agents_flows_ui"
MAX_BODY = 512 * 1024
PAGE = Path(__file__).with_name("flows_ui.html")


def _error_status(message: str) -> int:
    code = message.split(":", 1)[0]
    return {"flow_conflict": 409, "flow_not_found": 404, "flow_read_only": 403,
            "repo_scope_unavailable": 400, "workspace_invalid": 400}.get(code, 400)


class FlowsUI:
    def __init__(self, service):
        self.service = service
        self.codes = {}
        self.sessions = {}

    # --- access ----------------------------------------------------------------------

    def hosts(self):
        return {f"127.0.0.1:{self.service.port}", f"localhost:{self.service.port}"}

    def issue_code(self) -> str:
        now = time.monotonic()
        self.codes = {code: expiry for code, expiry in self.codes.items() if expiry > now}
        code = secrets.token_urlsafe(24)
        self.codes[code] = now + CODE_TTL
        return code

    def _session(self, request: Request) -> bool:
        sid = request.cookies.get(COOKIE)
        record = self.sessions.get(sid) if sid else None
        now = time.monotonic()
        if not record or now - record["last"] > SESSION_IDLE or now - record["created"] > SESSION_MAX:
            self.sessions.pop(sid, None)
            return False
        record["last"] = now
        return True

    def _same_origin(self, request: Request) -> bool:
        return request.headers.get("origin", "") in {f"http://{host}" for host in self.hosts()}

    # --- dispatch --------------------------------------------------------------------

    async def __call__(self, scope, receive, send):
        request = Request(scope, receive)
        path = scope["path"]
        if request.headers.get("host", "") not in self.hosts():
            return await self._json({"error": "host_not_allowed"}, 403)(scope, receive, send)
        if path == "/ui" and request.method == "GET":
            nonce = secrets.token_urlsafe(16)
            page = PAGE.read_text(encoding="utf-8").replace("{{NONCE}}", nonce).replace(
                "{{VERSION}}", html.escape(agents_core_version()))
            headers = {
                "Content-Security-Policy": (
                    f"default-src 'none'; script-src 'nonce-{nonce}'; style-src 'nonce-{nonce}'; "
                    "connect-src 'self'; img-src data:; base-uri 'none'; form-action 'none'; "
                    "frame-ancestors 'none'"),
                "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
                "Referrer-Policy": "no-referrer",
            }
            return await HTMLResponse(page, headers=headers)(scope, receive, send)
        if not path.startswith("/ui/api/"):
            return await self._json({"error": "not_found"}, 404)(scope, receive, send)
        if request.method != "GET" and (not self._same_origin(request)
                                        or request.headers.get("x-agents-ui") != "1"):
            return await self._json({"error": "origin_not_allowed"}, 403)(scope, receive, send)
        if path == "/ui/api/session" and request.method == "POST":
            response = await self._login(request)
            return await response(scope, receive, send)
        if not self._session(request):
            return await self._json({"error": "session_required"}, 401)(scope, receive, send)
        if self.service.state == "draining":
            return await self._json({"error": "draining"}, 503)(scope, receive, send)
        self.service.inflight += 1
        self.service.last_activity = time.monotonic()
        try:
            response = await self._api(request, path)
        finally:
            self.service.inflight -= 1
            self.service.last_activity = time.monotonic()
        return await response(scope, receive, send)

    @staticmethod
    def _json(value, status=200, **kwargs):
        return JSONResponse(value, status, headers={"Cache-Control": "no-store"}, **kwargs)

    async def _body(self, request: Request) -> dict:
        raw = await request.body()
        if len(raw) > MAX_BODY:
            raise FlowError("flow_invalid: request too large")
        try:
            value = json.loads(raw or b"{}")
        except ValueError:
            raise FlowError("flow_invalid: body must be JSON") from None
        if not isinstance(value, dict):
            raise FlowError("flow_invalid: body must be a JSON object")
        return value

    async def _login(self, request: Request):
        try:
            code = (await self._body(request)).get("code")
        except FlowError:
            code = None
        expiry = self.codes.pop(code, None) if isinstance(code, str) else None
        if expiry is None or expiry < time.monotonic():
            return self._json({"error": "code_invalid"}, 401)
        sid = secrets.token_urlsafe(32)
        now = time.monotonic()
        self.sessions[sid] = {"created": now, "last": now}
        response = self._json({"status": "ok"})
        response.set_cookie(COOKIE, sid, max_age=SESSION_MAX, path="/ui", httponly=True,
                            samesite="strict")
        return response

    def _library(self, workspace: str | None) -> FlowLibrary:
        root, error = None, None
        if workspace:
            try:
                root = self.service.registry.resolve(workspace)
            except WorkspaceError as failure:
                error = str(failure)
        return FlowLibrary(FlowCatalog(), repo_root=root, repo_error=error or "workspace_required")

    async def _api(self, request: Request, path: str):
        query = request.query_params
        try:
            if path == "/ui/api/workspaces" and request.method == "GET":
                records = await asyncio.to_thread(read_json, self.service.registry.path, {})
                return self._json({"workspaces": [
                    {"id": identity, "path": root, "name": Path(root).name}
                    for identity, root in sorted(records.items(), key=lambda item: item[1])]})
            if path == "/ui/api/flows" and request.method == "GET":
                library = self._library(query.get("workspace"))
                return self._json(await asyncio.to_thread(library.list, "all"))
            if path == "/ui/api/flow":
                if request.method == "GET":
                    library = self._library(query.get("workspace"))
                    return self._json(await asyncio.to_thread(
                        library.get, query.get("id", ""), query.get("version") or None))
                body = await self._body(request)
                library = self._library(body.get("workspace"))
                if request.method == "PUT":
                    return self._json(await asyncio.to_thread(
                        library.save, str(body.get("id", "")), body.get("content"),
                        scope=body.get("scope") or "user",
                        expected_revision=body.get("expected_revision") or None,
                        override=bool(body.get("override"))))
                if request.method == "DELETE":
                    return self._json(await asyncio.to_thread(
                        library.delete, str(body.get("id", "")),
                        expected_revision=str(body.get("expected_revision") or "")))
            return self._json({"error": "not_found"}, 404)
        except FlowError as error:
            return self._json({"status": "error", "error": str(error)}, _error_status(str(error)))
        except OSError as error:
            return self._json({"status": "error", "error": f"storage_error: {error.strerror or error}"}, 500)
