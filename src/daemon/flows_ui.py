"""Local flow editor served by the daemon at /ui.

The browser never sees the service bearer token. The page signs in by itself: a
sign-in request without a code succeeds when the loopback connection belongs to a
process of the OS user running the daemon (``peer.py``), carries no forwarding
header and ``flows-ui --auto off`` has not been set. Otherwise ``python -m src.daemon flows-ui``
asks the daemon (with the bearer token) for a one-use code and opens
``/ui#code=...``. Either way the page receives an HttpOnly, SameSite=Strict
session cookie that authorizes only ``/ui/api/*``. The cookie is signed with a key
in the private state directory, so a session lasts 30 days from the last visit and
survives daemon restarts and updates; ``flows-ui --revoke`` replaces the key and
ends every session. Every UI request must use the loopback Host; mutations,
sign-in included, also need a matching Origin and the ``X-Agents-UI`` header,
which a cross-site page cannot send. Library sync (``/ui/api/sync…``, ``sync_ui.py``)
asks for the header on GET as well, because some of its reads reach the network, and so do
the landing page's statistics and overview (``/ui/api/stats``, ``usage.py``; ``/ui/api/overview``,
``overview.py``), which only the page reads.
"""
import asyncio
import hashlib
import hmac
import html
import json
import secrets
import threading
import time
from pathlib import Path

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse

from src import component_toggles
from src.component_catalog import known_ids, list_agents, list_components
from src.flows import FlowCatalog, FlowError
from src.user_flows import FlowLibrary
from src.version import agents_core_version
from .peer import loopback_peer_is_owner
from .state import atomic_private, read_json
from .sync_loop import DRAINING
from .sync_ui import SyncUI, is_sync_path
from .workspaces import WorkspaceError

CODE_TTL = 120
SESSION_TTL = 30 * 24 * 3600
CLOCK_SKEW = 60
KEY_FILE = "ui_session_key"
KEY_BYTES = 32
COOKIE = "agents_flows_ui"
MAX_BODY = 512 * 1024
STATS_PATH = "/ui/api/stats"
OVERVIEW_PATH = "/ui/api/overview"
# Reads only the page makes, never activity: an open landing page must not hold back an update.
PAGE_READS = (STATS_PATH, OVERVIEW_PATH)
PAGE = Path(__file__).with_name("flows_ui.html")
AUTO_OFF_FILE = "ui_auto_sign_in_off"
# A proxy's own process says nothing about who sent the request it relays.
FORWARDING_HEADERS = ("forwarded", "x-forwarded-for", "x-forwarded-host", "x-real-ip")


def read_session_key(directory) -> bytes | None:
    try:
        key = (Path(directory) / KEY_FILE).read_bytes()
    except OSError:
        return None
    return key if len(key) == KEY_BYTES else None


def replace_session_key(directory) -> None:
    """Install a new random key; every cookie signed with the old one stops working."""
    key_path = Path(directory) / KEY_FILE
    atomic_private(key_path, secrets.token_bytes(KEY_BYTES))
    key_path.chmod(0o600)


def auto_sign_in_enabled(directory) -> bool:
    return not (Path(directory) / AUTO_OFF_FILE).exists()


def set_auto_sign_in(directory, enabled: bool) -> None:
    """``flows-ui --auto off`` leaves only the one-use code; ``on`` restores the default.

    Turning it off also replaces the session key: every session ends, including an
    automatic one minted while the switch was being set.
    """
    marker = Path(directory) / AUTO_OFF_FILE
    if enabled:
        marker.unlink(missing_ok=True)
    else:
        atomic_private(marker, b"off\n")
        replace_session_key(directory)


def _error_status(message: str) -> int:
    code = message.split(":", 1)[0]
    return {"flow_conflict": 409, "flow_not_found": 404, "flow_read_only": 403,
            "repo_scope_unavailable": 400, "workspace_invalid": 400}.get(code, 400)


class FlowsUI:
    def __init__(self, service, clock=time.time, peer_check=loopback_peer_is_owner):
        self.service = service
        self.codes = {}
        self.clock = clock
        self.peer_check = peer_check
        self._key_lock = threading.Lock()
        self._peer_lock = asyncio.Lock()  # one connection-table lookup at a time
        self.sync = SyncUI(service)

    # --- access ----------------------------------------------------------------------

    def hosts(self):
        return {f"127.0.0.1:{self.service.port}", f"localhost:{self.service.port}"}

    def issue_code(self) -> str:
        now = time.monotonic()
        self.codes = {code: expiry for code, expiry in self.codes.items() if expiry > now}
        code = secrets.token_urlsafe(24)
        self.codes[code] = now + CODE_TTL
        return code

    def _sign(self, key: bytes, payload: str) -> str:
        return hmac.new(key, payload.encode(), hashlib.sha256).hexdigest()

    def _new_cookie(self, key: bytes | None = None) -> str | None:
        """Sign with ``key`` (the one that authenticated the request) so a renewal never crosses a revocation."""
        key = key or read_session_key(self.service.directory)
        if key is None:
            return None
        payload = f"{int(self.clock())}.{secrets.token_urlsafe(12)}"
        return f"{payload}.{self._sign(key, payload)}"

    def _session(self, request: Request) -> bytes | None:
        """A cookie is valid when signed with the current key and renewed within SESSION_TTL.

        The key is read on every check, so replacing it revokes sessions without a restart.
        """
        key = read_session_key(self.service.directory)
        parts = request.cookies.get(COOKIE, "").split(".")
        if key is None or len(parts) != 3 or not (parts[0].isascii() and parts[0].isdigit() and len(parts[0]) <= 12):
            return None
        issued, nonce, signature = parts
        if not signature.isascii() or not nonce.isascii():
            return None
        if not hmac.compare_digest(signature, self._sign(key, f"{issued}.{nonce}")):
            return None
        age = self.clock() - int(issued)
        return key if -CLOCK_SKEW <= age <= SESSION_TTL else None

    def _set_cookie(self, response, value: str) -> None:
        response.set_cookie(COOKIE, value, max_age=SESSION_TTL, path="/ui", httponly=True,
                            samesite="strict")

    def _same_origin(self, request: Request) -> bool:
        return request.headers.get("origin", "") in {f"http://{host}" for host in self.hosts()}

    def _page_request(self, request: Request) -> bool:
        """A GET that only the page sends: the header, which another origin cannot set without a
        preflight that fails here, and no Origin but our own (browsers omit it on same-origin GETs)."""
        return request.headers.get("x-agents-ui") == "1" and (
            "origin" not in request.headers or self._same_origin(request))

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
        syncing = is_sync_path(path)
        if (syncing or path in PAGE_READS) and request.method == "GET" and not self._page_request(request):
            return await self._json({"error": "origin_not_allowed"}, 403)(scope, receive, send)
        if path == "/ui/api/session" and request.method == "POST":
            response = await self._login(request)
            return await response(scope, receive, send)
        session_key = self._session(request)
        if session_key is None:
            return await self._json({"error": "session_required"}, 401)(scope, receive, send)
        if self.service.state == "draining":
            return await self._json(DRAINING if syncing else {"error": "draining"}, 503)(scope, receive, send)
        # Sync's operations follow the sync task's drain, as /admin/user-sync does (sync_loop.py):
        # a queued one must not hold the drain that would end it.
        counted = not syncing
        # The Sync page's and the statistics' polls are not activity: an open page must not hold
        # back an automatic update.
        active = not ((syncing and self.sync.passive(request.method, path)) or path in PAGE_READS)
        if counted:
            self.service.inflight += 1
        if active:
            self.service.last_activity = time.monotonic()
        try:
            response = await self._api(request, path)
            renewed = await asyncio.to_thread(self._new_cookie, session_key)
            if renewed:
                self._set_cookie(response, renewed)  # sliding window
        finally:
            if counted:
                self.service.inflight -= 1
            if active:
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
            body = await self._body(request)
        except FlowError:
            return self._json({"error": "code_invalid"}, 401)
        refused = self._json({"error": "sign_in_required"}, 401)
        if "code" in body:
            code = body["code"]
            expiry = self.codes.pop(code, None) if isinstance(code, str) else None
            if expiry is None or expiry < time.monotonic():
                return self._json({"error": "code_invalid"}, 401)
        elif any(name in request.headers for name in FORWARDING_HEADERS) or \
                not await asyncio.to_thread(auto_sign_in_enabled, self.service.directory):
            return refused
        try:
            key = await asyncio.to_thread(self._session_key)
        except OSError:
            return self._json({"error": "session_key_unavailable"}, 500)
        if "code" not in body:
            async with self._peer_lock:
                owner = await asyncio.to_thread(self.peer_check, request.scope.get("client"),
                                                request.scope.get("server"))
            # `flows-ui --auto off` or `--revoke` may have run during the check. Refuse then;
            # a later revocation still wins, because the cookie is signed with this key.
            if not owner or not await asyncio.to_thread(self._still_admitted, key):
                return refused
        response = self._json({"status": "ok"})
        self._set_cookie(response, self._new_cookie(key))
        return response

    def _session_key(self) -> bytes:
        with self._key_lock:  # two first sign-ins must not each create a key
            if read_session_key(self.service.directory) is None:
                replace_session_key(self.service.directory)
            key = read_session_key(self.service.directory)
        if key is None:
            raise OSError("the session key cannot be read back")
        return key

    def _still_admitted(self, key: bytes) -> bool:
        return auto_sign_in_enabled(self.service.directory) and read_session_key(self.service.directory) == key

    def _library(self, workspace: str | None, repo: str | None = None) -> FlowLibrary:
        """A library for one request.

        An existing repository flow is addressed by its key (``repo``); a workspace
        is needed only to create one, because the key is derived from its remote.
        """
        root, error = None, None
        if workspace:
            try:
                root = self.service.registry.resolve(workspace)
            except WorkspaceError as failure:
                error = str(failure)
        return FlowLibrary(FlowCatalog(), repo_root=root, repo_error=error or "workspace_required",
                           repo_key=repo or None)

    @staticmethod
    def _with_key(library: FlowLibrary, result: dict) -> dict:
        flow = result.get("flow")
        if isinstance(flow, dict) and flow.get("source") == "repo":
            flow["repo_key"] = library.repo()[0]
        return result

    async def _api(self, request: Request, path: str):
        if is_sync_path(path):
            return await self.sync.handle(request, path)
        query = request.query_params
        try:
            if path == STATS_PATH and request.method == "GET":
                return self._json(await asyncio.to_thread(self.service.usage.stats))
            if path == OVERVIEW_PATH and request.method == "GET":
                return self._json(await asyncio.to_thread(self.service.overview.read))
            if path == "/ui/api/workspaces" and request.method == "GET":
                records = await asyncio.to_thread(read_json, self.service.registry.path, {})
                return self._json({"workspaces": [
                    {"id": identity, "path": root, "name": Path(root).name}
                    for identity, root in sorted(records.items(), key=lambda item: item[1])]})
            if path == "/ui/api/flows" and request.method == "GET":
                # The page sends no workspace; the parameter stays for API callers.
                library = self._library(query.get("workspace"))
                # `with_content=1` adds each flow's text, which the page searches.
                with_content = query.get("with_content") == "1"
                listing = await asyncio.to_thread(
                    lambda: library.list("all", with_content=with_content))
                listing["repositories"] = await asyncio.to_thread(
                    lambda: library.repositories(with_content=with_content))
                return self._json(listing)
            if path == "/ui/api/flow":
                if request.method == "GET":
                    library = self._library(query.get("workspace"), query.get("repo"))
                    result = await asyncio.to_thread(
                        library.get, query.get("id", ""), query.get("version") or None)
                    return self._json(self._with_key(library, result))
                body = await self._body(request)
                library = self._library(body.get("workspace"), body.get("repo"))
                if request.method == "PUT":
                    result = await asyncio.to_thread(
                        library.save, str(body.get("id", "")), body.get("content"),
                        scope=body.get("scope") or "user",
                        expected_revision=body.get("expected_revision") or None,
                        override=bool(body.get("override")))
                    return self._json(self._with_key(library, result))
                if request.method == "DELETE":
                    return self._json(await asyncio.to_thread(
                        library.delete, str(body.get("id", "")),
                        expected_revision=str(body.get("expected_revision") or "")))
            if path == "/ui/api/flow/persona" and request.method == "PUT":
                body = await self._body(request)
                if not isinstance(body.get("reset", False), bool):
                    raise FlowError("flow_invalid: reset must be true or false")
                library = self._library(body.get("workspace"), body.get("repo"))
                result = await asyncio.to_thread(
                    library.set_persona, str(body.get("id", "")), body.get("persona"),
                    reset=body.get("reset", False))
                return self._json(self._with_key(library, result))
            if path == "/ui/api/agents" and request.method == "GET":
                # `with_content=1` adds what the Agents tab shows and searches; the Persona
                # picker asks without it and keeps getting the short entries.
                if query.get("with_content") == "1":
                    agents = await asyncio.to_thread(list_agents, with_content=True)
                else:
                    agents = await asyncio.to_thread(list_agents)
                return self._json({"agents": agents})
            if path == "/ui/api/components" and request.method == "GET":
                kind = query.get("kind", "")
                if kind not in component_toggles.KINDS:
                    raise FlowError("flow_invalid: kind must be rules, skills or implants")
                return self._json({"kind": kind, "items": await asyncio.to_thread(list_components, kind)})
            if path == "/ui/api/component" and request.method == "PUT":
                body = await self._body(request)
                kind, component_id = body.get("kind"), body.get("id")
                if kind not in component_toggles.KINDS or not isinstance(body.get("enabled"), bool):
                    raise FlowError("flow_invalid: kind and a boolean enabled are required")
                if not isinstance(component_id, str) or component_id not in await asyncio.to_thread(
                        known_ids, kind):
                    raise FlowError("flow_not_found: unknown component")
                await asyncio.to_thread(component_toggles.set_enabled, kind, component_id,
                                        body["enabled"])
                return self._json({"status": "ok", "kind": kind, "id": component_id,
                                   "enabled": body["enabled"]})
            return self._json({"error": "not_found"}, 404)
        except (FlowError, component_toggles.ToggleError) as error:
            return self._json({"status": "error", "error": str(error)}, _error_status(str(error)))
        except OSError as error:
            return self._json({"status": "error", "error": f"storage_error: {error.strerror or error}"}, 500)
