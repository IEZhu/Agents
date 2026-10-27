"""Production artifact tool metadata, untouched inputs and authenticated transport."""
import asyncio
import copy
from contextlib import asynccontextmanager
import hashlib
import json
from pathlib import Path
import logging
import os
from uuid import uuid4

import httpx
from mcp.types import CallToolRequest, CallToolRequestParams
import pytest

import src.server as server
from src.artifacts import tools
from src.artifacts.service import ArtifactService, REQUEST_MODELS
from src.daemon.app import create_app
from src.daemon.workspaces import WorkspaceRegistry
from src.local_inference.config import OwnerConfig
from src.local_inference.service import LocalInferenceService
from src.schemas.subagent import PreparedPackage
from tests.test_artifact_service import reference
from tests.test_subagent import request, report, worker_tree, write_mdc

TOKEN = "artifact-mcp-private-http-token" * 3
VALID = {
    "artifact_publish": {"name": "input.txt", "content": "sentinel-private-body"},
    "artifact_manifest_create": {"task_id": "task-1", "entries": [
        {"artifact_id": "art_" + "a" * 64, "sha256": "a" * 64, "purpose": "Shared data"}]},
    "artifact_manifest_get": {"manifest_id": "manifest_" + "a" * 32, "manifest_sha256": "b" * 64},
    "artifact_manifest_verify": {"manifest_id": "manifest_" + "a" * 32, "manifest_sha256": "b" * 64},
    "artifact_manifest_release": {"manifest_id": "manifest_" + "a" * 32, "manifest_sha256": "b" * 64},
    "artifact_read": {"manifest_id": "manifest_" + "a" * 32, "manifest_sha256": "b" * 64,
                      "artifact_id": "art_" + "a" * 64},
}


def runtime(port):
    mcp = server._WorkerInputMCP("artifact transport", host="127.0.0.1", port=port,
                                stateless_http=True, json_response=True)
    tools.register_tools(mcp, server.TOOL_HINTS)
    for name in ("get_subagent_context", "validate_subagent_result"):
        mcp.add_tool(getattr(server, name), **server.TOOL_HINTS[name])
    return mcp, None


@asynccontextmanager
async def application(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    registry = WorkspaceRegistry((tmp_path / "daemon").resolve())
    identity = registry.register(repo)
    artifacts = ArtifactService((tmp_path / "artifact-store").resolve())
    local = LocalInferenceService(tmp_path / "local", config=OwnerConfig())
    app = create_app(registry.directory, TOKEN, runtime_loader=runtime,
                     local_inference_service=local, artifact_service=artifacts)
    async with app.router.lifespan_context(app):
        async def ready():
            while app.state.service.state == "starting":
                await asyncio.sleep(0)
            assert app.state.service.state == "ready"
        await asyncio.wait_for(ready(), 2)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer " + TOKEN, "Accept": "application/json, text/event-stream",
                     "X-Agents-Workspace": identity}) as http:
            yield http, artifacts, app, repo.resolve()


async def rpc(http, name, arguments=None, **options):
    response = await http.post("/mcp", json={"jsonrpc": "2.0", "id": str(uuid4()),
        "method": "tools/call", "params": {"name": name, "arguments": arguments or {}}}, **options)
    assert response.status_code == 200, response.text
    return response.json()["result"]


def value(result):
    assert not result.get("isError"), result
    parsed = json.loads(result["content"][0]["text"])
    assert parsed == result["structuredContent"]
    return parsed


async def pinned(http, body="sentinel-exact-shared-artifact-body"):
    artifact = value(await rpc(http, "artifact_publish", {"name": "data.txt", "content": body}))["data"]
    manifest = value(await rpc(http, "artifact_manifest_create", {"task_id": "task-1", "entries": [
        {"artifact_id": artifact["artifact_id"], "sha256": artifact["sha256"], "purpose": "Exact shared input"}]}))["data"]
    return artifact, manifest


@pytest.mark.asyncio
async def test_all_six_production_schemas_and_exact_tool_hints():
    listed = {tool.name: tool for tool in await server.mcp.list_tools()}
    readers = {"artifact_manifest_get", "artifact_read", "artifact_manifest_verify"}
    for name in tools.TOOL_NAMES:
        tool = listed[name]
        assert tool.title and tool.annotations.title == tool.title
        assert tool.annotations.readOnlyHint is (name in readers)
        assert tool.annotations.openWorldHint is False
        assert tool.annotations.idempotentHint is (name != "artifact_manifest_create")
        if name not in readers:
            assert tool.annotations.destructiveHint is (name == "artifact_manifest_release")
        assert set(tool.inputSchema["properties"]) == set(REQUEST_MODELS[name].model_fields)
        for prop in tool.inputSchema["properties"].values():
            assert prop["type"] in {"string", "integer", "array"}
            assert prop["description"] and not {"anyOf", "$ref", "enum"} & prop.keys()
        assert tool.outputSchema["type"] == "object"
    for name in ("get_subagent_context", "local_context_create"):
        prop = listed[name].inputSchema["properties"]["artifact_manifest"]
        assert prop["type"] == "object" and prop["description"]
        assert not {"anyOf", "$ref"} & prop.keys()


@pytest.mark.asyncio
@pytest.mark.parametrize("name", tools.TOOL_NAMES)
async def test_production_handler_rejects_unknown_artifact_fields_before_resolution(name, monkeypatch, caplog):
    async def unexpected(*args, **kwargs):
        pytest.fail("Malformed artifact arguments reached workspace resolution")
    monkeypatch.setattr(tools, "resolve_client_context", unexpected)
    caplog.set_level(logging.DEBUG)
    result = (await server.mcp._mcp_server.request_handlers[CallToolRequest](
        CallToolRequest(params=CallToolRequestParams(name=name,
            arguments={**VALID[name], "private_path": "sentinel-private-extra"})))).root
    assert result.isError and result.content[0].text == "Invalid artifact arguments."
    assert "sentinel-private" not in result.content[0].text + caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("name,updates", [
    ("artifact_publish", {"content": {"secret": "sentinel-private"}}),
    ("artifact_publish", {"name": 7}),
    ("artifact_manifest_create", {"entries": '[{"purpose":"sentinel-private"}]'}),
    ("artifact_manifest_create", {"ttl_seconds": True}),
    ("artifact_manifest_create", {"ttl_seconds": "30"}),
    ("artifact_manifest_create", {"entries": [{"artifact_id": "art_" + "a" * 64,
        "sha256": "a" * 64, "purpose": "sentinel-private", "required": 1}]}),
    ("artifact_manifest_create", {"entries": [{"artifact_id": "art_" + "a" * 64,
        "sha256": "a" * 64, "purpose": "data", "private": "sentinel-private"}]}),
    ("artifact_read", {"offset": True}),
    ("artifact_read", {"offset": "0"}),
    ("artifact_read", {"max_bytes": 4.0}),
])
async def test_real_mcp_dispatch_preserves_strict_raw_artifact_types(name, updates, monkeypatch, caplog):
    async def unexpected(*args, **kwargs):
        pytest.fail("Malformed arguments reached a file service")
    monkeypatch.setattr(tools, "resolve_service", unexpected)
    caplog.set_level(logging.DEBUG)
    result = (await server.mcp._mcp_server.request_handlers[CallToolRequest](
        CallToolRequest(params=CallToolRequestParams(name=name, arguments={**VALID[name], **updates})))).root
    assert result.isError and result.content[0].text == "Invalid artifact arguments."
    assert "sentinel-private" not in result.content[0].text + caplog.text


@pytest.mark.asyncio
async def test_authenticated_asgi_artifact_lifecycle_workspace_isolation_and_exact_json_text(tmp_path):
    async with application(tmp_path) as (http, artifacts, app, root):
        denied = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                 headers={"Authorization": "Bearer wrong"})
        assert denied.status_code == 401 and not artifacts.directory.exists()
        for header, code in (("", "workspace_required"), ("forged", "workspace_invalid")):
            refused = value(await rpc(http, "artifact_publish", VALID["artifact_publish"],
                headers={"X-Agents-Workspace": header}))
            assert refused["error"]["code"] == code and not artifacts.directory.exists()
        body = '{"instructions":["exact text"],"data":true}'
        artifact, manifest = await pinned(http, body)
        assert value(await rpc(http, "artifact_manifest_get", reference(manifest)))["data"] == manifest
        data = value(await rpc(http, "artifact_read", {**reference(manifest), "artifact_id": artifact["artifact_id"]}))["data"]
        assert data["text"] == body and data["eof"]
        assert value(await rpc(http, "artifact_manifest_verify", reference(manifest)))["data"]["verified"]
        (root / "capture.md").write_text("exact captured source", encoding="utf-8")
        captured = value(await rpc(http, "artifact_publish", {"name": "capture.md", "source_path": "capture.md"}))
        if os.name == "posix":
            assert captured["status"] == "ok"
        else:
            assert captured["error"]["code"] == "unsupported"
        other = tmp_path / "other"; other.mkdir()
        identity = app.state.service.registry.register(other)
        cross = value(await rpc(http, "artifact_read", {**reference(manifest), "artifact_id": artifact["artifact_id"]},
                                headers={"X-Agents-Workspace": identity}))
        assert cross["error"]["code"] == "artifact_unavailable"
        for _ in range(2):
            assert value(await rpc(http, "artifact_manifest_release", reference(manifest)))["data"]["released"]
        refused = value(await rpc(http, "artifact_read", {**reference(manifest), "artifact_id": artifact["artifact_id"]}))
        assert refused["error"]["code"] == "artifact_unavailable"


@pytest.mark.asyncio
async def test_preparation_uses_live_snapshot_and_retained_contract_without_inline_body(worker_tree, monkeypatch, caplog):
    write_mdc(worker_tree / "rules/subagents/rule-worker-artifacts.mdc",
              {"name": "worker-artifacts", "description": "Read pinned inputs completely"},
              "Use artifact_read for the exact pinned ID and SHA256 before reporting evidence.")
    def unexpected(*args, **kwargs):
        pytest.fail("Artifact transport or worker preparation attempted routing, history or tracing")
    for name in ("_readiness_problem", "load_persona", "route_persona", "HistoryWriter", "debug_log", "log_interaction", "get_langfuse"):
        monkeypatch.setattr(server, name, unexpected)
    monkeypatch.setattr(server.debug_logger, "AGENTS_DEBUG", True)
    caplog.set_level(logging.DEBUG)
    async with application(worker_tree) as (http, artifacts, app, root):
        body = "sentinel-exact-shared-artifact-body"
        artifact, manifest = await pinned(http, body)
        receipt = value(await rpc(http, "get_subagent_context", request(artifact_manifest=manifest)))
        assert receipt["delivery"] == "file", receipt
        text_context = Path(receipt["context_path"]).read_text(encoding="utf-8")
        assert manifest["sha256"] in text_context and artifact["artifact_id"] in text_context
        assert "artifact_read(" in text_context and body not in text_context
        package = value(await rpc(http, "get_subagent_context", request(
            artifact_manifest=manifest, delivery="inline", result_format="json")))
        assert package["status"] == "prepared", package
        assert package["contract"]["artifact_manifest"] == manifest
        assert "worker-artifacts" in package["descriptor"]["rules_loaded"]
        assert artifact["artifact_id"] in package["worker_context"]
        assert manifest["sha256"] in package["worker_context"]
        assert "artifact_read(" in package["worker_context"] and body not in package["worker_context"]
        result = report(PreparedPackage.model_validate(package))
        assert value(await rpc(http, "validate_subagent_result", {"contract": package["contract"], "result": result}))["valid"]
        wrong_task = value(await rpc(http, "get_subagent_context", request(task_id="different", artifact_manifest=manifest)))
        assert wrong_task["status"] == "error" and "worker_context" not in wrong_task
        forged = copy.deepcopy(manifest)
        forged["entries"][0]["artifact"]["name"] = "forged.txt"
        unsigned = {key: item for key, item in forged.items() if key != "sha256"}
        forged["sha256"] = hashlib.sha256(json.dumps(unsigned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
        refused = value(await rpc(http, "get_subagent_context", request(artifact_manifest=forged)))
        assert refused["status"] == "error" and "worker_context" not in refused
        assert "forged.txt" not in json.dumps(refused)
        other = worker_tree / "other"; other.mkdir()
        identity = app.state.service.registry.register(other)
        cross = value(await rpc(http, "get_subagent_context", request(artifact_manifest=manifest),
                                headers={"X-Agents-Workspace": identity}))
        assert cross["status"] == "error" and "worker_context" not in cross
        assert value(await rpc(http, "artifact_manifest_release", reference(manifest)))["status"] == "ok"
        released = value(await rpc(http, "get_subagent_context", request(artifact_manifest=manifest)))
        assert released["status"] == "error" and "worker_context" not in released
        # Structural output validation remains distinct from fresh evidence authorization.
        assert value(await rpc(http, "validate_subagent_result", {"contract": package["contract"], "result": result}))["valid"]
        assert value(await rpc(http, "artifact_manifest_verify", reference(manifest)))["status"] == "error"
        assert body not in caplog.text and not (root / "history.md").exists()


@pytest.mark.asyncio
async def test_preparation_without_artifacts_never_resolves_artifact_storage(worker_tree, monkeypatch):
    async def unexpected(*args, **kwargs):
        pytest.fail("Artifact-free worker preparation resolved artifact storage")
    monkeypatch.setattr(tools, "verify_preparation", unexpected)
    content, structured = await server.mcp.call_tool("get_subagent_context", request(delivery="inline"))
    assert structured["status"] == "prepared" and json.loads(content[0].text) == structured
    assert "artifact_manifest" not in structured["contract"]


@pytest.mark.asyncio
async def test_generated_output_manifest_roundtrip_keeps_original_input_contract(worker_tree):
    write_mdc(worker_tree / "rules/subagents/rule-worker-artifacts.mdc",
              {"name": "worker-artifacts", "description": "Read pinned input and return output references"},
              "Read required inputs with artifact_read; publish outputs only within assigned scope.")
    async with application(worker_tree) as (http, artifacts, app, root):
        _, input_manifest = await pinned(http, "original task input")
        package = value(await rpc(http, "get_subagent_context", request(
            artifact_manifest=input_manifest, scope=["Read input and publish a generated report artifact."],
            delivery="inline", result_format="json")))
        assert package["status"] == "prepared", package
        retained = copy.deepcopy(package["contract"])
        output, output_manifest = await pinned(http, "exact generated report: αβγ")
        uri = f"artifact://{output_manifest['manifest_id']}/{output_manifest['sha256']}/{output['artifact_id']}"
        result = report(PreparedPackage.model_validate(package))
        result["artifacts"] = [{"path": uri, "purpose": "Generated report"}]
        assert value(await rpc(http, "validate_subagent_result", {"contract": retained, "result": result}))["valid"]
        manifest_id, manifest_sha256, artifact_id = uri.removeprefix("artifact://").split("/")
        output_ref = {"manifest_id": manifest_id, "manifest_sha256": manifest_sha256}
        retrieved = value(await rpc(http, "artifact_manifest_get", output_ref))["data"]
        assert retrieved["task_id"] == retained["task_id"]
        assert retrieved["workspace_sha256"] == input_manifest["workspace_sha256"]
        assert retrieved["entries"][0]["artifact"]["artifact_id"] == artifact_id
        read = value(await rpc(http, "artifact_read", {**output_ref, "artifact_id": artifact_id}))["data"]
        assert read["text"] == "exact generated report: αβγ" and read["eof"]
        assert value(await rpc(http, "artifact_manifest_verify", output_ref))["data"]["verified"]
        assert retained == package["contract"] and retained["artifact_manifest"] == input_manifest
        assert output_manifest["manifest_id"] != input_manifest["manifest_id"]
        assert value(await rpc(http, "artifact_manifest_release", output_ref))["status"] == "ok"
        assert value(await rpc(http, "artifact_manifest_verify", reference(input_manifest)))["data"]["verified"]
