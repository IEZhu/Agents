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


class Refusing:
    claimed = "/work/unregistered"
    client = "codex"

    def require_root(self):
        raise server.WorkspaceError("workspace_required: no workspace")


@pytest.mark.asyncio
async def test_a_turn_without_a_project_goes_to_the_fallback_journal(writer, monkeypatch, fallback_journal):
    """#292: the turn is kept, with the directory the client named, and nothing is written there."""
    monkeypatch.setattr(server, "resolve_client_context", AsyncMock(return_value=Refusing()))
    response = await call(persona=FULL)
    assert response["timestamp"] and response["history"] == {"status": "queued"}
    assert response["workspace"] == {"root": None, "source": "fallback", "claimed": "/work/unregistered",
                                     "reason": "workspace_required", "detail": "workspace_required: no workspace",
                                     "journal": str(fallback_journal / "history.md")}
    assert "fallback journal" in response["instruction"] and "Mention this once" in response["instruction"]
    assert server.drain_pending_logs(5)
    assert writer.call_args.args == (str(fallback_journal / "history.md"), str(fallback_journal / "history"))
    metadata = writer.return_value.append_entry.call_args.args[5]
    assert metadata["claimed_workspace"] == "/work/unregistered" and metadata["unresolved"] == "workspace_required"
    assert metadata["client"] == "codex"


@pytest.mark.asyncio
async def test_without_the_fallback_journal_a_workspace_error_still_writes_nothing(writer, monkeypatch):
    def unavailable(*args, **kwargs):
        raise PermissionError("read-only state")
    monkeypatch.setattr(server, "resolve_client_context", AsyncMock(return_value=Refusing()))
    monkeypatch.setattr(server.fallback, "journal_root", unavailable)
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


@pytest.mark.parametrize("phrase", [
    "If Agents-Core is listed, make every call there, the first too; retry Desktop denials there.",
    "Return its result only.",
    "Complete ROUTE_REQUIRED with get_agent_context in the same turn before answering",
    "A message that ends the turn is a final answer even when it only reports progress",
    "Then send the final answer.",
    "call get_subagent_context before spawning with a canonical agent_name, task, acceptance_criteria",
])
def test_per_turn_directives_stay_inside_the_instruction_window(phrase):
    """#508: Claude Code shows the first 2048 characters of the server instructions (#421). The
    directives every turn depends on stay inside them, so new guidance placed ahead of them fails
    here instead of silently dropping them from the window."""
    text = server.mcp.instructions
    assert phrase in text and text.index(phrase) + len(phrase) < 2048


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


@pytest.mark.asyncio
@pytest.mark.parametrize("field,bad", [("skills", {"skill-a": True}), ("rules", ["ok", 3]), ("implants", 5)])
async def test_set_flow_persona_rejects_malformed_lists_instead_of_clearing(field, bad):
    from mcp.server.fastmcp.exceptions import ToolError
    with pytest.raises(ToolError):
        await server.mcp.call_tool("set_flow_persona", {"flow": "x", "agent": "lawyer", field: bad})


@pytest.mark.asyncio
async def test_generated_request_id_is_in_the_request_diagnostic(writer, monkeypatch):
    seen = []
    monkeypatch.setattr(server, "debug_log", lambda tool, kind, data: seen.append((kind, data)))
    response = await call()
    req = next(d for k, d in seen if k == "req")
    assert req["request_id"] == response["request_id"]


@pytest.mark.asyncio
async def test_a_symlinked_fallback_directory_is_an_error_not_a_crash(writer, monkeypatch):
    def linked():
        raise ValueError("Service directory must not be a symlink")
    monkeypatch.setattr(server, "resolve_client_context", AsyncMock(return_value=Refusing()))
    monkeypatch.setattr(server.fallback, "journal_root", linked)
    response = await call(persona=FULL)
    assert response["status"] == "ERROR" and response["instruction"].startswith("Nothing was logged.")
    writer.assert_not_called()


@pytest.mark.asyncio
async def test_logging_guidance_keeps_the_request_verbatim_apart_from_one_redaction_set():
    """Every surface that says what to log keeps the request verbatim and names the same
    redactions; one revision of #487 dropped the verbatim rule from three of them."""
    from pathlib import Path
    from src.engine.persona import APPLY_INSTRUCTION
    root = Path(__file__).resolve().parents[1]
    tool = {t.name: t for t in await server.mcp.list_tools()}["log_interaction"]
    properties = tool.inputSchema["properties"]
    surfaces = {"APPLY_INSTRUCTION": APPLY_INSTRUCTION, "tool description": tool.description,
                "query description": properties["query"]["description"]}
    for name in ("scripts/templates/routing-protocol-core.md", "scripts/templates/memory-routing.md",
                 "CLAUDE.md", "docs/routing_flow.md"):
        surfaces[name] = (root / name).read_text(encoding="utf-8")
    redactions = ("capability values", "safe references", "prohibited personal details")
    for name, text in surfaces.items():
        flat = " ".join(text.split())
        assert "request verbatim" in flat, name
        assert all(term in flat for term in redactions), name
    response = properties["response_content"]["description"]
    assert all(term in response for term in redactions)
    # The server instructions are kept short and defer the redaction set to the tool description.
    assert "query verbatim, redacted as log_interaction's description says" in server.mcp.instructions


def test_server_choice_guidance_is_visible_before_truncation():
    """#421: a Code-tab session lists Agents-Core and the denied Agents-Core-Desktop; every
    protocol surface says which to call, and Claude Code's 2048-character window keeps it."""
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    text = server.mcp.instructions
    for phrase in ("If Agents-Core is listed, make every call there, the first too; retry Desktop denials there.",
                   # The guidance must not push the delegation directive out of the window.
                   "call get_subagent_context before spawning with a canonical agent_name, task, "
                   "acceptance_criteria"):
        assert phrase in text and text.index(phrase) + len(phrase) < 2048, phrase
    for name in ("scripts/templates/routing-protocol-core.md", "CLAUDE.md"):
        flat = " ".join((root / name).read_text(encoding="utf-8").split())
        assert ("When both `Agents-Core` and `Agents-Core-Desktop` are listed, make every call through "
                "`Agents-Core`, the first one included.") in flat, name
        assert "repeat it through `Agents-Core`; never continue without the persona or the log." in flat, name
    reminder = " ".join((root / "scripts/templates/memory-routing.md").read_text(encoding="utf-8").split())
    assert "call `Agents-Core` from the first call on" in reminder
