"""MCP wrappers expose the real cached subscription lifecycle."""
import json

import pytest

from src import server, user_rules

CONTENT = "---\nname: example\n---\nUse the relevant evidence.\n"


@pytest.mark.asyncio
async def test_mcp_lifecycle(monkeypatch):
    monkeypatch.setattr(user_rules, "fetch_rule", lambda *args, **kwargs: CONTENT)
    created = json.loads(await server.subscribe_user_rule("https://rules.example.org/example.mdc"))
    assert created["id"] == "user:example"
    assert json.loads(await server.list_user_rules())[0]["id"] == created["id"]
    current = json.loads(await server.get_user_rule(created["id"]))
    disabled = json.loads(await server.set_user_rule_enabled(created["id"], False, current["revision"]))
    refreshed = json.loads(await server.refresh_user_rule(created["id"], disabled["revision"]))["rule"]
    restored = json.loads(await server.rollback_user_rule(created["id"], created["content_sha256"], refreshed["revision"]))
    assert not restored["enabled"]
    conflict = json.loads(await server.delete_user_rule(created["id"], created["revision"]))
    assert conflict["status"] == "error" and "rule_conflict" in conflict["error"]
    assert json.loads(await server.delete_user_rule(created["id"], restored["revision"]))["status"] == "deleted"
    assert json.loads(await server.get_user_rule(created["id"]))["status"] == "error"
