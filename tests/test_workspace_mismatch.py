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
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest

import src.server as server
from src.daemon.app import create_app
from src.daemon.workspaces import (ClientContext, WorkspaceMismatch, WorkspaceRegistry,
                                   _same_directory,
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
    assert_narrow_remedy(payload["remedy"], requested)
    assert "starts a session" in payload["remedy"]


# What `python -m src.daemon migrate` rewrites unless it is told otherwise: a refusal that
# enforces a grant must not hand back a command that reconfigures three AI clients (P-C4).
MIGRATE_CLIENTS = "codex,claude,cursor"


def assert_narrow_remedy(remedy, requested):
    """The remedy names the narrow action, addresses the user, and quotes no caller path."""
    assert "workspace register" in remedy
    assert "migrate" not in remedy, "the remedy must not name the configuration rewriter"
    assert str(requested) not in remedy, "a caller-supplied path is not put in a command line"
    assert "The user" in remedy, "the remedy is addressed to the user, not to the model"


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
    assert details["requested_workspace"] == str(tmp_path / "b"), "reported as data"
    assert_narrow_remedy(details["remedy"], tmp_path / "b")


def test_the_remedy_names_no_subcommand_that_rewrites_a_client_configuration(tmp_path):
    """A refusal that enforces a grant must not return the command that widens it (P-C4).

    `migrate` rewrites the configuration of every client in `--clients`, which defaults to
    all three, so an unattended run acting on this sentence would reconfigure three AI
    clients on the user's machine and be granted, next session, the repository the refusal had
    just denied. `workspace register` only records a root. The requested path is a
    caller-supplied string and is reported as data, never interpolated into a command line.
    """
    from src.daemon import control

    remedy = WorkspaceMismatch(tmp_path / "a", str(tmp_path / "b")).details()["remedy"]
    assert_narrow_remedy(remedy, tmp_path / "b")
    for command in ("migrate", "audit", "install", "restore-clients"):
        assert f"src.daemon {command}" not in remedy, command
    # Why `migrate` is the one that must not be named, and `workspace register` the one that is.
    source = Path(control.__file__).read_text(encoding="utf-8")
    assert f'migrate.add_argument("--clients", default="{MIGRATE_CLIENTS}")' in source
    assert "workspace register requires a path" in source, "the narrow action, by its own name"


def test_a_place_inside_the_root_that_does_not_exist_yet_is_still_the_same_directory(tmp_path):
    """`samefile` confirms the ancestor slice, not the whole requested path, and that is right.

    A session naming a directory it is about to create, under another spelling of the root, still
    names a place inside the registered repository. Requiring the requested path itself to exist
    would take every repository-scoped tool away from that session over a directory that is one
    mkdir old, which is the refusal this function exists to remove. A path whose ancestor slice is
    not the root stays refused whether it exists or not, and that is what is asserted last.
    """
    root = (tmp_path / "Registered").resolve()
    root.mkdir()
    assert _same_directory(root, str(root)), "the root under its own spelling"
    elsewhere = tmp_path.resolve() / "Other"
    elsewhere.mkdir()
    assert not _same_directory(root, str(elsewhere)), "a sibling is not the root"
    assert not _same_directory(root, str(elsewhere / "not-created-yet"))
    variant = tmp_path.resolve() / "registered"
    if not variant.exists():
        pytest.skip("a case-sensitive filesystem: the respelling names a different directory")
    assert _same_directory(root, str(variant)), "the root respelled"
    assert _same_directory(root, str(variant / "not-created-yet"))
    assert _same_directory(root, str(variant / "not" / "created" / "yet"))


def test_the_shared_workspace_description_does_not_promise_an_unconditional_roots_check():
    """21 parameters share this sentence, so what it promises has to be what happens.

    Over stdio the argument is checked against the client's MCP roots only when this session's
    root is not already pinned: `resolve_client_context` returns a native hook context first, and
    an `AGENTS_CLIENT_REPO_ROOT` override decides alone, with the argument never compared to a
    root at all. The description said the path must lie inside the client's roots, full stop.
    """
    from src.schemas.tool_args import WORKSPACE_DESC

    assert "already pinned" in WORKSPACE_DESC, (
        "the stdio sentence has to name the case where the argument is ignored instead of checked")
    assert "never widens" in WORKSPACE_DESC


def test_the_fallback_journal_remedy_is_narrow_too():
    """The same rule, in the other place that offers registration.

    `src/memory/fallback.py` tells a session why its history went to the fallback journal and how
    to keep it in the project. That note reaches a model in a tool result exactly as the refusal
    above does, so it carries the same obligation: name the action that records a root, not the one
    that rewrites three clients' configuration files.
    """
    from src.memory import fallback

    assert_narrow_remedy(fallback.REMEDY, Path("/some/caller/path"))


@pytest.mark.asyncio
async def test_history_tools_keep_the_registered_workspace_and_note_the_ignored_argument(repositories):
    """log_interaction, read_history and claim_lookup never refuse (#576): the note says what was not followed."""
    registered, other, inside = repositories
    ctx = http_context(registered)

    async def log(named):
        return json.loads(await server.log_interaction(
            agent_name="universal_agent", query="q", response_content="r", ctx=ctx, workspace=named))

    async def read(named):
        return json.loads(await server.read_history(ctx=ctx, workspace=named))

    async def lookup(named):
        return json.loads(await server.claim_lookup(ctx=ctx, workspace=named))

    for call in (log, read, lookup):
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
async def test_claim_record_refuses_another_workspace_and_writes_nothing(repositories):
    """A verdict checked in repository B must not be stored as if it belonged to A (#576)."""
    registered, other, inside = repositories
    ctx = http_context(registered)
    refused = json.loads(await server.claim_record(subject="s", claim="c", verdict="confirmed", level="V2",
                                                   ctx=ctx, workspace=str(other)))
    assert refused["status"] == "error" and refused["code"] == "workspace_mismatch"
    assert refused["registered_workspace"] == str(registered)
    assert not (registered / "data" / "claims").exists() and not (other / "data" / "claims").exists()
    for named in (str(registered), str(inside), None):
        recorded = json.loads(await server.claim_record(subject="s", claim="c", verdict="confirmed",
                                                        level="V2", ctx=ctx, workspace=named))
        assert recorded["status"] == "recorded" and recorded["workspace"]["root"] == str(registered)


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
# The writer refuses a workspace the grant does not cover; the reader notes it like history.
CLAIM_WRITE_TOOLS = frozenset({"claim_record"})
CLAIM_READ_TOOLS = frozenset({"claim_lookup"})
CLAIM_TOOLS = CLAIM_WRITE_TOOLS | CLAIM_READ_TOOLS
# Reads the registered workspace's project and notes an argument outside it like history; the
# behaviour is checked in tests/test_shared_memory.py, which fakes the clients' memory stores.
SHARED_MEMORY_TOOLS = frozenset({"read_shared_memory"})
WORKSPACE_TOOLS = (COOPERATION_TOOLS | REPOSITORY_TOOLS | FLOW_TOOLS | ACTIVATION_TOOLS
                   | HISTORY_TOOLS | CLAIM_TOOLS | SHARED_MEMORY_TOOLS)


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
    for name in REPOSITORY_TOOLS | FLOW_TOOLS | CLAIM_WRITE_TOOLS:
        assert "workspace_mismatch" in docstrings[name], name
    for name in ACTIVATION_TOOLS:
        assert "repo_context" in docstrings[name], name
    for name in HISTORY_TOOLS | CLAIM_READ_TOOLS | SHARED_MEMORY_TOOLS:
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
        assert_narrow_remedy(payload["remedy"], other)
        # Three fail for want of the repository scope; set_flow_persona finds no flow in a
        # library it cannot search, and says so, with the same roots and remedy beside it.
        expected = ("flow_not_found: " if name == "set_flow_persona" else
                    "repo_scope_unavailable: workspace_mismatch: ")
        assert payload["error"].startswith(expected), (name, payload["error"])
    # A built-in flow needs no repository scope, so the same call still answers.
    answered = json.loads(await server.get_flow("check", ctx=ctx, workspace=str(other)))
    assert answered["status"] == "success"


def _case_insensitive(root):
    """Whether this filesystem gives the same directory under another spelling of its name."""
    try:
        return root.with_name(root.name.lower()).samefile(root)
    except OSError:
        return False


@pytest.mark.asyncio
async def test_a_registered_root_named_under_another_spelling_is_noted_not_refused(tmp_path,
                                                                                  flow_library):
    """One respelling must not cost a session its repository tools for good (P-C5).

    `_covers` decides by exact containment of resolved paths and `Path.resolve()` does not
    normalize case, so on this filesystem one real directory named under another spelling was
    a mismatch: `require_workspace_match` then refused `describe_repo`, `write_repo_summary`,
    `run_flow` and a `scope=repo` listing for the whole session, with two paths in the message
    that the user reads as identical.
    """
    registered = tmp_path / "Registered"
    registered.mkdir()
    _seed_repo(registered)
    registered = registered.resolve()
    if not _case_insensitive(registered):
        pytest.skip("this filesystem holds the two spellings apart")
    if sys.platform == "win32":
        # Seen in CI: there the respelled name resolves to the registered root itself, so it is
        # neither a mismatch nor a respelling and no note is due; the refusal this test guards
        # against cannot arise.
        pytest.skip("Path.resolve() returns the directory's own spelling on Windows")
    respelled = str(registered.with_name(registered.name.lower()))
    assert respelled != str(registered) and Path(respelled).samefile(registered)
    ctx = http_context(registered)
    for named in (respelled, str(Path(respelled) / "src")):
        context = await resolve_client_context(ctx, named)
        assert context.root == registered, "the grant still decides the root"
        assert context.mismatch is None, named
        context.require_workspace_match()
        note = context.workspace_note()
        assert note and named in note and str(registered) in note and "ignored" in note
    described = json.loads(await server.describe_repo(ctx=ctx, workspace=respelled))
    assert described["status"] == "needs_summary" and described["repo_path"] == str(registered)
    listed = json.loads(await server.list_flows(scope="repo", ctx=ctx, workspace=respelled))
    assert listed["status"] == "success" and listed["repo"]["path"] == str(registered)
    started = json.loads(await server.run_flow("check", ctx=ctx, workspace=respelled))
    assert started["status"] == "needs_execution" and started["repo_path"] == str(registered)
    # A path that is genuinely elsewhere keeps the refusal, however it is spelled.
    elsewhere = tmp_path / "Other"
    elsewhere.mkdir()
    for named in (str(elsewhere), str(elsewhere).lower(), str(registered) + "-sibling"):
        refused = json.loads(await server.describe_repo(ctx=ctx, workspace=named))
        assert refused["status"] == "error" and refused["code"] == "workspace_mismatch", named
        assert refused["requested_workspace"] == named


def test_a_respelling_the_filesystem_does_not_confirm_stays_refused(tmp_path):
    """The casefolded match decides nothing on its own: `samefile` confirms it (P-C5).

    A case-sensitive filesystem can hold two different directories whose names differ only in
    case, which is why the comparison is a shortlist and the filesystem settles it. A removed
    directory asks that same question: the strings still match, the identity check cannot, and
    the refusal stands.
    """
    from src.daemon import workspaces
    root = tmp_path / "Registered"
    root.mkdir()
    root = root.resolve()
    (root / "src").mkdir()
    respelled = str(root.with_name(root.name.lower()))
    for named in (respelled, str(Path(respelled) / "src")):
        assert workspaces._same_directory(root, named) is _case_insensitive(root), named
    (root / "src").rmdir()
    root.rmdir()
    assert workspaces._same_directory(root, respelled) is False
    assert workspaces._same_directory(root, str(root)) is False
