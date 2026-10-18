"""Wire-level proxy results, policy gates and native-tool coexistence."""
import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
from mcp import types
from mcp.server.fastmcp import FastMCP
import pytest

from src.daemon.app import create_app
from src.daemon.mcp_proxy.exposure import ProxyToolsMixin, tool_name


class ProxyMCP(ProxyToolsMixin, FastMCP):
    pass


class FakeProxy:
    def __init__(self):
        self.calls = []
        self.denied = False

    def cached(self, app):
        if app != "codex" or self.denied:
            return []
        definition = {"alias": "fake", "title": "Fake", "tools": {"deny": ["hidden"]}}
        return [(definition, types.Tool(name=name, inputSchema={"type": "object"},
                annotations=types.ToolAnnotations(readOnlyHint=True), _meta={"custom": "kept"}))
                for name in ("echo", "hidden", "dotted.tool")]

    async def call(self, alias, tool, arguments, app):
        self.calls.append((alias, tool, arguments, app))
        return types.CallToolResult(content=[types.TextContent(type="text", text="original"),
            types.ImageContent(type="image", data="AA==", mimeType="image/png")],
            structuredContent={"answer": 7}, isError=True, _meta={"upstream": True})

    def begin_drain(self):
        pass

    def resume(self):
        pass

    async def close(self):
        pass

    def status(self):
        return []

    def health(self):
        return {"servers": 0, "ready": 0, "failed": 0}


@asynccontextmanager
async def application(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTS_USER_FLOWS_DIR", str(tmp_path / "library"))
    def runtime(port):
        mcp = ProxyMCP("test", stateless_http=True, json_response=True)
        @mcp.tool()
        def own():
            return "own result"
        return mcp, None
    app = create_app(tmp_path / "state", "d" * 64, runtime_loader=runtime)
    proxy = FakeProxy()
    app.state.service.mcp_proxy = proxy
    async with app.router.lifespan_context(app):
        async def ready():
            while app.state.service.state == "starting":
                await asyncio.sleep(0)
        await asyncio.wait_for(ready(), 2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765",
                headers={"Authorization": "Bearer " + "d" * 64,
                         "Accept": "application/json, text/event-stream", "X-Agents-Client": "codex"}) as http:
            yield http, proxy


async def rpc(http, method, params=None, **kwargs):
    response = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": method,
                                           "params": params or {}}, **kwargs)
    assert response.status_code == 200, response.text
    return response.json()["result"]


@pytest.mark.asyncio
async def test_wire_results_and_policy(tmp_path, monkeypatch):
    async with application(tmp_path, monkeypatch) as (http, proxy):
        listed = (await rpc(http, "tools/list"))["tools"]
        assert {tool["name"] for tool in listed} == {"own", "fake__echo", tool_name("fake", "dotted.tool")}
        assert next(tool for tool in listed if tool["name"] == "fake__echo")["_meta"] == {"custom": "kept"}
        assert not proxy.calls
        result = await rpc(http, "tools/call", {"name": "fake__echo", "arguments": {"x": 1}})
        assert result == {"content": [{"type": "text", "text": "original"},
                          {"type": "image", "data": "AA==", "mimeType": "image/png"}],
                          "structuredContent": {"answer": 7}, "isError": True, "_meta": {"upstream": True}}
        denied = await rpc(http, "tools/call", {"name": "fake__hidden"})
        assert denied["isError"] and len(proxy.calls) == 1
        proxy.denied = True
        assert (await rpc(http, "tools/call", {"name": "fake__echo"}))["isError"]
        assert len(proxy.calls) == 1
        assert not (await rpc(http, "tools/call", {"name": "own"}))["isError"]


@pytest.mark.asyncio
async def test_unknown_apps_and_standalone_see_no_proxy(tmp_path, monkeypatch):
    assert [tool.name for tool in await ProxyMCP("standalone").list_tools()] == []
    async with application(tmp_path, monkeypatch) as (http, proxy):
        for app in ("", "cursor", "unknown"):
            listed = await rpc(http, "tools/list", headers={"X-Agents-Client": app})
            assert [tool["name"] for tool in listed["tools"]] == ["own"]
            assert (await rpc(http, "tools/call", {"name": "fake__echo"}, headers={"X-Agents-Client": app}))["isError"]
        assert not proxy.calls


@pytest.mark.asyncio
async def test_bad_optional_registry_does_not_hide_native_tools(tmp_path, monkeypatch):
    from src.mcp_registry import RegistryError
    async with application(tmp_path, monkeypatch) as (http, proxy):
        def bad_registry(app):
            raise RegistryError("invalid", "Invalid local MCP registry")
        proxy.cached = bad_registry
        assert [tool["name"] for tool in (await rpc(http, "tools/list"))["tools"]] == ["own"]
        assert not (await rpc(http, "tools/call", {"name": "own"}))["isError"]


def test_names_are_stable_and_fit_client_budget():
    names = ["create_pull_request", "a.b", "a_b", "x" * 200, "🔥", "my.tool", "my_tool"]
    exposed = [tool_name("abcdefghijkl", name) for name in names]
    assert len(set(exposed)) == len(names)
    assert all(len("mcp__Agents-Core__" + name) <= 64 for name in exposed)


@pytest.mark.asyncio
async def test_management_authentication_and_confirmation(tmp_path, monkeypatch):
    async with application(tmp_path, monkeypatch) as (http, proxy):
        response = await http.get("/admin/mcp", headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401
        response = await http.get("/ui/api/mcp")
        assert response.status_code == 403
        service = http._transport.app.state.service
        code = service.flows_ui.issue_code()
        headers = {"Origin": "http://127.0.0.1:8765", "X-Agents-UI": "1"}
        assert (await http.post("/ui/api/session", json={"code": code}, headers=headers)).status_code == 200
        assert (await http.get("/ui/api/mcp", headers={"X-Agents-UI": "1"})).status_code == 200
        definition = {"alias": "fake", "transport": {"type": "http", "url": "http://127.0.0.1:12345/mcp"}}
        response = await http.post("/ui/api/mcp", json={"action": "save", "definition": definition}, headers=headers)
        assert response.status_code == 200, response.text
        revision = response.json()["result"]["revision"]
        response = await http.post("/ui/api/mcp", json={"action": "enable", "alias": "fake",
                                   "apps": ["codex"], "revision": revision}, headers=headers)
        assert response.status_code == 200, response.text
        assert service.mcp_registry.resolved("codex")[0]["alias"] == "fake"
        definition["transport"]["headers"] = {"Authorization": "Bearer secret-sentinel"}
        response = await http.post("/ui/api/mcp", json={"action": "save", "definition": definition}, headers=headers)
        assert response.status_code == 400 and "secret-sentinel" not in response.text
        assert (await http.post("/ui/api/mcp", json={"action": "connect", "alias": "fake"},
                                headers={"Origin": "https://untrusted.example", "X-Agents-UI": "1"})).status_code == 403
        response = await http.post("/admin/mcp", content=b"x" * 65537)
        assert response.status_code == 413
        service.state = "draining"
        response = await http.post("/admin/mcp", json={"action": "connect", "alias": "fake"})
        assert response.status_code == 503
