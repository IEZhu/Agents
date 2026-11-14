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


def http_context(root, identity="workspace-a", grants=()):
    """An MCP context for an HTTP request bound to the workspace registered for *root*.

    ``grants`` are the owner-registered physical checkouts the middleware carries beside the
    memory root (``cooperation_roots``), which is where a linked worktree's own path lives.
    """
    return SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(
        state=SimpleNamespace(client_context=ClientContext(
            "request", "http", workspace_id=identity, root=root,
            cooperation_roots=tuple(grants))))))


def linked_worktree(main, worktree, name="wt"):
    """The on-disk shape of a linked worktree: a `.git` file pointing into the main checkout."""
    administrative = main / ".git" / "worktrees" / name
    administrative.mkdir(parents=True)
    (administrative / "commondir").write_text("../..\n", encoding="utf-8")
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {administrative}\n", encoding="utf-8")
    _seed_repo(worktree)
    return worktree


@pytest.fixture
def flow_library(tmp_path, monkeypatch):
    """One built-in flow and an empty personal library, so the flow tools can answer."""
    install = tmp_path / "install" / "flows"
    install.mkdir(parents=True)
    (install / "check.md").write_text("# Check repository\n\nInspect README.md.\n", encoding="utf-8")
    monkeypatch.setattr(server, "FlowCatalog", lambda: FlowCatalog(install))
    monkeypatch.setenv("AGENTS_USER_FLOWS_DIR", str(tmp_path / "library"))
    return install


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


@pytest.mark.asyncio
async def test_history_tools_keep_the_registered_workspace_and_note_the_ignored_argument(repositories):
    """log_interaction and read_history never refuse (#576): the note says what was not followed."""
    registered, other, inside = repositories
    ctx = http_context(registered)

    async def log(named):
        return json.loads(await server.log_interaction(
            agent_name="universal_agent", query="q", response_content="r", ctx=ctx, workspace=named))

    async def read(named):
        return json.loads(await server.read_history(ctx=ctx, workspace=named))

    for call in (log, read):
        ignored = await call(str(other))
        # The workspace actually used is the registered one, exactly as without a note.
        assert ignored["workspace"]["root"] == str(registered)
        assert ignored["workspace"] == (await call(None))["workspace"]
        note = ignored["workspace_note"]
        assert str(other) in note and str(registered) in note and "ignored" in note
        # A matching argument, a directory inside the root and no argument add no note.
        for named in (str(registered), str(inside), None):
            answered = await call(named)
            assert "workspace_note" not in answered
            assert answered["workspace"]["root"] == str(registered)


@pytest.mark.asyncio
async def test_the_workspace_note_leaves_the_history_it_writes_in_the_registered_root(repositories):
    registered, other, _ = repositories
    ctx = http_context(registered)
    result = json.loads(await server.log_interaction(
        agent_name="universal_agent", query="note-check", response_content="kept", ctx=ctx,
        workspace=str(other)))
    assert result["workspace_note"] and result["workspace"]["root"] == str(registered)
    assert await asyncio.to_thread(server.drain_pending_logs, 5)
    assert "note-check" in (registered / "history.md").read_text(encoding="utf-8")
    assert not (other / "history.md").exists()


COOPERATION_TOOLS = frozenset(f"cooperation_{name}" for name in (
    "register", "read", "apply", "send", "receive", "ack", "request_status", "request_update",
    "wait"))
REPOSITORY_TOOLS = frozenset({"describe_repo", "write_repo_summary", "run_flow", "list_flows"})
FLOW_TOOLS = frozenset({"get_flow", "save_flow", "delete_flow", "set_flow_persona"})
ACTIVATION_TOOLS = frozenset({"route_and_load", "get_agent_context"})
HISTORY_TOOLS = frozenset({"log_interaction", "read_history"})
WORKSPACE_TOOLS = (COOPERATION_TOOLS | REPOSITORY_TOOLS | FLOW_TOOLS | ACTIVATION_TOOLS
                   | HISTORY_TOOLS)


@pytest.mark.asyncio
async def test_the_workspace_parameter_is_true_for_every_tool_that_shares_it():
    """The shared text must hold for all of them; the consequence is each tool's own (#576).

    The expected set is pinned by name: a tool that loses the parameter, or a new one that
    takes it without saying what it does with a path outside the grant, fails here rather
    than passing a floor.
    """
    descriptions, docstrings = {}, {}
    for tool in await server.mcp.list_tools():
        properties = (tool.inputSchema or {}).get("properties", {})
        if "workspace" in properties:
            descriptions[tool.name] = properties["workspace"]["description"]
            docstrings[tool.name] = tool.description or ""
    assert set(descriptions) == set(WORKSPACE_TOOLS), sorted(descriptions)
    for name, description in descriptions.items():
        assert "never widens" in description, name
        assert "MCP roots" in description, name
        # False for the nine cooperation tools, which do follow the argument inside a grant.
        assert "never followed" not in description, name
    for name in COOPERATION_TOOLS:
        assert "checkout grant" in docstrings[name], name
    for name in REPOSITORY_TOOLS | FLOW_TOOLS:
        assert "workspace_mismatch" in docstrings[name], name
    for name in ACTIVATION_TOOLS:
        assert "repo_context" in docstrings[name], name
    for name in HISTORY_TOOLS:
        assert "workspace_note" in docstrings[name], name


@pytest.mark.asyncio
async def test_a_linked_worktree_of_the_registered_repository_is_answered(repositories, tmp_path,
                                                                         flow_library):
    """A worktree session naming its own directory is one repository, not a foreign one (#576).

    The memory root is the main checkout because history normalizes a linked worktree to it,
    and the physical checkout stays beside it as the owner-registered grant this request
    already reads the project's instruction files from.
    """
    main, other, _inside = repositories
    worktree = linked_worktree(main, tmp_path / "worktree")
    registry = WorkspaceRegistry(tmp_path / "service")
    identity = registry.register(main, physical_root=worktree)
    assert engine_config._main_checkout(worktree) == main, "the normalization that causes this"
    assert registry.cooperation_roots(identity) == (worktree,)
    ctx = http_context(main, grants=(worktree,))
    for named in (str(worktree), str(worktree / "src")):
        context = await resolve_client_context(ctx, named)
        assert context.root == main, "the grant still decides the root"
        assert context.mismatch is None, named
        context.require_workspace_match()
        assert context.workspace_note() is None, named
    described = json.loads(await server.describe_repo(ctx=ctx, workspace=str(worktree)))
    assert described["status"] == "needs_summary" and described["repo_path"] == str(main)
    written = json.loads(await server.write_repo_summary(
        summary=_valid_summary(), repo_hash=described["repo_hash"],
        repo_path=described["repo_path"], workspace_id=described["workspace_id"],
        ctx=ctx, workspace=str(worktree)))
    assert written["status"] == "refreshed" and written["path"] == str(main / "CLAUDE.md")
    started = json.loads(await server.run_flow("check", ctx=ctx, workspace=str(worktree)))
    assert started["status"] == "needs_execution" and started["repo_path"] == str(main)
    listed = json.loads(await server.list_flows(scope="repo", ctx=ctx, workspace=str(worktree)))
    assert listed["status"] == "success" and listed["repo"]["status"] == "available"
    assert listed["repo"]["path"] == str(main)
    wide = json.loads(await server.list_flows(ctx=ctx, workspace=str(worktree)))
    assert [flow["id"] for flow in wide["flows"]] == ["check"]
    # A repository no authorized root covers is still refused, with both roots.
    refused = json.loads(await server.describe_repo(ctx=ctx, workspace=str(other)))
    assert_mismatch(refused, main, other)


@pytest.mark.asyncio
async def test_the_four_flow_tools_keep_the_mismatch_code_and_both_roots(repositories,
                                                                        flow_library):
    """A repo: flow of another workspace must not flatten the refusal into one string (#576)."""
    registered, other, _inside = repositories
    ctx = http_context(registered)
    calls = {
        "get_flow": server.get_flow("repo:own", ctx=ctx, workspace=str(other)),
        "save_flow": server.save_flow("own", "# Own\n\nSteps.\n", scope="repo", ctx=ctx,
                                      workspace=str(other)),
        "delete_flow": server.delete_flow("repo:own", "r1", ctx=ctx, workspace=str(other)),
        "set_flow_persona": server.set_flow_persona("repo:own", agent="software_engineer",
                                                    ctx=ctx, workspace=str(other)),
    }
    for name, call in calls.items():
        payload = json.loads(await call)
        assert payload["status"] == "error", name
        assert payload["code"] == "workspace_mismatch", name
        assert payload["registered_workspace"] == str(registered), name
        assert payload["requested_workspace"] == str(other), name
        assert f"migrate --workspace {other}" in payload["remedy"], name
        # Three fail for want of the repository scope; set_flow_persona finds no flow in a
        # library it cannot search, and says so, with the same roots and remedy beside it.
        expected = ("flow_not_found: " if name == "set_flow_persona" else
                    "repo_scope_unavailable: workspace_mismatch: ")
        assert payload["error"].startswith(expected), (name, payload["error"])
    # A built-in flow needs no repository scope, so the same call still answers.
    answered = json.loads(await server.get_flow("check", ctx=ctx, workspace=str(other)))
    assert answered["status"] == "success"
