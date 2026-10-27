"""Logging imports stay idle; concurrent first writes still have one consumer."""
import json
from pathlib import Path
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import src.server as server


def test_importing_tools_does_not_start_log_sink_threads():
    script = """
import json
import threading
from types import SimpleNamespace
import dotenv
from src.engine import router
dotenv.load_dotenv = lambda *args, **kwargs: False
router.SemanticRouter = lambda: SimpleNamespace(available_agents=[])
import src.server as server
print(json.dumps({"workers": [server._history_worker._thread, server._langfuse_worker._thread],
                  "threads": [t.name for t in threading.enumerate()
                              if t.name in ("log-history", "log-langfuse")]}))
"""
    result = subprocess.run([sys.executable, "-B", "-c", script],
                            cwd=Path(__file__).resolve().parents[1],
                            capture_output=True, text=True, timeout=20, check=True)
    assert json.loads(result.stdout) == {"workers": [None, None], "threads": []}


def test_unused_worker_drains_without_starting_a_thread():
    worker = server._SinkWorker("unused")
    assert worker._thread is None
    assert worker.drain(time.monotonic() - 1)
    assert worker._thread is None


def test_concurrent_first_submissions_start_one_consumer(monkeypatch):
    real_thread = threading.Thread
    created = []

    def thread_factory(*args, **kwargs):
        thread = real_thread(*args, **kwargs)
        if kwargs.get("name") == "log-concurrent":
            created.append(thread)
        return thread

    monkeypatch.setattr(server.threading, "Thread", thread_factory)
    worker = server._SinkWorker("concurrent")
    barrier = threading.Barrier(12)
    written = []
    accepted = []

    def submit(number):
        barrier.wait(timeout=5)
        accepted.append(worker.submit(lambda: written.append(number)))

    producers = [real_thread(target=submit, args=(number,)) for number in range(12)]
    for producer in producers:
        producer.start()
    for producer in producers:
        producer.join(timeout=5)
        assert not producer.is_alive()
    assert accepted == [True] * 12
    assert worker.drain(time.monotonic() + 5)
    assert sorted(written) == list(range(12))
    assert len(created) == 1
    assert created[0].daemon


def test_failed_first_start_leaves_no_pending_write_and_can_retry(monkeypatch, caplog):
    real_thread = threading.Thread
    attempts = []
    written = []

    def fail_start():
        raise RuntimeError("thread unavailable")

    def thread_factory(*args, **kwargs):
        attempts.append(1)
        return SimpleNamespace(start=fail_start) if len(attempts) == 1 else real_thread(*args, **kwargs)

    monkeypatch.setattr(server.threading, "Thread", thread_factory)
    worker = server._SinkWorker("retry")
    assert not worker.submit(lambda: written.append("dropped"))
    assert worker._thread is None
    assert worker.drain(time.monotonic())
    assert "could not start; dropping a write" in caplog.text
    assert worker.submit(lambda: written.append("retried"))
    assert worker.drain(time.monotonic() + 5)
    assert written == ["retried"]
