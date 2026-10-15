"""Actual strict MCP metadata/dispatch and model-free authenticated ASGI jobs."""
import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
import json
import logging
from types import SimpleNamespace
from uuid import uuid4

import httpx
from mcp.types import CallToolRequest, CallToolRequestParams
import pytest

import src.server as server
from src.daemon.app import create_app
from src.daemon.workspaces import ClientContext, WorkspaceRegistry
from src.local_inference import tools
from src.local_inference.config import OwnerConfig
from src.local_inference.service import LocalInferenceService
from tests.local_inference_fake import FakeBackend

TOKEN = "test-local-inference-private-http-token" * 2


VALID = {
    "local_inference_status": {},
    "local_context_create": {"common_system": "", "shared_context": "data"},
    "local_context_grant": {"context_id": "ctx", "owner_token": "owner", "label": "child", "max_requests": 1, "max_total_output_tokens": 8},
    "local_context_revoke": {"context_id": "ctx", "owner_token": "owner", "grant_id": "grant"},
    "local_generate": {"context_id": "ctx", "access_token": "access", "request_id": str(uuid4()), "task": "task"},
    "local_generation_result": {"context_id": "ctx", "capability_token": "cap", "job_id": "job"},
    "local_generation_cancel": {"context_id": "ctx", "capability_token": "cap", "job_id": "job"},
    "local_context_release": {"context_id": "ctx", "owner_token": "owner"},
}


def runtime(port):
    mcp = server._WorkerInputMCP("fake local transport", host="127.0.0.1", port=port,
                                 stateless_http=True, json_response=True)
    tools.register_tools(mcp, server.TOOL_HINTS)
    @mcp.tool()
    async def ping() -> dict[str, str]:
        return {"ordinary": "available"}
    return mcp, None


@asynccontextmanager
async def application(tmp_path, *, enabled=True, token_factory=None):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    config = OwnerConfig(enabled=enabled, llama_server_binary="/fake/engine", model_path="/fake/model",
                         shutdown_grace_seconds=.1)
    backend = FakeBackend()
    local = LocalInferenceService(tmp_path / "local", config=config, backend=backend)
    if token_factory:
        from src.local_inference.context import ContextRegistry
        local.registry = ContextRegistry(config, token_factory=token_factory)
    workspaces = WorkspaceRegistry(tmp_path / "daemon")
    identity = workspaces.register(repo)
    app = create_app(workspaces.directory, TOKEN, runtime_loader=runtime, local_inference_service=local)
    client = ClientContext("sdk-request", "http", identity, repo)
    async with app.router.lifespan_context(app):
        for _ in range(100):
            if app.state.service.state != "starting":
                break
            await asyncio.sleep(.001)
        assert app.state.service.state == "ready"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765",
            headers={"Authorization": "Bearer " + TOKEN, "Accept": "application/json, text/event-stream",
                     "X-Agents-Workspace": identity}) as http:
            yield http, local, backend, client, app


async def rpc(http, name, arguments=None, *, headers=None, method="tools/call"):
    payload = {"jsonrpc": "2.0", "id": str(uuid4()), "method": method,
               "params": {"name": name, "arguments": arguments or {}}}
    response = await http.post("/mcp", json=payload, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["result"]


def value(result):
    assert not result.get("isError"), result
    parsed = json.loads(result["content"][0]["text"])
    assert parsed == result["structuredContent"]
    return parsed


async def context(http, **updates):
    return value(await rpc(http, "local_context_create", {"common_system": "rules", "shared_context": "shared data", **updates}))["data"]


def generate(created, **updates):
    return {"context_id": created["context_id"], "access_token": created["access_token"],
            "request_id": str(uuid4()), "task": "task", "max_output_tokens": 8, **updates}


def result_args(created, job, **updates):
    return {"context_id": created["context_id"], "capability_token": created["owner_token"],
            "job_id": job["job_id"], **updates}


@pytest.mark.asyncio
async def test_all_eight_real_metadata_flat_schemas_and_exact_hints():
    listed = {tool.name: tool for tool in await server.mcp.list_tools()}
    assert set(tools.TOOL_NAMES) <= set(listed)
    readers = {"local_inference_status", "local_generation_result"}
    destructive = {"local_context_revoke", "local_generation_cancel", "local_context_release"}
    idempotent = readers | destructive
    for name in tools.TOOL_NAMES:
        tool = listed[name]
        assert tool.title and tool.annotations.title == tool.title
        assert tool.annotations.readOnlyHint is (name in readers)
        assert tool.annotations.openWorldHint is False
        assert tool.annotations.idempotentHint is (name in idempotent)
        if name not in readers:
            assert tool.annotations.destructiveHint is (name in destructive)
        expected = set() if name == "local_inference_status" else set(tools.REQUEST_MODELS[name].model_fields)
        assert set(tool.inputSchema["properties"]) == expected
        for prop in tool.inputSchema["properties"].values():
            assert prop["type"] in {"string", "integer", "number", "array", "object"}
            assert prop["description"] and not {"anyOf", "$ref", "enum"} & prop.keys()
            if prop["type"] == "array":
                assert prop["items"] == {"type": "string"}
        assert tool.outputSchema["type"] == "object"


@pytest.mark.asyncio
@pytest.mark.parametrize("name", tools.TOOL_NAMES)
async def test_raw_unknown_fields_rejected_without_value_echo(name, caplog):
    caplog.set_level(logging.DEBUG)
    arguments = {**VALID[name], "engine_url": "sentinel-private-extra"}
    result = (await server.mcp._mcp_server.request_handlers[CallToolRequest](
        CallToolRequest(params=CallToolRequestParams(name=name, arguments=arguments)))).root
    assert result.isError and result.content[0].text == tools.INVALID_ARGUMENTS
    assert "sentinel-private-extra" not in result.content[0].text + caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("updates", [{"max_output_tokens": True}, {"max_output_tokens": "8"},
    {"temperature": True}, {"temperature": float("nan")}, {"timeout_seconds": "120"},
    {"task": {"sentinel-task-secret": "not text"}}, {"task": "\ud800"}, {"request_id": str(uuid4()).upper()}])
async def test_raw_types_ranges_and_unicode_fail_before_fastmcp_conversion(updates):
    result = (await server.mcp._mcp_server.request_handlers[CallToolRequest](CallToolRequest(
        params=CallToolRequestParams(name="local_generate", arguments={**VALID["local_generate"], **updates})))).root
    assert result.isError and result.content[0].text == tools.INVALID_ARGUMENTS


@pytest.mark.asyncio
async def test_array_string_is_not_reparsed_and_stdio_stays_model_free(monkeypatch):
    def unexpected(*a, **k):
        pytest.fail("stdio attempted workspace or broker access")
    monkeypatch.setattr(tools, "resolve_client_context", unexpected)
    result = (await server.mcp._mcp_server.request_handlers[CallToolRequest](CallToolRequest(
        params=CallToolRequestParams(name="local_context_create", arguments={**VALID["local_context_create"], "source_paths": '["a.py"]'})))).root
    assert result.isError and result.content[0].text == tools.INVALID_ARGUMENTS
    for name in tools.TOOL_NAMES:
        content, structured = await server.mcp.call_tool(name, VALID[name])
        assert json.loads(content[0].text) == structured
        assert structured["error"]["code"] == "transport_unsupported"


@pytest.mark.asyncio
async def test_real_asgi_bearer_workspace_disabled_and_metadata(tmp_path):
    async with application(tmp_path, enabled=False) as (http, local, backend, client, app):
        response = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                                   headers={"Authorization": "Bearer wrong"})
        assert response.status_code == 401 and not backend.calls
        status = value(await rpc(http, "local_inference_status", headers={"X-Agents-Workspace": ""}))
        assert status["data"]["enabled"] is False
        refused = value(await rpc(http, "local_context_create", VALID["local_context_create"]))
        assert refused["error"]["code"] == "disabled" and not backend.calls
        assert app.state.service.health()["state"] == "ready"
        raw = (await http.post("/mcp", json={"jsonrpc": "2.0", "id": 2, "method": "tools/list"})).json()["result"]
        assert set(tools.TOOL_NAMES) <= {tool["name"] for tool in raw["tools"]}


@pytest.mark.asyncio
async def test_enabled_workspace_errors_and_cross_workspace_capability_refusal(tmp_path):
    async with application(tmp_path) as (http, local, backend, client, app):
        for header, code in [("", "workspace_required"), ("forged-workspace", "workspace_invalid")]:
            refused = value(await rpc(http, "local_context_create", VALID["local_context_create"], headers={"X-Agents-Workspace": header}))
            assert refused["error"]["code"] == code
        created = await context(http)
        other = tmp_path / "other"; other.mkdir()
        identity = app.state.service.registry.register(other)
        refused = value(await rpc(http, "local_generate", generate(created), headers={"X-Agents-Workspace": identity}))
        assert refused["error"]["code"] == "context_unavailable" and not backend.calls


@pytest.mark.asyncio
async def test_json_looking_text_preserved_sdk_mcp_consistency_and_reconnect(tmp_path):
    async with application(tmp_path) as (http, local, backend, client, app):
        raw_system = '{"system":["exact"]}'
        raw_data = '["shared", {"data":true}]'
        raw_task = '{"task":["review"]}'
        created = await context(http, common_system=raw_system, shared_context=raw_data)
        backend.allow.clear()
        arguments = generate(created, task=raw_task)
        job = value(await rpc(http, "local_generate", arguments))["data"]
        await asyncio.wait_for(backend.started.wait(), 1)
        record = local.registry.contexts[created["context_id"]]
        assert (record.captured.common_system, record.captured.shared_context) == (raw_system, raw_data)
        assert record.jobs[job["job_id"]].request.task == raw_task
        duplicate = await local.call("local_generate", client, arguments)
        mcp_duplicate = value(await rpc(http, "local_generate", arguments))
        assert duplicate == mcp_duplicate and duplicate["data"]["deduplicated"]
        assert await local.call("local_inference_status", client, {}) == value(await rpc(http, "local_inference_status"))
        sdk_error = await local.call("local_generate", client, {**arguments, "task": "different"})
        assert sdk_error == value(await rpc(http, "local_generate", {**arguments, "task": "different"}))
        disconnected = asyncio.create_task(rpc(http, "local_generation_result", result_args(created, job, wait_seconds=20)))
        await asyncio.sleep(.01)
        disconnected.cancel(); await asyncio.gather(disconnected, return_exceptions=True)
        assert not record.jobs[job["job_id"]].cancel_event.is_set() and len(backend.calls) == 1
        assert value(await rpc(http, "ping")) == {"ordinary": "available"}
        assert value(await rpc(http, "local_inference_status"))["data"]["active"] == 1
        backend.allow.set()
        terminal = value(await rpc(http, "local_generation_result", result_args(created, job, wait_seconds=1)))
        assert terminal["data"]["state"] == "succeeded" and len(backend.calls) == 1
        assert await local.call("local_generation_result", client, result_args(created, job)) == value(
            await rpc(http, "local_generation_result", result_args(created, job)))


@pytest.mark.asyncio
async def test_real_asgi_explicit_cancel_then_grant_revoke_release(tmp_path):
    async with application(tmp_path) as (http, local, backend, client, app):
        created = await context(http)
        grant_args = {"context_id": created["context_id"], "owner_token": created["owner_token"],
                      "label": "reviewer", "max_requests": 2, "max_total_output_tokens": 32}
        grant = value(await rpc(http, "local_context_grant", grant_args))["data"]
        backend.allow.clear()
        job = value(await rpc(http, "local_generate", generate(created, access_token=grant["access_token"])))["data"]
        await backend.started.wait()
        cancelled = value(await rpc(http, "local_generation_cancel", result_args(created, job)))
        assert cancelled["data"]["state"] == "cancelling"
        terminal = value(await rpc(http, "local_generation_result", result_args(created, job, wait_seconds=1)))
        assert terminal["data"]["state"] == "cancelled" and backend.live == 0
        assert terminal["data"]["result"]["consumed_budget"] == {"model_requests": 1, "output_tokens": 8}
        revoke = {"context_id": created["context_id"], "owner_token": created["owner_token"], "grant_id": grant["grant_id"]}
        assert value(await rpc(http, "local_context_revoke", revoke))["data"]["cleanup_state"] == "done"
        release = {"context_id": created["context_id"], "owner_token": created["owner_token"]}
        assert value(await rpc(http, "local_context_release", release))["status"] == "ok"
        assert value(await rpc(http, "local_generate", generate(created)))["error"]["code"] == "context_unavailable"


@pytest.mark.asyncio
async def test_sensitive_inputs_outputs_credentials_never_enter_logging_sinks(tmp_path, monkeypatch, caplog):
    sentinel_system, sentinel_data, sentinel_task = "sentinel-system-secret", "sentinel-data-secret", "sentinel-task-secret"
    sentinel_owner, sentinel_access = "sentinel-owner-secret", "sentinel-access-secret"
    sentinel_output, sentinel_credential = "sentinel-output-secret", "sentinel-redis-credential"
    sequence = iter((sentinel_owner, sentinel_access))
    monkeypatch.setenv("AGENTS_LOCAL_KV_REDIS_URL", sentinel_credential)
    caplog.set_level(logging.DEBUG)
    def unexpected(*a, **k):
        pytest.fail("local generation attempted automatic tracing/history/debug logging")
    for name in ("debug_log", "log_interaction", "get_langfuse", "_readiness_problem"):
        monkeypatch.setattr(server, name, unexpected)
    monkeypatch.setattr(server, "HistoryWriter", unexpected)
    monkeypatch.setattr(server.debug_logger, "AGENTS_DEBUG", True)
    async with application(tmp_path, token_factory=lambda: next(sequence)) as (http, local, backend, client, app):
        created = await context(http, common_system=sentinel_system, shared_context=sentinel_data)
        backend.corrupt = {"output_text": sentinel_output}
        arguments = generate(created, task=sentinel_task)
        job = value(await rpc(http, "local_generate", arguments))["data"]
        terminal = value(await rpc(http, "local_generation_result", result_args(created, job, wait_seconds=1)))
        assert terminal["data"]["result"]["data"]["output_text"] == sentinel_output
        malformed = await rpc(http, "local_generate", {**arguments, "max_output_tokens": "sentinel-error-secret"})
        assert malformed["isError"] and malformed["content"][0]["text"] == tools.INVALID_ARGUMENTS
        assert "sentinel-error-secret" not in json.dumps(malformed)
        assert all(sentinel not in caplog.text for sentinel in (sentinel_system, sentinel_data, sentinel_task,
            sentinel_owner, sentinel_access, sentinel_output, sentinel_credential, "sentinel-error-secret"))
        assert not (client.root / "history.md").exists()
