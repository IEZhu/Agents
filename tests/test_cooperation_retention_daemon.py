"""Cooperation maintenance follows the daemon lifecycle without live stores or models."""
import asyncio
from contextlib import asynccontextmanager
import threading
import time
from types import SimpleNamespace

import pytest

from src.daemon import app as daemon


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(.001)


@pytest.fixture
def coordinator(monkeypatch):
    import src.cooperation.service as cooperation

    class Coordinator:
        instances = []
        fail_once = False
        block = False

        def __init__(self, directory, *, retention_days):
            self.directory = directory
            self.retention_days = retention_days
            self.calls = []
            self.times = []
            self.started = self.closed = False
            self.entered = threading.Event()
            self.release = threading.Event()
            self.collecting = False
            self.instances.append(self)

        def start(self):
            self.started = True

        def collect_terminal(self):
            assert self.started and not self.closed and not self.collecting
            self.collecting = True
            self.calls.append(threading.get_ident())
            self.times.append(time.monotonic())
            self.entered.set()
            try:
                if self.block:
                    assert self.release.wait(3), "collection was not released"
                if self.fail_once and len(self.calls) == 1:
                    raise RuntimeError("PRIVATE_STORE_CONTENT")
            finally:
                self.collecting = False

        def close(self):
            assert not self.collecting, "closed coordinator while collection was running"
            self.closed = True

    monkeypatch.setattr(cooperation, "CooperationService", Coordinator)
    monkeypatch.setenv("AGENTS_COOPERATION_ENABLED", "1")
    monkeypatch.setenv("AGENTS_COOPERATION_RETENTION_DAYS", "45")
    monkeypatch.setattr(daemon, "COOPERATION_RETENTION_INTERVAL", .02, raising=False)
    return Coordinator


def runtime(*, fail_exit=False):
    @asynccontextmanager
    async def running():
        yield
        if fail_exit:
            raise RuntimeError("runtime exit failure")

    return lambda port: (SimpleNamespace(
        streamable_http_app=lambda: object(),
        session_manager=SimpleNamespace(run=running)), None)


@pytest.mark.asyncio
async def test_retention_repeats_off_loop_and_retries_failure(tmp_path, coordinator, caplog):
    coordinator.fail_once = True
    service = daemon.Service(tmp_path, "token", runtime_loader=runtime())
    task = asyncio.create_task(service.start_runtime())
    try:
        await until(lambda: coordinator.instances and len(coordinator.instances[0].calls) >= 2)
        owner = coordinator.instances[0]
        assert service.state == "ready" and not task.done()
        assert owner.retention_days == 45
        assert all(worker != threading.get_ident() for worker in owner.calls)
        assert owner.times[1] - owner.times[0] >= daemon.COOPERATION_RETENTION_INTERVAL
        assert "Cooperation retention failed" in caplog.text
        assert "PRIVATE_STORE_CONTENT" not in caplog.text
    finally:
        service.stop.set()
        await asyncio.wait_for(task, 2)
    assert owner.closed


@pytest.mark.asyncio
async def test_disabled_runtime_adds_no_cooperation_maintenance(tmp_path, coordinator, monkeypatch):
    monkeypatch.delenv("AGENTS_COOPERATION_ENABLED")
    service = daemon.Service(tmp_path, "token", runtime_loader=runtime())

    async def forbidden(*args):
        pytest.fail("disabled cooperation started maintenance")

    monkeypatch.setattr(service, "retain_cooperation", forbidden, raising=False)
    task = asyncio.create_task(service.start_runtime())
    try:
        await until(lambda: service.state == "ready")
        await asyncio.sleep(.05)
        assert not coordinator.instances
        assert service.cooperation is None
        assert not (tmp_path / "cooperation").exists()
    finally:
        service.stop.set()
        await asyncio.wait_for(task, 2)


@pytest.mark.asyncio
@pytest.mark.parametrize("shutdown", ["stop", "cancel", "runtime_failure"])
async def test_runtime_joins_collection_before_coordinator_close(tmp_path, coordinator, shutdown):
    coordinator.block = True
    service = daemon.Service(tmp_path, "token", runtime_loader=runtime(fail_exit=shutdown == "runtime_failure"))
    task = asyncio.create_task(service.start_runtime())
    try:
        await until(lambda: coordinator.instances and coordinator.instances[0].entered.is_set())
        owner = coordinator.instances[0]
        await asyncio.sleep(.05)
        assert len(owner.calls) == 1  # A slow pass cannot overlap another one.
        if shutdown == "cancel":
            task.cancel()
        else:
            service.stop.set()
        await asyncio.sleep(.01)
        assert not owner.closed and not task.done()
    finally:
        if coordinator.instances:
            coordinator.instances[0].release.set()
        service.stop.set()
        if shutdown == "cancel":
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await asyncio.wait_for(task, 2)
    assert owner.closed and not owner.collecting
    assert len(owner.calls) == 1
    if shutdown == "runtime_failure":
        assert service.state == "failed"


@pytest.mark.asyncio
async def test_retention_counts_as_io_and_pauses_during_drain(tmp_path, coordinator):
    coordinator.block = True
    service = daemon.Service(tmp_path, "token", runtime_loader=runtime())
    async with service.lifespan(None):
        try:
            await until(lambda: coordinator.instances and coordinator.instances[0].entered.is_set())
            owner = coordinator.instances[0]
            assert service.health()["io_pending"] >= 1
            service.state = "draining"
        finally:
            if coordinator.instances:
                coordinator.instances[0].release.set()
        await until(lambda: service.io.inflight == 0)
        await asyncio.sleep(.05)
        assert len(owner.calls) == 1
        service.state = "ready"
        await until(lambda: len(owner.calls) >= 2)
    assert owner.closed and not owner.collecting
