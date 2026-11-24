"""The persona tools, log_interaction and describe_repo deliver their JSON object once (#734).

FastMCP gives a str-returning tool the structured content {"result": <its JSON text>}, which
Claude Code shows the model as JSON.stringify(structuredContent): the object escaped twice.
Over stdio these tools answer with the object as structured content and the unchanged text;
the shared service keeps the wrapper for clients that still hold the former listing.
"""

import json
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import jsonschema
import pytest
from mcp.shared.memory import create_connected_server_and_client_session

import src.server as server
from src.daemon.workspaces import ClientContext
from src.schemas.protocol import PersonaDescriptor

PERSONA = {
    "agent": "software_engineer", "activation_id": "new", "bundle_revision": "b" * 64,
    "scope": "Implementation and debugging", "skills_loaded": ["skill-dev-clean-code"],
    "implants_loaded": [], "rules_loaded": ["language-match"],
}
# A bundle as Markdown: newlines, quotes, a backslash and non-ASCII text, all escaped once in JSON.
SUCCESS = {
    "protocol_version": 2, "status": "SUCCESS", "replaces_activation_id": "old", "persona": PERSONA,
    "persona_block": "# Software engineer\n\nRead the code \"before\" editing; keep C:\\paths as они есть.\n",
    "rules_block": "- Match the user's language.\n- Quote `\"x\"` exactly.\n",
    "skills_block": "## skill-dev-clean-code\nSmall functions.\n", "implants_block": "",
    "footer": "---\nAgent: software_engineer | Skills: skill-dev-clean-code | Implants: — | Rules: language-match",
    "instruction": "Apply the four blocks.",
}
TEXT = json.dumps(SUCCESS, ensure_ascii=False)
ACTIVE = PersonaDescriptor(**{**PERSONA, "activation_id": "old"}).model_dump()
CALLS = {
    "route_and_load": {"query": "Fix the parser", "protocol_version": 2},
    "get_agent_context": {"agent_name": "software_engineer", "query": "Fix the parser", "protocol_version": 2},
    "refresh_persona_context": {"query": "Fix the parser", "current_persona": ACTIVE},
}


@pytest.fixture
def persona_text(monkeypatch):
    """route_persona and load_persona answer TEXT; the handlers' own return path stays real."""
    monkeypatch.setattr(server, "route_persona", AsyncMock(return_value=TEXT))
    monkeypatch.setattr(server, "load_persona", AsyncMock(return_value=TEXT))


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


def _holds_json(text):
    try:
        return isinstance(json.loads(text), (dict, list))
    except ValueError:
        return False


def _shown(structured):
    """What Claude Code shows the model for a tool with structured output: JSON.stringify of it."""
    return json.dumps(structured, ensure_ascii=False, separators=(",", ":"))


async def _former_schemas():
    """The output schemas FastMCP derives itself, the ones clients listed before #734."""
    return {tool.name: tool.outputSchema for tool in await server.FastMCP.list_tools(server.mcp)
            if tool.name in server.SINGLE_ENCODED_TOOLS}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", sorted(CALLS))
async def test_a_persona_success_reaches_the_client_encoded_once(persona_text, tool):
    content, structured = await server.mcp.call_tool(tool, CALLS[tool])

    assert [item.text for item in content] == [TEXT]  # the text content is the handler's JSON, unchanged
    assert json.loads(content[0].text) == structured == SUCCESS  # every field keeps its name and value
    assert "result" not in structured
    assert not any(_holds_json(text) for text in _strings(structured))
    shown, legacy = _shown(structured), _shown({"result": TEXT})
    assert shown.count("\\") == _shown(SUCCESS).count("\\") < legacy.count("\\")
    assert len(shown) < len(legacy)


@pytest.mark.asyncio
async def test_a_client_session_accepts_the_object_against_the_listed_schema(persona_text):
    """The in-memory client validates structured content against the listing, as Claude Code does."""
    async with create_connected_server_and_client_session(
        server.mcp, read_timeout_seconds=timedelta(seconds=30),
    ) as client:
        listed = {tool.name: tool.outputSchema for tool in (await client.list_tools()).tools}
        for tool in ("route_and_load", "get_agent_context"):
            result = await client.call_tool(tool, CALLS[tool])
            assert not result.isError
            assert result.structuredContent == json.loads(result.content[0].text) == SUCCESS
            jsonschema.validate(result.structuredContent, listed[tool])


@pytest.mark.asyncio
async def test_the_shared_service_keeps_the_result_wrapper(persona_text, monkeypatch):
    # Its clients reach a restarted process without listing again; the former schema requires "result".
    monkeypatch.setenv("AGENTS_TRANSPORT", "http")
    former = await _former_schemas()
    listed = {tool.name: tool.outputSchema for tool in await server.mcp.list_tools()}
    for tool in ("route_and_load", "get_agent_context"):
        content, structured = await server.mcp.call_tool(tool, CALLS[tool])
        assert structured == {"result": content[0].text} and json.loads(content[0].text) == SUCCESS
        jsonschema.validate(structured, former[tool])
        jsonschema.validate(structured, listed[tool])
    with pytest.raises(jsonschema.ValidationError):
        jsonschema.validate(SUCCESS, former["get_agent_context"])


@pytest.mark.asyncio
async def test_only_the_output_schema_of_these_tools_changes():
    """Descriptions, input schemas, annotations and the result-size declaration stay as FastMCP lists them."""
    base = {tool.name: tool for tool in await server.FastMCP.list_tools(server.mcp)}
    listed = {tool.name: tool for tool in await server.mcp.list_tools()}
    assert set(listed) == set(base)
    for name, tool in listed.items():
        if name not in server.SINGLE_ENCODED_TOOLS:
            assert tool == base[name]
            continue
        assert tool.model_copy(update={"outputSchema": None}) == base[name].model_copy(update={"outputSchema": None})
        assert base[name].outputSchema["required"] == ["result"]
        assert tool.outputSchema["type"] == "object"
        jsonschema.validate(SUCCESS, tool.outputSchema)
        jsonschema.validate({"result": TEXT}, tool.outputSchema)


@pytest.mark.asyncio
async def test_log_interaction_returns_its_object_once(monkeypatch, tmp_path):
    writer = Mock()
    writer.return_value.append_entry.return_value = {"status": "recorded"}
    monkeypatch.setattr(server, "HistoryWriter", writer)
    monkeypatch.setattr(server, "_drain_abandoned", False)
    monkeypatch.setattr(server, "is_langfuse_configured", lambda: False)
    monkeypatch.setattr(server, "resolve_client_context", AsyncMock(
        return_value=ClientContext("r", "stdio", "w", tmp_path)))
    content, structured = await server.mcp.call_tool(
        "log_interaction", {"agent_name": "lawyer", "query": "q", "response_content": "r"})
    assert server.drain_pending_logs(5)
    assert structured == json.loads(content[0].text) and structured and "result" not in structured


@pytest.mark.parametrize("text", ["[1, 2]", "\"text\"", "not json", "{\"x\": NaN}", "{\"x\": 1} trailing"])
def test_a_text_that_is_not_one_finite_json_object_keeps_the_wrapper(text):
    content = [Mock(text=text)]
    assert server._single_encoded((content, {"result": text})) == (content, {"result": text})


def test_other_result_shapes_pass_unchanged():
    content = [Mock(text="{}")]
    for result in (content, (content, {"status": "ok"}), (content, {"result": 1}),
                   (content, {"result": "{}", "extra": 1})):
        assert server._single_encoded(result) is result
