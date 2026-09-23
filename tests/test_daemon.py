"""Fast transport, workspace and quota checks. No embedding model is loaded."""
import asyncio
import json
from pathlib import Path
import threading

import httpx
import pytest
from mcp.server.fastmcp import FastMCP, Context

from src.daemon.app import create_app
from src.daemon.execution import TrackedExecutor, request_jobs, finish_jobs
from src.daemon.workspaces import WorkspaceRegistry, WorkspaceError, client_context, HistoryStores, ClientContext


TOKEN = "x" * 48


def fake_runtime(port):
    mcp = FastMCP("test", host="127.0.0.1", port=port, stateless_http=True, json_response=True)
    @mcp.tool()
    def identity(ctx: Context):
        context = client_context(ctx)
        return {"root": str(context.require_root()), "request_id": context.request_id}
    @mcp.tool()
    def route():
        return "works without workspace"
    return mcp, None


@pytest.mark.asyncio
async def test_http_stateless_auth_and_workspace(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir(); b.mkdir()
    registry = WorkspaceRegistry(tmp_path / "service")
    aid, bid = registry.register(a), registry.register(b)
    app = create_app(registry.directory, TOKEN, runtime_loader=fake_runtime)
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if app.state.service.state != "starting": break
            await asyncio.sleep(.01)
        assert app.state.service.state == "ready"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765") as http:
            assert (await http.get("/health")).status_code == 401
            headers = {"Authorization": "Bearer " + TOKEN, "Accept": "application/json, text/event-stream"}
            assert (await http.get("/health", headers=headers)).status_code == 200
            payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "identity", "arguments": {}}}
            responses = await asyncio.gather(*(http.post("/mcp", json=payload, headers={**headers, "X-Agents-Workspace": identity}) for identity in [aid, bid] * 10))
            contents = [json.loads(r.json()["result"]["content"][0]["text"]) for r in responses]
            assert {v["root"] for v in contents} == {str(a), str(b)}
            assert len({v["request_id"] for v in contents}) == 20
            assert all("mcp-session-id" not in r.headers for r in responses)
            no_workspace = await http.post("/mcp", json=payload, headers=headers)
            assert "workspace_required" in no_workspace.text
            assert not (tmp_path / "history.md").exists()
            for extra in [{"Host": "attacker.example"}, {"Origin": "https://attacker.example"}]:
                response = await http.post("/mcp", json=payload, headers={**headers, **extra})
                assert response.status_code in (400, 403, 421)
                assert "access-control-allow-origin" not in response.headers
            await http.post("/admin/drain", headers=headers)
            assert (await http.post("/mcp", json=payload, headers=headers)).status_code == 503


def test_registry_identity_and_bounds(tmp_path):
    project = tmp_path / "repo"; project.mkdir()
    alias = tmp_path / "alias"; alias.symlink_to(project)
    registry = WorkspaceRegistry(tmp_path / "service")
    identity = registry.register(project)
    assert registry.register(alias) == identity
    with pytest.raises(WorkspaceError, match="invalid"):
        registry.resolve("not-a-uuid")
    context = ClientContext("request", "http", identity, project)
    with pytest.raises(WorkspaceError): context.target("..")
    project.rmdir()
    with pytest.raises(WorkspaceError): registry.resolve(identity)


def test_memory_symlink_cannot_escape_workspace(tmp_path):
    project = tmp_path / "project"; project.mkdir()
    external = tmp_path / "outside"; external.mkdir()
    (project / "history.md").symlink_to(external / "history.md")
    context = ClientContext("request", "http", "unused", project)
    with pytest.raises(WorkspaceError, match="escapes"):
        context.require_root()
    assert not list(external.iterdir())


@pytest.mark.asyncio
async def test_admission_is_atomic_and_disconnect_retains_requests(tmp_path):
    released = asyncio.Event()
    def loader(port):
        mcp = FastMCP("test", host="127.0.0.1", port=port, stateless_http=True, json_response=True)
        @mcp.tool()
        async def wait():
            await released.wait()
            return "done"
        return mcp, None
    app = create_app(tmp_path, TOKEN, runtime_loader=loader)
    async with app.router.lifespan_context(app):
        while app.state.service.state == "starting": await asyncio.sleep(.001)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer " + TOKEN, "Accept": "application/json, text/event-stream"}) as http:
            payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "wait", "arguments": {}}}
            calls = [asyncio.create_task(http.post("/mcp", json=payload)) for _ in range(32)]
            while app.state.service.inflight < 32: await asyncio.sleep(.001)
            assert (await http.post("/mcp", json=payload)).status_code == 503
            calls[0].cancel()
            await asyncio.gather(calls[0], return_exceptions=True)
            assert app.state.service.inflight == 32
            released.set()
            await asyncio.gather(*calls[1:])
            while app.state.service.inflight: await asyncio.sleep(.001)
            assert not app.state.service.requests


@pytest.mark.asyncio
async def test_idle_streams_are_not_work_and_drain_closes_them(tmp_path):
    """#76: a client's GET /mcp stream must not hold drain open forever."""
    app = create_app(tmp_path, TOKEN, runtime_loader=fake_runtime)
    service = app.state.service
    async with app.router.lifespan_context(app):
        while service.state == "starting": await asyncio.sleep(.001)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer " + TOKEN, "Accept": "application/json, text/event-stream"}) as http:
            async def open_stream():
                task = asyncio.create_task(http.get("/mcp"))
                deadline = asyncio.get_running_loop().time() + 5
                while service.streams < 1:
                    assert not task.done(), "stream ended before it was admitted"
                    assert asyncio.get_running_loop().time() < deadline, "stream was never admitted"
                    await asyncio.sleep(.001)
                return task
            stream = await open_stream()
            health = (await http.get("/health")).json()
            assert health["inflight"] == 0 and health["streams"] == 1
            payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "route", "arguments": {}}}
            assert (await http.post("/mcp", json=payload)).status_code == 200
            assert (await http.get("/health")).json()["idle_seconds"] >= 0
            drained = (await http.post("/admin/drain")).json()
            response = await asyncio.wait_for(stream, timeout=5)
            assert response.status_code == 200  # ended cleanly, not reset
            assert service.streams == 0 and not service.stream_closers
            assert drained["inflight"] == 0
            assert (await http.get("/mcp")).status_code == 503  # no new streams while draining
            # A drain that times out resumes the service; streams work again and close again.
            await http.post("/admin/resume")
            stream = await open_stream()
            await http.post("/admin/drain")
            assert (await asyncio.wait_for(stream, timeout=5)).status_code == 200
            assert service.streams == 0


def test_history_lru_retains_active_stores(tmp_path):
    stores = HistoryStores(capacity=1)
    a = ClientContext("a", "http", root=tmp_path / "a")
    b = ClientContext("b", "http", root=tmp_path / "b")
    with stores.acquire(a):
        with pytest.raises(WorkspaceError, match="busy"):
            with stores.acquire(b): pass
    with stores.acquire(b):
        assert list(stores.entries) == [b.root]


@pytest.mark.asyncio
async def test_cancelled_waiter_retains_worker_quota():
    executor = TrackedExecutor(workers=1, capacity=1)
    release, started = threading.Event(), threading.Event()
    jobs = []
    context = request_jobs.set(jobs)
    def work():
        started.set(); release.wait(2)
    future = asyncio.get_running_loop().run_in_executor(executor, work)
    request_jobs.reset(context)
    while not started.is_set(): await asyncio.sleep(.001)
    future.cancel()
    assert executor.inflight == 1
    with pytest.raises(RuntimeError, match="busy"): executor.submit(lambda: None)
    release.set()
    await finish_jobs(jobs)
    assert executor.inflight == 0
    executor.shutdown()
