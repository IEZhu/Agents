"""User sync in the daemon (#167): triggers, drain and admin endpoints with a fake runtime and engine.

No embedding model and no git: a fake runtime serves MCP, and ``FakeSyncer`` stands in for
``src.user_sync.engine.Syncer``. The overlap test uses the real engine's sync lock with its locked
part replaced, so it needs no repository either. Intervals are fractions of a second.
"""
import asyncio
from collections import namedtuple
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import json
import socket
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

from src import user_library
from src.daemon import control
from src.daemon.app import create_app
from src.daemon.state import write_json
from src.daemon.sync_loop import UserSync, library_fingerprint
from src.user_sync.engine import Settings, SyncError, Syncer

TOKEN = "x" * 48
HEADERS = {"Authorization": "Bearer " + TOKEN}
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaFakeKeyForTests agents-core-sync:mac-0001"
Run = namedtuple("Run", "time force confirm")


def fake_runtime(port):
    mcp = FastMCP("test", host="127.0.0.1", port=port, stateless_http=True, json_response=True)

    @mcp.tool()
    def route():
        return "ok"
    return mcp, None


class FakeSyncer:
    """What the daemon calls on the engine; records the calls and needs no git."""

    def __init__(self, library, *, started=True, paused=False, fetch_minutes=5):
        library.mkdir(parents=True, exist_ok=True)
        self.library = library.resolve()
        self.config = SimpleNamespace(started="2026-10-05T00:00:00+00:00" if started else None,
                                      paused=paused, fetch_minutes=fetch_minutes)
        self.set_up = True
        self.pending = 0
        self.runs, self.calls = [], []
        self.statuses = 0
        self.guard = threading.Lock()
        self.release = threading.Event()
        self.release.set()            # cleared: runs wait for it
        self.running = threading.Event()
        self.result = None            # what run() returns instead of a plain success
        self.errors = {}              # method name -> exception to raise

    def _fail(self, name):
        if name in self.errors:
            raise self.errors[name]

    def settings(self):
        return self.config if self.set_up else None

    def status(self):
        with self.guard:
            self.statuses += 1
        if not self.set_up:
            return {"state": "off", "reason": None, "message": "sync is not set up", "conflicts": 0}
        state = "paused" if self.config.paused else "waiting_for_access" if not self.config.started \
            else "pending" if self.pending else "synced"
        return {"state": state, "reason": None, "message": None, "pending": self.pending,
                "last_success": "2026-10-05T10:00:00+00:00", "conflicts": 2, "public_key": PUBLIC_KEY,
                "remote": "github.com/me/library", "identity": {"name": "Me", "email": "me@example.com"},
                "label": "mac-0001", "retry_at": None}

    def run(self, *, force=False, confirm=None):
        self.runs.append(Run(time.monotonic(), force, confirm))
        self.running.set()
        self.release.wait(30)
        self.running.clear()
        self._fail("run")
        self.pending = 0
        return self.result or {"status": "synced", "reason": None, "sent": ["common/a.md"], "received": [],
                               "conflicts": [], "pushed": True}

    def _record(self, method, /, **arguments):
        self.calls.append((method, arguments))
        self._fail(method)

    def setup(self, **arguments):
        self._record("setup", **arguments)
        return {"status": "waiting_for_access", "public_key": PUBLIC_KEY, "remote": "github.com/me/library"}

    def check(self):
        self._record("check")
        return {"status": "ok", "remote_state": "empty", "remote_head": None}

    def preview(self):
        self._record("preview")
        return {"hash": "abc123", "upload": [], "download": []}

    def start(self, confirm):
        self._record("start", confirm=confirm)
        self.config.started = "2026-10-05T11:00:00+00:00"
        return {"status": "synced", "sent": [], "received": [], "conflicts": [], "pushed": True}

    def pause(self):
        self._record("pause")
        self.config.paused = True
        return self.status()

    def resume(self):
        self._record("resume")
        self.config.paused = False
        return self.status()

    def disconnect(self):
        self._record("disconnect")
        self.set_up = False
        return {"status": "off", "message": "sync is off"}


def make_app(tmp_path, syncer=None, **options):
    directory = tmp_path / "service"
    directory.mkdir(exist_ok=True)
    app = create_app(directory, TOKEN, runtime_loader=fake_runtime)
    service = app.state.service
    options = {"debounce": .3, "scan_every": .2, "drain_budget": .5, "recheck": .1, "minute": 60.0, **options}
    service.user_sync = UserSync(service, syncer=syncer, **options)
    return app, service


async def until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached in time"
        await asyncio.sleep(.01)


@asynccontextmanager
async def running(app):
    """The service's lifespan with an HTTP client; ready once the fake runtime loaded."""
    async with app.router.lifespan_context(app):
        await until(lambda: app.state.service.state == "ready")
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765",
                                     headers=HEADERS) as http:
            yield http


async def settled(service, syncer, runs):
    """The task finished ``runs`` cycles and is idle."""
    await until(lambda: len(syncer.runs) == runs and not service.user_sync.syncing
                and not service.user_sync.calls)


def free_port():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


# --- triggers ---------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_first_change_of_a_burst_schedules_the_cycle(tmp_path):
    syncer = FakeSyncer(tmp_path / "library")
    app, service = make_app(tmp_path, syncer, debounce=.5)
    async with running(app):
        await settled(service, syncer, 1)  # the catch-up cycle once the service is ready
        assert syncer.runs[0].force is False
        first = time.monotonic()
        for _ in range(8):  # changes for 0.4 s: a debounce that each change extended would end at 0.85 s
            user_library.notify(syncer.library, ["common/a.md"])
            await asyncio.sleep(.05)
        await settled(service, syncer, 2)
        assert first + .45 <= syncer.runs[1].time < first + .8
        await asyncio.sleep(.8)
        assert len(syncer.runs) == 2  # nothing changed after the cycle read the library
        user_library.notify(tmp_path / "elsewhere", ["common/a.md"])  # another library is not ours
        await asyncio.sleep(.7)
        assert len(syncer.runs) == 2


@pytest.mark.asyncio
async def test_a_change_during_a_cycle_schedules_one_more(tmp_path):
    syncer = FakeSyncer(tmp_path / "library")
    app, service = make_app(tmp_path, syncer, debounce=.2)
    async with running(app):
        await settled(service, syncer, 1)
        syncer.release.clear()
        user_library.notify(syncer.library, ["common/a.md"])
        await until(syncer.running.is_set)
        assert service.health()["user_sync"]["state"] == "syncing"
        during = time.monotonic()
        for _ in range(3):
            user_library.notify(syncer.library, ["common/b.md"])
        syncer.release.set()
        await settled(service, syncer, 3)
        assert syncer.runs[2].time >= during + .2
        await asyncio.sleep(.6)
        assert len(syncer.runs) == 3


@pytest.mark.asyncio
async def test_scan_finds_writes_of_other_processes_without_reading_an_unchanged_library(tmp_path):
    syncer = FakeSyncer(tmp_path / "library")
    app, service = make_app(tmp_path, syncer, scan_every=.15)
    async with running(app):
        await settled(service, syncer, 1)
        statuses = syncer.statuses
        await asyncio.sleep(.6)  # four scans of an unchanged library
        assert syncer.statuses == statuses and len(syncer.runs) == 1
        (syncer.library / "common").mkdir()
        (syncer.library / "common" / "stdio.md").write_text("saved by a stdio server\n")
        syncer.pending = 1  # what the engine's status reports for that file
        await settled(service, syncer, 2)
        assert syncer.runs[1].force is False
        assert service.health()["user_sync"]["pending"] == 0


@pytest.mark.asyncio
async def test_fetch_interval_follows_the_setting(tmp_path):
    syncer = FakeSyncer(tmp_path / "library", fetch_minutes=1)
    app, service = make_app(tmp_path, syncer, minute=.3, scan_every=1.0)
    async with running(app):
        await until(lambda: len(syncer.runs) >= 4, timeout=3)
        gaps = [later.time - earlier.time for earlier, later in zip(syncer.runs, syncer.runs[1:])]
        assert all(.28 <= gap < .6 for gap in gaps[:3]), gaps
        assert not any(run.force for run in syncer.runs)


def test_fetch_minutes_are_clamped_to_one_to_sixty():
    loop = UserSync(service=None)
    assert loop._fetch_seconds(None) == 300
    assert loop._fetch_seconds(SimpleNamespace(fetch_minutes=0)) == 60
    assert loop._fetch_seconds(SimpleNamespace(fetch_minutes=15)) == 900
    assert loop._fetch_seconds(SimpleNamespace(fetch_minutes=600)) == 3600
    assert loop._fetch_seconds(SimpleNamespace(fetch_minutes="5")) == 300
    assert loop._fetch_seconds(SimpleNamespace(fetch_minutes=True)) == 300


@pytest.mark.asyncio
async def test_offline_cycle_waits_for_retry_at(tmp_path):
    syncer = FakeSyncer(tmp_path / "library")
    retry_at = []
    original_status, original_run = syncer.status, syncer.run

    def status():  # the engine keeps "offline" with its retry_at until a cycle succeeds
        value = original_status()
        if retry_at:
            value.update(state="offline", reason="network", retry_at=retry_at[0])
        return value

    def run(**options):
        result = original_run(**options)
        if len(syncer.runs) == 2:  # the network fails once
            retry_at.append((datetime.now(timezone.utc) + timedelta(seconds=1)).isoformat())
            return {"status": "offline", "reason": "network", "sent": [], "received": [], "conflicts": [],
                    "retry_at": retry_at[0]}
        retry_at.clear()
        return result
    syncer.status, syncer.run = status, run
    app, service = make_app(tmp_path, syncer)
    async with running(app):
        await settled(service, syncer, 1)
        user_library.notify(syncer.library, ["common/a.md"])
        await settled(service, syncer, 2)
        failed = syncer.runs[1].time
        await settled(service, syncer, 3)  # at retry_at plus a second's margin, not the 5-minute fetch
        assert 1.5 <= syncer.runs[2].time - failed < 4
        assert service.health()["user_sync"]["state"] == "synced"


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", ["off", "waiting_for_access", "paused"])
async def test_nothing_runs_unless_sync_is_set_up_started_and_not_paused(tmp_path, setting):
    syncer = FakeSyncer(tmp_path / "library", started=setting != "waiting_for_access", paused=setting == "paused")
    syncer.set_up = setting != "off"
    app, service = make_app(tmp_path, syncer, debounce=.1)
    async with running(app) as http:
        await asyncio.sleep(.2)
        user_library.notify(syncer.library, ["common/a.md"])
        (syncer.library / "manual.md").write_text("edited by hand\n")
        await asyncio.sleep(.6)
        assert syncer.runs == []
        assert (await http.get("/health")).json()["user_sync"]["state"] == setting
    assert syncer.runs == []  # not even on drain


@pytest.mark.asyncio
async def test_update_transaction_holds_the_loop(tmp_path):
    syncer = FakeSyncer(tmp_path / "library")
    app, service = make_app(tmp_path, syncer, debounce=.1)
    barrier = service.directory / "transaction.json"
    write_json(barrier, {"phase": "probation"})
    async with running(app):
        user_library.notify(syncer.library, ["common/a.md"])
        await asyncio.sleep(.6)
        assert syncer.runs == []  # neither the catch-up cycle nor the change
        barrier.unlink()
        await settled(service, syncer, 1)
        await asyncio.sleep(.4)
        assert len(syncer.runs) == 1  # the held change and fetch ran as one cycle


# --- io_pending and the drain -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_engine_calls_count_in_io_pending(tmp_path):
    syncer = FakeSyncer(tmp_path / "library")
    syncer.release.clear()
    app, service = make_app(tmp_path, syncer)
    async with running(app) as http:
        await until(syncer.running.is_set)
        health = (await http.get("/health")).json()
        assert health["io_pending"] == 1 and service.io.inflight == 0  # its own thread, counted
        assert health["user_sync"]["state"] == "syncing"
        syncer.release.set()
        await until(lambda: service.health()["io_pending"] == 0)


@pytest.mark.asyncio
async def test_shutdown_runs_one_last_cycle_then_the_task_ends(tmp_path):
    syncer = FakeSyncer(tmp_path / "library")
    app, service = make_app(tmp_path, syncer, debounce=60)
    async with running(app):
        await settled(service, syncer, 1)
        user_library.notify(syncer.library, ["common/a.md"])  # its cycle would wait a minute
        await asyncio.sleep(.05)
        stopping = time.monotonic()
    assert time.monotonic() - stopping < 2
    assert len(syncer.runs) == 2 and syncer.runs[1].force is False
    assert service.user_sync.task.done() and service.user_sync.pending == 0


@pytest.mark.asyncio
async def test_shutdown_without_changes_skips_the_last_cycle(tmp_path):
    syncer = FakeSyncer(tmp_path / "library")
    app, service = make_app(tmp_path, syncer)
    async with running(app):
        await settled(service, syncer, 1)
    assert len(syncer.runs) == 1 and service.user_sync.task.done()


@pytest.mark.asyncio
async def test_drain_deadline_abandons_a_stuck_cycle(tmp_path):
    syncer = FakeSyncer(tmp_path / "library")
    syncer.release.clear()  # the engine hangs, as on a network call that does not answer
    app, service = make_app(tmp_path, syncer, drain_budget=.5)
    try:
        async with running(app):
            await until(syncer.running.is_set)
            stopping = time.monotonic()
        assert .4 <= time.monotonic() - stopping < 2.5
        assert service.user_sync.task.done() and service.user_sync.pending == 0
        assert service.user_sync.calls  # still running in its thread, abandoned
    finally:
        syncer.release.set()


@pytest.mark.asyncio
async def test_admin_drain_waits_for_the_last_cycle_and_resume_continues(tmp_path):
    syncer = FakeSyncer(tmp_path / "library")
    app, service = make_app(tmp_path, syncer, debounce=60)
    async with running(app) as http:
        await settled(service, syncer, 1)
        user_library.notify(syncer.library, ["common/a.md"])
        await asyncio.sleep(.05)
        drained = (await http.post("/admin/drain")).json()
        assert drained["io_pending"] >= 1  # the controller's drain waits for the last cycle
        await until(lambda: service.health()["io_pending"] == 0)
        assert len(syncer.runs) == 2
        assert (await http.post("/admin/user-sync/run")).status_code == 503
        assert (await http.get("/admin/user-sync/status")).status_code == 200
        await http.post("/admin/resume")
        response = await http.post("/admin/user-sync/run")
        assert response.status_code == 200 and response.json()["status"] == "synced"
        assert len(syncer.runs) == 3 and syncer.runs[2].force is True
        assert (await http.post("/admin/drain")).json()["io_pending"] >= 1  # a second drain flushes again
        await until(lambda: service.health()["io_pending"] == 0)


# --- admin endpoints --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_admin_endpoints_need_the_token(tmp_path):
    syncer = FakeSyncer(tmp_path / "library")
    app, service = make_app(tmp_path, syncer)
    async with running(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://127.0.0.1:8765") as anonymous:
            for command in ("status", "run", "setup", "check", "preview", "start", "pause", "resume", "disconnect"):
                method = anonymous.get if command == "status" else anonymous.post
                response = await method(f"/admin/user-sync/{command}")
                assert response.status_code == 401 and response.json() == {"error": "unauthorized"}
                wrong = await method(f"/admin/user-sync/{command}", headers={"Authorization": "Bearer " + "y" * 48})
                assert wrong.status_code == 401
    assert syncer.calls == [] and not syncer.config.paused


@pytest.mark.asyncio
async def test_admin_endpoints_answer_json(tmp_path):
    syncer = FakeSyncer(tmp_path / "library", started=False)
    app, service = make_app(tmp_path, syncer)
    async with running(app) as http:
        await until(lambda: service.user_sync.armed)
        status = await http.get("/admin/user-sync/status")
        assert status.status_code == 200 and status.headers["cache-control"] == "no-store"
        assert status.json()["state"] == "waiting_for_access" and status.json()["loop"]["state"] == "running"
        setup = await http.post("/admin/user-sync/setup", json={
            "remote": "git@github.com:me/library.git", "name": "Me", "email": "me@example.com", "label": None,
            "ask_new_repositories": True})
        assert setup.status_code == 200 and setup.json()["public_key"] == PUBLIC_KEY
        assert syncer.calls[-1] == ("setup", {"remote": "git@github.com:me/library.git", "name": "Me",
                                              "email": "me@example.com", "ask_new_repositories": True})
        assert (await http.post("/admin/user-sync/check")).json()["remote_state"] == "empty"
        assert (await http.post("/admin/user-sync/preview")).json()["hash"] == "abc123"
        assert syncer.runs == []  # not started yet: nothing runs by itself
        started = await http.post("/admin/user-sync/start", json={"confirm": "abc123"})
        assert started.status_code == 200 and syncer.calls[-1] == ("start", {"confirm": "abc123"})
        loop = (await http.get("/admin/user-sync/status")).json()["loop"]
        assert loop["active"] is True and loop["last_cycle"]["trigger"] == "start"
        paused = await http.post("/admin/user-sync/pause")
        assert paused.status_code == 200 and paused.json()["state"] == "paused"
        assert (await http.get("/health")).json()["user_sync"]["state"] == "paused"
        runs = len(syncer.runs)
        resumed = await http.post("/admin/user-sync/resume")
        assert resumed.status_code == 200 and resumed.json()["state"] == "synced"
        await until(lambda: len(syncer.runs) > runs)  # catches up after a resume
        disconnected = await http.post("/admin/user-sync/disconnect")
        assert disconnected.status_code == 200 and disconnected.json()["status"] == "off"


@pytest.mark.asyncio
async def test_admin_endpoints_refuse_bad_requests(tmp_path):
    syncer = FakeSyncer(tmp_path / "library", started=False)
    app, service = make_app(tmp_path, syncer)
    async with running(app) as http:
        valid = {"remote": "git@github.com:me/library.git", "name": "Me", "email": "me@example.com"}
        for body in ({**valid, "token": "x"}, {**valid, "name": 5}, {**valid, "confirm_private": "yes"},
                     {"remote": "git@github.com:me/library.git", "name": "Me"}, ["remote"]):
            response = await http.post("/admin/user-sync/setup", json=body)
            assert response.status_code == 400 and response.json()["error"] == "invalid_request", body
        assert (await http.post("/admin/user-sync/setup", content=b"{not json")).status_code == 400
        assert (await http.post("/admin/user-sync/run", content=b"{" + b" " * 70000 + b"}")).status_code == 413
        assert (await http.post("/admin/user-sync/start")).status_code == 400  # the preview's hash is required
        assert (await http.post("/admin/user-sync/check", json={"force": True})).status_code == 400
        assert (await http.get("/admin/user-sync/run")).status_code == 405
        assert (await http.post("/admin/user-sync/status")).status_code == 405
        assert (await http.post("/admin/user-sync/resolve")).status_code == 404
        assert syncer.calls == []

        syncer.errors["check"] = SyncError("lock_held", "another sync of this library is running", state="busy")
        refused = await http.post("/admin/user-sync/check")
        assert refused.status_code == 409
        assert refused.json() == {"status": "busy", "reason": "lock_held",
                                  "message": "another sync of this library is running"}
        syncer.errors["preview"] = RuntimeError("boom")
        broken = await http.post("/admin/user-sync/preview")
        assert broken.status_code == 500
        assert broken.json() == {"status": "error", "reason": "internal", "message": "RuntimeError: boom"}
        syncer.errors["pause"] = SyncError("not_set_up", "sync is not set up; run setup first", state="off")
        assert (await http.post("/admin/user-sync/pause")).status_code == 409


@pytest.mark.asyncio
async def test_health_summary_is_short_and_holds_no_secrets(tmp_path):
    syncer = FakeSyncer(tmp_path / "library")
    app, service = make_app(tmp_path, syncer)
    async with running(app) as http:
        await settled(service, syncer, 1)
        health = (await http.get("/health")).json()
        assert health["user_sync"] == {"state": "synced", "reason": None, "last_success": "2026-10-05T10:00:00+00:00",
                                       "conflicts": 2, "pending": 0}
        text = json.dumps(health)
        for secret in (PUBLIC_KEY, "AAAAC3Nza", "me@example.com", "github.com/me/library", "user-sync"):
            assert secret not in text


# --- the command line -------------------------------------------------------------------------


def test_cli_falls_back_to_the_engine_when_the_service_is_down(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    write_json(state / "service.json", {"port": free_port(), "installation": str(tmp_path / "install")})
    (state / "token").write_text(TOKEN + "\n")
    monkeypatch.setenv("AGENTS_USER_FLOWS_DIR", str(tmp_path / "library"))

    assert control.main(["--state", str(state), "user-sync", "status"]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status["via"] == "direct" and status["state"] == "off"

    assert control.main(["--state", str(state), "user-sync", "pause"]) == 1
    paused = json.loads(capsys.readouterr().out)
    assert paused == {"status": "off", "reason": "not_set_up", "message": "sync is not set up; run setup first",
                      "via": "direct"}

    monkeypatch.setattr(control.Controller, "launchctl",
                        lambda self, *args, check=True: SimpleNamespace(returncode=113))
    assert control.main(["--state", str(state), "status"]) == 0
    service = json.loads(capsys.readouterr().out)
    assert service["state"] == "stopped" and service["user_sync"]["state"] == "off"


def test_cli_runs_through_the_service(tmp_path, monkeypatch, capsys):
    import uvicorn
    syncer = FakeSyncer(tmp_path / "library")
    app, service = make_app(tmp_path, syncer)
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not (server.started and service.state == "ready" and syncer.runs):
            assert time.monotonic() < deadline and thread.is_alive()
            time.sleep(.02)
        write_json(service.directory / "service.json", {"port": port, "installation": str(tmp_path / "install")})
        (service.directory / "token").write_text(TOKEN + "\n")
        state = ["--state", str(service.directory)]

        assert control.main([*state, "user-sync", "status"]) == 0
        status = json.loads(capsys.readouterr().out)
        assert status["via"] == "service" and status["state"] == "synced" and "loop" in status

        assert control.main([*state, "user-sync", "run"]) == 0
        assert json.loads(capsys.readouterr().out)["via"] == "service"
        assert syncer.runs[-1].force is True

        assert control.main([*state, "user-sync", "setup", "--remote", "git@github.com:me/library.git",
                             "--name", "Me", "--email", "me@example.com"]) == 0
        capsys.readouterr()
        assert syncer.calls[-1] == ("setup", {"remote": "git@github.com:me/library.git", "name": "Me",
                                              "email": "me@example.com", "branch": "main"})

        assert control.main([*state, "status"]) == 0
        assert json.loads(capsys.readouterr().out)["user_sync"]["state"] in ("synced", "syncing")
    finally:
        server.should_exit = True
        thread.join(10)
    assert not thread.is_alive()


@pytest.mark.asyncio
async def test_service_and_direct_runs_never_overlap(tmp_path, monkeypatch, capsys):
    """The real engine's sync lock: whichever runner comes second reports lock_held."""
    library = tmp_path / "library"
    (library / ".git").mkdir(parents=True)  # the lock lives there; no repository is needed
    monkeypatch.setenv("AGENTS_USER_FLOWS_DIR", str(library))
    app, service = make_app(tmp_path)  # the default: the engine on the service's state directory
    (service.directory / "user-sync").mkdir()
    Settings(remote="git@github.com:me/library.git", name="Me", email="me@example.com", label="mac-test",
             started="2026-10-05T00:00:00+00:00").save(service.directory / "user-sync" / "user-sync.json")
    entered, release = threading.Event(), threading.Event()

    def locked(self, settings, state, confirm):
        entered.set()
        release.wait(10)
        return {"status": "synced", "sent": [], "received": [], "conflicts": [], "pushed": False}
    monkeypatch.setattr(Syncer, "_run_locked", locked)
    monkeypatch.setattr(Syncer, "status", lambda self: {"state": "synced", "reason": None, "conflicts": 0,
                                                        "pending": 0, "last_success": None})
    async with running(app) as http:
        await until(entered.is_set)  # the service's catch-up cycle holds the sync lock
        assert control.main(["--state", str(service.directory), "user-sync", "run"]) == 0
        direct = json.loads(capsys.readouterr().out)
        assert direct["via"] == "direct" and direct["status"] == "lock_held"
        release.set()
        await until(lambda: service.user_sync.last_cycle is not None)
        assert service.user_sync.last_cycle["status"] == "synced"

        entered.clear()
        release.clear()
        outside = threading.Thread(target=lambda: Syncer(library, service.directory / "user-sync").run(force=True))
        outside.start()  # e.g. python -m src.user_sync run, or the scheduler of #168
        await until(entered.is_set)
        response = await http.post("/admin/user-sync/run")
        assert response.status_code == 200 and response.json()["status"] == "lock_held"
        release.set()
        outside.join(10)


def test_fingerprint_sees_changes_but_not_git(tmp_path):
    library = tmp_path / "library"
    (library / ".git").mkdir(parents=True)
    (library / "common").mkdir()
    (library / "common" / "a.md").write_text("one\n")
    first = library_fingerprint(library)
    (library / ".git" / "index").write_text("git's own files do not count\n")
    assert library_fingerprint(library) == first
    (library / "common" / "a.md").write_text("two, longer\n")
    assert library_fingerprint(library) != first
