"""Tiered workers through the MCP tools: schemas, client detection, escalation, ledger."""
import json
from types import SimpleNamespace

import jsonschema
import pytest

import src.server as server
from src.daemon.workspaces import calling_app
from src.engine import model_catalogs, subagent_ledger
from src.schemas.subagent import PreparedPackage
from tests.test_subagent import report, request, worker_tree  # noqa: F401
from tests.test_subagent_mcp import call, inline


@pytest.fixture(autouse=True)
def catalogs(monkeypatch):
    monkeypatch.delenv("AGENTS_SUBAGENT_TIERING", raising=False)
    monkeypatch.delenv("AGENTS_SUBAGENT_LEDGER", raising=False)
    monkeypatch.setattr(model_catalogs, "codex_models", lambda path=None: ["gpt-6-luna", "gpt-6.1-sol"])
    monkeypatch.setattr(model_catalogs, "cursor_models", lambda path=None: ["default", "composer-2.5"])


async def schemas():
    return {tool.name: tool for tool in await server.mcp.list_tools()}


@pytest.mark.asyncio
async def test_tiered_package_and_escalation_match_the_output_schemas(worker_tree):
    tools = await schemas()
    receipt = await call("get_subagent_context", request(complexity="low", client="cursor"))
    jsonschema.validate(receipt, tools["get_subagent_context"].outputSchema)
    assert receipt["execution"]["spawn"] == {"tool": "Task", "arguments": {"model": "default"}}
    package = await call("get_subagent_context", inline(complexity="low", client="cursor"))
    jsonschema.validate(package, tools["get_subagent_context"].outputSchema)
    assert package["execution"]["spawn"] == {"tool": "Task", "arguments": {"model": "default"}}
    assert package["contract"]["execution"]["model"] == "default"
    value = report(PreparedPackage.model_validate(package), "partial")
    response = await call("validate_subagent_result", {"contract": package["contract"], "result": value})
    jsonschema.validate(response, tools["validate_subagent_result"].outputSchema)
    assert response["valid"] is True
    assert response["escalation"] == {"complexity": "standard", "tier": "standard", "reason": "status_partial"}


@pytest.mark.asyncio
async def test_untiered_responses_keep_their_shape(worker_tree):
    package = await call("get_subagent_context", inline())
    assert "execution" not in package and "execution" not in package["contract"]
    assert "execution" not in await call("get_subagent_context", request())
    response = await call("validate_subagent_result", {
        "contract": package["contract"], "result": report(PreparedPackage.model_validate(package), "failed")})
    assert response == {"valid": True, "errors": [], "verification": "structural_only",
                        "result_sha256": response["result_sha256"]}


@pytest.mark.asyncio
@pytest.mark.parametrize("app,client,model", [
    ("codex-mcp-client", "codex", "gpt-6-luna"), ("claude-code", "claude", "haiku"),
    ("cursor-vscode", "cursor", "default"), ("claude-desktop", None, None), (None, None, None)])
async def test_the_client_is_detected_when_omitted(worker_tree, monkeypatch, app, client, model):
    monkeypatch.setattr(server, "calling_app", lambda ctx=None: app)
    package = await call("get_subagent_context", request(complexity="low"))
    assert (package["execution"]["client"], package["execution"]["model"]) == (client, model)
    assert ("client:unknown" in package["execution"]["floor_reasons"]) is (client is None)


@pytest.mark.asyncio
async def test_an_explicit_client_wins_over_detection(worker_tree, monkeypatch):
    monkeypatch.setattr(server, "calling_app", lambda ctx=None: "cursor")
    package = await call("get_subagent_context", request(complexity="standard", client="codex"))
    assert package["execution"]["model"] == "gpt-6.1-sol"


@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{"complexity": "trivial"}, {"complexity": 1}, {"client": "vscode"},
                                  {"client": ["codex"]}])
async def test_invalid_tier_arguments_are_refused(worker_tree, extra):
    result = await call("get_subagent_context", request(**extra))
    assert result["status"] == "error" and "worker_context" not in result


@pytest.mark.asyncio
async def test_the_tools_write_the_ledger_only_when_enabled(worker_tree, tmp_path, monkeypatch):
    path = tmp_path / "ledger.jsonl"
    monkeypatch.setattr(subagent_ledger, "ledger_path", lambda: path)
    package = await call("get_subagent_context", request(complexity="low", client="claude"))
    assert not path.exists()
    monkeypatch.setenv("AGENTS_SUBAGENT_LEDGER", "1")
    package = await call("get_subagent_context", inline(complexity="low", client="claude"))
    await call("validate_subagent_result", {"contract": package["contract"],
                                            "result": report(PreparedPackage.model_validate(package))})
    events = [json.loads(line)["event"] for line in path.read_text(encoding="utf-8").splitlines()]
    assert events == ["prepared", "validated"]


def test_calling_app_reads_the_header_then_client_info():
    header = SimpleNamespace(request_context=SimpleNamespace(request=SimpleNamespace(
        state=SimpleNamespace(client_context=SimpleNamespace(client="codex")))))
    assert calling_app(header) == "codex"
    stdio = SimpleNamespace(session=SimpleNamespace(client_params=SimpleNamespace(
        clientInfo=SimpleNamespace(name="claude-code"))))
    assert calling_app(stdio) == "claude-code"
    assert calling_app(None) is None
