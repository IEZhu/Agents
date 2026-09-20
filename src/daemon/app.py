"""Stateless HTTP transport with a single outer runtime lifecycle."""
import asyncio
from contextlib import asynccontextmanager
import hmac
import importlib.metadata
import logging
import os
from pathlib import Path
import time
import uuid

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse

from .execution import TrackedExecutor, request_jobs, finish_jobs
from .workspaces import ClientContext, WorkspaceRegistry, WorkspaceError

logger = logging.getLogger(__name__)


def load_runtime(port):
    from src import server
    from src.engine.embedder import embed_query
    from src.engine.rules import get_rules
    from src.engine.fingerprint import configuration_revision
    embed_query("warmup")  # failure prevents ready
    # Rule warmup uses the same loader as normal enrichment.
    get_rules(strict=True)
    configuration_revision()
    server.mcp.settings.host = "127.0.0.1"
    server.mcp.settings.port = port
    server.mcp.settings.stateless_http = True
    server.mcp.settings.json_response = True
    server.mcp.remove_tool("clear_session_cache")
    return server.mcp, server


class Service:
    def __init__(self, directory, token, port=8765, runtime_loader=load_runtime):
        self.directory = Path(directory)
        self.token = token
        self.port = port
        self.registry = WorkspaceRegistry(directory)
        self.loader = runtime_loader
        self.state = "starting"
        self.started = time.monotonic()
        self.inflight = 0
        self.io = None
        self.transport = None
        self.server = None
        self.boot_id = str(uuid.uuid4())
        self.loop_lag_max = 0.0
        self.startup_loop_lag_max = 0.0
        self.stop = asyncio.Event()
        self.requests = set()

    def health(self):
        import sys
        inference = getattr(sys.modules.get("src.engine.embedder"), "_inference", None)
        return {"state": self.state, "service": "Agents-Core", "boot_id": self.boot_id,
                "pid": os.getpid(), "version": importlib.metadata.version("mcp"),
                "install_root": str(Path(__file__).resolve().parents[2]),
                "uptime_seconds": round(time.monotonic() - self.started, 3),
                "model_ready": self.transport is not None,
                "inflight": self.inflight, "io_pending": self.io.inflight if self.io else 0,
                "loop_lag_max_seconds": round(self.loop_lag_max, 6),
                "startup_loop_lag_max_seconds": round(self.startup_loop_lag_max, 6),
                "inference_pending": inference.pending if inference else 0}

    async def start_runtime(self):
        try:
            mcp, self.server = await asyncio.to_thread(self.loader, self.port)
            transport = mcp.streamable_http_app()
            async with mcp.session_manager.run():
                self.transport = transport
                self.state = "ready"
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
        asyncio.get_running_loop().set_default_executor(self.io)
        runtime = asyncio.create_task(self.start_runtime())
        diagnostics = asyncio.create_task(self.retain_diagnostics())
        loop_monitor = asyncio.create_task(self.monitor_loop())
        try:
            yield
        finally:
            self.state = "draining"
            deadline = time.monotonic() + 60
            while (self.inflight or self.io.inflight) and time.monotonic() < deadline:
                await asyncio.sleep(.05)
            self.stop.set()
            await runtime
            await diagnostics
            await loop_monitor
            self.io.shutdown(wait=True, cancel_futures=False)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return
        request = Request(scope, receive)
        supplied = request.headers.get("authorization", "")
        if not hmac.compare_digest(supplied.encode(), ("Bearer " + self.token).encode()):
            return await JSONResponse({"error": "unauthorized"}, 401)(scope, receive, send)
        path = scope["path"]
        if path == "/health":
            return await JSONResponse(self.health(), 200 if self.state == "ready" else 503)(scope, receive, send)
        if path == "/admin/drain" and request.method == "POST":
            self.state = "draining"
            return await JSONResponse(self.health())(scope, receive, send)
        if path == "/admin/resume" and request.method == "POST":
            if self.transport is not None:
                self.state = "ready"
            return await JSONResponse(self.health())(scope, receive, send)
        if path == "/admin/cache/clear" and request.method == "POST" and self.server:
            await self.server.clear_session_cache()
            return await JSONResponse({"status": "cleared"})(scope, receive, send)
        if path != "/mcp":
            return await JSONResponse({"error": "not_found"}, 404)(scope, receive, send)
        if self.state != "ready":
            return await JSONResponse({"error": self.state}, 503)(scope, receive, send)
        if self.inflight >= 32:
            return await JSONResponse({"error": "busy"}, 503, headers={"Retry-After": "1"})(scope, receive, send)
        self.inflight += 1  # Admission occurs before the first await.
        jobs = []
        tracking = request_jobs.set(jobs)
        async def handle():
            try:
                identity = request.headers.get("x-agents-workspace")
                root, error = None, None
                try:
                    root = await asyncio.to_thread(self.registry.resolve, identity)
                except WorkspaceError as failure:
                    error = str(failure)
                context = ClientContext(str(uuid.uuid4()), "http", identity, root, error)
                scope.setdefault("state", {})["client_context"] = context
                await self.transport(scope, receive, send)
            finally:
                await finish_jobs(jobs)
                self.inflight -= 1
        task = asyncio.create_task(handle())
        self.requests.add(task)
        task.add_done_callback(self.requests.discard)
        request_jobs.reset(tracking)
        # A transport disconnect cannot cancel an already submitted mutation.
        await asyncio.shield(task)


def create_app(directory, token, port=8765, runtime_loader=load_runtime):
    if not token or len(token) < 32:
        raise ValueError("A private bearer token of at least 32 characters is required")
    service = Service(directory, token, port, runtime_loader)
    app = Starlette(lifespan=service.lifespan)
    # Route the original scope directly so MCP receives the request state.
    app.router.default = service
    app.state.service = service
    return app
