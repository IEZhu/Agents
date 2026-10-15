"""Real MCP/HTTP boundaries and isolated journals; no embedding, live state or model."""
import asyncio
from contextlib import asynccontextmanager
import json
from types import SimpleNamespace
import sys

import httpx
import jsonschema
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import Implementation

import src.server as server
from src.cooperation.models import PROTOCOL
from src.daemon.app import create_app
from tests.test_cooperation_identity import checkouts


TOKEN = "x" * 48
NAMES = ("cooperation_register", "cooperation_read", "cooperation_apply",
         "cooperation_send", "cooperation_receive", "cooperation_ack",
         "cooperation_request_status", "cooperation_request_update", "cooperation_wait")


def registration(checkout):
    return {"protocol": PROTOCOL, "checkout_path": str(checkout), "root_task_id": "root", "task_id": "task",
            "attempt_id": "1", "scope": [{"resource_type": "physical_file", "resource_key": "file.py",
                                            "access": "write"}]}


def runtime(port):
    mcp = server._WorkerInputMCP("cooperation-test", host="127.0.0.1", port=port,
                                stateless_http=True, json_response=True)
    for name in NAMES:
        mcp.tool(**server.TOOL_HINTS[name])(getattr(server, name))
    mcp.resource("agents-core://cooperation/schema", mime_type="application/json")(server.cooperation_schema)
    return mcp, None


@pytest.fixture
def coordinator(monkeypatch):
    monkeypatch.delenv("AGENTS_COOPERATION_RETENTION_DAYS", raising=False)
    class Coordinator:
        instances = []

        def __init__(self, directory, *, retention_days=30):
            self.directory = directory
            self.retention_days = retention_days
            self.started = self.closed = 0
            self.calls = []
            self.instances.append(self)

        def start(self):
            self.directory.mkdir(parents=True)
            self.started += 1

        def close(self):
            self.closed += 1

        def register(self, request, identity):
            self.calls.append((request, identity))
            return {"protocol": PROTOCOL, "status": "ok", "participant_id": "actor",
                    "repository_id": identity.repository_id, "workspace_id": identity.workspace_id,
                    "checkout_id": identity.checkout_id, "generation": 1, "service_epoch": 1,
                    "capability": "private-test-capability", "revision": 1,
                    "communication_mode": request.communication_mode, "guidance": "Test response."}

        def read(self, request, identity):
            self.calls.append((request, identity))
            return {"protocol": PROTOCOL, "status": "ok", "repository_id": identity.repository_id,
                    "revision": 1, "service_epoch": 1, "minimum_cursor": 0, "snapshot_cursor": 0, "snapshot_time": 1000.0,
                    "next_cursor": 0, "has_more_events": False, "participants": [], "agreements": [],
                    "snapshot_counts": {"participants": 0, "agreements": 0}, "snapshot_truncated": False,
                    "claims": [], "artifacts": [], "dependencies": [], "ready": [], "events": [],
                    "limitations": "Test response."}

        def apply(self, request, identity):
            self.calls.append((request, identity))
            event = {"protocol": PROTOCOL, "event_id": "event", "operation_id": request.operation_id,
                     "repository_id": identity.repository_id, "revision": 2, "actor_id": request.participant_id,
                     "subject_id": None, "task_id": "task", "attempt_id": "1", "generation": 1,
                     "kind": "heartbeat", "agreement_id": None, "proposal_revision": None, "reply_to": None,
                     "created_at": 1.0, "payload": request.action.model_dump(mode="json")}
            return {"protocol": PROTOCOL, "status": "ok", "revision": 2, "generation": 1,
                    "service_epoch": 1, "event": event, "cursor": 1, "checkpoint_required": []}

    monkeypatch.setitem(sys.modules, "src.cooperation.service", SimpleNamespace(CooperationService=Coordinator))
    return Coordinator


async def ready(app):
    for _ in range(200):
        if app.state.service.state != "starting":
            break
        await asyncio.sleep(.01)
    assert app.state.service.state == "ready"


async def call(http, name, arguments, workspace_id=None):
    headers = {"Authorization": "Bearer " + TOKEN, "Accept": "application/json, text/event-stream"}
    if workspace_id is not None:
        headers["X-Agents-Workspace"] = workspace_id
    answer = await http.post("/mcp", headers=headers, json={"jsonrpc": "2.0", "id": 1,
                            "method": "tools/call", "params": {"name": name, "arguments": arguments}})
    assert answer.status_code == 200
    payload = answer.json()["result"]
    assert not payload.get("isError"), payload
    return json.loads(payload["content"][0]["text"])


@pytest.mark.asyncio
async def test_tool_schemas_and_stdio_unavailability(checkouts):
    main, _, _ = checkouts
    schemas = {tool.name: tool for tool in await server.mcp.list_tools()}
    for name in NAMES:
        tool = schemas[name]
        assert tool.inputSchema["properties"]["request"]["type"] == "object"
        assert tool.inputSchema["properties"]["workspace"]["type"] == "string"
        assert tool.outputSchema["type"] == "object"
        assert tool.annotations.openWorldHint is False
    content, structured = await server.mcp.call_tool("cooperation_register", {"request": registration(main)})
    assert json.loads(content[0].text) == structured
    assert structured["error"] == "store_unavailable"
    jsonschema.validate(structured, schemas["cooperation_register"].outputSchema)
    resources = {str(item.uri): item for item in await server.mcp.list_resources()}
    assert resources["agents-core://cooperation/schema"].mimeType == "application/json"
    contents = list(await server.mcp.read_resource("agents-core://cooperation/schema"))
    schemas = json.loads(contents[0].content)
    assert schemas["protocol"] == PROTOCOL
    assert {"RegisterRequest", "ReadRequest", "ApplyRequest", "RegisterResponse", "ReadResponse",
            "ApplyResponse", "Recover", "SendRequest", "ReceiveRequest", "AckRequest", "Message",
            "SendResponse", "ReceiveResponse", "AckResponse"} <= set(schemas["schemas"])


@pytest.mark.asyncio
async def test_http_retains_external_access_and_single_lifecycle(checkouts, coordinator, monkeypatch):
    main, external, directory = checkouts
    monkeypatch.setenv("AGENTS_COOPERATION_ENABLED", "1")
    monkeypatch.setenv("AGENTS_COOPERATION_RETENTION_DAYS", "45")
    app = create_app(directory, TOKEN, runtime_loader=runtime)
    async with app.router.lifespan_context(app):
        await ready(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765") as http:
            response = await http.post("/workspaces", headers={"Authorization": "Bearer " + TOKEN},
                                       json={"path": str(external), "roots": [external.as_uri()]})
            assert response.status_code == 200, response.text
            access = response.json()
            assert access["root"] == str(main)
            identity = access["workspace_id"]
            assert app.state.service.registry.cooperation_roots(identity) == (external,)
            registered = await call(http, "cooperation_register", {"request": registration(external)}, identity)
            assert registered["status"] == "ok"
            assert coordinator.instances[0].calls[0][1].checkout_path == str(external)
            read = {"protocol": PROTOCOL, "repository_id": registered["repository_id"]}
            assert (await call(http, "cooperation_read", {"request": read, "workspace": str(external)}, identity))["status"] == "ok"
            apply = {**read, "participant_id": "actor", "capability": "a" * 48, "operation_id": "op",
                     "expected_revision": 0, "expected_generation": 1, "action": {"kind": "heartbeat"}}
            assert (await call(http, "cooperation_apply", {"request": apply, "workspace": str(external)}, identity))["status"] == "ok"
            denied = await call(http, "cooperation_register", {"request": registration(main)}, identity)
            assert denied["error"] == "scope_denied"
            forged = await call(http, "cooperation_read", {"request": {**read, "repository_id": "repository_wrong"}}, identity)
            assert forged["error"] == "unauthorized_actor"
            assert len(coordinator.instances) == 1 and coordinator.instances[0].started == 1
            assert coordinator.instances[0].retention_days == 45
            assert len(coordinator.instances[0].calls) == 3
    assert coordinator.instances[0].closed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("origin", ["cwd", "CLAUDE_PROJECT_DIR", "hook", "AGENTS_CLIENT_REPO_ROOT"])
@pytest.mark.parametrize("roots_kind", ["absent", "package", "checkout"])
@pytest.mark.parametrize("linked", [False, True])
async def test_workspace_origin_does_not_expand_named_physical_grants(
        checkouts, coordinator, monkeypatch, origin, roots_kind, linked):
    main, external, directory = checkouts
    checkout = external if linked else main
    package = checkout / "pkg"
    package.mkdir()
    monkeypatch.setenv("AGENTS_COOPERATION_ENABLED", "1")
    body = {"path": str(package), "origin": origin}
    if roots_kind != "absent":
        body["roots"] = [(package if roots_kind == "package" else checkout).as_uri()]
    expanded = origin != "AGENTS_CLIENT_REPO_ROOT" and (
        roots_kind == "checkout" or roots_kind == "absent" and origin == "cwd")
    app = create_app(directory, TOKEN, runtime_loader=runtime)
    async with app.router.lifespan_context(app):
        await ready(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app),
                                    base_url="http://127.0.0.1:8765") as http:
            response = await http.post("/workspaces", json=body,
                                       headers={"Authorization": "Bearer " + TOKEN})
            assert response.status_code == 200, response.text
            access = response.json()
            memory_root = package if origin == "AGENTS_CLIENT_REPO_ROOT" and roots_kind == "absent" else main
            assert access["root"] == str(memory_root)
            grants = app.state.service.registry.cooperation_roots(access["workspace_id"])
            assert grants == (checkout if expanded else package,)
            actor = await call(http, "cooperation_register", {"request": registration(checkout)},
                               access["workspace_id"])
            if expanded:
                assert actor["status"] == "ok"
            else:
                assert actor["error"] == "scope_denied"
                assert not coordinator.instances[0].calls


@pytest.mark.asyncio
async def test_disabled_daemon_does_not_create_authority(checkouts, coordinator, monkeypatch):
    main, _, directory = checkouts
    monkeypatch.delenv("AGENTS_COOPERATION_ENABLED", raising=False)
    app = create_app(directory, TOKEN, runtime_loader=runtime)
    async with app.router.lifespan_context(app):
        await ready(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765") as http:
            answer = await call(http, "cooperation_register", {"request": registration(main)})
            assert answer["error"] == "store_unavailable"
    assert not coordinator.instances and not (directory / "cooperation").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    {"request": '{"protocol":"agents-core/cooperation/1","protocol":"agents-core/cooperation/1"}'},
    {"request": '{"protocol":"agents-core/cooperation/1","limit":NaN}'},
    {"request": {"protocol": PROTOCOL, "repository_id": "repo", "limit": True}},
    {"request": {"protocol": PROTOCOL, "repository_id": "repo", "secret-extra": "DO_NOT_ECHO"}},
    {"request": {"protocol": PROTOCOL, "repository_id": "repo"}, "forged_context": "DO_NOT_ECHO"},
    {"request": {"protocol": PROTOCOL, "repository_id": "repo"}, "workspace": ["DO_NOT_ECHO"]},
    {"request": "DO_NOT_ECHO" * 4000},
    {},
])
async def test_strict_mcp_errors_are_bounded_and_redacted(bad):
    content, structured = await server.mcp.call_tool("cooperation_read", bad)
    assert structured["error"] == "invalid_request"
    assert len(content[0].text) < 512 and "DO_NOT_ECHO" not in content[0].text


@pytest.mark.asyncio
@pytest.mark.parametrize("name", ["cooperation_send", "cooperation_receive", "cooperation_ack",
                                 "cooperation_request_status", "cooperation_request_update", "cooperation_wait"])
@pytest.mark.parametrize("bad", [
    {},
    {"request": {"protocol": PROTOCOL}, "unknown": "DO_NOT_ECHO"},
    {"request": {"protocol": PROTOCOL}, "workspace": True},
    {"request": {"protocol": PROTOCOL, "capability": "DO_NOT_ECHO", "body": "PRIVATE_BODY", "extra": 1}},
    {"request": '{"protocol":"agents-core/cooperation/1","protocol":"DO_NOT_ECHO"}'},
    {"request": "DO_NOT_ECHO" * 4000},
])
async def test_mailbox_mcp_wrappers_reject_invalid_input_without_leakage(name, bad):
    content, structured = await server.mcp.call_tool(name, bad)
    assert structured["error"] == "invalid_request"
    assert len(content[0].text) < 512
    assert "DO_NOT_ECHO" not in content[0].text and "PRIVATE_BODY" not in content[0].text


@pytest.mark.parametrize("model", [server.SendResponse, server.ReceiveResponse, server.AckResponse,
                                  server.RequestStatusResponse, server.WaitResponse])
def test_invalid_mailbox_response_cannot_leak_message_or_capability(model, caplog):
    response = server._cooperation_result(model, {
        "protocol": PROTOCOL, "status": "ok", "capability": "private-capability-do-not-log",
        "message": {"body": "private-message-do-not-log"},
    }).model_dump(mode="json")
    assert response["error"] == "store_unavailable"
    assert "private-capability-do-not-log" not in json.dumps(response) + caplog.text
    assert "private-message-do-not-log" not in json.dumps(response) + caplog.text


def test_invalid_internal_response_cannot_expose_capability(caplog):
    answer = server._cooperation_result(server.RegisterResponse, {
        "protocol": PROTOCOL, "status": "ok", "capability": "private-capability-do-not-log",
    }).model_dump(mode="json")
    assert answer["error"] == "store_unavailable"
    assert "private-capability-do-not-log" not in json.dumps(answer) + caplog.text


@pytest.mark.asyncio
async def test_failed_service_leaves_daemon_available(checkouts, coordinator, monkeypatch, caplog):
    main, _, directory = checkouts
    monkeypatch.setenv("AGENTS_COOPERATION_ENABLED", "1")
    def fail(self):
        raise RuntimeError("private-capability-do-not-log")
    monkeypatch.setattr(coordinator, "start", fail)
    app = create_app(directory, TOKEN, runtime_loader=runtime)
    async with app.router.lifespan_context(app):
        await ready(app)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765") as http:
            assert (await call(http, "cooperation_register", {"request": registration(main)}))["error"] == "store_unavailable"
    assert "private-capability-do-not-log" not in caplog.text
    assert coordinator.instances[0].closed == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("retention", ["29", "invalid", "30.5", ""])
async def test_invalid_retention_disables_only_optional_coordinator(checkouts, coordinator, monkeypatch, retention):
    main, _, directory = checkouts
    monkeypatch.setenv("AGENTS_COOPERATION_ENABLED", "1")
    monkeypatch.setenv("AGENTS_COOPERATION_RETENTION_DAYS", retention)
    app = create_app(directory, TOKEN, runtime_loader=runtime)
    async with app.router.lifespan_context(app):
        await ready(app)
        assert not coordinator.instances
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765") as http:
            answer = await call(http, "cooperation_register", {"request": registration(main)})
            assert answer["error"] == "store_unavailable"


@pytest.mark.asyncio
async def test_real_journal_two_roots_and_parent_relay(checkouts, monkeypatch):
    """Round-trip real persisted transitions through registered HTTP MCP tools."""
    main, external, directory = checkouts
    monkeypatch.setenv("AGENTS_COOPERATION_ENABLED", "1")
    monkeypatch.delenv("AGENTS_COOPERATION_RETENTION_DAYS", raising=False)
    app = create_app(directory, TOKEN, runtime_loader=runtime)
    access = app.state.service.registry.register(main)
    app.state.service.registry.register(main, physical_root=external)
    schemas = {tool.name: tool.outputSchema for tool in await server.mcp.list_tools() if tool.name in NAMES}
    file_resource = {"resource_type": "physical_file", "resource_key": "file.py", "access": "write"}
    child_resource = {**file_resource, "resource_key": "child.py"}
    integration = {"resource_type": "integration_target", "resource_key": "dev", "access": "integrate"}
    async with app.router.lifespan_context(app):
        await ready(app)
        assert app.state.service.cooperation is not None
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765") as http:
            async def invoke(name, request, checkout=main):
                result = await call(http, name, {"request": request, "workspace": str(checkout)}, access)
                jsonschema.validate(result, schemas[name])
                return result

            first = await invoke("cooperation_register", {
                **registration(main), "root_task_id": "root-a", "task_id": "parent",
                "scope": [{"resource_type": "physical_subtree", "resource_key": ".", "access": "write"}, integration],
                "execution_mode": "independent",
            })
            second = await invoke("cooperation_register", {
                **registration(external), "root_task_id": "root-b", "task_id": "independent",
                "scope": [file_resource, integration], "execution_mode": "independent",
            }, external)
            assert first["status"] == second["status"] == "ok", (first, second)
            assert first["repository_id"] == second["repository_id"]
            assert first["checkout_id"] != second["checkout_id"]
            revision, operation = second["revision"], 0

            async def mutate(actor, action, checkout=main, subject=None):
                nonlocal revision, operation
                operation += 1
                request = {"protocol": PROTOCOL, "repository_id": actor["repository_id"],
                           "participant_id": actor["participant_id"], "capability": actor["capability"],
                           "operation_id": f"op-{operation}", "expected_revision": revision,
                           "expected_generation": (subject or actor)["generation"], "action": action}
                if subject is not None:
                    request["subject_id"] = subject["participant_id"]
                result = await invoke("cooperation_apply", request, checkout)
                if result["status"] == "ok":
                    revision = result["revision"]
                return result

            acquire = {"kind": "acquire", "resources": [file_resource, integration],
                       "work_unit_id": "unit-a", "checkpoint": {"trigger": "before integration"}}
            first_claims = await mutate(first, acquire)
            assert first_claims["status"] == "ok", first_claims
            conflict = await mutate(second, {**acquire, "work_unit_id": "unit-b"}, external)
            assert conflict["error"] == "claim_conflict"
            private = await mutate(second, {**acquire, "resources": [file_resource], "work_unit_id": "private-b"}, external)
            assert private["status"] == "ok", private

            child = await invoke("cooperation_register", {
                **registration(main), "root_task_id": "root-a", "task_id": "child",
                "parent_id": first["participant_id"], "parent_capability": first["capability"],
                "contract_revision": "a" * 64, "scope": [child_resource], "communication_mode": "parent_relay",
            })
            assert child["status"] == "ok", child
            revision = child["revision"]
            denied = await mutate(second, {"kind": "heartbeat"}, external, subject=child)
            assert denied["error"] == "unauthorized_actor"
            proposal = {
                "kind": "propose", "agreement_id": "parent-child", "pattern": "partition_join",
                "required_participants": [first["participant_id"], child["participant_id"]],
                "task_bindings": [
                    {"participant_id": first["participant_id"], "task_id": "parent", "attempt_id": "1", "generation": 1},
                    {"participant_id": child["participant_id"], "task_id": "child", "attempt_id": "1", "generation": 1,
                     "contract_revision": "a" * 64, "parent_id": first["participant_id"]},
                ],
                "terms": {"scope": "Separate parent and child files", "resources": [
                    {**file_resource, "owner": first["participant_id"]},
                    {**child_resource, "owner": child["participant_id"]},
                ], "integrator": first["participant_id"], "checkpoint": {"trigger": "child ready"},
                    "completion_conditions": ["Parent validates unchanged child result"]},
            }
            proposed = await mutate(first, proposal)
            assert proposed["status"] == "ok", proposed
            acknowledge = {"kind": "acknowledge", "agreement_id": "parent-child", "proposal_revision": 1,
                           "terms_digest": proposed["agreement"]["terms_digest"]}
            assert (await mutate(first, acknowledge))["status"] == "ok"
            relay = await mutate(first, {**acknowledge, "acceptance_mode": "parent_relay"}, subject=child)
            assert relay["status"] == "ok", relay
            assert relay["agreement"]["state"] == "committed"
            assert relay["event"]["actor_id"] == first["participant_id"]
            assert relay["event"]["subject_id"] == child["participant_id"]
            claimed = await mutate(first, {**acquire, "resources": [child_resource], "work_unit_id": "child-work",
                                          "agreement_id": "parent-child"}, subject=child)
            assert claimed["status"] == "ok", claimed
            snapshot = await invoke("cooperation_read", {"protocol": PROTOCOL, "repository_id": first["repository_id"]})
            assert snapshot["status"] == "ok", snapshot
            assert len(snapshot["participants"]) == 3 and len(snapshot["claims"]) == 4
            assert snapshot["agreements"][0]["state"] == "active"
            serialized = json.dumps(snapshot)
            assert all(actor["capability"] not in serialized for actor in (first, second, child))
            assert not (main / "file.py").exists() and not (external / "file.py").exists()


@asynccontextmanager
async def independent_mcp_client(app, workspace_id, name):
    """A separate SDK session and HTTP transport, using only the local ASGI app."""
    headers = {"Authorization": "Bearer " + TOKEN, "X-Agents-Workspace": workspace_id}
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), headers=headers) as http:
        async with streamable_http_client("http://127.0.0.1:8765/mcp", http_client=http) as (read, write, _):
            async with ClientSession(read, write, client_info=Implementation(name=name, version="1")) as session:
                await session.initialize()
                yield session


@pytest.mark.asyncio
async def test_independent_mcp_clients_request_reply_ack_and_private_inboxes(checkouts, monkeypatch, caplog):
    """Two initialized MCP SDK clients exchange real durable private messages."""
    main, external, directory = checkouts
    monkeypatch.setenv("AGENTS_COOPERATION_ENABLED", "1")
    monkeypatch.delenv("AGENTS_COOPERATION_RETENTION_DAYS", raising=False)
    app = create_app(directory, TOKEN, runtime_loader=runtime)
    access = app.state.service.registry.register(main)
    app.state.service.registry.register(main, physical_root=external)
    async with app.router.lifespan_context(app):
        await ready(app)
        async with independent_mcp_client(app, access, "independent-a") as client_a:
            async with independent_mcp_client(app, access, "independent-b") as client_b:
                discovered = {tool.name: tool for tool in (await client_a.list_tools()).tools}
                assert set(NAMES) <= discovered.keys()
                assert discovered["cooperation_receive"].annotations.readOnlyHint is False
                assert discovered["cooperation_receive"].annotations.destructiveHint is False
                assert discovered["cooperation_receive"].annotations.idempotentHint is True
                assert discovered["cooperation_send"].annotations.idempotentHint is True
                assert discovered["cooperation_ack"].annotations.idempotentHint is True

                async def invoke(client, name, request, checkout):
                    result = await client.call_tool(name, {"request": request, "workspace": str(checkout)})
                    assert not result.isError, result
                    response = result.structuredContent
                    assert response == json.loads(result.content[0].text)
                    jsonschema.validate(response, discovered[name].outputSchema)
                    return response

                first = await invoke(client_a, "cooperation_register", {
                    **registration(main), "root_task_id": "root-a", "task_id": "independent-a",
                    "execution_mode": "independent",
                }, main)
                second = await invoke(client_b, "cooperation_register", {
                    **registration(external), "root_task_id": "root-b", "task_id": "independent-b",
                    "execution_mode": "independent",
                }, external)
                assert first["status"] == second["status"] == "ok"
                assert first["communication_mode"] == second["communication_mode"] == "mcp"
                assert first["repository_id"] == second["repository_id"]
                assert first["participant_id"] != second["participant_id"]
                assert first["checkout_id"] != second["checkout_id"]
                auth_a = {"protocol": PROTOCOL, "repository_id": first["repository_id"],
                          "participant_id": first["participant_id"], "capability": first["capability"]}
                auth_b = {"protocol": PROTOCOL, "repository_id": second["repository_id"],
                          "participant_id": second["participant_id"], "capability": second["capability"]}
                largest_cursor = await invoke(client_a, "cooperation_receive", {
                    **auth_a, "after_cursor": 2**63 - 1,
                }, main)
                assert largest_cursor["status"] == "ok" and largest_cursor["messages"] == []
                invalid_cursor = await invoke(client_a, "cooperation_receive", {
                    **auth_a, "after_cursor": 2**63,
                }, main)
                assert invalid_cursor["error"] == "invalid_request"
                outbound = {**auth_a, "operation_id": "send-request", "recipient_id": second["participant_id"],
                            "kind": "request", "body": "PRIVATE_REQUEST_BODY", "correlation_id": "discussion-1"}
                waiting = asyncio.create_task(invoke(client_b, "cooperation_wait", {
                    **auth_b, "timeout_seconds": 2,
                }, external))
                sent = await invoke(client_a, "cooperation_send", outbound, main)
                assert sent["status"] == "ok", sent
                message = sent["message"]
                event_page = await asyncio.wait_for(waiting, 3)
                assert event_page["events"][0]["kind"] == "message_received"
                assert event_page["events"][0]["message_id"] == message["message_id"]
                assert "PRIVATE_REQUEST_BODY" not in json.dumps(event_page)
                request_status = await invoke(client_a, "cooperation_request_status", {
                    **auth_a, "message_id": message["message_id"],
                }, main)
                assert request_status["request"]["state"] == "pending"
                assert request_status["request"]["delivery"] == "stored"
                accepted_update = {**auth_b, "operation_id": "accept-request", "message_id": message["message_id"],
                                   "expected_revision": 1, "state": "accepted"}
                accepted = await invoke(client_b, "cooperation_request_update", accepted_update, external)
                assert accepted["request"]["state"] == "accepted"
                assert await invoke(client_b, "cooperation_request_update", accepted_update, external) == accepted
                assert message["sender_id"] == message["actor_id"] == first["participant_id"]
                assert message["acknowledged_at"] is None and message["acknowledged_by"] is None
                assert await invoke(client_a, "cooperation_send", outbound, main) == sent
                conflict = await invoke(client_a, "cooperation_send", {**outbound, "body": "changed"}, main)
                assert conflict["error"] == "idempotency_conflict"
                assert (await invoke(client_a, "cooperation_receive", auth_a, main))["messages"] == []
                inbox = await invoke(client_b, "cooperation_receive", auth_b, external)
                assert inbox["messages"] == [message] and not inbox["has_more"]
                assert await invoke(client_b, "cooperation_receive", auth_b, external) == inbox

                forged = await invoke(client_b, "cooperation_receive", {
                    **auth_b, "capability": first["capability"],
                }, external)
                assert forged["error"] == "unauthorized_actor"
                sender_ack = await invoke(client_a, "cooperation_ack", {
                    **auth_a, "operation_id": "forbidden-ack", "message_ids": [message["message_id"]],
                }, main)
                assert sender_ack["error"] == "scope_denied"
                reply = await invoke(client_b, "cooperation_send", {
                    **auth_b, "operation_id": "send-response", "recipient_id": first["participant_id"],
                    "kind": "response", "body": "PRIVATE_RESPONSE_BODY", "reply_to": message["message_id"],
                    "correlation_id": "discussion-1",
                }, external)
                assert reply["status"] == "ok", reply
                response_inbox = await invoke(client_a, "cooperation_receive", auth_a, main)
                assert response_inbox["messages"] == [reply["message"]]
                assert reply["message"]["reply_to"] == message["message_id"]
                completed = await invoke(client_b, "cooperation_request_update", {
                    **auth_b, "operation_id": "complete-request", "message_id": message["message_id"],
                    "expected_revision": 2, "state": "completed", "response_message_id": reply["message"]["message_id"],
                }, external)
                assert completed["request"]["state"] == "completed"
                assert completed["request"]["delivery"] == "stored"

                for client, auth, checkout, delivered in (
                    (client_b, auth_b, external, message), (client_a, auth_a, main, reply["message"]),
                ):
                    acknowledge = {**auth, "operation_id": "ack-delivery", "message_ids": [delivered["message_id"]]}
                    acked = await invoke(client, "cooperation_ack", acknowledge, checkout)
                    assert acked["status"] == "ok", acked
                    assert acked["message_ids"] == [delivered["message_id"]]
                    assert await invoke(client, "cooperation_ack", acknowledge, checkout) == acked
                    assert (await invoke(client, "cooperation_receive", auth, checkout))["messages"] == []
                    history = await invoke(client, "cooperation_receive", {
                        **auth, "include_acknowledged": True,
                    }, checkout)
                    assert history["messages"][0]["acknowledged_at"] == acked["acknowledged_at"]
                    assert history["messages"][0]["acknowledged_by"] == auth["participant_id"]

                final_status = await invoke(client_a, "cooperation_request_status", {
                    **auth_a, "message_id": message["message_id"],
                }, main)
                assert final_status["request"]["state"] == "completed"
                assert final_status["request"]["delivery"] == "acknowledged"

                public = await invoke(client_a, "cooperation_read", {
                    "protocol": PROTOCOL, "repository_id": first["repository_id"],
                }, main)
                assert public["revision"] == second["revision"]
                assert public["events"] == [] and public["claims"] == [] and public["agreements"] == []
                serialized = json.dumps(public) + json.dumps(forged) + json.dumps(sender_ack) + caplog.text
                for private in (first["capability"], second["capability"], "PRIVATE_REQUEST_BODY", "PRIVATE_RESPONSE_BODY"):
                    assert private not in serialized
                assert not (main / "file.py").exists() and not (external / "file.py").exists()

        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765") as http:
            denied = await http.post("/mcp", headers={"Authorization": "Bearer wrong-token"}, json={
                "jsonrpc": "2.0", "id": 1, "method": "tools/call",
                "params": {"name": "cooperation_receive", "arguments": {"request": auth_b, "workspace": str(external)}},
            })
            assert denied.status_code == 401
            assert "PRIVATE_REQUEST_BODY" not in denied.text and second["capability"] not in denied.text
