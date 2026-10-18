"""The real MCP HTTP client, driven by an in-memory HTTP transport; no service/model."""
import asyncio
from contextlib import asynccontextmanager
from copy import deepcopy
import json
import threading

import httpx
import pytest
import pytest_asyncio

from src.daemon.mcp_proxy import upstreams
from src.daemon.mcp_proxy.upstreams import Supervisor, UpstreamError


class Registry:
    def __init__(self):
        self.definitions = {name: {
            "alias": name, "revision": "rev-1", "transport": {"type": "http", "url": f"https://{name}.example/mcp", "headers": {}},
            "auth": {"type": "bearer"}, "secrets": [{"name": "Authorization", "target": "header", "required": True}],
            "tools": {"expose": "all", "deny": ["denied"], "always_load": []},
            "limits": {"call_timeout_seconds": 2, "idle_seconds": 600},
        } for name in ("one", "two")}
        self.confirmed = {name: "rev-1" for name in self.definitions}

    def get(self, alias):
        return deepcopy(self.definitions[alias])

    def list(self):
        return [{**self.get(name), "enabled": True, "apps": ["codex"],
                 "confirmed": self.confirmed.get(name) == self.definitions[name]["revision"],
                 "status": "ready" if self.confirmed.get(name) == self.definitions[name]["revision"] else "unconfirmed"}
                for name in self.definitions]

    def resolved(self, app):
        return [self.get(name) for name in self.definitions
                if app == "codex" and self.confirmed.get(name) == self.definitions[name]["revision"]]


class Secrets:
    def __init__(self):
        self.values = {name: {"Authorization": f"Bearer private-{name}-credential"} for name in ("one", "two")}
        self.reads = 0

    def load(self, alias):
        self.reads += 1
        return self.values[alias].copy()


class Endpoint:
    def __init__(self):
        self.requests = []
        self.status = None
        self.invalid = None
        self.is_error = False
        self.output_schema = None
        self.forbid_catalog = False
        self.catalog_error = None
        self.echo = None
        self.block = False
        self.started, self.release = asyncio.Event(), asyncio.Event()
        self.catalogs = 0
        self.initialized = 0
        self.active, self.max_active = 0, 0

    async def handle(self, request):
        self.requests.append(request)
        if self.status:
            return httpx.Response(self.status, text="upstream refused")
        if request.method == "GET":
            return httpx.Response(405)
        if request.method == "DELETE":
            return httpx.Response(200)
        message = json.loads(request.content)
        method = message["method"]
        if self.invalid and method == "tools/list":
            return httpx.Response(200, headers={"Content-Type": "application/json"},
                                  json={"jsonrpc": "2.0", "id": message["id"], "result": self.invalid})
        if method.startswith("notifications/"):
            return httpx.Response(202)
        headers = {"Content-Type": "application/json", "mcp-session-id": "local-test-session"}
        if method == "initialize":
            self.initialized += 1
            result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "fake", "version": "1"}}
        elif method == "tools/list":
            assert not self.forbid_catalog, "implicit tools/list is forbidden"
            if self.catalog_error is not None:
                return httpx.Response(200, headers=headers, json={
                    "jsonrpc": "2.0", "id": message["id"], "error": self.catalog_error})
            self.catalogs += 1
            tool = {"name": "echo", "description": self.echo or "Echo input", "inputSchema": {"type": "object"}}
            if self.output_schema is not None:
                tool["outputSchema"] = self.output_schema
            result = {"tools": [tool], "nextCursor": "second"} if not message.get("params", {}).get("cursor") else {
                "tools": [{"name": "denied", "inputSchema": {"type": "object"}}]}
        elif method == "tools/call":
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            self.started.set()
            try:
                if self.block:
                    await self.release.wait()
                arguments = message["params"].get("arguments", {})
                if "rpc_error" in arguments:
                    return httpx.Response(200, headers=headers, json={
                        "jsonrpc": "2.0", "id": message["id"], "error": {
                            "code": arguments["rpc_error"], "message": "private-upstream-error"}})
                result = {"content": [
                    {"type": "text", "text": "answer"},
                    {"type": "image", "data": "AAAA", "mimeType": "image/png"},
                    {"type": "audio", "data": "AAAA", "mimeType": "audio/wav"},
                    {"type": "resource_link", "uri": "file:///fake.txt", "name": "fake"},
                    {"type": "resource", "resource": {"uri": "file:///inline.txt", "text": "inline"}},
                ], "structuredContent": {"value": [1, 2]}, "isError": self.is_error, "_meta": {"origin": "test"}}
                if arguments.get("invalid_output"):
                    result["structuredContent"] = {"value": "private-invalid-output"}
            finally:
                self.active -= 1
        else:
            raise AssertionError(method)
        return httpx.Response(200, headers=headers, json={"jsonrpc": "2.0", "id": message["id"], "result": result})


@pytest_asyncio.fixture
async def runtime(tmp_path, monkeypatch):
    registry, secrets = Registry(), Secrets()
    endpoints = {name: Endpoint() for name in registry.definitions}
    clients, contexts = [], []
    client_class = httpx.AsyncClient
    transport_factory = upstreams.streamable_http_client

    class Client(client_class):
        async def __aenter__(self):
            contexts.append(("enter", id(self), asyncio.current_task()))
            return await super().__aenter__()

        async def __aexit__(self, *args):
            contexts.append(("exit", id(self), asyncio.current_task()))
            return await super().__aexit__(*args)

    async def handle(request):
        return await endpoints[request.url.host.split(".")[0]].handle(request)

    def factory(**kwargs):
        assert kwargs["trust_env"] is False
        assert kwargs["follow_redirects"] is False
        client = Client(transport=httpx.MockTransport(handle), **kwargs)
        clients.append(client)
        return client

    @asynccontextmanager
    async def transport(*args, **kwargs):
        contexts.append(("transport-enter", id(kwargs["http_client"]), asyncio.current_task()))
        try:
            async with transport_factory(*args, **kwargs) as streams:
                yield streams
        finally:
            contexts.append(("transport-exit", id(kwargs["http_client"]), asyncio.current_task()))

    monkeypatch.setattr(upstreams.httpx, "AsyncClient", factory)
    monkeypatch.setattr(upstreams, "streamable_http_client", transport)
    supervisor = Supervisor(registry, secrets, tmp_path)
    yield supervisor, registry, secrets, endpoints, clients, contexts
    await supervisor.close()


@pytest.mark.asyncio
async def test_explicit_catalog_then_complete_result_and_same_task_context_exit(runtime):
    supervisor, registry, secrets, endpoints, clients, contexts = runtime
    assert supervisor.cached("codex") == []
    assert not clients and secrets.reads == 0
    endpoints["one"].is_error = True
    tools = await supervisor.connect("one", "codex")
    assert [tool.name for tool in tools] == ["echo", "denied"]
    assert [tool.name for _, tool in supervisor.cached("codex")] == ["echo"]
    result = await supervisor.call("one", "echo", {"input": "hello"}, "codex")
    assert [item.type for item in result.content] == ["text", "image", "audio", "resource_link", "resource"]
    assert result.structuredContent == {"value": [1, 2]}
    assert result.meta == {"origin": "test"} and result.isError is True
    assert all(request.headers["authorization"] == secrets.values["one"]["Authorization"] for request in endpoints["one"].requests)
    await supervisor.close()
    assert all(client.is_closed for client in clients)
    assert len({task for _, client, task in contexts if client == id(clients[0])}) == 1
    assert {kind for kind, _, _ in contexts} == {"enter", "exit", "transport-enter", "transport-exit"}


@pytest.mark.asyncio
async def test_cache_survives_restart_without_credentials_or_network(runtime, tmp_path):
    supervisor, registry, secrets, endpoints, clients, _ = runtime
    await supervisor.connect("one")
    await supervisor.close()
    count, reads = len(clients), secrets.reads
    restarted = Supervisor(registry, secrets, tmp_path)
    assert [tool.name for _, tool in restarted.cached("codex")] == ["echo"]
    assert len(clients) == count and secrets.reads == reads
    assert all(value not in restarted.catalog_path.read_text() for values in secrets.values.values() for value in values.values())
    registry.definitions["one"]["revision"] = "rev-2"
    assert restarted.cached("codex") == []
    with pytest.raises(UpstreamError, match="not_enabled"):
        await restarted.call("one", "echo", {}, "codex")
    registry.confirmed["one"] = "rev-2"
    with pytest.raises(UpstreamError, match="tool_not_cataloged"):
        await restarted.call("one", "echo", {}, "codex")
    assert len(clients) == count
    await restarted.close()


@pytest.mark.asyncio
async def test_revision_and_secret_change_restart_lazily(runtime):
    supervisor, registry, secrets, endpoints, clients, _ = runtime
    await supervisor.connect("one")
    registry.definitions["one"]["revision"] = "rev-2"
    registry.confirmed["one"] = "rev-2"
    assert supervisor.cached("codex") == []
    assert len(clients) == 1
    await supervisor.connect("one")
    assert len(clients) == 2 and clients[0].is_closed
    secrets.values["one"]["Authorization"] = "Bearer rotated-credential"
    await supervisor.call("one", "echo", {}, "codex")
    assert len(clients) == 3 and clients[1].is_closed
    assert endpoints["one"].initialized == 3


@pytest.mark.asyncio
async def test_idle_close_reopens_only_on_call_using_saved_catalog(runtime):
    supervisor, registry, secrets, endpoints, clients, _ = runtime
    registry.definitions["one"]["limits"]["idle_seconds"] = .02
    await supervisor.connect("one")
    await asyncio.wait_for(supervisor._owners["one"].task, .5)
    assert clients[0].is_closed
    assert [tool.name for _, tool in supervisor.cached("codex")] == ["echo"]
    assert len(clients) == 1
    await supervisor.call("one", "echo", {}, "codex")
    assert len(clients) == 2
    assert endpoints["one"].catalogs == 2  # two pages, no implicit re-catalog


@pytest.mark.asyncio
async def test_timeout_backoff_and_alias_isolation(runtime):
    supervisor, registry, secrets, endpoints, clients, _ = runtime
    registry.definitions["one"]["limits"]["call_timeout_seconds"] = .05
    await supervisor.connect("one")
    endpoints["one"].block = True
    with pytest.raises(UpstreamError, match="timeout"):
        await supervisor.call("one", "echo", {}, "codex")
    with pytest.raises(UpstreamError, match="backoff"):
        await supervisor.call("one", "echo", {}, "codex")
    assert clients[0].is_closed
    await supervisor.connect("two")
    assert (await supervisor.call("two", "echo", {}, "codex")).structuredContent == {"value": [1, 2]}
    assert supervisor.status()[0]["error"] == "timeout"


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [{"rpc_error": -32602}, {"invalid_output": True}])
async def test_call_sdk_errors_preserve_session_and_queued_work(runtime, arguments):
    supervisor, _, _, endpoints, clients, _ = runtime
    endpoint = endpoints["one"]
    endpoint.output_schema = {"type": "object", "properties": {"value": {"type": "array"}}}
    await supervisor.connect("one")
    owner = supervisor._owners["one"]
    endpoint.forbid_catalog = endpoint.block = True
    first = asyncio.create_task(supervisor.call("one", "echo", arguments, "codex"))
    await endpoint.started.wait()
    second = asyncio.create_task(supervisor.call("one", "echo", {}, "codex"))
    async with asyncio.timeout(1):
        while not owner.queue.qsize():
            await asyncio.sleep(0)
    endpoint.release.set()
    error, result = await asyncio.gather(first, second, return_exceptions=True)
    assert isinstance(error, UpstreamError) and error.code == "upstream_error"
    assert "private-" not in str(error)
    assert result.structuredContent == {"value": [1, 2]} and result.isError is False
    assert [item.type for item in result.content] == ["text", "image", "audio", "resource_link", "resource"]
    assert result.meta == {"origin": "test"}
    assert supervisor._owners["one"] is owner and owner.state == "connected" and not owner.active
    assert supervisor.health()["failed"] == 0 and supervisor.status()[0]["error"] is None
    assert (await supervisor.call("one", "echo", {}, "codex")).isError is False
    assert len(clients) == 1 and not clients[0].is_closed and endpoint.catalogs == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("code, category", [(-32000, "unavailable"), (408, "timeout")])
async def test_sdk_connection_and_timeout_errors_still_retire_session(runtime, code, category):
    supervisor, _, _, endpoints, clients, _ = runtime
    await supervisor.connect("one")
    endpoint, owner = endpoints["one"], supervisor._owners["one"]
    endpoint.block = True
    first = asyncio.create_task(supervisor.call("one", "echo", {"rpc_error": code}, "codex"))
    await endpoint.started.wait()
    second = asyncio.create_task(supervisor.call("one", "echo", {}, "codex"))
    async with asyncio.timeout(1):
        while not owner.queue.qsize():
            await asyncio.sleep(0)
    endpoint.release.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    assert all(isinstance(result, UpstreamError) and result.code == category for result in results)
    assert clients[0].is_closed and owner.state == "closed"
    with pytest.raises(UpstreamError, match="backoff"):
        await supervisor.call("one", "echo", {}, "codex")


@pytest.mark.asyncio
async def test_catalog_rpc_error_still_closes_session_and_backs_off(runtime):
    supervisor, _, _, endpoints, clients, _ = runtime
    endpoints["one"].catalog_error = {"code": -32602, "message": "private-catalog-error"}
    with pytest.raises(UpstreamError, match="unavailable"):
        await supervisor.connect("one")
    assert clients[0].is_closed and supervisor.health()["failed"] == 1
    assert not supervisor.catalog_path.exists()
    with pytest.raises(UpstreamError, match="backoff"):
        await supervisor.connect("one")


@pytest.mark.asyncio
async def test_async_paths_read_registry_and_persist_catalogs_off_loop(runtime, monkeypatch):
    supervisor, registry, _, _, _, _ = runtime
    loop_thread, calls = threading.get_ident(), []

    def checked(name, function):
        def wrapper(*args, **kwargs):
            assert threading.get_ident() != loop_thread, name
            calls.append(name)
            return function(*args, **kwargs)
        return wrapper

    for name in ("get", "list", "resolved"):
        monkeypatch.setattr(registry, name, checked(name, getattr(registry, name)))
    for name in ("_definition", "cached"):
        monkeypatch.setattr(supervisor, name, checked(name, getattr(supervisor, name)))
    for name in ("private_dir", "write_json"):
        monkeypatch.setattr(upstreams, name, checked(name, getattr(upstreams, name)))
    await supervisor.connect("one")
    await supervisor.connect("two", "codex")
    assert (await supervisor.call("one", "echo", {}, "codex")).isError is False
    assert calls.count("_definition") == 6 and calls.count("cached") == 1
    assert calls.count("private_dir") == calls.count("write_json") == 2
    assert {"get", "list", "resolved"} <= set(calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["connect", "call"])
@pytest.mark.parametrize("change, category", [("revision", "revision_changed"), ("approval", "not_enabled")])
async def test_off_loop_paths_recheck_revision_and_app_approval(runtime, monkeypatch, operation, change, category):
    supervisor, registry, _, endpoints, _, _ = runtime
    if operation == "call":
        await supervisor.connect("one")
    original = supervisor._owner

    async def changed(definition):
        result = await original(definition)
        if change == "revision":
            registry.definitions["one"]["revision"] = registry.confirmed["one"] = "rev-2"
        else:
            registry.confirmed.pop("one")
        return result

    monkeypatch.setattr(supervisor, "_owner", changed)
    with pytest.raises(UpstreamError, match=category):
        if operation == "connect":
            await supervisor.connect("one", "codex")
        else:
            await supervisor.call("one", "echo", {}, "codex")
    assert not any(json.loads(request.content).get("method") == "tools/call"
                   for request in endpoints["one"].requests if request.method == "POST")
    assert supervisor.cached("codex") == []


@pytest.mark.asyncio
async def test_catalog_write_failure_preserves_confirmed_cache(runtime, monkeypatch):
    supervisor, _, _, _, _, _ = runtime
    await supervisor.connect("one")
    previous, stored = deepcopy(supervisor._catalogs), supervisor.catalog_path.read_bytes()

    def failed_write(*args):
        raise OSError("private-write-error")

    monkeypatch.setattr(upstreams, "write_json", failed_write)
    for alias in ("one", "two"):
        with pytest.raises(UpstreamError, match="catalog_write_failed"):
            await supervisor.connect(alias)
        assert supervisor._catalogs == previous and supervisor.catalog_path.read_bytes() == stored
        assert not supervisor._owners[alias].accepting


@pytest.mark.asyncio
async def test_cancelled_catalog_writer_stays_serial_and_drain_waits_for_it(runtime, monkeypatch):
    supervisor, _, _, _, _, _ = runtime
    started, release = asyncio.Event(), threading.Event()
    loop, original_write = asyncio.get_running_loop(), upstreams.write_json
    snapshots = []

    def blocked_write(path, value):
        snapshots.append(set(value["catalogs"]))
        if len(snapshots) == 1:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(2), "test did not release background catalog writer"
        original_write(path, value)

    monkeypatch.setattr(upstreams, "write_json", blocked_write)
    first = asyncio.create_task(supervisor.connect("one"))
    second = close = None
    try:
        await asyncio.wait_for(started.wait(), 1)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        second = asyncio.create_task(supervisor.connect("two"))
        async with asyncio.timeout(1):
            while len(supervisor._catalog_writes) < 2:
                await asyncio.sleep(0)
        assert supervisor._catalogs == {} and snapshots == [{"one"}]
        close = asyncio.create_task(supervisor.close())
        await asyncio.sleep(0)
        assert not close.done()
    finally:
        release.set()
        await asyncio.gather(first, *(task for task in (second, close) if task is not None), return_exceptions=True)
    assert snapshots == [{"one"}, {"one", "two"}]
    assert set(supervisor._catalogs) == {"one", "two"}
    assert set(json.loads(supervisor.catalog_path.read_text())["catalogs"]) == {"one", "two"}


@pytest.mark.asyncio
async def test_drain_waits_for_catalog_confirmation_still_reading_registry(runtime, monkeypatch):
    supervisor, _, _, _, clients, _ = runtime
    started, release = asyncio.Event(), threading.Event()
    loop, original = asyncio.get_running_loop(), supervisor._definition
    reads = 0

    def blocked_definition(*args):
        nonlocal reads
        result = original(*args)
        reads += 1
        if reads == 2:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(2), "test did not release background registry read"
        return result

    monkeypatch.setattr(supervisor, "_definition", blocked_definition)
    connect = asyncio.create_task(supervisor.connect("one"))
    close = None
    try:
        await asyncio.wait_for(started.wait(), 1)
        close = asyncio.create_task(supervisor.close())
        async with asyncio.timeout(1):
            while not clients[0].is_closed:
                await asyncio.sleep(0)
        assert not close.done() and not supervisor.catalog_path.exists()
    finally:
        release.set()
        results = await asyncio.gather(connect, *([close] if close else []), return_exceptions=True)
    assert not any(isinstance(result, BaseException) for result in results)
    assert set(json.loads(supervisor.catalog_path.read_text())["catalogs"]) == {"one"}


@pytest.mark.asyncio
async def test_serial_calls_and_drain_wait_for_accepted_work(runtime):
    supervisor, _, _, endpoints, clients, _ = runtime
    await supervisor.connect("one")
    endpoints["one"].block = True
    first = asyncio.create_task(supervisor.call("one", "echo", {"call": 1}, "codex"))
    await endpoints["one"].started.wait()
    second = asyncio.create_task(supervisor.call("one", "echo", {"call": 2}, "codex"))
    # _owner loads injected secrets in a thread; wait until the second call is queued.
    while supervisor._owners["one"].queue.qsize() == 0:
        await asyncio.sleep(.001)
    supervisor.begin_drain()
    close = asyncio.create_task(supervisor.close())
    with pytest.raises(UpstreamError, match="draining"):
        await supervisor.call("one", "echo", {}, "codex")
    assert not close.done() and not clients[0].is_closed
    endpoints["one"].release.set()
    await asyncio.gather(first, second, close)
    assert endpoints["one"].max_active == 1
    assert clients[0].is_closed


@pytest.mark.asyncio
async def test_caller_cancellation_does_not_break_session_owner(runtime):
    supervisor, _, _, endpoints, clients, _ = runtime
    await supervisor.connect("one")
    endpoints["one"].block = True
    call = asyncio.create_task(supervisor.call("one", "echo", {}, "codex"))
    await endpoints["one"].started.wait()
    call.cancel()
    with pytest.raises(asyncio.CancelledError):
        await call
    endpoints["one"].release.set()
    assert (await supervisor.call("one", "echo", {}, "codex")).isError is False
    assert len(clients) == 1


@pytest.mark.asyncio
async def test_catalog_credential_echo_is_refused_even_when_json_escaped(runtime):
    supervisor, _, secrets, endpoints, _, _ = runtime
    secret = 'Bearer sensitive-"escaped"-token'
    secrets.values["one"]["Authorization"] = secret
    endpoints["one"].echo = secret[7:]
    with pytest.raises(UpstreamError, match="catalog_contains_credentials"):
        await supervisor.connect("one")
    assert not supervisor.catalog_path.exists()
    assert supervisor.cached("codex") == []
    assert secret not in json.dumps(supervisor.status())


@pytest.mark.asyncio
async def test_authentication_failure_is_safe_and_does_not_connect_other_alias(runtime, caplog):
    supervisor, _, secrets, endpoints, clients, _ = runtime
    endpoints["one"].status = 401
    with pytest.raises(UpstreamError) as error:
        await supervisor.connect("one")
    assert error.value.code in ("authentication_failed", "unavailable")
    assert not supervisor.catalog_path.exists()
    assert endpoints["two"].requests == []
    for value in secrets.values["one"].values():
        assert value not in str(error.value) + json.dumps(supervisor.status()) + caplog.text
    assert clients[0].is_closed


@pytest.mark.asyncio
async def test_exposure_enablement_missing_credentials_and_health_are_local(runtime):
    supervisor, registry, secrets, endpoints, clients, _ = runtime
    with pytest.raises(UpstreamError, match="app_required"):
        await supervisor.call("one", "echo", {}, None)
    with pytest.raises(UpstreamError, match="not_enabled"):
        await supervisor.connect("one", "other-app")
    secrets.values["one"] = {}
    with pytest.raises(UpstreamError, match="credentials_missing"):
        await supervisor.connect("one")
    assert not clients
    assert supervisor.health() == {"servers": 0, "ready": 0, "failed": 0}
    secrets.values["one"] = {"Authorization": "Bearer restored"}
    await supervisor.connect("one")
    with pytest.raises(UpstreamError, match="tool_not_cataloged"):
        await supervisor.call("one", "denied", {}, "codex")
    monkey_reads = secrets.reads
    registry.list = registry.resolved = lambda *args: (_ for _ in ()).throw(AssertionError("filesystem read"))
    assert supervisor.health() == {"servers": 1, "ready": 1, "failed": 0}
    assert secrets.reads == monkey_reads


@pytest.mark.asyncio
async def test_admin_connect_needs_confirmed_revision_before_network(runtime):
    supervisor, registry, secrets, endpoints, clients, _ = runtime
    registry.confirmed["one"] = "old-revision"
    with pytest.raises(UpstreamError, match="not_enabled"):
        await supervisor.connect("one")
    assert not clients and secrets.reads == 0


@pytest.mark.asyncio
async def test_public_server_skips_secret_store_and_removed_headers_stay_removed(runtime):
    supervisor, registry, secrets, endpoints, clients, _ = runtime
    registry.definitions["one"]["auth"] = {"type": "none"}
    registry.definitions["one"]["secrets"] = []
    await supervisor.connect("one")
    assert secrets.reads == 0
    assert all("authorization" not in request.headers for request in endpoints["one"].requests)
    secrets.values["two"]["X-Stale-Key"] = "removed-secret"
    await supervisor.connect("two")
    assert all("x-stale-key" not in request.headers for request in endpoints["two"].requests)


@pytest.mark.asyncio
async def test_status_tolerates_invalid_registry_record_without_echoing_its_error(runtime):
    supervisor, registry, _, _, _, _ = runtime
    registry.list = lambda: [{"alias": "broken", "status": "invalid", "error": "private-string"}]
    status = supervisor.status()
    assert status[0]["state"] == "invalid" and status[0]["revision"] is None
    assert "private-string" not in json.dumps(status)


@pytest.mark.asyncio
async def test_sdk_malformed_response_diagnostics_do_not_leak_credentials(runtime, caplog):
    supervisor, registry, secrets, endpoints, _, _ = runtime
    registry.definitions["one"]["limits"]["call_timeout_seconds"] = .05
    endpoints["one"].invalid = secrets.values["one"]["Authorization"]
    with pytest.raises(UpstreamError) as error:
        await supervisor.connect("one")
    assert error.value.code == "timeout"
    assert "private-one-credential" not in caplog.text
    assert "details withheld" in caplog.text
    assert supervisor.cached("codex") == []


@pytest.mark.asyncio
async def test_success_after_backoff_clears_failure_state(runtime, monkeypatch):
    from types import SimpleNamespace
    supervisor, registry, _, endpoints, clients, _ = runtime
    clock = [100.0]
    monkeypatch.setattr(upstreams, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    # Keep the ordinary session-start budget: this test exercises recovery,
    # and Windows scheduling can exceed 30 ms even for the in-memory server.
    await supervisor.connect("one")
    endpoints["one"].block = True
    with pytest.raises(UpstreamError, match="timeout"):
        await supervisor.call("one", "echo", {}, "codex")
    endpoints["one"].block = False
    clock[0] += 1.1
    await supervisor.call("one", "echo", {}, "codex")
    assert len(clients) == 2
    assert supervisor.health()["failed"] == 0
    assert supervisor.status()[0]["error"] is None


@pytest.mark.asyncio
async def test_cancelled_close_does_not_cancel_inflight_session_work(runtime):
    supervisor, _, _, endpoints, clients, _ = runtime
    await supervisor.connect("one")
    endpoints["one"].block = True
    call = asyncio.create_task(supervisor.call("one", "echo", {}, "codex"))
    await endpoints["one"].started.wait()
    close = asyncio.create_task(supervisor.close())
    await asyncio.sleep(0)
    close.cancel()
    with pytest.raises(asyncio.CancelledError):
        await close
    assert not clients[0].is_closed
    endpoints["one"].release.set()
    assert (await call).isError is False
    await supervisor.close()
    assert clients[0].is_closed


@pytest.mark.asyncio
async def test_malformed_disk_catalog_is_ignored_without_connecting(runtime, tmp_path):
    supervisor, registry, secrets, _, clients, _ = runtime
    supervisor.catalog_path.write_text(json.dumps({"version": 1, "catalogs": {
        "one": {"revision": "rev-1", "tools": None},
        "two": {"revision": "rev-1", "tools": [{"name": "invalid"}]},
    }}))
    restarted = Supervisor(registry, secrets, tmp_path)
    assert restarted.cached("codex") == []
    assert not clients and secrets.reads == 0
    assert all(item["catalog_confirmed"] is False for item in restarted.status())
    await restarted.close()


@pytest.mark.asyncio
async def test_successful_lazy_reopen_uses_confirmed_schema_without_listing(runtime, tmp_path):
    supervisor, registry, secrets, endpoints, clients, _ = runtime
    registry.definitions["one"]["limits"]["idle_seconds"] = .02
    endpoints["one"].output_schema = {"type": "object", "required": ["value"],
                                        "properties": {"value": {"type": "array"}}}
    await supervisor.connect("one")
    await asyncio.wait_for(supervisor._owners["one"].task, .5)
    endpoints["one"].forbid_catalog = True
    result = await supervisor.call("one", "echo", {}, "codex")
    assert result.isError is False and result.structuredContent == {"value": [1, 2]}
    await supervisor.close()
    restarted = Supervisor(registry, secrets, tmp_path)
    try:
        result = await restarted.call("one", "echo", {}, "codex")
        assert result.isError is False and result.structuredContent == {"value": [1, 2]}
        assert endpoints["one"].catalogs == 2
        assert len(clients) == 3
    finally:
        await restarted.close()


@pytest.mark.asyncio
async def test_resume_allows_lazy_new_work_after_drain(runtime):
    supervisor, _, secrets, endpoints, clients, _ = runtime
    await supervisor.connect("one")
    supervisor.begin_drain()
    await supervisor.close()
    clients_before, reads_before = len(clients), secrets.reads
    supervisor.resume()
    assert len(clients) == clients_before and secrets.reads == reads_before
    assert [tool.name for _, tool in supervisor.cached("codex")] == ["echo"]
    assert (await supervisor.call("one", "echo", {}, "codex")).isError is False
    assert len(clients) == clients_before + 1


@pytest.mark.asyncio
async def test_secret_header_names_match_case_insensitively(runtime):
    supervisor, registry, secrets, endpoints, _, _ = runtime
    secrets.values["one"] = {"authorization": "Bearer lower-case-credential"}
    await supervisor.connect("one")
    assert (await supervisor.call("one", "echo", {}, "codex")).isError is False
    assert all(request.headers["authorization"] == "Bearer lower-case-credential" for request in endpoints["one"].requests)
    assert "lower-case-credential" not in supervisor.catalog_path.read_text()
