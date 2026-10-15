"""Cooperation and shared artifacts coexist in one authenticated daemon request scope."""
import asyncio
import json

import httpx
import pytest

import src.server as server
from src.artifacts import tools as artifact_tools
from src.artifacts.service import ArtifactService
from src.cooperation.models import PROTOCOL
from src.daemon.app import create_app
from src.daemon.workspaces import WorkspaceRegistry
from src.local_inference import tools as inference_tools
from src.local_inference.config import OwnerConfig
from src.local_inference.service import LocalInferenceService
from tests.test_artifact_mcp import rpc, value
from tests.test_artifact_service import reference
from tests.test_cooperation_identity import checkouts
from tests.test_cooperation_mcp import NAMES, TOKEN, registration


def runtime(port):
    mcp = server._WorkerInputMCP("combined runtime", host="127.0.0.1", port=port,
                                stateless_http=True, json_response=True)
    artifact_tools.register_tools(mcp, server.TOOL_HINTS)
    inference_tools.register_tools(mcp, server.TOOL_HINTS)
    for name in NAMES:
        mcp.add_tool(getattr(server, name), **server.TOOL_HINTS[name])
    return mcp, None


@pytest.mark.asyncio
async def test_artifact_reference_crosses_mailbox_with_all_services_bound(checkouts, monkeypatch):
    main, _, authority = checkouts
    monkeypatch.setenv("AGENTS_COOPERATION_ENABLED", "1")
    monkeypatch.delenv("AGENTS_COOPERATION_RETENTION_DAYS", raising=False)
    registry = WorkspaceRegistry(authority)
    workspace = registry.register(main)
    artifacts = ArtifactService(authority / "artifacts")
    local = LocalInferenceService(authority / "local", config=OwnerConfig(), artifact_service=artifacts)
    app = create_app(authority, TOKEN, runtime_loader=runtime,
                     local_inference_service=local, artifact_service=artifacts)
    async with app.router.lifespan_context(app):
        async with asyncio.timeout(3):
            while app.state.service.state == "starting":
                await asyncio.sleep(.001)
        assert app.state.service.state == "ready"
        headers = {"Authorization": "Bearer " + TOKEN, "X-Agents-Workspace": workspace,
                   "Accept": "application/json, text/event-stream"}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                    base_url="http://127.0.0.1:8765", headers=headers) as http:
            status = value(await rpc(http, "local_inference_status"))["data"]
            assert status["enabled"] is False and local.backend is None
            artifact = value(await rpc(http, "artifact_publish", {
                "name": "shared.txt", "content": "exact shared result"}))["data"]
            manifest = value(await rpc(http, "artifact_manifest_create", {
                "task_id": "sender", "entries": [{"artifact_id": artifact["artifact_id"],
                "sha256": artifact["sha256"], "purpose": "Exchange an immutable result"}]}))["data"]
            actors = [value(await rpc(http, "cooperation_register", {"request": {
                **registration(main), "task_id": name, "root_task_id": name}}))
                for name in ("sender", "recipient")]
            sender, recipient = actors
            credentials = lambda actor: {"protocol": PROTOCOL, "repository_id": actor["repository_id"],
                "participant_id": actor["participant_id"], "capability": actor["capability"]}
            pointer = {**reference(manifest), "artifact_id": artifact["artifact_id"]}
            sent = value(await rpc(http, "cooperation_send", {"request": {
                **credentials(sender), "recipient_id": recipient["participant_id"],
                "operation_id": "publish-reference", "body": json.dumps(pointer)}}))
            assert sent["status"] == "ok"
            inbox = value(await rpc(http, "cooperation_receive", {"request": credentials(recipient)}))
            assert len(inbox["messages"]) == 1
            received = json.loads(inbox["messages"][0]["body"])
            assert received == pointer
            content = value(await rpc(http, "artifact_read", received))["data"]
            assert content["text"] == "exact shared result" and content["eof"]
            assert value(await rpc(http, "artifact_manifest_verify", reference(manifest)))["data"]["verified"]
            other = authority / "other"
            other.mkdir()
            foreign_workspace = registry.register(other)
            denied = value(await rpc(http, "artifact_read", received,
                                     headers={"X-Agents-Workspace": foreign_workspace}))
            assert denied["error"]["code"] == "artifact_unavailable"
            assert local.backend is None  # Message delivery never starts a model.
    assert app.state.service.cooperation is None and local._closed
