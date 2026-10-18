"""Saved host inventory uses the existing authenticated MCP management surface."""
import pytest

from src.mcp_registry import Registry
from tests.test_history_ui import UI, daemon, login  # noqa: F401
from tests.test_mcp_registry import definition


@pytest.mark.asyncio
async def test_host_inventory_requires_session_and_ui_header(daemon, tmp_path):
    http, _ = daemon
    service = http._transport.app.state.service
    service.mcp_registry = Registry(tmp_path / "library", tmp_path / "state")
    # Keep the fixture's unused supervisor intact for ordinary lifespan cleanup.
    service.mcp_proxy.status = lambda: []
    saved = service.mcp_registry.save(definition())
    service.mcp_registry.enable("example", ["codex"], saved["revision"])
    assert (await http.get("/ui/api/mcp", headers=UI)).status_code == 401
    await login(http)
    assert (await http.get("/ui/api/mcp")).status_code == 403
    response = await http.get("/ui/api/mcp", headers=UI)
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    [host] = response.json()["machines"]
    assert host["current"] is True
    assert host["servers"]["example"]["apps"] == ["codex"]
    assert "state" not in host and "secret" not in host
    assert "Saved hosts" in (await http.get("/ui/mcp")).text
