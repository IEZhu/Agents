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
    ("relative/path", None),
    ("%2Fwith%00nul", None),
    ("/" + "a" * 9000, None),
    (None, None),
])
def test_the_daemon_takes_only_an_absolute_claim(value, expected):
    headers = {} if value is None else {"x-agents-claimed-workspace": value}
    assert claimed_workspace(headers) == expected


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
