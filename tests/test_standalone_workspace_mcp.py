"""Native transport metadata reaches async and sync workspace tools alike."""
import hashlib
import hmac
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import server, stdio_workspace
from src.daemon.workspaces import ClientContext, client_context, resolve_client_context
from src.engine import config
from tests.test_subagent import worker_tree, request


@pytest.fixture
def native(tmp_path, monkeypatch):
    for name in ("AGENTS_CLIENT_REPO_ROOT", "CLAUDE_PROJECT_DIR", "AGENTS_TRANSPORT"):
        monkeypatch.delenv(name, raising=False)
    project = tmp_path / "empty project"
    project.mkdir()
    auth = tmp_path / "private.json"
    auth.write_text(json.dumps({"transport": "stdio", "installation": str(stdio_workspace.runtime_paths.installation_root()),
        "headers": {"Authorization": "Bearer native-secret"}}))
    auth.chmod(0o600)
    if os.name == "nt":
        from src.daemon.acl import restrict
        restrict(auth)
    monkeypatch.setenv(stdio_workspace.AUTH_CONFIG_ENV, str(auth))
    monkeypatch.chdir(tmp_path.anchor)
    config._reset_client_repo_root_cache()
    proof = hmac.new(b"native-secret", ("workspace:" + str(project)).encode(), hashlib.sha256).hexdigest()
    yield project.resolve(), {"workspace": str(project), "workspace_signature": proof}
    config._reset_client_repo_root_cache()


@pytest.mark.asyncio
async def test_native_metadata_is_removed_before_schema_validation_and_resets(native):
    project, arguments = native
    mcp = server._WorkerInputMCP("native test")

    @mcp.tool()
    async def probe() -> dict:
        return {"sync": str(client_context().require_root()), "async": str((await resolve_client_context()).require_root())}

    content = await mcp.call_tool("probe", arguments)
    assert json.loads(content[0].text) == {"sync": str(project), "async": str(project)}
    assert stdio_workspace.request_workspace.get() is None
    assert "workspace_signature" in arguments
    assert list(project.iterdir()) == []


@pytest.mark.asyncio
async def test_http_and_explicit_environment_keep_priority_over_native_proof(native, monkeypatch, tmp_path):
    project, arguments = native
    own = tmp_path / "owner"
    own.mkdir()
    with stdio_workspace.bind(arguments):
        monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(own))
        assert client_context().require_root() == own.resolve()
        assert (await resolve_client_context()).require_root() == own.resolve()
        http = ClientContext("request", "http", root=project)
        ctx = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(state=SimpleNamespace(client_context=http))))
        assert client_context(ctx) is http
        assert await resolve_client_context(ctx) is http


@pytest.mark.asyncio
async def test_native_proof_does_not_relax_strict_worker_arguments(native):
    _project, arguments = native
    content, structured = await server.mcp.call_tool("get_subagent_context", {**arguments, "unexpected": 1})
    assert structured["status"] == "error"
    paths = [error["path"] for error in structured["errors"]]
    assert "$.unexpected" in paths
    assert "$.workspace_signature" not in paths and "$.workspace" not in paths


@pytest.mark.asyncio
async def test_forwarded_native_proof_is_consumed_before_http_worker_validation(native, worker_tree, monkeypatch):
    import importlib.util
    from unittest.mock import AsyncMock
    bridge_path = Path(__file__).resolve().parents[1] / "bridge/stdio.py"
    spec = importlib.util.spec_from_file_location("native_forward_bridge", bridge_path)
    bridge_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bridge_module)
    bridge = bridge_module.Bridge({"workspace": "auto", "headers": {"Authorization": "Bearer http-secret"}}, None)
    bridge.register = AsyncMock(return_value="registered-project")
    _project, proof = native
    message = {"method": "tools/call", "params": {"name": "get_subagent_context", "arguments": {**request(), **proof}}}
    assert await bridge.workspace_for(message) == "registered-project"
    forwarded = message["params"]["arguments"]
    assert forwarded == request()
    monkeypatch.setenv("AGENTS_TRANSPORT", "http")
    _content, prepared = await server.mcp.call_tool("get_subagent_context", forwarded)
    assert prepared["status"] == "prepared"
    _content, rejected = await server.mcp.call_tool("get_subagent_context", {**forwarded, "unexpected": 1})
    assert rejected["errors"][0]["path"] == "$.unexpected"
