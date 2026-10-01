"""log_interaction answers with a timestamp before its sinks finish."""

import json
import logging
import re
import threading
import time
from unittest.mock import Mock

import pytest

import src.server as server
from src.memory.history import HistoryWriter

TIMESTAMP = re.compile(r"\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2}")


@pytest.fixture
def workspace(tmp_path, monkeypatch):
    (tmp_path / "CLAUDE.md").write_text("")
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(tmp_path))
    from src.engine import config
    config._reset_client_repo_root_cache()
    yield tmp_path
    server.drain_pending_logs(5)
    config._reset_client_repo_root_cache()


@pytest.mark.asyncio
async def test_returns_exact_format_timestamp_and_queued_sinks(workspace, monkeypatch):
    monkeypatch.setattr(server, "is_langfuse_configured", lambda: False)
    response = json.loads(await server.log_interaction("software_engineer", "q", "r"))
    assert TIMESTAMP.fullmatch(response["timestamp"])
    assert response["langfuse"] == {"status": "queued"}
    assert response["history"] == {"status": "queued"}
    assert server.drain_pending_logs(5)


@pytest.mark.asyncio
async def test_returns_promptly_with_hanging_sinks(workspace, monkeypatch):
    release = threading.Event()
    started = threading.Event()
    monkeypatch.setattr(server, "is_langfuse_configured", lambda: True)

    def hang(*args, **kwargs):
        started.set()
        release.wait(10)
        raise RuntimeError("released")

    monkeypatch.setattr(server.langfuse, "create_trace_id", hang)
    real_append = HistoryWriter.append_entry

    def blocked_append(self, *args, **kwargs):
        release.wait(10)
        return real_append(self, *args, **kwargs)

    monkeypatch.setattr(HistoryWriter, "append_entry", blocked_append)
    try:
        begin = time.monotonic()
        response = json.loads(await server.log_interaction("software_engineer", "q", "r"))
        assert time.monotonic() - begin < 2
        assert TIMESTAMP.fullmatch(response["timestamp"])
        assert started.wait(5)
    finally:
        release.set()
    assert server.drain_pending_logs(10)


@pytest.mark.asyncio
async def test_background_history_entry_carries_issued_timestamp(workspace, monkeypatch):
    monkeypatch.setattr(server, "is_langfuse_configured", lambda: False)
    response = json.loads(await server.log_interaction("software_engineer", "the query", "the answer"))
    assert server.drain_pending_logs(5)
    text = (workspace / "history.md").read_text(encoding="utf-8")
    assert response["timestamp"] in text
    assert "the answer" in text
    from src.memory.history import HistoryReader
    entries = HistoryReader(str(workspace / "history.md")).read_recent(limit=5)
    assert [e.intent for e in entries] == ["the query"]


@pytest.mark.asyncio
async def test_sink_failures_are_logged_not_returned(workspace, monkeypatch, caplog):
    monkeypatch.setattr(server, "is_langfuse_configured", lambda: True)
    monkeypatch.setattr(server.langfuse, "create_trace_id", Mock(side_effect=RuntimeError("lf down")))
    monkeypatch.setattr(HistoryWriter, "append_entry", Mock(side_effect=OSError("disk full")))
    with caplog.at_level(logging.ERROR, logger="mcp-server"):
        response = json.loads(await server.log_interaction("software_engineer", "q", "r"))
        assert server.drain_pending_logs(5)
    assert "error" not in json.dumps(response).lower()
    assert "lf down" in caplog.text and "disk full" in caplog.text


@pytest.mark.asyncio
async def test_drain_waits_for_pending_writes(workspace, monkeypatch):
    monkeypatch.setattr(server, "is_langfuse_configured", lambda: False)
    release = threading.Event()
    real_append = HistoryWriter.append_entry

    def slow_append(self, *args, **kwargs):
        release.wait(10)
        return real_append(self, *args, **kwargs)

    monkeypatch.setattr(HistoryWriter, "append_entry", slow_append)
    await server.log_interaction("software_engineer", "q", "r")
    assert not server.drain_pending_logs(0.05)
    release.set()
    assert server.drain_pending_logs(5)
    assert (workspace / "history.md").exists()


@pytest.mark.asyncio
async def test_invalid_attribution_returns_error_without_timestamp(monkeypatch):
    writer = Mock()
    monkeypatch.setattr(server, "HistoryWriter", writer)
    response = json.loads(await server.log_interaction("software_engineer", "q", "r", persona_action="keep"))
    assert response["status"] == "ERROR"
    assert "timestamp" not in response
    writer.assert_not_called()


def test_sink_workers_are_daemon_threads():
    assert server._history_worker._thread.daemon
    assert server._langfuse_worker._thread.daemon


def test_full_queue_drops_writes_and_logs(monkeypatch, caplog):
    monkeypatch.setattr(server, "LOG_QUEUE_MAX", 1)
    worker = server._SinkWorker("test")
    release = threading.Event()
    started = threading.Event()

    def block():
        started.set()
        release.wait(10)

    try:
        assert worker.submit(block)
        assert started.wait(5)
        assert worker.submit(lambda: None)  # fills the queue
        with caplog.at_level(logging.ERROR, logger="mcp-server"):
            assert not worker.submit(lambda: None)
        assert "dropping a write" in caplog.text
        assert not worker.drain(time.monotonic() + 0.05)
    finally:
        release.set()
    assert worker.drain(time.monotonic() + 5)
