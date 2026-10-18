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

from src.version import agents_core_branch, agents_core_version
from src.runtime_paths import installation_root, runtime_root
from .execution import TrackedExecutor, request_jobs, finish_jobs
from .flows_ui import FlowsUI
from .overview import Overview
from .sync_loop import UserSync
from .usage import Usage, app_name, observing
from .workspaces import ClientContext, WorkspaceRegistry, WorkspaceError

logger = logging.getLogger(__name__)

MAX_INFLIGHT = 32
MAX_STREAMS = 32
COOPERATION_RETENTION_INTERVAL = 60 * 60
# Where a bridge's `/workspaces` path came from (`register_workspace`).
ORIGINS = ("cwd", "CLAUDE_PROJECT_DIR", "AGENTS_CLIENT_REPO_ROOT", "hook")


# How a bridge says why the workspace a call named was refused (#292).
REFUSED = ("workspace_required", "workspace_invalid", "workspace_unsafe")


def claimed_workspace(headers):
    """The directory a bridge names when no workspace resolved (#292): an absolute path, percent-encoded
    in X-Agents-Claimed-Workspace. Unverified; it only travels with a turn into the fallback journal."""
    from urllib.parse import unquote
    from src.memory.fallback import MAX_CLAIM, clean_claim
    value = headers.get("x-agents-claimed-workspace")
    if not value or len(value) > 3 * MAX_CLAIM:
        return None
    try:
        return clean_claim(unquote(value, errors="strict"))
    except UnicodeDecodeError:
        return None


def refusal(headers, error):
    """The reason a bridge gives for a refused named workspace, else the daemon's own error."""
    reason = headers.get("x-agents-workspace-refused")
    if reason in REFUSED:
        return f"{reason}: the workspace this call named was refused (see the bridge's stderr for details)"
    return error


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
    def __init__(self, directory, token, port=8765, runtime_loader=load_runtime, *, hand_off_schedule=False,
                 local_inference_service=None, artifact_service=None):
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
        self.cooperation = None
        self.boot_id = str(uuid.uuid4())
        self.loop_lag_max = 0.0
        self.startup_loop_lag_max = 0.0
        self.stop = asyncio.Event()
        self.requests = set()
        self.usage = Usage(self)  # answer counts and the apps that use the daemon (#187)
        self.overview = Overview(self)  # the landing page's facts about the installation (#188)
        from src.mcp_registry import Registry
        from src.mcp_secrets import Secrets
        from .mcp_proxy.upstreams import Supervisor
        library = Path(os.environ.get("AGENTS_USER_FLOWS_DIR") or installation_root() / "flows" / ".user")
        self.mcp_registry = Registry(library, self.directory)
        self.mcp_secrets = Secrets(self.directory)
        self.mcp_proxy = Supervisor(self.mcp_registry, self.mcp_secrets, self.directory)
        self.flows_ui = FlowsUI(self)
        # Only the service's entry point hands the library over from the OS scheduler (#168).
        self.user_sync = UserSync(self, hand_off_schedule=hand_off_schedule)
        # This separate lazy broker does not read configuration or start a model here.
        from src.local_inference.service import LocalInferenceService
        from src.artifacts.service import ArtifactService
        self.artifacts = artifact_service if artifact_service is not None else ArtifactService(self.directory / "artifacts")
        self.local_inference = (local_inference_service if local_inference_service is not None else
                                LocalInferenceService(self.directory / "local-inference", artifact_service=self.artifacts))

    def health(self):
        import sys
        inference = getattr(sys.modules.get("src.engine.embedder"), "_inference", None)
        return {"state": self.state, "service": "Agents-Core", "boot_id": self.boot_id,
                "pid": os.getpid(), "version": importlib.metadata.version("mcp"),
                "agents_core_version": agents_core_version(),
                # The checked-out branch; `channel show` names the configured one (#381).
                "agents_core_branch": agents_core_branch(),
                "install_root": str(installation_root()), "runtime_root": str(runtime_root()),
                "uptime_seconds": round(time.monotonic() - self.started, 3),
                "model_ready": self.transport is not None,
                "inflight": self.inflight, "streams": self.streams,
                "idle_seconds": 0.0 if self.inflight else round(time.monotonic() - self.last_activity, 1),
                # User sync's engine calls run in their own threads (sync_loop.py) and count here too.
                "io_pending": (self.io.inflight if self.io else 0) + self.user_sync.pending + self.artifacts.pending,
                "loop_lag_max_seconds": round(self.loop_lag_max, 6),
                "startup_loop_lag_max_seconds": round(self.startup_loop_lag_max, 6),
                "inference_pending": inference.pending if inference else 0,
                "user_sync": self.user_sync.summary(), "mcp": self.mcp_proxy.health(), **self.local_inference.health()}

    async def start_runtime(self):
        maintenance = None
        maintenance_stop = asyncio.Event()
        try:
            mcp, self.server = await asyncio.to_thread(self.loader, self.port)
            if os.environ.get("AGENTS_COOPERATION_ENABLED") == "1":
                # This outer daemon, never a stdio/worktree server, owns the
                # single optional coordinator and its service epoch.
                coordinator = None
                try:
                    from src.cooperation.service import CooperationService
                    retention_days = int(os.environ.get("AGENTS_COOPERATION_RETENTION_DAYS", "30"))
                    if retention_days < 30:
                        raise ValueError("Cooperation retention must be at least 30 days")
                    coordinator = CooperationService(self.directory / "cooperation", retention_days=retention_days)
                    await asyncio.to_thread(coordinator.start)
                    self.cooperation = coordinator
                except Exception:
                    # Ordinary persona/history tools remain usable. Do not log
                    # arbitrary exception text from a private coordination store.
                    logger.warning("Cooperation service is unavailable")
                    if coordinator is not None:
                        try:
                            await asyncio.to_thread(coordinator.close)
                        except Exception:
                            logger.warning("Cooperation startup cleanup failed")
            transport = mcp.streamable_http_app()
            async with mcp.session_manager.run():
                self.transport = transport
                self.state = "ready"
                self.user_sync.wake()  # automatic sync waits for readiness
                if self.cooperation is not None:
                    maintenance = asyncio.create_task(self.retain_cooperation(self.cooperation, maintenance_stop))
                await self.stop.wait()
        except Exception:
            self.state = "failed"
            logger.exception("Runtime initialization failed")
        finally:
            maintenance_stop.set()
            if maintenance is not None:
                # A cancelled to_thread waiter leaves its SQLite work running.
                # Join the maintenance task before releasing coordinator ownership.
                await maintenance
            if self.cooperation is not None:
                coordinator, self.cooperation = self.cooperation, None
                try:
                    await asyncio.to_thread(coordinator.close)
                except Exception:
                    logger.warning("Cooperation shutdown failed")

    async def retain_cooperation(self, coordinator, stopped):
        """One bounded-rate pass at a time, owned by the coordinator lifecycle."""
        while not stopped.is_set() and not self.stop.is_set():
            if self.state == "ready":
                try:
                    # The default TrackedExecutor includes maintenance in health
                    # and drain accounting. Never start another pass while draining.
                    await asyncio.to_thread(coordinator.collect_terminal)
                except Exception:
                    # Retry next interval without exposing private store contents.
                    logger.warning("Cooperation retention failed")
            try:
                await asyncio.wait_for(stopped.wait(), timeout=COOPERATION_RETENTION_INTERVAL)
            except asyncio.TimeoutError:
                pass

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
        await asyncio.to_thread(agents_core_branch)
        asyncio.get_running_loop().set_default_executor(self.io)
        # Migrate existing installations into the portable host inventory.
        # Imported inventories never become execution consent on this machine.
        if (self.directory / "mcp" / "registry.json").is_file():
            try:
                await asyncio.to_thread(self.mcp_registry.publish_machine)
            except (OSError, ValueError):
                logger.warning("MCP host inventory could not be published")
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
            self.mcp_proxy.begin_drain()
            self.local_inference.begin_drain()
            # Detached generation is fenced before any generic executor shutdown.
            try:
                await self.local_inference.close()
            except Exception:
                logger.warning("Optional local inference cleanup was not confirmed")
            await self.artifacts.close()
            deadline = time.monotonic() + 60
            while (self.inflight or self.io.inflight or self.user_sync.pending) and time.monotonic() < deadline:
                await asyncio.sleep(.05)
            await self.mcp_proxy.close()
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
        if path == "/admin/mcp":
            if self.state != "ready":
                return await JSONResponse({"error": self.state}, 503)(scope, receive, send)
            if self.inflight >= MAX_INFLIGHT:
                return await JSONResponse({"error": "busy"}, 503)(scope, receive, send)
            from .mcp_ui import handle
            self.inflight += 1
            try:
                response = await handle(self, request)
                return await response(scope, receive, send)
            finally:
                self.inflight -= 1
        if path == "/health":
            return await JSONResponse(self.health(), 200 if self.state == "ready" else 503)(scope, receive, send)
        if path == "/admin/drain" and request.method == "POST":
            self.state = "draining"
            # Clients reconnect their streams to the next process; stateless
            # requests carry no session, so nothing else is lost.
            self.close_streams()
            # Sync pushes what is left; io_pending covers it until it ends or is abandoned.
            self.user_sync.drain()
            self.mcp_proxy.begin_drain()
            self.local_inference.begin_drain()
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
                self.mcp_proxy.resume()
                self.local_inference.resume()
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
        directory the client named and is used as named; `origin: "hook"` is a
        client path authenticated by the stdio bridge's HMAC verification;
        `{path, origin: "AGENTS_CLIENT_REPO_ROOT"}` is a stdio client's override
        (#266), taken as a stdio server takes it; `{path, roots}` is a tool
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
                # The bridge resolves a relative value where its client started it; here it would
                # count from the installation, the service's working directory.
                if not os.path.isabs(directory):
                    raise ClientRootError(f"AGENTS_CLIENT_REPO_ROOT {directory!r} is not an absolute path",
                                          code="workspace_invalid")
                root = client_root_from_override(directory)
            elif roots is None:
                root = client_root_from_directory(directory, launch_directory=origin == "cwd")
            else:
                root = client_root_from_workspace(directory, roots)
            # Keep the physical authorization separately, before memory maps a
            # linked worktree to its main checkout. MCP arguments never register
            # or expand this grant.
            from src.engine.config import _find_marker_upwards, _root_uri_path
            physical = Path(directory).resolve(strict=True)
            if origin != "AGENTS_CLIENT_REPO_ROOT":
                marked = _find_marker_upwards(physical)
                if marked is not None:
                    authorized = (None if roots is None else
                                  [value.resolve() for value in map(_root_uri_path, roots) if value is not None])
                    # Only cwd implies project discovery. Named directories
                    # stay bounded unless explicit MCP roots cover the marker.
                    if ((roots is None and origin == "cwd") or (authorized is not None and
                            any(marked.is_relative_to(value) for value in authorized))):
                        physical = marked
            return root, self.registry.register(root, physical_root=physical)
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
        if root is None:
            error = refusal(request.headers, error)
        physical_roots = ()
        if root is not None:
            try:
                physical_roots = await asyncio.to_thread(self.registry.cooperation_roots, identity)
            except WorkspaceError:
                # A malformed or removed physical grant disables cooperation;
                # it does not change memory's independently verified root.
                pass
        context = ClientContext(str(uuid.uuid4()), "http", identity, root, error, client=app,
                                claimed=None if root else claimed_workspace(request.headers),
                                cooperation_roots=physical_roots)
        scope.setdefault("state", {})["client_context"] = context
        scope["state"]["local_inference_service"] = self.local_inference
        scope["state"]["artifact_service"] = self.artifacts
        scope["state"]["mcp_proxy"] = self.mcp_proxy
        scope["state"]["cooperation_service"] = self.cooperation
        scope["state"]["cooperation_authority"] = self.directory / "cooperation"
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


def create_app(directory, token, port=8765, runtime_loader=load_runtime, *, hand_off_schedule=False,
               local_inference_service=None, artifact_service=None):
    """``hand_off_schedule``: the user-sync task removes the scheduled sync run of #168 when it
    starts; only ``bootstrap.serve`` sets it, so tests never reach the OS scheduler."""
    if not token or len(token) < 32:
        raise ValueError("A private bearer token of at least 32 characters is required")
    service = Service(directory, token, port, runtime_loader, hand_off_schedule=hand_off_schedule,
                      local_inference_service=local_inference_service, artifact_service=artifact_service)
    app = Starlette(lifespan=service.lifespan)
    # Route the original scope directly so MCP receives the request state.
    app.router.default = service
    app.state.service = service
    return app
