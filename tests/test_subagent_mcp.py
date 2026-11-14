"""Exercise advertised schemas and untouched strict inputs through FastMCP."""
import json
from types import SimpleNamespace

import jsonschema
import pytest
from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolRequest, CallToolRequestParams

import src.server as server
from tests.test_subagent import worker_tree, request, report
from src.schemas.subagent import MAX_REQUEST_CHARS, PreparedPackage


def inline(**extra):
    """The full JSON package of the compatibility path; the default receipt is in test_subagent_exchange."""
    return request(delivery="inline", result_format="json", **extra)


async def call(name, arguments):
    content, structured = await server.mcp.call_tool(name, arguments)
    assert json.loads(content[0].text) == structured
    return structured


@pytest.mark.asyncio
async def test_registered_input_and_output_schemas(worker_tree):
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    context_tool = tools["get_subagent_context"]
    types = {"agent_name": "string", "task": "string", "acceptance_criteria": "array",
             "task_id": "string", "attempt_id": "string", "context": "array", "scope": "array",
             "non_goals": "array", "result_type": "string", "max_skills": "integer",
             "max_implants": "integer", "max_context_chars": "integer", "complexity": "string",
             "client": "string", "delivery": "string", "result_format": "string"}
    for name, field_type in types.items():
        schema = context_tool.inputSchema["properties"][name]
        assert schema["type"] == field_type and schema["description"]
        assert not {"anyOf", "$ref", "enum"} & schema.keys()
    assert context_tool.outputSchema["type"] == "object"
    package = await call("get_subagent_context", inline())
    jsonschema.validate(package, context_tool.outputSchema)
    assert package["result_schema"] != context_tool.outputSchema
    assert package["result_schema"]["additionalProperties"] is False
    value = report(PreparedPackage.model_validate(package))
    jsonschema.validate(value, package["result_schema"])
    validated = await call("validate_subagent_result", {"contract": package["contract"], "result": value})
    jsonschema.validate(validated, tools["validate_subagent_result"].outputSchema)
    assert validated == {"valid": True, "errors": [], "verification": "structural_only",
                         "result_sha256": validated["result_sha256"]}
    invalid = await call("get_subagent_context", inline(task=""))
    jsonschema.validate(invalid, context_tool.outputSchema)
    assert set(invalid) == {"protocol", "status", "errors"}


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"max_skills": True}, {"max_skills": "3"}, {"max_skills": 3.0},
                                  {"acceptance_criteria": '["done"]'}, {"task": {"text": "bad"}},
                                  {"context": "[]"}, {"task_id": []}, {"result_type": "unknown"},
                                  {"agent_name": "../engineer"}, {"max_context_chars": 2000}])
async def test_mcp_does_not_coerce_invalid_inputs(worker_tree, extra):
    result = await call("get_subagent_context", inline(**extra))
    assert result["status"] == "error" and result["errors"]
    assert "worker_context" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["generic", "review", "implementation", "research"])
@pytest.mark.parametrize("status", ["completed", "partial", "input_required", "failed"])
async def test_mcp_all_payloads_and_statuses(worker_tree, kind, status):
    package = await call("get_subagent_context", inline(result_type=kind))
    value = report(PreparedPackage.model_validate(package), status)
    response = await call("validate_subagent_result", {"contract": package["contract"], "result": json.dumps(value)})
    assert response["valid"]
    value["result"]["extra"] = "closed"
    assert not (await call("validate_subagent_result", {"contract": package["contract"], "result": value}))["valid"]


@pytest.mark.asyncio
@pytest.mark.parametrize("raw,code", [('{"a":1,"a":2}', "duplicate_key"), ('{"a":NaN}', "non_finite"),
                                      ('{"a":Infinity}', "non_finite"), ('{} trailing', "invalid_json"),
                                      ('```json\n{}\n```', "invalid_json")])
async def test_mcp_preserves_raw_json_for_strict_parser(worker_tree, raw, code):
    package = await call("get_subagent_context", inline())
    response = await call("validate_subagent_result", {"contract": package["contract"], "result": raw})
    assert not response["valid"]
    assert code in {item["code"] for item in response["errors"]}


@pytest.mark.asyncio
async def test_preparation_never_uses_parent_routing_readiness_or_memory(worker_tree, monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("Worker preparation accessed session/runtime state")
    monkeypatch.setattr(server, "_readiness_problem", unexpected)
    monkeypatch.setattr(server, "load_persona", unexpected)
    monkeypatch.setattr(server, "route_persona", unexpected)
    monkeypatch.setattr(server, "HistoryWriter", unexpected)
    before = dict(server.SESSION_CACHE)
    first, second = await call("get_subagent_context", inline()), await call("get_subagent_context", inline(attempt_id="2"))
    assert dict(server.SESSION_CACHE) == before
    response = await call("validate_subagent_result", {"contract": first["contract"],
                          "result": report(PreparedPackage.model_validate(second))})
    assert not response["valid"]


@pytest.mark.asyncio
async def test_mcp_preserves_json_looking_task_text(worker_tree):
    text = '{"instructions": ["review"]}'
    package = await call("get_subagent_context", inline(task=text))
    assert package["status"] == "prepared" and package["contract"]["task"] == text


@pytest.mark.asyncio
async def test_mcp_missing_disabled_and_budgeted_components(worker_tree, monkeypatch):
    from src.engine import subagent
    monkeypatch.setattr(subagent.component_toggles, "disabled", lambda kind: {"skill-test", "implant-debug"})
    package = await call("get_subagent_context", inline(max_implants=2))
    assert "skill-test" not in package["descriptor"]["skills_loaded"]
    assert package["descriptor"]["implants_loaded"] == ["implant-plan"]
    (worker_tree / "agents/engineer/subagent.mdc").unlink()
    response = await call("get_subagent_context", inline())
    assert response["status"] == "error" and "worker_context" not in response


@pytest.mark.asyncio
async def test_mcp_retained_contract_and_evidence_errors(worker_tree):
    package = await call("get_subagent_context", inline(result_type="research"))
    value = report(PreparedPackage.model_validate(package))
    value["result"]["conclusions"] = [{"claim": "Claim", "evidence_ids": ["missing"]}]
    response = await call("validate_subagent_result", {"contract": package["contract"], "result": value})
    assert not response["valid"] and response["errors"][0]["code"] == "unknown_evidence_id"
    package["contract"]["budgets"]["max_skills"] = True
    response = await call("validate_subagent_result", {"contract": package["contract"], "result": value})
    assert not response["valid"] and response["errors"][0]["path"].startswith("$.contract")


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["get_subagent_context", "validate_subagent_result"])
async def test_mcp_rejects_unknown_worker_arguments(worker_tree, name, monkeypatch):
    package = await call("get_subagent_context", inline())
    arguments = (request(max_context_chars_typo=2000) if name == "get_subagent_context" else
                 {"contract": package["contract"], "result": report(PreparedPackage.model_validate(package)),
                  "unexpected": "must not be echoed"})

    def unexpected(*args, **kwargs):
        pytest.fail("Unknown arguments reached the worker tool body")
    monkeypatch.setattr(server, "prepare_subagent", unexpected)
    monkeypatch.setattr(server, "assess_result", unexpected)
    # Exercise the handler installed for real MCP requests, including output validation.
    response = (await server.mcp._mcp_server.request_handlers[CallToolRequest](
        CallToolRequest(params=CallToolRequestParams(name=name, arguments=arguments))
    )).root
    assert not response.isError
    structured = response.structuredContent
    assert json.loads(response.content[0].text) == structured
    assert structured.get("status") == "error" or structured.get("valid") is False
    assert structured["errors"][0]["code"] == "extra_forbidden"
    assert "worker_context" not in structured
    assert "must not be echoed" not in response.content[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["x" * MAX_REQUEST_CHARS, "\n" * (MAX_REQUEST_CHARS // 2)],
                         ids=["raw-characters", "canonical-escaping"])
async def test_mcp_bounds_original_preparation_arguments(worker_tree, value):
    arguments = request(unexpected=value)
    response = await call("get_subagent_context", arguments)
    assert response["status"] == "error"
    assert response["errors"][0]["code"] == "payload_too_large"
    assert "worker_context" not in response


@pytest.mark.asyncio
async def test_mcp_validation_unknown_arguments_have_bounded_errors(worker_tree):
    package = await call("get_subagent_context", inline())
    arguments = {"contract": package["contract"], "result": report(PreparedPackage.model_validate(package)),
                 "unexpected" * 10000: "must not be echoed" * 10000,
                 **{f"extra_{i}": "must not be echoed" for i in range(1000)}}
    response = await call("validate_subagent_result", arguments)
    assert not response["valid"]
    assert response["errors"][0]["code"] == "extra_forbidden"
    assert len(response["errors"]) <= 20
    assert len(json.dumps(response)) < 1000
    assert "must not be echoed" not in json.dumps(response)


@pytest.mark.asyncio
async def test_mcp_ordinary_tools_keep_fastmcp_dispatch(monkeypatch):
    monkeypatch.setattr(server, "router", SimpleNamespace(available_agents=["engineer"]))
    arguments = {"include_metadata": "false", "unexpected": "x" * MAX_REQUEST_CHARS}
    actual = await server.mcp.call_tool("list_agents", arguments)
    expected = await FastMCP.call_tool(server.mcp, "list_agents", arguments)
    assert actual == expected
    assert json.loads(actual[0][0].text) == {"agents": ["engineer"]}


async def raw_call(arguments):
    """The handler installed for real MCP requests, which sees the arguments as the client sent them."""
    response = (await server.mcp._mcp_server.request_handlers[CallToolRequest](
        CallToolRequest(params=CallToolRequestParams(name="get_subagent_context", arguments=arguments)))).root
    assert not response.isError
    return response.structuredContent


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapper", ["__unparsedToolInput", "unparsedToolInput"])
async def test_mcp_names_a_wrapped_request_and_what_the_tool_accepts(worker_tree, wrapper):
    payload = json.dumps({"agent_name": "engineer", "task": "secret task text", "acceptance_criteria": ["done"]})
    response = await raw_call({wrapper: payload})
    assert response["status"] == "error" and "worker_context" not in response
    codes = {item["code"] for item in response["errors"]}
    assert codes == {"arguments_wrapped"}
    text = " ".join(item["message"] for item in response["errors"])
    assert wrapper in text and f"{len(payload)} chars" in text  # what arrived
    for field in ("agent_name", "task", "acceptance_criteria", "delivery", "max_context_chars"):
        assert field in text  # what the tool accepts
    assert str(MAX_REQUEST_CHARS) in text and "4000" in text and "50 items" in text  # the limits
    assert all(len(item["message"]) <= 240 for item in response["errors"])
    assert "secret task text" not in json.dumps(response)


@pytest.mark.asyncio
async def test_mcp_wrapper_next_to_named_fields_is_still_diagnosed_as_wrapped(worker_tree):
    response = await raw_call({**request(), "__unparsedToolInput": "{}"})
    assert {item["code"] for item in response["errors"]} == {"arguments_wrapped"}


@pytest.mark.asyncio
async def test_mcp_unknown_named_field_is_not_taken_for_a_wrapper(worker_tree):
    response = await raw_call(request(max_context_chars_typo=2000))
    assert [item["code"] for item in response["errors"]] == ["extra_forbidden"]
    assert response["errors"][0]["path"] == "$.max_context_chars_typo"


@pytest.mark.asyncio
async def test_mcp_empty_reference_names_both_transcript_locations_whole(worker_tree, tmp_path):
    """#581: diagnostic() cuts a message at 240 characters, so the path has to fit in the response."""
    package = await call("get_subagent_context", inline())
    running = tmp_path / "agent-running.jsonl"
    running.write_text(json.dumps({"type": "user", "message": {"content": "start"}}) + "\n", encoding="utf-8")
    response = await call("validate_subagent_result",
                          {"contract": package["contract"], "result_ref": str(running)})
    assert not response["valid"]
    message = next(item["message"] for item in response["errors"] if item["code"] == "result_ref_empty")
    assert len(message) < 240 and message.endswith(".")
    assert "~/.claude/projects/<project>/<session>/subagents/agent-<id>.jsonl" in message
    assert "workflows/wf_<id>/" in message  # the Workflow location, the tail a cut message loses


@pytest.mark.asyncio
async def test_the_tool_descriptions_carry_the_transcript_paths_and_the_marker(worker_tree):
    """Neither gate generates these two texts, and they are what a calling model reads (#581, #589)."""
    tools = {tool.name: tool for tool in await server.mcp.list_tools()}
    reference = tools["validate_subagent_result"].inputSchema["properties"]["result_ref"]["description"]
    assert "~/.claude/projects/<project>/<session>/subagents/" in reference
    assert "workflows/wf_<id>/" in reference and "Workflow transcript dir" not in reference
    assert "truncated.produced" in tools["validate_subagent_result"].description
    assert "omitted" in tools["get_subagent_context"].description
