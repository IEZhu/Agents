"""Exercise advertised schemas and untouched strict inputs through FastMCP."""
import json

import jsonschema
import pytest

import src.server as server
from tests.test_subagent import worker_tree, request, report
from src.schemas.subagent import PreparedPackage


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
             "max_implants": "integer", "max_context_chars": "integer"}
    for name, field_type in types.items():
        schema = context_tool.inputSchema["properties"][name]
        assert schema["type"] == field_type and schema["description"]
        assert not {"anyOf", "$ref", "enum"} & schema.keys()
    assert context_tool.outputSchema["type"] == "object"
    package = await call("get_subagent_context", request())
    jsonschema.validate(package, context_tool.outputSchema)
    assert package["result_schema"] != context_tool.outputSchema
    assert package["result_schema"]["additionalProperties"] is False
    value = report(PreparedPackage.model_validate(package))
    jsonschema.validate(value, package["result_schema"])
    validated = await call("validate_subagent_result", {"contract": package["contract"], "result": value})
    jsonschema.validate(validated, tools["validate_subagent_result"].outputSchema)
    assert validated == {"valid": True, "errors": [], "verification": "structural_only"}
    invalid = await call("get_subagent_context", request(task=""))
    jsonschema.validate(invalid, context_tool.outputSchema)
    assert set(invalid) == {"protocol", "status", "errors"}


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"max_skills": True}, {"max_skills": "3"}, {"max_skills": 3.0},
                                  {"acceptance_criteria": '["done"]'}, {"task": {"text": "bad"}},
                                  {"context": "[]"}, {"task_id": []}, {"result_type": "unknown"},
                                  {"agent_name": "../engineer"}, {"max_context_chars": 2000}])
async def test_mcp_does_not_coerce_invalid_inputs(worker_tree, extra):
    result = await call("get_subagent_context", request(**extra))
    assert result["status"] == "error" and result["errors"]
    assert "worker_context" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["generic", "review", "implementation", "research"])
@pytest.mark.parametrize("status", ["completed", "partial", "input_required", "failed"])
async def test_mcp_all_payloads_and_statuses(worker_tree, kind, status):
    package = await call("get_subagent_context", request(result_type=kind))
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
    package = await call("get_subagent_context", request())
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
    first, second = await call("get_subagent_context", request()), await call("get_subagent_context", request(attempt_id="2"))
    assert dict(server.SESSION_CACHE) == before
    response = await call("validate_subagent_result", {"contract": first["contract"],
                          "result": report(PreparedPackage.model_validate(second))})
    assert not response["valid"]


@pytest.mark.asyncio
async def test_mcp_preserves_json_looking_task_text(worker_tree):
    text = '{"instructions": ["review"]}'
    package = await call("get_subagent_context", request(task=text))
    assert package["status"] == "prepared" and package["contract"]["task"] == text


@pytest.mark.asyncio
async def test_mcp_missing_disabled_and_budgeted_components(worker_tree, monkeypatch):
    from src.engine import subagent
    monkeypatch.setattr(subagent.component_toggles, "disabled", lambda kind: {"skill-test", "implant-debug"})
    package = await call("get_subagent_context", request(max_implants=2))
    assert "skill-test" not in package["descriptor"]["skills_loaded"]
    assert package["descriptor"]["implants_loaded"] == ["implant-plan"]
    (worker_tree / "agents/engineer/subagent.mdc").unlink()
    response = await call("get_subagent_context", request())
    assert response["status"] == "error" and "worker_context" not in response


@pytest.mark.asyncio
async def test_mcp_retained_contract_and_evidence_errors(worker_tree):
    package = await call("get_subagent_context", request(result_type="research"))
    value = report(PreparedPackage.model_validate(package))
    value["result"]["conclusions"] = [{"claim": "Claim", "evidence_ids": ["missing"]}]
    response = await call("validate_subagent_result", {"contract": package["contract"], "result": value})
    assert not response["valid"] and response["errors"][0]["code"] == "unknown_evidence_id"
    package["contract"]["budgets"]["max_skills"] = True
    response = await call("validate_subagent_result", {"contract": package["contract"], "result": value})
    assert not response["valid"] and response["errors"][0]["path"].startswith("$.contract")
