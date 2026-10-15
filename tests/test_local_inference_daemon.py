"""Detached generation participates in daemon health, drain, resume and close."""
import asyncio
import json

import httpx
from mcp.server.fastmcp import FastMCP, Context
import pytest

from src.daemon.app import create_app
from src.daemon.control import Controller
from src.daemon.workspaces import WorkspaceRegistry
from tests.test_daemon import TOKEN
from tests.test_local_inference_service import make, arguments, poll


def runtime(port):
    mcp = FastMCP("fake ordinary runtime", host="127.0.0.1", port=port, stateless_http=True, json_response=True)
    @mcp.tool()
    async def ping(ctx: Context):
        return {"ordinary": "ready", "local_broker": id(ctx.request_context.request.state.local_inference_service)}
    return mcp, None


async def ready(app):
    for _ in range(100):
        if app.state.service.state != "starting":
            return
        await asyncio.sleep(.001)
    raise AssertionError("fake runtime did not start")


@pytest.mark.asyncio
async def test_one_injected_broker_health_drain_resume_and_shutdown(tmp_path):
    local, backend, client, context = await make(tmp_path)
    app = create_app(tmp_path / "daemon", TOKEN, runtime_loader=runtime, local_inference_service=local)
    registry = WorkspaceRegistry(tmp_path / "daemon")
    identity = registry.register(client.root)
    headers = {"Authorization": "Bearer " + TOKEN, "Accept": "application/json, text/event-stream",
               "X-Agents-Workspace": identity}
    async with app.router.lifespan_context(app):
        await ready(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765", headers=headers) as http:
            backend.allow.clear()
            job = (await local.call("local_generate", client, arguments(context)))["data"]
            await backend.started.wait()
            payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "ping", "arguments": {}}}
            response = (await http.post("/mcp", json=payload)).json()
            value = json.loads(response["result"]["content"][0]["text"])
            assert value == {"ordinary": "ready", "local_broker": id(local)}
            health = (await http.get("/health")).json()
            assert health["local_inference_pending"] == 1 and health["inference_pending"] == 0
            assert health["model_ready"] and health["local_inference_ready"]
            await http.post("/admin/drain")
            assert (await local.call("local_generate", client, arguments(context)))["error"]["code"] == "busy"
            backend.allow.set()
            assert (await poll(local, client, context, job))["data"]["state"] == "succeeded"
            await http.post("/admin/resume")
            backend.allow.clear(); backend.started.clear()
            await local.call("local_generate", client, arguments(context))
            await backend.started.wait()
    assert backend.live == 0 and local.pending == 0 and local._closed


@pytest.mark.asyncio
async def test_optional_invalid_configuration_does_not_fail_ordinary_runtime(tmp_path, monkeypatch):
    path = tmp_path / "invalid.json"; path.write_text('{"unrecognized":"sentinel-secret"}')
    monkeypatch.setenv("AGENTS_LOCAL_INFERENCE_CONFIG", str(path))
    app = create_app(tmp_path / "daemon", TOKEN, runtime_loader=runtime)
    async with app.router.lifespan_context(app):
        await ready(app)
        local = app.state.service.local_inference
        assert local.config is None  # even ordinary health keeps loading lazy
        assert app.state.service.health()["state"] == "ready"
        assert local.status()["error"]["code"] == "configuration_invalid"
        assert app.state.service.health()["state"] == "ready" and local.backend is None


def test_controller_drain_counts_detached_generation(monkeypatch):
    controller = object.__new__(Controller)
    calls, slept = [], []
    pending = [1]
    def request(path="/health", *, method=None):
        calls.append((path, method))
        return {"inflight": 0, "io_pending": 0, "streams": 0, "local_inference_pending": pending[0]}
    controller.request = request
    def sleep(_):
        slept.append(True); pending[0] = 0
    monkeypatch.setattr("src.daemon.control.time.sleep", sleep)
    controller._drain(timeout=1)
    assert slept and calls[0] == ("/admin/drain", "POST")
    assert not any(path == "/admin/resume" for path, _ in calls)
