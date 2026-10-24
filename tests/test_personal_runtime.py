"""Characterize Personal entry points with isolated startup boundaries.

The stdio checks execute the actual server file's __main__ path after the lease
bootstrap; lease behavior is covered separately in test_startup.py. No model,
GitHub, updater, user configuration or live daemon is used here.
"""
import asyncio
import atexit
import runpy
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

ROOT = Path(__file__).resolve().parents[1]
TOKEN = "x" * 48


@pytest.fixture
def stdio_startup(tmp_path, monkeypatch):
    import dotenv
    from src import self_update
    from src.engine import readiness, router
    from src.user_sync import history, trigger

    events, callbacks = [], []
    options = {"history_failure": False, "trigger_failure": False,
               "transport_failure": False, "finish_readiness": False}
    monkeypatch.setenv("AGENTS_USER_FLOWS_DIR", str(tmp_path / "library"))
    monkeypatch.setenv("AGENTS_AUTO_UPDATE", "0")
    monkeypatch.delenv("AGENTS_COOPERATION_ENABLED", raising=False)
    # Server imports still register the actual MCP tools, but the router and
    # dotenv boundary cannot load a model, store or installation configuration.
    monkeypatch.setattr(dotenv, "load_dotenv", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(router, "SemanticRouter", lambda: SimpleNamespace(available_agents=[]))
    monkeypatch.setattr(readiness, "start", lambda: events.append("readiness.start"))
    monkeypatch.setattr(readiness, "when_done", callbacks.append)
    monkeypatch.setattr(readiness, "join", lambda timeout: events.append(("readiness.join", timeout)))

    def install_history():
        events.append("history.install")
        if options["history_failure"]:
            raise RuntimeError("isolated history failure")

    def start_trigger():
        events.append("library.start")
        if options["trigger_failure"]:
            raise RuntimeError("isolated trigger failure")

    monkeypatch.setattr(history, "install", install_history)
    monkeypatch.setattr(trigger, "start_for_stdio", start_trigger)
    monkeypatch.setattr(self_update, "log_last_update", lambda: events.append("update.log"))
    monkeypatch.setattr(self_update, "start_background_update", lambda: events.append("update.start"))

    def register(callback):
        # Observe the existing shutdown boundary without waiting for real sinks
        # or leaving this isolated server's callbacks registered in pytest.
        if callback.__name__ == "drain_pending_logs":
            callback.__globals__["drain_pending_logs"] = lambda: events.append("logs.drain")
        return callback

    def serve(_mcp, *_args, **_kwargs):
        events.append("transport.run")
        if options["finish_readiness"]:
            for callback in callbacks:
                callback()
        if options["transport_failure"]:
            raise RuntimeError("isolated transport failure")

    monkeypatch.setattr(atexit, "register", register)
    monkeypatch.setattr(FastMCP, "run", serve)
    # server.py appends its source parent when it runs as a script.
    import sys
    monkeypatch.setattr(sys, "path", list(sys.path))

    def run(*, imported=False):
        return runpy.run_path(str(ROOT / "src" / "server.py"),
                              run_name="personal_import_check" if imported else "__main__",
                              init_globals={"_agents_bootstrapped": True})

    return SimpleNamespace(run=run, events=events, callbacks=callbacks, options=options)


def test_stdio_serves_before_deferred_personal_work(stdio_startup):
    startup = stdio_startup
    startup.run()
    assert startup.events == ["readiness.start", "history.install", "update.log", "transport.run",
                              ("readiness.join", 5.0), "logs.drain"]
    assert len(startup.callbacks) == 2
    # when_done runs after warmup completes, including its failure case. Neither
    # callback gates the transport's initial handshake on a completed warmup.
    for callback in startup.callbacks:
        callback()
    assert startup.events[-2:] == ["update.start", "library.start"]


def test_importing_server_does_not_start_personal_work(stdio_startup):
    stdio_startup.run(imported=True)
    assert stdio_startup.events == []
    assert stdio_startup.callbacks == []


def test_personal_sync_failures_leave_stdio_usable(stdio_startup, caplog):
    startup = stdio_startup
    startup.options.update(history_failure=True, trigger_failure=True, finish_readiness=True)
    startup.run()
    assert startup.events == ["readiness.start", "history.install", "update.log", "transport.run",
                              "update.start", "library.start", ("readiness.join", 5.0), "logs.drain"]
    assert "History sync between machines is unavailable" in caplog.text
    assert "Library sync trigger did not start" in caplog.text


def test_stdio_transport_failure_still_joins_before_log_drain(stdio_startup):
    stdio_startup.options["transport_failure"] = True
    with pytest.raises(RuntimeError, match="isolated transport failure"):
        stdio_startup.run()
    assert stdio_startup.events[-2:] == [("readiness.join", 5.0), "logs.drain"]


def test_daemon_runtime_installs_history_only_after_strict_readiness(monkeypatch):
    import src
    from src.daemon import app
    from src.engine import fingerprint, persona, readiness
    from src.user_sync import history

    events = []
    mcp = SimpleNamespace(settings=SimpleNamespace(), remove_tool=lambda name: events.append(("remove", name)))
    server = SimpleNamespace(mcp=mcp)
    monkeypatch.setattr(src, "server", server, raising=False)
    monkeypatch.setattr(readiness, "run_blocking", lambda: events.append("readiness"))
    monkeypatch.setattr(fingerprint, "configuration_revision", lambda: events.append("revision"))
    monkeypatch.setattr(persona, "configure_ui_port", lambda port: events.append(("ui.port", port)))
    monkeypatch.setattr(history, "install", lambda: events.append("history.install"))
    # The compatibility helper also lets this characterization run on the
    # pre-extraction entry point without changing its observable behavior.
    server.install_history_sync = lambda: history.install()

    assert app.load_runtime(9876) == (mcp, server)
    assert events == ["readiness", "revision", ("ui.port", 9876),
                      ("remove", "clear_session_cache"), "history.install"]
    assert vars(mcp.settings) == {"host": "127.0.0.1", "port": 9876,
                                  "stateless_http": True, "json_response": True}

    def fail_readiness():
        raise RuntimeError("isolated strict readiness failure")

    events.clear()
    monkeypatch.setattr(readiness, "run_blocking", fail_readiness)
    with pytest.raises(RuntimeError, match="isolated strict readiness failure"):
        app.load_runtime(9876)
    assert events == []


@pytest.mark.parametrize("hand_off", [False, True])
def test_daemon_keeps_the_personal_default_without_starting_it(tmp_path, monkeypatch, hand_off):
    from src.daemon.app import create_app
    from src.daemon.flows_ui import FlowsUI
    from src.daemon.sync_loop import UserSync

    monkeypatch.setenv("AGENTS_USER_FLOWS_DIR", str(tmp_path / "library"))
    options = {"hand_off_schedule": True} if hand_off else {}
    service = create_app(tmp_path / "service", TOKEN, **options).state.service
    assert isinstance(service.flows_ui, FlowsUI)
    assert isinstance(service.user_sync, UserSync)
    assert service.flows_ui.service is service.user_sync.service is service
    assert service.user_sync.hand_off_schedule is hand_off
    assert service.user_sync.task is None
    assert service.user_sync._syncer is None


@pytest.mark.asyncio
async def test_daemon_lifecycle_keeps_personal_start_drain_resume_and_finish(tmp_path, monkeypatch):
    from src.daemon import app as app_module

    monkeypatch.setenv("AGENTS_USER_FLOWS_DIR", str(tmp_path / "library"))
    monkeypatch.delenv("AGENTS_COOPERATION_ENABLED", raising=False)
    events = []

    def loader(port):
        return FastMCP("isolated", host="127.0.0.1", port=port,
                       stateless_http=True, json_response=True), None

    application = app_module.create_app(tmp_path / "service", TOKEN, runtime_loader=loader)
    service = application.state.service

    class SyncLifecycle:
        pending = 0

        def start(self): events.append(("start", service.state))
        def wake(self): events.append(("wake", service.state))
        def drain(self): events.append(("drain", service.state))
        def resume(self): events.append(("resume", service.state))
        def summary(self): return {"state": "off"}
        async def finish(self): events.append(("finish", service.state))

    service.user_sync = SyncLifecycle()

    async def wait_for_stop():
        await service.stop.wait()

    # No diagnostics retention or event-loop timing is needed for this lifecycle.
    monkeypatch.setattr(service, "retain_diagnostics", wait_for_stop)
    monkeypatch.setattr(service, "monitor_loop", wait_for_stop)
    async with application.router.lifespan_context(application):
        async with asyncio.timeout(5):
            while service.state == "starting":
                await asyncio.sleep(.001)
        assert service.state == "ready"
        async with httpx.AsyncClient(transport=httpx.ASGITransport(application),
                                     base_url="http://127.0.0.1:8765",
                                     headers={"Authorization": "Bearer " + TOKEN}) as http:
            assert (await http.post("/admin/drain")).json()["state"] == "draining"
            assert (await http.post("/admin/resume")).json()["state"] == "ready"
    assert events == [("start", "starting"), ("wake", "ready"), ("drain", "draining"),
                      ("resume", "ready"), ("drain", "draining"), ("wake", "draining"),
                      ("finish", "draining")]
