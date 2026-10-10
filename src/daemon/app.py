"""Stateless HTTP transport with a single outer runtime lifecycle."""
import asyncio
from contextlib import asynccontextmanager
import hmac
import importlib.metadata
import logging
import os
from pathlib import Path
import signal
import time
import uuid

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from src.version import agents_core_version
from .execution import TrackedExecutor, request_jobs, finish_jobs
from .flows_ui import FlowsUI
from .overview import Overview
from .sync_loop import UserSync
from .usage import Usage, app_name, observing
from .workspaces import ClientContext, WorkspaceRegistry, WorkspaceError

logger = logging.getLogger(__name__)

MAX_INFLIGHT = 32
MAX_STREAMS = 32
# Where a bridge's `/workspaces` path came from (`register_workspace`).
ORIGINS = ("cwd", "CLAUDE_PROJECT_DIR", "AGENTS_CLIENT_REPO_ROOT")


def load_runtime(port):
    from src import server
    from src.engine import readiness
    from src.engine.fingerprint import configuration_revision
    # Stores, embedding model and the strict rule load that persona bundles use;
    # any failure prevents ready, and /health stays 503 until all of it is done.
    readiness.run_blocking()
    configuration_revision()
    server.mcp.settings.host = "127.0.0.1"
    server.mcp.settings.port = port
    from src.engine.persona import configure_ui_port
    configure_ui_port(port)
    server.mcp.settings.stateless_http = True
    server.mcp.settings.json_response = True
    server.mcp.remove_tool("clear_session_cache")
    server.install_history_sync()
    return server.mcp, server


class Service:
    def __init__(self, directory, token, port=8765, runtime_loader=load_runtime, *, hand_off_schedule=False):
        self.directory = Path(directory)
        self.token = token
        self.port = port
        self.registry = WorkspaceRegistry(directory)
        self.loader = runtime_loader
        self.state = "starting"
        self.started = time.monotonic()
        # `inflight` counts work (POST/DELETE) and is what drain waits for.
        # A GET /mcp is an idle notification stream that lives as long as its
        # client (#76); it is counted apart and closed on drain instead.
        self.inflight = 0
        self.streams = 0
        self.stream_closers = set()
        self.last_activity = time.monotonic()
        self.io = None
        self.transport = None
        self.server = None
        self.boot_id = str(uuid.uuid4())
        self.loop_lag_max = 0.0
        self.startup_loop_lag_max = 0.0
        self.stop = asyncio.Event()
        self.requests = set()
        self.usage = Usage(self)  # answer counts and the apps that use the daemon (#187)
        self.overview = Overview(self)  # the landing page's facts about the installation (#188)
        self.flows_ui = FlowsUI(self)
        # Only the service's entry point hands the library over from the OS scheduler (#168).
        self.user_sync = UserSync(self, hand_off_schedule=hand_off_schedule)

    def health(self):
        import sys
        inference = getattr(sys.modules.get("src.engine.embedder"), "_inference", None)
        return {"state": self.state, "service": "Agents-Core", "boot_id": self.boot_id,
                "pid": os.getpid(), "version": importlib.metadata.version("mcp"),
                "agents_core_version": agents_core_version(),
                "install_root": str(Path(__file__).resolve().parents[2]),
                "uptime_seconds": round(time.monotonic() - self.started, 3),
                "model_ready": self.transport is not None,
                "inflight": self.inflight, "streams": self.streams,
                "idle_seconds": 0.0 if self.inflight else round(time.monotonic() - self.last_activity, 1),
                # User sync's engine calls run in their own threads (sync_loop.py) and count here too.
                "io_pending": (self.io.inflight if self.io else 0) + self.user_sync.pending,
                "loop_lag_max_seconds": round(self.loop_lag_max, 6),
                "startup_loop_lag_max_seconds": round(self.startup_loop_lag_max, 6),
                "inference_pending": inference.pending if inference else 0,
                "user_sync": self.user_sync.summary()}

    async def start_runtime(self):
        try:
            mcp, self.server = await asyncio.to_thread(self.loader, self.port)
            transport = mcp.streamable_http_app()
            async with mcp.session_manager.run():
                self.transport = transport
                self.state = "ready"
                self.user_sync.wake()  # automatic sync waits for readiness
                await self.stop.wait()
        except Exception:
            self.state = "failed"
            logger.exception("Runtime initialization failed")

    async def retain_diagnostics(self):
        from .diagnostics import prune_debug
        while not self.stop.is_set():
            await asyncio.to_thread(prune_debug, self.directory / "debug")
            try:
                await asyncio.wait_for(self.stop.wait(), timeout=30)
            except asyncio.TimeoutError:
                pass

    async def monitor_loop(self):
        while not self.stop.is_set():
            started = time.monotonic()
            ready = self.state == "ready"
            await asyncio.sleep(.05)
            lag = time.monotonic() - started - .05
            if ready and self.state == "ready":
                self.loop_lag_max = max(self.loop_lag_max, lag)
            else:
                self.startup_loop_lag_max = max(self.startup_loop_lag_max, lag)

    @asynccontextmanager
    async def lifespan(self, app):
        self.io = TrackedExecutor()
        # Fix the version before the first request can compute it on the event loop.
        await asyncio.to_thread(agents_core_version)
        asyncio.get_running_loop().set_default_executor(self.io)
        runtime = asyncio.create_task(self.start_runtime())
        diagnostics = asyncio.create_task(self.retain_diagnostics())
        loop_monitor = asyncio.create_task(self.monitor_loop())
        # Automatic sync begins once the service is ready; admin operations work from the start.
        self.user_sync.start()
        try:
            yield
        finally:
            self.state = "draining"
            self.close_streams()
            self.user_sync.drain()
            deadline = time.monotonic() + 60
            while (self.inflight or self.io.inflight or self.user_sync.pending) and time.monotonic() < deadline:
                await asyncio.sleep(.05)
            self.stop.set()
            self.user_sync.wake()
            await runtime
            await diagnostics
            await loop_monitor
            await self.user_sync.finish()
            # Queued log_interaction writes run on their own executor; flush them
            # before the process can exit.
            if self.server is not None:
                await asyncio.to_thread(self.server.drain_pending_logs)
            self.io.shutdown(wait=True, cancel_futures=False)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        if scope["path"] == "/ui" or scope["path"].startswith("/ui/"):
            # The browser editor has its own signed session (see flows_ui); it never receives the bearer token.
            return await self.flows_ui(scope, receive, send)
        request = Request(scope, receive)
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + self.token).encode()):
            return await JSONResponse({"error": "unauthorized"}, 401)(scope, receive, send)
        path = scope["path"]
        if path == "/health":
            return await JSONResponse(self.health(), 200 if self.state == "ready" else 503)(scope, receive, send)
        if path == "/admin/drain" and request.method == "POST":
            self.state = "draining"
            # Clients reconnect their streams to the next process; stateless
            # requests carry no session, so nothing else is lost.
            self.close_streams()
            # Sync pushes what is left; io_pending covers it until it ends or is abandoned.
            self.user_sync.drain()
            return await JSONResponse(self.health())(scope, receive, send)
        if path == "/admin/exit" and request.method == "POST":
            # Windows has no SIGTERM from outside: Task Scheduler's /End terminates the process, and the
            # lifespan's shutdown, which flushes queued log writes, would never run. The controller asks
            # here instead; uvicorn then shuts down as it does on the signal launchd sends.
            asyncio.get_running_loop().call_soon(signal.raise_signal, signal.SIGTERM)
            return await JSONResponse({"state": "exiting"})(scope, receive, send)
        if path == "/admin/resume" and request.method == "POST":
            if self.transport is not None:
                self.state = "ready"
                self.user_sync.resume()
            return await JSONResponse(self.health())(scope, receive, send)
        if path.startswith("/admin/user-sync/"):
            response = await self.user_sync.handle(request, path[len("/admin/user-sync/"):])
            return await response(scope, receive, send)
        if path == "/admin/ui/code" and request.method == "POST":
            code = self.flows_ui.issue_code()
            return await JSONResponse({"code": code, "url": f"http://127.0.0.1:{self.port}/ui#code={code}"})(
                scope, receive, send)
        if path == "/admin/cache/clear" and request.method == "POST" and self.server:
            await self.server.clear_session_cache()
            return await JSONResponse({"status": "cleared"})(scope, receive, send)
        if path == "/workspaces" and request.method == "POST":
            return await (await self.register_workspace(request))(scope, receive, send)
        if path != "/mcp":
            return await JSONResponse({"error": "not_found"}, 404)(scope, receive, send)
        if self.state != "ready":
            return await JSONResponse({"error": self.state}, 503)(scope, receive, send)
        app = app_name(request.headers.get("x-agents-client"))
        if request.method == "GET":
            return await self.stream(request, scope, receive, send, app)
        if self.inflight >= MAX_INFLIGHT:
            return await JSONResponse({"error": "busy"}, 503, headers={"Retry-After": "1"})(scope, receive, send)
        self.inflight += 1  # Admission occurs before the first await.
        self.last_activity = time.monotonic()
        self.usage.apps.request(app)
        jobs = []
        tracking = request_jobs.set(jobs)
        async def handle():
            try:
                await self.dispatch(request, scope, receive, send, app)
            finally:
                await finish_jobs(jobs)
                self.inflight -= 1
                self.last_activity = time.monotonic()
        task = asyncio.create_task(handle())
        self.requests.add(task)
        task.add_done_callback(self.requests.discard)
        request_jobs.reset(tracking)
        # A transport disconnect cannot cancel an already submitted mutation.
        await asyncio.shield(task)

    async def register_workspace(self, request):
        """Register the project a session's bridge names and answer its workspace UUID (#253).

        `{path}` is the bridge's launch directory, which counts only with a `.git` or
        `CLAUDE.md` at or above it; `{path, origin: "CLAUDE_PROJECT_DIR"}` is the
        directory the client named and is used as named; `{path, origin:
        "AGENTS_CLIENT_REPO_ROOT"}` is a stdio client's override (#266), taken as a
        stdio server takes it; `{path, roots}` is a tool
        call's workspace, which must lie inside the client's MCP roots. The checks of
        `src.engine.config` decide what is a project. The route takes the bearer token
        and is no MCP tool, so a model never reaches it.
        """
        try:
            body = await request.json()
            directory, roots, origin = body.get("path"), body.get("roots"), body.get("origin", "cwd")
            if not isinstance(directory, str) or origin not in ORIGINS or not (
                    roots is None or isinstance(roots, list) and all(isinstance(root, str) for root in roots)):
                raise ValueError("expected {path, origin?, roots?}")
        except (ValueError, AttributeError) as error:
            return JSONResponse({"error": "workspace_invalid", "message": str(error)}, 400)
        from src.engine.config import (ClientRootError, client_root_from_directory, client_root_from_override,
                                       client_root_from_workspace)

        def register():
            if origin == "AGENTS_CLIENT_REPO_ROOT" and roots is None:
                root = client_root_from_override(directory)
            elif roots is None:
                root = client_root_from_directory(directory, launch_directory=origin == "cwd")
            else:
                root = client_root_from_workspace(directory, roots)
            return root, self.registry.register(root)
        try:
            root, identity = await asyncio.to_thread(register)
        except ClientRootError as error:
            return JSONResponse({"error": error.code, "message": str(error)}, 400)
        except (WorkspaceError, OSError) as error:
            return JSONResponse({"error": "workspace_invalid", "message": str(error)}, 400)
        return JSONResponse({"workspace_id": identity, "root": root})

    async def dispatch(self, request, scope, receive, send, app=None):
        identity = request.headers.get("x-agents-workspace")
        root, error = None, None
        try:
            root = await asyncio.to_thread(self.registry.resolve, identity)
        except WorkspaceError as failure:
            error = str(failure)
        context = ClientContext(str(uuid.uuid4()), "http", identity, root, error, client=app)
        scope.setdefault("state", {})["client_context"] = context
        if request.method == "POST":
            # Only an initialize names the client (clientInfo); the transport still reads every byte.
            receive = observing(receive, self.usage.apps, app, identity if root else None)
        await self.transport(scope, receive, send)

    async def stream(self, request, scope, receive, send, app=None):
        if self.streams >= MAX_STREAMS:
            return await JSONResponse({"error": "busy"}, 503, headers={"Retry-After": "1"})(scope, receive, send)
        response = {"started": False, "ended": False}
        closing = asyncio.Event()

        async def tracked_send(message):
            await send(message)
            # Record only delivered messages: uvicorn may suspend a send for
            # flow control before it processes the message.
            if message["type"] == "http.response.start":
                response["started"] = True
            elif message["type"] == "http.response.body" and not message.get("more_body", False):
                response["ended"] = True

        async def closable_receive():
            # Drain ends a stream by reporting a disconnect, so the SSE response
            # and the MCP transport (terminate()) shut down through their own
            # cleanup instead of being cancelled halfway.
            if closing.is_set():
                return {"type": "http.disconnect"}
            message = asyncio.ensure_future(receive())
            closed = asyncio.ensure_future(closing.wait())
            done, _ = await asyncio.wait({message, closed}, return_when=asyncio.FIRST_COMPLETED)
            if message in done:
                closed.cancel()
                return message.result()
            message.cancel()
            return {"type": "http.disconnect"}

        self.stream_closers.add(closing)
        self.streams += 1
        self.usage.apps.stream_opened(app)
        try:
            await self.dispatch(request, scope, closable_receive, tracked_send, app)
            if closing.is_set():
                # The client is still connected: finish the response so it sees
                # an ended stream, not a reset, and reconnects to the next process.
                if not response["started"]:
                    await JSONResponse({"error": "draining"}, 503)(scope, receive, send)
                elif not response["ended"]:
                    await send({"type": "http.response.body", "body": b"", "more_body": False})
        finally:
            self.stream_closers.discard(closing)
            self.streams -= 1
            self.usage.apps.stream_closed(app)

    def close_streams(self):
        for closing in list(self.stream_closers):
            closing.set()


def create_app(directory, token, port=8765, runtime_loader=load_runtime, *, hand_off_schedule=False):
    """``hand_off_schedule``: the user-sync task removes the scheduled sync run of #168 when it
    starts; only ``bootstrap.serve`` sets it, so tests never reach the OS scheduler."""
    if not token or len(token) < 32:
        raise ValueError("A private bearer token of at least 32 characters is required")
    service = Service(directory, token, port, runtime_loader, hand_off_schedule=hand_off_schedule)
    app = Starlette(lifespan=service.lifespan)
    # Route the original scope directly so MCP receives the request state.
    app.router.default = service
    app.state.service = service
    return app
