"""The background readiness gate (src/engine/readiness.py) and the gated tools."""
import asyncio
import json
import threading
import time

import pytest

from src.engine import readiness


@pytest.fixture(autouse=True)
def fresh_gate():
    readiness.reset_for_tests()
    yield
    readiness.reset_for_tests()


def _wait_until(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, "condition not reached"
        time.sleep(0.01)


def test_not_started_gate_does_not_block():
    assert asyncio.run(readiness.wait(0.05)) is None
    assert readiness.state() == "warming_up" and not readiness.is_warming()


def test_wait_returns_once_phases_finish():
    release = threading.Event()
    assert readiness.start([("slow", release.wait)]) is True
    assert readiness.is_warming() and readiness.state() == "warming_up"

    async def scenario():
        waiter = asyncio.create_task(readiness.wait(5))
        await asyncio.sleep(0.05)
        assert not waiter.done()
        release.set()
        return await waiter

    assert asyncio.run(scenario()) is None
    assert readiness.is_ready() and not readiness.is_warming()


def test_wait_times_out_with_warming_up_and_later_succeeds():
    release = threading.Event()
    readiness.start([("slow", release.wait)])
    started = time.monotonic()
    assert asyncio.run(readiness.wait(0.1)) == "warming_up"
    assert time.monotonic() - started < 2
    release.set()
    _wait_until(readiness.is_ready)
    assert asyncio.run(readiness.wait(0.1)) is None


def test_failure_is_stored_and_reported_without_hanging():
    def boom():
        raise RuntimeError("store is corrupt")

    readiness.start([("boom", boom), ("never", lambda: pytest.fail("ran after failure"))])
    _wait_until(lambda: readiness.state() == "failed")
    problem = asyncio.run(readiness.wait(5))
    assert "store is corrupt" in problem and problem != "warming_up"
    assert not readiness.is_ready() and not readiness.is_warming()


def test_start_runs_the_initializer_once():
    calls = []
    assert readiness.start([("count", lambda: calls.append(1))]) is True
    assert readiness.start([("count", lambda: calls.append(2))]) is False
    _wait_until(readiness.is_ready)
    assert calls == [1]


def test_concurrent_starts_run_once():
    calls = []
    gate = threading.Barrier(8)

    def racer():
        gate.wait()
        readiness.start([("count", lambda: calls.append(1))])

    threads = [threading.Thread(target=racer) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    _wait_until(readiness.is_ready)
    assert calls == [1]


def test_initializer_is_a_daemon_thread():
    release = threading.Event()
    readiness.start([("slow", release.wait)])
    assert readiness._thread.daemon
    release.set()


def test_when_done_runs_after_failure_too():
    seen = threading.Event()
    readiness.when_done(seen.set)
    readiness.start([("boom", lambda: 1 / 0)])
    assert seen.wait(5)


def test_run_blocking_is_strict_and_start_once(monkeypatch):
    calls = []
    monkeypatch.setattr(readiness, "_default_phases", lambda strict=False: [("p", lambda: calls.append(strict))])
    readiness.run_blocking()
    readiness.run_blocking()
    assert calls == [True]


def test_lenient_phases_swallow_warmup_failures(monkeypatch):
    def fail():
        raise OSError("no network")

    monkeypatch.setattr(readiness, "_warmup_embedding_model", fail)
    monkeypatch.setattr(readiness, "_build_retrievers", lambda: None)
    monkeypatch.setattr(readiness, "_warmup_rules", lambda strict: None)
    for name, step in readiness._default_phases(strict=False):
        step()  # lenient: nothing raises
    with pytest.raises(OSError):
        dict(readiness._default_phases(strict=True))["embedding_model"]()


# --- tools -------------------------------------------------------------------------------------

def _tool_results(server):
    return [
        ("route_and_load", server.route_and_load("hello")),
        ("get_agent_context", server.get_agent_context("universal_agent", "hello")),
        ("load_implants", server.load_implants(query="think")),
        ("read_history", server.read_history(query="x")),
    ]


def test_gated_tools_report_warming_up_past_the_cap(monkeypatch):
    import src.server as server
    release = threading.Event()
    monkeypatch.setattr(readiness, "WARMUP_WAIT_SECONDS", 0.05)
    readiness.start([("slow", release.wait)])

    async def scenario():
        return {name: await call for name, call in _tool_results(server)}

    try:
        results = asyncio.run(scenario())
    finally:
        release.set()
    for name in ("route_and_load", "get_agent_context"):
        payload = json.loads(results[name])
        assert payload["status"] == "ERROR" and payload["message"].startswith("warming_up")
    assert results["load_implants"].startswith("warming_up")
    assert json.loads(results["read_history"])["status"] == "warming_up"


def test_gated_tools_report_a_failed_initialization(monkeypatch):
    import src.server as server
    readiness.start([("boom", lambda: 1 / 0)])
    _wait_until(lambda: readiness.state() == "failed")

    async def scenario():
        return json.loads(await server.route_and_load("hello"))

    payload = asyncio.run(scenario())
    assert payload["status"] == "ERROR" and "initialization failed" in payload["message"]


def test_tools_that_never_wait_answer_while_warming(monkeypatch):
    import src.server as server
    release = threading.Event()
    readiness.start([("slow", release.wait)])

    async def scenario():
        agents = json.loads(await asyncio.wait_for(server.list_agents(include_metadata=False), 2))
        flows = await asyncio.wait_for(server.list_flows(), 2)
        return agents, flows

    try:
        agents, _ = asyncio.run(scenario())
    finally:
        release.set()
    assert agents["agents"]


def test_log_interaction_skips_langfuse_only_while_warming(monkeypatch, tmp_path):
    import src.server as server
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(tmp_path))
    (tmp_path / ".git").mkdir()
    release = threading.Event()
    readiness.start([("slow", release.wait)])
    submitted = []
    monkeypatch.setattr(server._langfuse_worker, "submit", lambda fn: submitted.append(fn) or True)
    monkeypatch.setattr(server._history_worker, "submit", lambda fn: True)

    async def log():
        return json.loads(await server.log_interaction("universal_agent", "q", "a"))

    try:
        warming = asyncio.run(log())
    finally:
        release.set()
    assert warming["langfuse"] == {"status": "skipped", "reason": "warming_up"} and not submitted
    _wait_until(readiness.is_ready)
    assert asyncio.run(log())["langfuse"] == {"status": "queued"} and submitted
