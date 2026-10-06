"""User sync in the daemon (#167): triggers, drain and admin endpoints with a fake runtime and engine.

No embedding model and no git: a fake runtime serves MCP, and ``FakeSyncer`` stands in for
``src.user_sync.engine.Syncer``. The overlap test uses the real engine's sync lock with its locked
part replaced, so it needs no repository either. Intervals are fractions of a second.
"""
import asyncio
from collections import namedtuple
from contextlib import asynccontextmanager, contextmanager
from datetime import datetime, timedelta, timezone
import json
import socket
import struct
import threading
import time
from types import SimpleNamespace

import httpx
import pytest
from mcp.server.fastmcp import FastMCP

from src import user_library
from src.daemon import control, sync_loop
from src.daemon.app import create_app
from src.daemon.state import write_json
from src.daemon.sync_loop import UserSync, library_fingerprint
from src.file_lock import file_lock
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
        self.hold_next_status = False  # the next status() waits for status_released
        self.holding_status = threading.Event()
        self.status_released = threading.Event()

    def _fail(self, name):
        if name in self.errors:
            raise self.errors[name]

    def settings(self):
        """A snapshot, as the engine loads ``user-sync.json`` afresh on every call."""
        return SimpleNamespace(**vars(self.config)) if self.set_up else None

    def status(self):
        with self.guard:
            self.statuses += 1
            hold, self.hold_next_status = self.hold_next_status, False
        if hold:
            self.holding_status.set()
            self.status_released.wait(10)
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
    async with running(app) as http:
        user_library.notify(syncer.library, ["common/a.md"])
        await asyncio.sleep(.6)
        assert syncer.runs == []  # neither the catch-up cycle nor the change
        # A transaction file left behind must not stop sync silently.
        assert (await http.get("/health")).json()["user_sync"]["held"] == "update"
        assert (await http.get("/admin/user-sync/status")).json()["loop"]["held"] == "update"
        barrier.unlink()
        await settled(service, syncer, 1)
        await asyncio.sleep(.4)
        assert len(syncer.runs) == 1  # the held change and fetch ran as one cycle
        assert (await http.get("/health")).json()["user_sync"]["held"] is None


# --- the hand-off from the OS scheduler -------------------------------------------------------


@pytest.mark.asyncio
async def test_only_the_service_entry_point_hands_off_from_the_scheduler(tmp_path, scheduled_sync_calls):
    syncer = FakeSyncer(tmp_path / "library")
    app, service = make_app(tmp_path, syncer)
    async with running(app):
        await settled(service, syncer, 1)
    assert scheduled_sync_calls == []  # tests and other callers of create_app never reach the scheduler
    assert service.user_sync.hand_off_schedule is False
    assert create_app(tmp_path / "service", TOKEN, hand_off_schedule=True).state.service.user_sync.hand_off_schedule


@pytest.mark.asyncio
async def test_the_loop_hands_off_once_when_asked(tmp_path, scheduled_sync_calls):
    syncer = FakeSyncer(tmp_path / "library")
    app, service = make_app(tmp_path, syncer, hand_off_schedule=True)
    async with running(app) as http:
        await settled(service, syncer, 1)
        await http.post("/admin/drain")
        await http.post("/admin/resume")
        await asyncio.sleep(.5)  # more scans after a drain and a resume
    assert scheduled_sync_calls == [None]  # this installation, once


@pytest.mark.asyncio
async def test_a_failed_hand_off_is_logged_and_sync_goes_on(tmp_path, monkeypatch, caplog):
    monkeypatch.setattr(control, "stop_scheduled_sync", lambda installation=None: "failed: launchctl refused")
    syncer = FakeSyncer(tmp_path / "library")
    app, service = make_app(tmp_path, syncer, hand_off_schedule=True)
    with caplog.at_level("WARNING", logger="src.daemon.sync_loop"):
        async with running(app):
            await settled(service, syncer, 1)
    assert "the scheduled sync run of this installation stays: failed: launchctl refused" in caplog.text


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
        user_library.notify(syncer.library, ["common/b.md"])
        await asyncio.sleep(.05)
        assert (await http.post("/admin/drain")).json()["io_pending"] >= 1  # a second drain flushes again
        await until(lambda: service.health()["io_pending"] == 0)
        assert len(syncer.runs) == 4 and syncer.runs[3].force is False
        assert service.user_sync.due is None


@pytest.mark.asyncio
async def test_a_drain_after_a_resume_gets_its_own_last_cycle(tmp_path):
    """drain, resume while the drain's last cycle still runs, a save, drain again: two last cycles."""
    syncer = FakeSyncer(tmp_path / "library")
    app, service = make_app(tmp_path, syncer, debounce=60, drain_budget=2.0)
    async with running(app) as http:
        await settled(service, syncer, 1)
        user_library.notify(syncer.library, ["common/a.md"])
        await asyncio.sleep(.05)
        syncer.release.clear()  # the first drain's last cycle takes a moment
        await http.post("/admin/drain")
        await until(syncer.running.is_set)
        await http.post("/admin/resume")  # e.g. the controller's drain timed out on other work
        user_library.notify(syncer.library, ["common/b.md"])  # saved after that cycle read the library
        await asyncio.sleep(.05)
        assert (await http.post("/admin/drain")).json()["io_pending"] >= 2
        syncer.release.set()
        await until(lambda: service.health()["io_pending"] == 0, timeout=8)
        assert len(syncer.runs) == 3 and syncer.runs[2].force is False
        assert service.user_sync.due is None and not service.user_sync.flush_wanted


@pytest.mark.asyncio
async def test_the_last_cycle_waits_for_saves_still_in_flight(tmp_path):
    """The drain's last cycle starts only after the requests admitted before the drain end."""
    syncer = FakeSyncer(tmp_path / "library")
    released, saved = asyncio.Event(), []

    def runtime(port):
        mcp = FastMCP("test", host="127.0.0.1", port=port, stateless_http=True, json_response=True)

        @mcp.tool()
        async def save():
            await released.wait()
            saved.append(time.monotonic())
            user_library.notify(syncer.library, ["common/from-mcp.md"])  # what save_flow reports
            return "saved"
        return mcp, None
    directory = tmp_path / "service"
    directory.mkdir()
    app = create_app(directory, TOKEN, runtime_loader=runtime)
    service = app.state.service
    service.user_sync = UserSync(service, syncer=syncer, debounce=60, scan_every=.2, drain_budget=1.0)
    async with running(app) as http:
        await settled(service, syncer, 1)
        payload = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "save", "arguments": {}}}
        call = asyncio.create_task(http.post("/mcp", json=payload,
                                             headers={"Accept": "application/json, text/event-stream"}))
        await until(lambda: service.inflight == 1)
        drained = (await http.post("/admin/drain")).json()
        assert drained["inflight"] == 1 and drained["io_pending"] >= 1
        await asyncio.sleep(.4)
        assert len(syncer.runs) == 1 and service.user_sync.drain_deadline is None  # waits for the save
        released.set()
        assert (await call).status_code == 200
        await until(lambda: service.health()["io_pending"] == 0)
        assert len(syncer.runs) == 2 and syncer.runs[1].time > saved[0]


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
async def test_a_pause_during_a_cycle_keeps_the_loop_off(tmp_path):
    """The cycle read the settings before the pause; its outcome must not turn the loop back on."""
    syncer = FakeSyncer(tmp_path / "library")
    app, service = make_app(tmp_path, syncer, debounce=.1, scan_every=30)  # no scan corrects it meanwhile
    async with running(app) as http:
        await settled(service, syncer, 1)
        syncer.release.clear()
        user_library.notify(syncer.library, ["common/a.md"])
        await until(syncer.running.is_set)  # the cycle runs; it read "not paused"
        paused = await http.post("/admin/user-sync/pause")
        assert paused.status_code == 200 and service.user_sync.active is False
        syncer.release.set()
        await settled(service, syncer, 2)
        assert service.user_sync.active is False
        loop = (await http.get("/admin/user-sync/status")).json()["loop"]
        assert loop["active"] is False and loop["last_cycle"]["trigger"] == "change"  # the cycle still counts


@pytest.mark.asyncio
async def test_a_pause_during_a_job_keeps_the_loop_off(tmp_path):
    """Pause answers at once; the start job read the settings before it and must not undo it."""
    syncer = FakeSyncer(tmp_path / "library", started=False)
    app, service = make_app(tmp_path, syncer, scan_every=30)  # no scan corrects the loop meanwhile
    async with running(app) as http:
        await until(lambda: service.user_sync.armed and not service.user_sync.calls)
        original_start = syncer.start

        def start(confirm):
            result = original_start(confirm)
            syncer.hold_next_status = True  # the job's status read, after its settings read, waits
            return result
        syncer.start = start
        starting = asyncio.create_task(http.post("/admin/user-sync/start", json={"confirm": "abc123"}))
        await until(syncer.holding_status.is_set)
        paused = await http.post("/admin/user-sync/pause")  # not queued behind the running job
        assert paused.status_code == 200 and paused.json()["state"] == "paused"
        assert service.user_sync.active is False
        syncer.status_released.set()
        assert (await starting).status_code == 200
        await asyncio.sleep(.3)
        assert service.user_sync.active is False  # the job's earlier read does not turn it back on
        assert (await http.get("/admin/user-sync/status")).json()["loop"]["active"] is False
        assert syncer.runs == []
        resumed = await http.post("/admin/user-sync/resume")
        assert resumed.status_code == 200 and service.user_sync.active is True
        await until(lambda: len(syncer.runs) == 1)  # resume catches up


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
        deep = await http.post("/admin/user-sync/setup", content=b"[" * 60000 + b"]" * 60000)
        assert deep.status_code == 413  # larger than 64 KiB
        nested = await http.post("/admin/user-sync/setup", content=b"[" * 30000 + b"]" * 30000)
        assert nested.status_code == 400 and nested.json()["error"] == "invalid_request"  # RecursionError
        assert (await http.post("/admin/user-sync/run", content=b"{" + b" " * 70000 + b"}")).status_code == 413
        exact = b"{" + b" " * (64 * 1024 - 2) + b"}"  # 64 KiB exactly: accepted
        assert (await http.post("/admin/user-sync/check", content=exact)).status_code == 200
        assert (await http.post("/admin/user-sync/start")).status_code == 400  # the preview's hash is required
        assert (await http.post("/admin/user-sync/check", json={"force": True})).status_code == 400
        assert (await http.get("/admin/user-sync/run")).status_code == 405
        assert (await http.post("/admin/user-sync/status")).status_code == 405
        assert (await http.post("/admin/user-sync/resolve")).status_code == 404
        assert syncer.calls == [("check", {})]
        syncer.calls.clear()

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
                                       "conflicts": 2, "pending": 0, "held": None}
        text = json.dumps(health)
        for secret in (PUBLIC_KEY, "AAAAC3Nza", "me@example.com", "github.com/me/library", "user-sync"):
            assert secret not in text


@pytest.mark.asyncio
async def test_a_client_that_leaves_while_sending_gets_json(tmp_path):
    from starlette.requests import Request
    app, service = make_app(tmp_path, FakeSyncer(tmp_path / "library"))

    async def receive():
        return {"type": "http.disconnect"}
    request = Request({"type": "http", "method": "POST", "path": "/admin/user-sync/run", "headers": [],
                       "query_string": b""}, receive)
    response = await service.user_sync.handle(request, "run")
    assert response.status_code == 400 and json.loads(response.body)["error"] == "invalid_request"


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


def configured(tmp_path, port):
    """A controller state directory whose service listens (or not) on ``port``."""
    state = tmp_path / "state"
    write_json(state / "service.json", {"port": port, "installation": str(tmp_path / "install")})
    (state / "token").write_text(TOKEN + "\n")
    return state


@contextmanager
def misbehaving(mode):
    """A port whose server never answers, resets, or answers with a 500 or a status."""
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(4)
    listener.settimeout(.1)
    stop = threading.Event()

    def read_request(connection):
        data = b""
        while b"\r\n\r\n" not in data:
            data += connection.recv(65536)
        head, _, body = data.partition(b"\r\n\r\n")
        length = next((int(line.split(b":")[1]) for line in head.split(b"\r\n")
                       if line.lower().startswith(b"content-length:")), 0)
        while len(body) < length:
            body += connection.recv(65536)

    def answer(connection, status, body, kind):
        connection.sendall(b"HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %d\r\nConnection: close\r\n\r\n%s"
                           % (status, kind, len(body), body))

    def serve():
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except OSError:
                continue
            with connection:
                if mode == "hang":
                    stop.wait(5)
                    continue
                if mode == "reset":  # close at once with a TCP reset
                    connection.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, struct.pack("ii", 1, 0))
                    continue
                read_request(connection)
                if mode == "html500":
                    answer(connection, b"500 Internal Server Error", b"<html>oops</html>", b"text/html")
                elif mode == "json500":
                    answer(connection, b"500 Internal Server Error",
                           b'{"status": "error", "reason": "internal", "message": "RuntimeError: boom"}',
                           b"application/json")
                else:  # a status that needs attention
                    answer(connection, b"200 OK", b'{"state": "attention", "reason": "stale", "loop": {}}',
                           b"application/json")

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    try:
        yield listener.getsockname()[1]
    finally:
        stop.set()
        thread.join(5)
        listener.close()


@pytest.mark.parametrize("mode, reason", [("hang", "timeout"), ("reset", "service_unreachable"),
                                          ("html500", "service_error"), ("json500", "internal")])
def test_cli_never_bypasses_a_service_that_answers_badly(tmp_path, monkeypatch, mode, reason):
    """Only a refused connection runs the engine here: a slow or failing service may still finish."""
    calls = []
    monkeypatch.setattr(sync_loop, "direct", lambda *arguments: calls.append(arguments[2]) or (200, {}))
    monkeypatch.setattr(sync_loop, "LONG_REQUEST_TIMEOUT", .5)
    with misbehaving(mode) as port:
        failed, result = control.Controller(configured(tmp_path, port)).user_sync("run", {})
    assert failed and calls == [] and result["via"] == "service" and result["reason"] == reason


def test_cli_falls_back_only_when_nothing_listens(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sync_loop, "direct", lambda *arguments: calls.append(arguments[2]) or (200, {"status": "synced"}))
    failed, result = control.Controller(configured(tmp_path, free_port())).user_sync("run", {})
    assert not failed and calls == ["run"] and result == {"status": "synced", "via": "direct"}


def test_cli_status_fails_when_sync_needs_attention(tmp_path):
    with misbehaving("attention") as port:
        failed, result = control.Controller(configured(tmp_path, port)).user_sync("status", {})
    assert failed and result["state"] == "attention" and result["via"] == "service"
    assert control.Controller._sync_failed(200, {"state": "synced"}) is False
    assert control.Controller._sync_failed(409, {"status": "busy", "reason": "lock_held"}) is False
    assert control.Controller._sync_failed(503, {"error": "draining"}) is True


def test_the_direct_fallback_holds_the_session_lease(tmp_path, monkeypatch, capsys):
    """As python -m src.user_sync: never while an update is installed, and an update waits for it."""
    state = configured(tmp_path, free_port())
    monkeypatch.setenv("AGENTS_USER_FLOWS_DIR", str(tmp_path / "library"))
    lease = tmp_path / "install" / "data" / ".sessions.lock"
    with file_lock(lease, blocking=False):  # an update activating
        assert control.main(["--state", str(state), "user-sync", "run"]) == 0
    busy = json.loads(capsys.readouterr().out)
    assert busy == {"status": "busy", "reason": "busy", "message": "an update of Agents-Core is being installed; "
                    "try again shortly", "via": "direct"}
    seen = []

    def status(self):
        try:
            with file_lock(lease, blocking=False):
                seen.append("free")
        except BlockingIOError:
            seen.append("held")
        return {"state": "off", "reason": None, "conflicts": 0}
    monkeypatch.setattr(Syncer, "status", status)
    assert control.main(["--state", str(state), "user-sync", "status"]) == 0
    assert json.loads(capsys.readouterr().out)["via"] == "direct" and seen == ["held"]


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
    write_json(service.directory / "service.json", {"port": free_port(), "installation": str(tmp_path / "install")})
    (service.directory / "token").write_text(TOKEN + "\n")  # the service "is down" for the CLI: direct runs
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
