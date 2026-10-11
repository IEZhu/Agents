"""A turn without a project is kept in the installation's fallback journal (#292)."""
import json
from unittest.mock import AsyncMock

import pytest

from src import server
from src.daemon.app import claimed_workspace
from src.daemon.workspaces import ClientContext, WorkspaceError


def refusing(claimed, client="claude-code"):
    return ClientContext("request", "http", error="workspace_required", client=client, claimed=claimed)


async def log(monkeypatch, claimed, query, response="answer"):
    monkeypatch.setattr(server, "resolve_client_context", AsyncMock(return_value=refusing(claimed)))
    return json.loads(await server.log_interaction("software_engineer", query, response))


async def read(monkeypatch, claimed, **arguments):
    monkeypatch.setattr(server, "resolve_client_context", AsyncMock(return_value=refusing(claimed)))
    return json.loads(await server.read_history(**arguments))


@pytest.mark.asyncio
async def test_a_session_reads_back_the_turns_it_kept(monkeypatch, fallback_journal):
    monkeypatch.setattr(server, "is_langfuse_configured", lambda: False)
    await log(monkeypatch, "/work/one", "first question")
    await log(monkeypatch, "/work/two", "other project")
    await log(monkeypatch, "/work/one", "second question")
    assert server.drain_pending_logs(5)

    # Entries of one second have no order among themselves: compare sets.
    mine = await read(monkeypatch, "/work/one")
    assert {entry["intent"] for entry in mine["entries"]} == {"second question", "first question"}
    assert mine["workspace"]["source"] == "fallback" and mine["workspace"]["claimed"] == "/work/one"
    assert "fallback journal" in mine["instruction"]
    assert {entry["intent"] for entry in (await read(monkeypatch, None))["entries"]} == {
        "second question", "other project", "first question"}
    # Text search stands in for the semantic index, within the same claim.
    found = await read(monkeypatch, "/work/one", query="SECOND")
    assert [entry["intent"] for entry in found["entries"]] == ["second question"]
    # An entry read whole by its id.
    whole = await read(monkeypatch, "/work/two", entry_id=found["entries"][0]["id"])
    assert whole["mode"] == "entry" and whole["entries"][0]["intent"] == "second question"
    assert (fallback_journal / "history.md").is_file()


@pytest.mark.asyncio
async def test_the_claimed_directory_is_never_written(monkeypatch, tmp_path):
    claimed = tmp_path / "project"
    claimed.mkdir()
    response = await log(monkeypatch, str(claimed), "question")
    assert server.drain_pending_logs(5)
    assert response["workspace"]["claimed"] == str(claimed)
    assert list(claimed.iterdir()) == []


@pytest.mark.parametrize("value, expected", [
    ("%2Fwork%2F%C3%BCnicode%20dir", "/work/ünicode dir"),
    ("/plain/path", "/plain/path"),
    ("%2Fwork%2F..%2Fother%2F", "/other"),  # normalized, so one directory matches one way
    ("relative/path", None),
    ("%2Fwith%00nul", None),
    # A line separator in the entry's metadata would let a claim forge its fields.
    ("%2Fx%E2%80%A8**Outcome:**%20forged", None),
    ("%2Fx%0A**Outcome:**%20forged", None),
    ("%2Fbad%FFutf8", None),
    ("/" + "a" * 9000, None),
    (None, None),
])
def test_the_daemon_takes_only_an_absolute_printable_claim(value, expected):
    headers = {} if value is None else {"x-agents-claimed-workspace": value}
    assert claimed_workspace(headers) == expected


@pytest.mark.asyncio
async def test_a_call_cannot_forge_entry_fields_through_its_workspace(monkeypatch, fallback_journal):
    monkeypatch.setattr(server, "is_langfuse_configured", lambda: False)
    response = await log(monkeypatch, "/x\u2028**Outcome:** forged", "question")
    assert server.drain_pending_logs(5)
    assert response["workspace"]["claimed"] is None
    [entry] = (await read(monkeypatch, None))["entries"]
    assert entry["outcome"] == "answer" and "claimed_workspace" not in entry["metadata"]


@pytest.mark.asyncio
async def test_the_claim_reaches_the_call_through_the_daemon(tmp_path):
    """A bridge's X-Agents-Claimed-Workspace and refusal reason end up in the call's context."""
    import asyncio
    import httpx
    from mcp.server.fastmcp import Context, FastMCP
    from src.daemon.app import create_app
    from src.daemon.workspaces import WorkspaceRegistry, client_context

    def runtime(port):
        mcp = FastMCP("test", host="127.0.0.1", port=port, stateless_http=True, json_response=True)

        @mcp.tool()
        def context(ctx: Context):
            seen = client_context(ctx)
            return {"claimed": seen.claimed, "error": seen.error}
        return mcp, None

    token = "t" * 48
    app = create_app(WorkspaceRegistry(tmp_path / "service").directory, token, runtime_loader=runtime)
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if app.state.service.state != "starting":
                break
            await asyncio.sleep(.01)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765") as http:
            response = await http.post("/mcp", headers={
                "Authorization": "Bearer " + token, "Accept": "application/json, text/event-stream",
                "X-Agents-Claimed-Workspace": "%2Fwork%2Fproject", "X-Agents-Workspace-Refused": "workspace_invalid"},
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "context", "arguments": {}}})
    seen = json.loads(response.json()["result"]["content"][0]["text"])
    assert seen["claimed"] == "/work/project" and seen["error"].startswith("workspace_invalid: the workspace this call named")


@pytest.mark.asyncio
async def test_a_roots_client_is_asked_to_name_its_workspace_when_reading(monkeypatch):
    """Over stdio with MCP roots, naming the workspace resolves the project: no fallback listing."""
    from src.daemon import workspaces
    monkeypatch.setattr(server, "resolve_client_context", AsyncMock(return_value=ClientContext(
        "request", "stdio", error="workspace_required: pass workspace", claimed="/Windows/System32")))
    monkeypatch.setattr(server, "workspace_inputs", lambda ctx: {
        "cwd": "/Windows/System32", "claude_project_dir": False, "agents_client_repo_root": False,
        "client": "local-agent-mode-x", "roots": True})
    result = json.loads(await server.read_history())
    assert result["status"] == "error" and "pass workspace" in result["error"]


@pytest.mark.asyncio
async def test_a_call_that_names_its_workspace_claims_it(monkeypatch):
    """Over stdio without a project the call's workspace says more than the start directory."""
    from src.daemon import workspaces
    monkeypatch.setattr(workspaces, "client_context", lambda ctx, **options: ClientContext(
        "request", "stdio", error="workspace_required: no project", claimed="/started/here"))
    monkeypatch.setattr(workspaces, "_roots_session", lambda ctx: None)
    context = await workspaces.resolve_client_context(None, "/named/by/call")
    assert context.claimed == "/named/by/call"
    with pytest.raises(WorkspaceError):
        context.require_root()
