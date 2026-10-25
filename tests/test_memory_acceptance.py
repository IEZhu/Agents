"""MEM-01: durable journal acceptance and retry semantics, with no model/network."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import AsyncMock

import pytest

from src.memory import history
from src.memory.history import HistoryReader, HistoryWriter, delete_entry


def symlink_or_skip(path, target):
    try:
        path.symlink_to(target)
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows runner lacks the privilege to create symlinks")
        raise


def write(path, **kwargs):
    return HistoryWriter(str(path)).append_entry("intent", "action", "outcome",
                                                 idempotency_key="write-1", **kwargs)


def test_concurrent_retries_return_original_receipt(tmp_path):
    path = tmp_path / "history.md"
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda _: write(path), range(20)))
    assert all(result == results[0] for result in results)
    assert results[0]["state"] == "durably_accepted"
    assert len(HistoryReader(str(path)).journal_entries()) == 1


def test_legacy_torn_entry_is_repaired_before_acknowledgement(tmp_path):
    path = tmp_path / "history.md"
    identifier = HistoryWriter._compute_entry_hash("intent", "action", "outcome")
    path.write_text(f"## 2026-01-01T00:00:00+00:00 | {identifier}\n**Intent:** intent\n")
    result = HistoryWriter(str(path)).append_entry("intent", "action", "outcome")
    entries = HistoryReader(str(path)).journal_entries()
    assert result["status"] == "recorded"
    assert len(entries) == 1 and entries[0].outcome == "outcome"


def test_complete_legacy_id_conflict_is_preserved(tmp_path):
    path = tmp_path / "history.md"
    identifier = HistoryWriter._compute_entry_hash("intent", "action", "outcome")
    path.write_text(f"## 2026-01-01T00:00:00+00:00 | {identifier}\n"
                    "**Intent:** different\n**Action:** action\n**Outcome:** valuable content\n")
    before = path.read_bytes()
    with pytest.raises(history.MemoryWriteRejected, match="history_entry_conflict"):
        HistoryWriter(str(path)).append_entry("intent", "action", "outcome")
    assert path.read_bytes() == before


@pytest.mark.parametrize("changed", [{"files": ["different.py"]}, {"tags": ["different"]},
                                     {"metadata": {"different": True}}])
def test_changed_payload_rejected_without_mutation(tmp_path, changed):
    path = tmp_path / "history.md"
    write(path)
    before = path.read_bytes()
    with pytest.raises(ValueError, match="idempotency_conflict"):
        write(path, **changed)
    assert path.read_bytes() == before


def test_distinct_keys_and_workspace_scopes(tmp_path):
    first = tmp_path / "one" / "history.md"
    second = tmp_path / "two" / "history.md"
    a, b = write(first), write(second)
    assert a["entry_id"] != b["entry_id"]
    c = HistoryWriter(str(first)).append_entry("intent", "action", "outcome", idempotency_key="write-2")
    assert c["entry_id"] != a["entry_id"]
    assert len(HistoryReader(str(first)).journal_entries()) == 2


def test_archived_receipt_survives_restart_without_ttl(tmp_path):
    path = tmp_path / "history.md"
    writer = HistoryWriter(str(path), rotation_kb=0)
    original = writer.append_entry("intent", "action", "outcome", idempotency_key="write-1")
    archive = next((tmp_path / "history").glob("*.md"))
    # Neither mtime nor the live tail bounds the retry window.
    os.utime(archive, (0, 0))
    assert write(path) == original
    assert len(HistoryReader(str(path)).journal_entries()) == 1


def test_pending_rotation_receipt_is_replayed(tmp_path):
    path = tmp_path / "history.md"
    original = write(path)
    path.replace(str(path) + ".rotating")
    assert write(path) == original
    assert len(HistoryReader(str(path)).journal_entries()) == 1


def test_archive_deletions_preserve_adjacent_receipts(tmp_path):
    path = tmp_path / "history.md"
    writer = HistoryWriter(str(path), rotation_kb=0)
    first = writer.append_entry("intent", "action", "outcome", idempotency_key="write-1")
    second = writer.append_entry("other", "action", "outcome", idempotency_key="write-2")
    month = next((tmp_path / "history").glob("*.md")).stem
    for receipt in (first, second):
        delete_entry(str(path), month, receipt["entry_id"], receipt["timestamp"])
    assert write(path) == first
    assert writer.append_entry("other", "action", "outcome", idempotency_key="write-2") == second
    assert HistoryReader(str(path)).journal_entries() == []


@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
def test_deleted_entry_is_not_resurrected_by_delayed_retry(tmp_path, newline):
    path = tmp_path / "history.md"
    original = write(path)
    delete_entry(str(path), None, original["entry_id"], original["timestamp"])
    path.write_bytes(path.read_bytes().replace(b"\n", newline))
    assert write(path) == original
    assert HistoryReader(str(path)).journal_entries() == []
    assert "**Outcome:**" not in path.read_text()
    with pytest.raises(ValueError, match="idempotency_conflict"):
        write(path, tags=["changed"])


@pytest.mark.parametrize("failure", ["replace", "fsync"])
def test_failure_before_commit_keeps_previous_journal(tmp_path, monkeypatch, failure):
    path = tmp_path / "history.md"
    HistoryWriter(str(path)).append_entry("old", "action", "outcome")
    before = path.read_bytes()

    def fail(*args):
        raise OSError("disk unavailable")

    with monkeypatch.context() as patch:
        patch.setattr(history.os, failure, fail)
        with pytest.raises(OSError):
            write(path)
    assert path.read_bytes() == before
    assert write(path)["state"] == "durably_accepted"
    assert len(HistoryReader(str(path)).journal_entries()) == 2


def test_directory_sync_failure_is_uncertain_then_retry_recovers(tmp_path, monkeypatch):
    path = tmp_path / "history.md"
    with monkeypatch.context() as patch:
        patch.setattr(history, "_sync_directory", lambda _: (_ for _ in ()).throw(OSError("sync failed")))
        with pytest.raises(OSError):
            write(path)
    receipt = write(path)
    assert receipt["state"] == "durably_accepted"
    assert len(HistoryReader(str(path)).journal_entries()) == 1


def test_unreadable_archives_fail_closed(tmp_path, monkeypatch):
    path = tmp_path / "history.md"
    HistoryWriter(str(path), rotation_kb=0).append_entry("intent", "action", "outcome", idempotency_key="write-1")
    before = path.read_bytes()
    real_listdir = os.listdir

    def denied(directory):
        if str(directory) == str(tmp_path / "history"):
            raise PermissionError("archive unavailable")
        return real_listdir(directory)

    monkeypatch.setattr(history.os, "listdir", denied)
    with pytest.raises(PermissionError):
        write(path, tags=["changed"])
    assert path.read_bytes() == before


def test_journal_symlink_is_rejected_without_retargeting(tmp_path):
    path, target = tmp_path / "history.md", tmp_path / "real.md"
    target.write_text("original")
    symlink_or_skip(path, target)
    with pytest.raises(ValueError, match="journal_symlink_unsupported"):
        write(path)
    assert path.is_symlink() and target.read_text() == "original"


def test_preexisting_temporary_symlink_cannot_redirect_write(tmp_path):
    path, target = tmp_path / "history.md", tmp_path / "unrelated"
    target.write_text("keep me")
    symlink_or_skip(tmp_path / "history.md.tmp", target)
    assert write(path)["state"] == "durably_accepted"
    assert target.read_text() == "keep me"


def test_windows_receipt_sync_uses_writable_handle_without_changing_content(tmp_path, monkeypatch):
    path = tmp_path / "history.md"
    path.write_bytes(b"accepted entry")
    real_open = open

    def windows_open(filename, mode, **kwargs):
        if "+" not in mode:
            raise PermissionError("FlushFileBuffers requires GENERIC_WRITE")
        return real_open(filename, mode, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(history.os, "name", "nt")
        patch.setattr(history, "open", windows_open, raising=False)
        history._sync_file(str(path))
    assert path.read_bytes() == b"accepted entry"


def test_retry_reschedules_export_without_marking_entry_fresh(tmp_path, monkeypatch):
    path = tmp_path / "history.md"
    calls = []
    monkeypatch.setattr(history, "_appended", lambda *args: calls.append(args))
    with monkeypatch.context() as patch:
        patch.setattr(history, "_sync_directory", lambda _: (_ for _ in ()).throw(OSError("sync failed")))
        with pytest.raises(OSError):
            write(path)
    assert not calls
    write(path)
    assert calls == [(str(path), None)]


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
@pytest.mark.parametrize("extra_bytes", [b"", b"\xff"])
def test_suffix_recovery_syncs_archive_before_removing_pending(tmp_path, monkeypatch, newline, extra_bytes):
    writer = HistoryWriter(str(tmp_path / "history.md"))
    archive_dir = tmp_path / "history"
    archive_dir.mkdir()
    payload = "## 2026-01-02T03:04:05+00:00 | abcdef123456\nENTRY\n".replace("\n", newline)
    archive, pending = archive_dir / "2026-01.md", tmp_path / "history.md.rotating"
    payload_bytes = payload.encode() + extra_bytes
    archive.write_bytes(payload_bytes)
    pending.write_bytes(payload_bytes)
    real_sync = history._sync_directory

    def fail_archive(directory):
        if str(directory) == str(archive_dir):
            raise OSError("archive directory sync failed")
        real_sync(directory)

    with monkeypatch.context() as patch:
        patch.setattr(history, "_sync_directory", fail_archive)
        with pytest.raises(OSError):
            writer._merge_pending_locked(str(pending))
    assert pending.read_bytes() == payload_bytes
    writer._merge_pending_locked(str(pending))
    assert not pending.exists()
    assert archive.read_bytes() == payload_bytes


@pytest.mark.parametrize("existing_archive", [False, True])
def test_failed_archive_sync_keeps_recovery_source_without_duplicate(tmp_path, monkeypatch, existing_archive):
    path = tmp_path / "history.md"
    writer = HistoryWriter(str(path), rotation_kb=0)
    if existing_archive:
        writer.append_entry("older", "a", "o")
    real_sync = history._sync_directory

    def fail_archive(directory):
        if str(directory) == str(tmp_path / "history"):
            raise OSError("archive sync failed")
        real_sync(directory)

    with monkeypatch.context() as patch:
        patch.setattr(history, "_sync_directory", fail_archive)
        original = writer.append_entry("intent", "action", "outcome", idempotency_key="write-1")
    assert original["state"] == "durably_accepted"
    assert Path(str(path) + ".rotating").exists()
    writer.append_entry("later", "a", "o")
    archived = "".join(item.read_text() for item in (tmp_path / "history").glob("*.md"))
    assert archived.count(" | " + original["entry_id"]) == 1
    assert write(path) == original


CHILD = '''
import json, os, sys
from src.memory import history
path, crash = sys.argv[1:]
if crash == "before":
    history.os.replace = lambda *_: os._exit(91)
elif crash == "after":
    history._appended = lambda *_: os._exit(92)
result = history.HistoryWriter(path).append_entry("intent", "action", "outcome", idempotency_key="write-1")
print(json.dumps(result))
'''


def child(path, crash="none"):
    return subprocess.run([sys.executable, "-B", "-c", CHILD, str(path), crash],
                          capture_output=True, text=True, timeout=15)


@pytest.mark.parametrize("crash,code", [("before", 91), ("after", 92)])
def test_real_process_crash_before_response_and_restart(tmp_path, crash, code):
    path = tmp_path / "history.md"
    assert child(path, crash).returncode == code
    if crash == "before":
        assert not path.exists()
    first = child(path)
    second = child(path)
    assert first.returncode == second.returncode == 0, first.stderr + second.stderr
    assert json.loads(first.stdout) == json.loads(second.stdout)
    assert len(HistoryReader(str(path)).journal_entries()) == 1


def test_cross_process_same_key(tmp_path):
    path = tmp_path / "history.md"
    with ThreadPoolExecutor(max_workers=4) as pool:
        outputs = list(pool.map(lambda _: child(path), range(4)))
    assert all(item.returncode == 0 for item in outputs)
    results = [json.loads(item.stdout) for item in outputs]
    assert all(result == results[0] for result in results)
    assert len(HistoryReader(str(path)).journal_entries()) == 1


@pytest.fixture
def server_workspace(tmp_path, monkeypatch):
    import src.server as server
    from src.engine import config
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(tmp_path))
    monkeypatch.setattr(server, "is_langfuse_configured", lambda: False)
    config._reset_client_repo_root_cache()
    yield server, tmp_path
    server.drain_pending_logs(5)
    config._reset_client_repo_root_cache()


@pytest.mark.asyncio
async def test_api_waits_for_commit_replays_timestamp_and_rejects_change(server_workspace, monkeypatch):
    server, path = server_workspace
    # A full telemetry queue must not change durable memory acceptance.
    monkeypatch.setattr(server._langfuse_worker, "submit", lambda _: False)
    args = {"agent_name": "software_engineer", "query": "q", "response_content": "r", "idempotency_key": "turn-1"}
    first = json.loads(await server.log_interaction(**args))
    assert first["history"]["state"] == "durably_accepted"
    assert len(HistoryReader(str(path / "history.md")).journal_entries()) == 1
    second = json.loads(await server.log_interaction(**args))
    assert first["history"] == second["history"]
    assert first["timestamp"] == second["timestamp"]
    conflict = json.loads(await server.log_interaction(**{**args, "files": ["different.py"]}))
    assert conflict["history"] == {"status": "error", "state": "rejected", "error": "idempotency_conflict"}


@pytest.mark.asyncio
async def test_api_retry_accepts_persona_model_and_equivalent_object(server_workspace):
    from src.schemas.protocol import PersonaDescriptor

    server, path = server_workspace
    persona = PersonaDescriptor(
        agent="software_engineer", activation_id="test", bundle_revision="a" * 64,
        scope="Memory acceptance", skills_loaded=[], implants_loaded=[], rules_loaded=[],
    )
    args = dict(agent_name=persona.agent, query="q", response_content="r",
                persona_action="keep", idempotency_key="typed-persona")
    first = json.loads(await server.log_interaction(**args, persona=persona))
    replay = json.loads(await server.log_interaction(**args, persona=persona.model_dump()))
    assert first["history"]["state"] == "durably_accepted"
    assert replay["history"] == first["history"]
    assert len(HistoryReader(str(path / "history.md")).journal_entries()) == 1
    changed = {**persona.model_dump(), "activation_id": "different"}
    conflict = json.loads(await server.log_interaction(**args, persona=changed))
    assert conflict["history"]["error"] == "idempotency_conflict"


@pytest.mark.asyncio
async def test_full_memory_queue_rejects_without_writing(server_workspace, monkeypatch):
    server, path = server_workspace
    monkeypatch.setattr(server._history_worker, "submit", lambda _: False)
    result = json.loads(await server.log_interaction("software_engineer", "q", "r", idempotency_key="key"))
    assert result["history"] == {"status": "error", "state": "rejected", "error": "history_busy", "retryable": True}
    assert not (path / "history.md").exists()


@pytest.mark.asyncio
async def test_stuck_writes_have_bounded_admission_and_wait(server_workspace, monkeypatch):
    from src.daemon.app import MAX_INFLIGHT
    from src.daemon.execution import finish_jobs, request_jobs
    server, path = server_workspace
    release = threading.Event()
    real_append = HistoryWriter.append_entry
    monkeypatch.setattr(server, "HISTORY_WAIT_SECONDS", 0.05, raising=False)

    def stuck(self, *args, **kwargs):
        assert release.wait(5)
        return real_append(self, *args, **kwargs)

    monkeypatch.setattr(HistoryWriter, "append_entry", stuck)
    jobs = []
    token = request_jobs.set(jobs)
    try:
        tasks = [asyncio.create_task(server.log_interaction("software_engineer", str(i), "r",
                                                          idempotency_key=str(i))) for i in range(40)]
        results = [json.loads(value) for value in await asyncio.wait_for(asyncio.gather(*tasks), 1)]
        assert 0 < len(jobs) <= 8 < MAX_INFLIGHT
        assert sum(item["history"]["error"] == "history_busy" for item in results) >= 32
        assert any(item["history"]["error"] == "history_timeout" for item in results)
        assert all(item["history"]["retryable"] for item in results)
        assert all("same idempotency_key" in item["instruction"] for item in results)
    finally:
        release.set()
        await finish_jobs(jobs)
        request_jobs.reset(token)
    assert len(HistoryReader(str(path / "history.md")).journal_entries()) <= 8


@pytest.mark.asyncio
async def test_running_write_survives_client_cancellation_and_is_tracked(server_workspace, monkeypatch):
    from src.daemon.execution import finish_jobs, request_jobs
    server, path = server_workspace
    started, release = threading.Event(), threading.Event()
    original = HistoryWriter.append_entry

    def blocked(self, *args, **kwargs):
        assert threading.current_thread().name == "log-history"
        started.set()
        assert release.wait(5)
        return original(self, *args, **kwargs)

    monkeypatch.setattr(HistoryWriter, "append_entry", blocked)
    jobs = []
    token = request_jobs.set(jobs)
    args = dict(agent_name="software_engineer", query="q", response_content="r", idempotency_key="key")
    try:
        task = asyncio.create_task(server.log_interaction(**args))
        assert await asyncio.to_thread(started.wait, 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len(jobs) == 1 and jobs[0].running()
        release.set()
        await finish_jobs(jobs)
        receipt = json.loads(await server.log_interaction(**args))["history"]
        assert receipt["state"] == "durably_accepted"
        assert len(HistoryReader(str(path / "history.md")).journal_entries()) == 1
    finally:
        release.set()
        await finish_jobs(jobs)
        request_jobs.reset(token)


@pytest.mark.asyncio
@pytest.mark.parametrize("newline", [b"\n", b"\r\n"])
async def test_malformed_receipt_is_storage_failure_not_semantic_rejection(server_workspace, newline):
    server, path = server_workspace
    (path / "history.md").write_bytes(b"<!-- agents-write-receipt {invalid} -->" + newline)
    result = json.loads(await server.log_interaction("software_engineer", "q", "r", idempotency_key="key"))
    assert result["history"] == {"status": "error", "state": "unknown", "error": "history_unwritable"}


def test_crlf_bytes_are_preserved_and_receipt_has_no_local_path(tmp_path):
    path = tmp_path / "history.md"
    original = b"---\r\nformat_version: 1\r\n---\r\n"
    path.write_bytes(original)
    write(path)
    assert path.read_bytes().startswith(original)
    receipt = HistoryReader(str(path)).journal_entries()[0].metadata["_agents_write"]
    assert str(tmp_path) not in json.dumps(receipt)


@pytest.mark.asyncio
async def test_keyed_write_refuses_unauthorized_workspace(server_workspace, monkeypatch):
    server, path = server_workspace
    monkeypatch.setattr(server, "resolve_client_context", AsyncMock(side_effect=ValueError("workspace_invalid")))
    result = json.loads(await server.log_interaction("software_engineer", "q", "r", idempotency_key="key", persona_action="keep"))
    assert result["status"] == "ERROR"
    assert "timestamp" not in result
    assert not (path / "history.md").exists()


@pytest.mark.asyncio
async def test_api_reports_failure_in_current_call(server_workspace, monkeypatch):
    server, _ = server_workspace
    monkeypatch.setattr(history, "_replace", lambda *_: (_ for _ in ()).throw(OSError("full disk")))
    result = json.loads(await server.log_interaction("software_engineer", "q", "r", idempotency_key="key", persona_action="keep"))
    assert result["history"]["status"] == "error"
    assert result["history"]["state"] == "unknown"
    assert result["history_last_error"]["code"] == "history_unwritable"
    assert "same idempotency_key" in result["instruction"]
    assert "The turn was logged" not in result["instruction"]
    assert "Do not retry logging" not in result["instruction"]
