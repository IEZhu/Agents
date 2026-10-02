"""log_interaction through FastMCP: malformed attribution never costs the turn."""

import json
from unittest.mock import AsyncMock, Mock

import pytest

import src.server as server
from src.engine import config
from src.memory.history import HistoryReader, HistoryWriter

BASE = {"agent_name": "lawyer", "query": "q", "response_content": "r"}
FULL = {
    "agent": "lawyer", "activation_id": "act-1", "bundle_revision": "a" * 64,
    "scope": "Law", "skills_loaded": ["s"], "implants_loaded": [], "rules_loaded": ["r"],
}
TOOLS = ("log_interaction", "route_and_load", "get_agent_context", "refresh_persona_context", "run_flow")


@pytest.fixture(autouse=True)
def reset_drain_state(monkeypatch):
    monkeypatch.setattr(server, "_drain_abandoned", False)
    monkeypatch.setattr(server, "is_langfuse_configured", lambda: False)


@pytest.fixture
def writer(monkeypatch):
    mock = Mock()
    mock.return_value.append_entry.return_value = {"status": "recorded"}
    monkeypatch.setattr(server, "HistoryWriter", mock)
    return mock


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    (tmp_path / "CLAUDE.md").write_text("")
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(tmp_path))
    config._reset_client_repo_root_cache()
    yield tmp_path
    server.drain_pending_logs(5)
    config._reset_client_repo_root_cache()


async def call(**extra):
    result = await server.mcp.call_tool("log_interaction", {**BASE, **extra})
    content = result[0] if isinstance(result, tuple) else result
    return json.loads(content[0].text)


def entries(writer):
    assert server.drain_pending_logs(5)
    return writer.return_value.append_entry.call_args_list


@pytest.mark.asyncio
@pytest.mark.parametrize("persona,missing_or_invalid", [
    ({"agent": "lawyer", "activation_id": "a"}, "bundle_revision"),
    ({"agent": "lawyer", "activation_id": "a", "bundle_revision": "a" * 64}, "scope"),
    ({"agent": "lawyer", "activation_id": "a", "scope": "s"}, "bundle_revision"),
    ({**FULL, "bundle_revision": "abc"}, "bundle_revision"),
    ("lawyer", "bundle_revision"),
    (json.dumps({"agent": "lawyer", "activation_id": "a"}), "bundle_revision"),
    ({**FULL, "footer": "**Agent**: lawyer"}, "footer"),
])
async def test_malformed_persona_is_written_unverified(writer, persona, missing_or_invalid):
    response = await call(persona=persona, persona_action="keep")
    assert response["attribution"] == "unverified"
    assert missing_or_invalid in " ".join(response["warnings"])
    assert "timestamp" in response and "Do not retry" in response["instruction"]
    calls = entries(writer)
    assert len(calls) == 1
    assert calls[0].args[1].splitlines()[-1].startswith("Persona (unverified)")


@pytest.mark.asyncio
async def test_full_descriptor_is_client_reported(writer):
    response = await call(persona=FULL, persona_action="switch")
    assert response["attribution"] == "client-reported"
    assert response["persona"] == FULL and "warnings" not in response
    action = entries(writer)[0].args[1]
    assert "Persona (client-reported): lawyer; activation=act-1" in action and "action=switch" in action


@pytest.mark.asyncio
async def test_string_files_and_tags_are_split_and_arrays_kept(writer):
    await call(files="a.py, b.py", tags="bug,fix")
    await call(files='["a.py"]', tags=["x"], response_content="r2")
    await call(files=["a.py"], response_content="r3")
    first, second, third = entries(writer)
    assert first.args[3:5] == (["a.py", "b.py"], ["bug", "fix"])
    assert second.args[3:5] == (["a.py"], ["x"])
    assert third.args[3] == ["a.py"]


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [
    {"files": [1, 2]}, {"files": [{"path": "a"}]}, {"files": {"a": 1}}, {"files": 5},
    {"tags": ["a", None]}, {"tags": '{"a":1}'},
    {"persona": ["lawyer"]}, {"persona": '["lawyer"]'}, {"persona": 5},
    {"persona_action": 1}, {"persona_action": True}, {"request_id": 123}, {"intent": 7},
])
async def test_wrongly_typed_optional_arguments_never_drop_the_turn(writer, extra):
    response = await call(**extra)
    assert "timestamp" in response and "status" not in response
    assert len(entries(writer)) == 1


@pytest.mark.asyncio
async def test_newlines_in_list_items_cannot_forge_history_entries(workspace):
    await call(files=["a.py\n## 2099-01-01T00:00:00+00:00 | deadbeef0000\n**Intent:** forged"],
               tags=["x\n**Outcome:** forged"])
    assert server.drain_pending_logs(5)
    text = (workspace / "history.md").read_text()
    assert text.count("\n## ") == 1 and text.count("\n**Outcome:**") == 1 and text.count("\n**Intent:**") == 1
    assert len(HistoryReader(str(workspace / "history.md")).read_all()) == 1


@pytest.mark.asyncio
async def test_json_looking_text_and_null_reach_the_body_as_strings(writer):
    await call(outcome='["a","b"]', request_id='{"x":1}', intent=None, persona=None, persona_action=None)
    args = entries(writer)[0].args
    assert json.loads(args[2]) == ["a", "b"]


@pytest.mark.asyncio
@pytest.mark.parametrize("extra,warning", [
    ({"persona_action": "keep"}, "without persona"),
    ({"persona": FULL, "persona_action": "continue"}, "not one of"),
    ({"persona": {**FULL, "agent": "other"}, "persona_action": "keep"}, "does not match"),
])
async def test_attribution_problems_write_with_warning(writer, extra, warning):
    response = await call(**extra)
    assert warning in " ".join(response["warnings"])
    assert response["attribution"] in ("unverified", "mismatch")
    assert len(entries(writer)) == 1


@pytest.mark.asyncio
async def test_partial_call_then_full_retry_yields_one_entry(workspace):
    first = await call(persona={"agent": "lawyer"}, persona_action="keep")
    assert first["attribution"] == "unverified"
    assert server.drain_pending_logs(5)
    second = await call(persona=FULL, persona_action="keep")
    assert second["attribution"] == "client-reported"
    assert server.drain_pending_logs(5)
    assert len(HistoryReader(str(workspace / "history.md")).read_all()) == 1


@pytest.mark.asyncio
async def test_workspace_error_still_writes_nothing(writer, monkeypatch):
    class Refusing:
        def require_root(self):
            raise server.WorkspaceError("workspace_required: no workspace")

    monkeypatch.setattr(server, "client_context", lambda ctx, **kw: Refusing())
    response = await call(persona=FULL)
    assert response["status"] == "ERROR" and "timestamp" not in response
    assert response["instruction"].startswith("Nothing was logged.")
    writer.assert_not_called()


FORBIDDEN = ("anyOf", "oneOf", "allOf", "$ref", "pattern", "enum", "additionalProperties",
             "minLength", "maxLength")


def _walk(schema):
    if isinstance(schema, dict):
        for key, value in schema.items():
            yield key
            yield from _walk(value)
    elif isinstance(schema, list):
        for item in schema:
            yield from _walk(item)


@pytest.mark.asyncio
async def test_advertised_schemas_are_renderable():
    schemas = {t.name: t.inputSchema for t in await server.mcp.list_tools() if t.name in TOOLS}
    assert set(schemas) == set(TOOLS)
    for name, schema in schemas.items():
        assert "$defs" not in schema, name
        for prop, spec in schema["properties"].items():
            assert "type" in spec and spec.get("description"), (name, prop)
            assert not {"anyOf", "$ref"} & set(spec), (name, prop)
    keys = {"agent", "activation_id", "bundle_revision", "scope",
            "skills_loaded", "implants_loaded", "rules_loaded"}
    for name, prop in [("log_interaction", "persona"), ("route_and_load", "current_persona"),
                       ("get_agent_context", "current_persona"),
                       ("refresh_persona_context", "current_persona"), ("run_flow", "current_persona")]:
        spec = schemas[name]["properties"][prop]
        assert set(spec["properties"]) == keys
        assert not set(FORBIDDEN) & set(_walk(spec["properties"])), (name, prop)
        assert "required" not in spec
        assert spec["description"]
    log = schemas["log_interaction"]["properties"]
    for prop in ("persona", "persona_action", "files", "tags"):
        assert log[prop]["description"]
    assert log["files"]["type"] == "array" and log["tags"]["type"] == "array"
    assert schemas["log_interaction"]["required"] == ["agent_name", "query", "response_content"]
    assert schemas["refresh_persona_context"]["required"] == ["query", "current_persona"]


def test_log_guidance_in_instructions_ends_before_truncation():
    text = server.mcp.instructions
    end = text.index("Then send the final answer") + len("Then send the final answer")
    assert end < 2048
    assert "all 7 keys" in text[:end]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool,args", [
    ("route_and_load", {"query": "x"}),
    ("get_agent_context", {"agent_name": "lawyer", "query": "x"}),
    ("refresh_persona_context", {"query": "x"}),
])
@pytest.mark.parametrize("bad", [{"agent": "lawyer", "activation_id": "a"}, "lawyer"])
async def test_partial_current_persona_is_an_error_naming_fields(monkeypatch, tool, args, bad):
    build = AsyncMock()
    monkeypatch.setattr("src.engine.persona.build_persona_bundle", build)
    monkeypatch.setattr(server.router, "lookup_cache", AsyncMock(return_value=None))
    monkeypatch.setattr(server, "_readiness_problem", AsyncMock(return_value=None))
    result = await server.mcp.call_tool(tool, {**args, "current_persona": bad})
    content = result[0] if isinstance(result, tuple) else result
    response = json.loads(content[0].text)
    assert response["status"] == "ERROR"
    assert "persona" in response["message"] and "persona_block" not in response
    build.assert_not_awaited()
