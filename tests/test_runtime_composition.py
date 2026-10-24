"""Exercise explicit composition without a model, sync settings or Git remote."""
import asyncio
from types import SimpleNamespace

import httpx
from mcp.server.fastmcp import FastMCP
import pytest
from starlette.responses import JSONResponse, PlainTextResponse

from src.daemon.runtime import DaemonComponents
from src.stdio_runtime import run_stdio


@pytest.mark.parametrize("failure", [None, "startup", "transport"])
def test_stdio_joins_and_drains_after_composed_startup(failure):
    events = []

    def startup(readiness):
        events.append("startup")
        if failure == "startup":
            raise RuntimeError("startup failed")

    def serve():
        events.append("serve")
        if failure == "transport":
            raise RuntimeError("transport failed")

    readiness = SimpleNamespace(start=lambda: events.append("warmup"),
                                join=lambda seconds: events.append(("join", seconds)))
    options = dict(startup=startup, drain_logs=lambda: events.append("drain"), shutdown_wait=3.0)
    if failure:
        with pytest.raises(RuntimeError, match=failure + " failed"):
            run_stdio(SimpleNamespace(run=serve), readiness, **options)
    else:
        run_stdio(SimpleNamespace(run=serve), readiness, **options)
    assert events == ["warmup", "startup", *([] if failure == "startup" else ["serve"]),
                      ("join", 3.0), "drain"]


@pytest.mark.parametrize("hook_failure", [False, True])
def test_daemon_installs_the_supplied_history_hook_after_readiness(monkeypatch, hook_failure):
    import src
    from src import personal_runtime
    from src.daemon.app import load_runtime
    from src.engine import fingerprint, persona, readiness

    events = []
    transport = SimpleNamespace(settings=SimpleNamespace(), remove_tool=lambda name: None)
    server = SimpleNamespace(mcp=transport)
    monkeypatch.setattr(src, "server", server, raising=False)
    monkeypatch.setattr(readiness, "run_blocking", lambda: events.append("ready"))
    monkeypatch.setattr(fingerprint, "configuration_revision", lambda: None)
    monkeypatch.setattr(persona, "configure_ui_port", lambda port: None)

    def unexpected_personal():
        pytest.fail("Injected history hook must not install Personal sync as well")

    monkeypatch.setattr(personal_runtime, "install_history_sync", unexpected_personal)

    def install_history():
        assert vars(transport.settings) == {"host": "127.0.0.1", "port": 8765,
                                             "stateless_http": True, "json_response": True}
        events.append("history")
        if hook_failure:
            raise RuntimeError("composed history unavailable")

    if hook_failure:
        with pytest.raises(RuntimeError, match="composed history unavailable"):
            load_runtime(8765, history_sync_installer=install_history)
    else:
        assert load_runtime(8765, history_sync_installer=install_history) == (transport, server)
    assert events == ["ready", "history"]


@pytest.mark.asyncio
async def test_daemon_uses_supplied_components_through_the_full_lifecycle(tmp_path, monkeypatch):
    from src import personal_runtime
    from src.daemon.app import create_app

    monkeypatch.setenv("AGENTS_USER_FLOWS_DIR", str(tmp_path / "library"))
    monkeypatch.delenv("AGENTS_COOPERATION_ENABLED", raising=False)

    def unexpected_personal(*args, **kwargs):
        pytest.fail("Injected composition must not construct another Personal sync owner")

    monkeypatch.setattr(personal_runtime, "daemon_components", unexpected_personal)
    calls = []
    events = []
    ready = asyncio.Event()

    class Editor:
        async def __call__(self, scope, receive, send):
            await PlainTextResponse("composed editor")(scope, receive, send)

        def issue_code(self):
            return "test-code"

    class Sync:
        pending = 0

        def __init__(self, owner):
            self.owner = owner

        def start(self): events.append(("start", self.owner.state))
        def drain(self): events.append(("drain", self.owner.state))
        def resume(self): events.append(("resume", self.owner.state))
        async def finish(self): events.append(("finish", self.owner.state))
        def summary(self): return {"state": "composed"}

        def wake(self):
            events.append(("wake", self.owner.state))
            if self.owner.state == "ready":
                ready.set()

        async def handle(self, request, command):
            events.append(("handle", command))
            return JSONResponse({"command": command})

    def factory(service, *, hand_off_schedule):
        calls.append((service, hand_off_schedule))
        return DaemonComponents(Editor(), Sync(service))

    def loader(port):
        return FastMCP("composed", host="127.0.0.1", port=port,
                       stateless_http=True, json_response=True), None

    application = create_app(tmp_path / "service", "x" * 48,
                             hand_off_schedule=True, components_factory=factory, runtime_loader=loader)
    service = application.state.service
    assert calls == [(service, True)]

    async def wait_for_stop():
        await service.stop.wait()

    monkeypatch.setattr(service, "retain_diagnostics", wait_for_stop)
    monkeypatch.setattr(service, "monitor_loop", wait_for_stop)
    async with application.router.lifespan_context(application):
        await asyncio.wait_for(ready.wait(), timeout=5)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(application),
                                     base_url="http://127.0.0.1",
                                     headers={"Authorization": "Bearer " + "x" * 48}) as client:
            assert (await client.get("/health")).json()["user_sync"] == {"state": "composed"}
            assert (await client.get("/ui")).text == "composed editor"
            assert (await client.post("/admin/ui/code")).json()["code"] == "test-code"
            assert (await client.post("/admin/user-sync/pause")).json() == {"command": "pause"}
            assert (await client.post("/admin/drain")).json()["state"] == "draining"
            assert (await client.post("/admin/resume")).json()["state"] == "ready"
    assert calls == [(service, True)]
    assert events == [("start", "starting"), ("wake", "ready"), ("handle", "pause"),
                      ("drain", "draining"), ("resume", "ready"), ("drain", "draining"),
                      ("wake", "draining"), ("finish", "draining")]

@pytest.mark.asyncio
async def test_daemon_sync_start_failure_closes_owned_resources(tmp_path, monkeypatch):
    from src.daemon import app as app_module

    monkeypatch.setenv("AGENTS_USER_FLOWS_DIR", str(tmp_path / "library"))
    monkeypatch.delenv("AGENTS_COOPERATION_ENABLED", raising=False)
    events = []
    failure = RuntimeError("injected sync could not start")
    real_executor = app_module.TrackedExecutor

    class ObservedExecutor(real_executor):
        def shutdown(self, **options):
            events.append("executor.close")
            return super().shutdown(**options)

    monkeypatch.setattr(app_module, "TrackedExecutor", ObservedExecutor)
    monkeypatch.setattr(app_module, "agents_core_version", lambda: "test")
    monkeypatch.setattr(app_module, "agents_core_branch", lambda: "test")
    monkeypatch.setattr(app_module, "agents_core_commit", lambda: "test")

    class Sync:
        pending = 0

        def start(self):
            events.append("sync.start")
            raise failure

        def drain(self):
            events.append("sync.drain")

        def wake(self):
            events.append("sync.wake")

        async def finish(self):
            events.append("sync.finish")

    class Inference:
        def begin_drain(self):
            events.append("inference.drain")

        async def close(self):
            events.append("inference.close")

    class Artifacts:
        async def close(self):
            events.append("artifacts.close")

    def components(service, *, hand_off_schedule):
        return DaemonComponents(SimpleNamespace(), Sync())

    application = app_module.create_app(
        tmp_path / "service", "x" * 48, components_factory=components,
        local_inference_service=Inference(), artifact_service=Artifacts())
    service = application.state.service

    async def unexpected_background():
        events.append("background.started")
        await service.stop.wait()

    async def close_proxy():
        events.append("proxy.close")

    monkeypatch.setattr(service, "start_runtime", unexpected_background)
    monkeypatch.setattr(service, "retain_diagnostics", unexpected_background)
    monkeypatch.setattr(service, "monitor_loop", unexpected_background)
    monkeypatch.setattr(service, "close_streams", lambda: events.append("streams.close"))
    monkeypatch.setattr(service.mcp_proxy, "begin_drain", lambda: events.append("proxy.drain"))
    monkeypatch.setattr(service.mcp_proxy, "close", close_proxy)
    existing = asyncio.all_tasks()
    try:
        with pytest.raises(RuntimeError) as raised:
            async with application.router.lifespan_context(application):
                pytest.fail("A failed sync start must not yield a running application")
        assert raised.value is failure
        assert service.state == "draining"
        assert service.stop.is_set()
        assert events == [
            "sync.start", "streams.close", "sync.drain", "proxy.drain",
            "inference.drain", "inference.close", "artifacts.close",
            "proxy.close", "sync.wake", "sync.finish", "executor.close"]
    finally:
        # Keep the regression safe on the unfixed baseline too.
        service.stop.set()
        leaked = [task for task in asyncio.all_tasks() - existing
                  if getattr(task.get_coro(), "cr_code", None) is unexpected_background.__code__]
        for task in leaked:
            task.cancel()
        if leaked:
            await asyncio.gather(*leaked, return_exceptions=True)
        if service.io is not None and "executor.close" not in events:
            service.io.shutdown(wait=True, cancel_futures=False)
