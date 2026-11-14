"""A repository-scoped call that names another workspace is reported, never answered (#576).

Over HTTP the registered workspace wins by design (`resolve_client_context`): a
``workspace`` argument must not widen the grant. These tests pin the other half of
that contract — a tool that answers *about* a repository says so instead of
describing, writing or running in the registered one, while the stdio path, where
the argument is checked against the client's own MCP roots, is untouched.
"""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import src.server as server
from src.daemon.app import create_app
from src.daemon.workspaces import (ClientContext, WorkspaceMismatch, WorkspaceRegistry,
                                   resolve_client_context)
from src.engine import config as engine_config
from src.flows import FlowCatalog
from tests.test_describer import _seed_repo, _valid_summary


TOKEN = "e" * 48


@pytest.fixture
def repositories(tmp_path, monkeypatch):
    """The registered repository, a second one outside it, and a directory inside it."""
    registered, other = tmp_path / "registered", tmp_path / "other"
    for root in (registered, other):
        root.mkdir()
        _seed_repo(root)
    inside = registered / "nested"
    inside.mkdir()
    for name in ("AGENTS_CLIENT_REPO_ROOT", "CLAUDE_PROJECT_DIR", "AGENTS_TRANSPORT"):
        monkeypatch.delenv(name, raising=False)
    engine_config._reset_client_repo_root_cache()
    yield registered.resolve(), other.resolve(), inside.resolve()
    engine_config._reset_client_repo_root_cache()


def http_context(root, identity="workspace-a"):
    """An MCP context for an HTTP request bound to the workspace registered for *root*."""
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(
        state=SimpleNamespace(client_context=ClientContext(
            "request", "http", workspace_id=identity, root=root)))))


def assert_mismatch(payload, registered, requested):
    """The agreed error shape: the code, both roots and how to reach the other repository."""
    assert payload["status"] == "error"
    assert payload["error"].startswith("workspace_mismatch: ")
    assert str(registered) in payload["error"] and str(requested) in payload["error"]
    assert payload["code"] == "workspace_mismatch"
    assert payload["registered_workspace"] == str(registered)
    assert payload["requested_workspace"] == str(requested)
    assert f"migrate --workspace {requested}" in payload["remedy"]
    assert "start a session" in payload["remedy"]


@pytest.mark.asyncio
async def test_resolution_marks_only_a_workspace_the_registered_root_does_not_cover(repositories):
    """The grant still decides the root; the ignored argument is recorded beside it."""
    registered, other, inside = repositories
    ctx = http_context(registered)
    for named in (None, str(registered), str(registered) + os.sep, str(inside), "nested"):
        context = await resolve_client_context(ctx, named)
        assert context.root == registered and context.mismatch is None
    for named in (str(other), str(other / "src"), str(other.parent)):
        context = await resolve_client_context(ctx, named)
        assert context.root == registered, "an argument never widens the grant"
        assert context.mismatch == named
        with pytest.raises(WorkspaceMismatch) as refused:
            context.require_workspace_match()
        assert refused.value.registered == str(registered)
        assert refused.value.requested == named


@pytest.mark.asyncio
async def test_describe_repo_reports_the_mismatch_instead_of_the_registered_repository(repositories):
    registered, other, inside = repositories
    ctx = http_context(registered)
    refused = json.loads(await server.describe_repo(ctx=ctx, workspace=str(other)))
    assert_mismatch(refused, registered, other)
    assert "repo_path" not in refused and "summary_preview" not in refused
    assert not (registered / "CLAUDE.md").exists() and not (other / "CLAUDE.md").exists()
    # The repo_path route refused before this change too, but named no workspace (#576).
    both = json.loads(await server.describe_repo(ctx=ctx, workspace=str(other), repo_path=str(other)))
    assert_mismatch(both, registered, other)
    # The registered root, a directory inside it and no argument at all are unchanged.
    for named in (None, str(registered), str(inside)):
        answered = json.loads(await server.describe_repo(ctx=ctx, workspace=named))
        assert answered["status"] == "needs_summary"
        assert answered["repo_path"] == str(registered)


@pytest.mark.asyncio
async def test_write_repo_summary_refuses_before_writing_anything(repositories):
    registered, other, _ = repositories
    ctx = http_context(registered)
    needs = json.loads(await server.describe_repo(ctx=ctx))
    arguments = dict(summary=_valid_summary(), repo_hash=needs["repo_hash"],
                     repo_path=needs["repo_path"], workspace_id=needs["workspace_id"])
    refused = json.loads(await server.write_repo_summary(**arguments, ctx=ctx, workspace=str(other)))
    assert_mismatch(refused, registered, other)
    assert not (registered / "CLAUDE.md").exists() and not (other / "CLAUDE.md").exists()
    assert not (registered / ".agents-description.lock").exists()
    written = json.loads(await server.write_repo_summary(**arguments, ctx=ctx, workspace=str(registered)))
    assert written["status"] == "refreshed" and written["path"] == str(registered / "CLAUDE.md")


@pytest.mark.asyncio
async def test_run_flow_reports_the_mismatch_instead_of_the_registered_repository(repositories, tmp_path,
                                                                                 monkeypatch):
    registered, other, _ = repositories
    install = tmp_path / "install" / "flows"
    install.mkdir(parents=True)
    (install / "check.md").write_text("# Check repository\n\nInspect README.md.\n", encoding="utf-8")
    monkeypatch.setattr(server, "FlowCatalog", lambda: FlowCatalog(install))
    ctx = http_context(registered)
    refused = json.loads(await server.run_flow("check", ctx=ctx, workspace=str(other)))
    assert_mismatch(refused, registered, other)
    assert "content" not in refused and "repo_path" not in refused
    started = json.loads(await server.run_flow("check", ctx=ctx, workspace=str(registered)))
    assert started["status"] == "needs_execution" and started["repo_path"] == str(registered)


@pytest.mark.asyncio
async def test_list_flows_refuses_the_repo_scope_and_still_lists_the_rest(repositories, tmp_path,
                                                                         monkeypatch):
    registered, other, _ = repositories
    install = tmp_path / "install" / "flows"
    install.mkdir(parents=True)
    (install / "check.md").write_text("# Check repository\n", encoding="utf-8")
    monkeypatch.setattr(server, "FlowCatalog", lambda: FlowCatalog(install))
    monkeypatch.setenv("AGENTS_USER_FLOWS_DIR", str(tmp_path / "library"))
    ctx = http_context(registered)
    refused = json.loads(await server.list_flows(scope="repo", ctx=ctx, workspace=str(other)))
    assert_mismatch(refused, registered, other)
    assert "flows" not in refused
    # A listing that also asks for built-in and personal flows keeps them, and names the gap.
    listing = json.loads(await server.list_flows(ctx=ctx, workspace=str(other)))
    assert listing["status"] == "success"
    assert [flow["id"] for flow in listing["flows"]] == ["check"]
    assert listing["repo"]["status"] == "unavailable"
    assert "workspace_mismatch: " in listing["repo"]["error"]
    assert str(other) in listing["repo"]["error"]
    # The registered workspace still reaches its own repository scope.
    own = json.loads(await server.list_flows(scope="repo", ctx=ctx, workspace=str(registered)))
    assert own["status"] == "success" and own["repo"]["status"] == "available"
    assert own["repo"]["path"] == str(registered)


@pytest.mark.asyncio
async def test_stdio_keeps_answering_for_the_root_its_client_proved(repositories, monkeypatch):
    """Over stdio the argument is checked against the client's roots, so nothing changes."""
    registered, other, _ = repositories
    roots = SimpleNamespace(session=SimpleNamespace(
        check_client_capability=lambda _: True,
        list_roots=AsyncMock(return_value=SimpleNamespace(roots=[SimpleNamespace(uri=registered.as_uri())])),
    ))
    context = await resolve_client_context(roots, str(other))
    assert context.mismatch is None and context.root is None  # refused by the roots check, as before
    assert "workspace_invalid" in context.error and context.claimed == str(other)
    refused = json.loads(await server.describe_repo(ctx=roots, workspace=str(other)))
    assert refused["status"] == "error" and "workspace_mismatch" not in refused["error"]

    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(registered))
    engine_config._reset_client_repo_root_cache()
    override = await resolve_client_context(None, str(other))
    assert override.root == registered and override.mismatch is None
    answered = json.loads(await server.describe_repo(workspace=str(other)))
    assert answered["status"] == "needs_summary" and answered["repo_path"] == str(registered)


@pytest.mark.asyncio
async def test_registered_http_session_is_refused_through_the_real_transport(repositories, tmp_path,
                                                                            monkeypatch):
    """The reproduction of #576: one session, two repositories, over the production middleware."""
    registered, other, _ = repositories
    registry = WorkspaceRegistry(tmp_path / "service")
    identity = registry.register(registered)
    monkeypatch.setattr(server.mcp.settings, "stateless_http", True)
    monkeypatch.setattr(server.mcp.settings, "json_response", True)
    monkeypatch.setattr(server.mcp, "_session_manager", None)
    app = create_app(registry.directory, TOKEN, runtime_loader=lambda port: (server.mcp, None))
    headers = {"Authorization": "Bearer " + TOKEN, "Accept": "application/json, text/event-stream",
               "X-Agents-Workspace": identity}
    async with app.router.lifespan_context(app):
        for _ in range(200):
            if app.state.service.state != "starting":
                break
            await asyncio.sleep(.01)
        assert app.state.service.state == "ready"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                     base_url="http://127.0.0.1:8765", headers=headers) as http:
            async def describe(**arguments):
                response = await http.post("/mcp", json={
                    "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                    "params": {"name": "describe_repo", "arguments": arguments}})
                assert response.status_code == 200
                return json.loads(response.json()["result"]["content"][0]["text"])

            refused = await describe(workspace=str(other))
            assert_mismatch(refused, registered, other)
            own = await describe(workspace=str(registered))
            assert own["status"] == "needs_summary" and own["repo_path"] == str(registered)
            assert own["workspace_id"] == identity
    assert not (registered / "CLAUDE.md").exists() and not (other / "CLAUDE.md").exists()


def test_the_mismatch_error_is_a_workspace_error_with_both_roots(tmp_path):
    """C4 and C5 build the first-activation signal on this shape."""
    error = WorkspaceMismatch(tmp_path / "a", str(tmp_path / "b"))
    assert isinstance(error, server.WorkspaceError)
    assert error.registered == str(tmp_path / "a") and error.requested == str(tmp_path / "b")
    details = error.details()
    assert set(details) == {"code", "registered_workspace", "requested_workspace", "remedy"}
    assert details["code"] == "workspace_mismatch"
    assert str(Path(tmp_path / "b")) in details["remedy"]
