"""Persistent artifact behavior through the bounded asynchronous service."""
import asyncio
import json
import logging
import os
import threading

import pytest

from src.artifacts.service import ArtifactService
from src.artifacts.store import ArtifactError
from src.daemon.workspaces import ClientContext


def workspace(tmp_path, name="repo"):
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    return ClientContext("artifact-request", "http", name, root.resolve())


def reference(manifest):
    return {"manifest_id": manifest["manifest_id"], "manifest_sha256": manifest["sha256"]}


async def publish(service, client, content="exact artifact text", **updates):
    result = await service.call("artifact_publish", client,
                                {"name": "fixture.txt", "content": content, **updates})
    assert result["schema"] == "agents-core/artifacts/1" and result["status"] == "ok", result
    return result["data"]


async def manifest_for(service, client, artifact, **updates):
    result = await service.call("artifact_manifest_create", client,
        {"task_id": "task-1", "entries": [{"artifact_id": artifact["artifact_id"],
         "sha256": artifact["sha256"], "purpose": "Exact shared evidence"}], **updates})
    assert result["status"] == "ok", result
    return result["data"]


@pytest.mark.asyncio
async def test_capture_publish_manifest_read_verify_release_survive_new_service(tmp_path):
    client = workspace(tmp_path)
    source = client.root / "input.md"
    original = "αβγ\nExact JSON-looking text: {\"data\":[1,true]}\n"
    source.write_text(original, encoding="utf-8")
    directory = (tmp_path / "artifacts").resolve()
    service = ArtifactService(directory)
    artifact = await publish(service, client, original, name="input.md", media_type="text/markdown")
    captured = await service.call("artifact_publish", client,
        {"name": "input.md", "source_path": "input.md", "media_type": "text/markdown"})
    if os.name == "posix":
        assert captured["status"] == "ok" and captured["data"] == artifact, captured
    else:
        assert captured["error"]["code"] == "unsupported", captured
    pinned = await manifest_for(service, client, artifact)
    source.write_text("changed after capture", encoding="utf-8")
    duplicate = await publish(service, client, original, name="later-name.txt")
    assert duplicate == artifact  # First descriptor persists along with the immutable bytes.
    await service.close()

    fresh = ArtifactService(directory)
    try:
        assert (await fresh.call("artifact_manifest_get", client, reference(pinned)))["data"] == pinned
        assert (await fresh.verify_snapshot(client, pinned)) == pinned
        pieces, offset = [], 0
        while True:
            result = await fresh.call("artifact_read", client,
                {**reference(pinned), "artifact_id": artifact["artifact_id"], "offset": offset, "max_bytes": 5})
            assert result["status"] == "ok", result
            chunk = result["data"]
            assert len(chunk["text"].encode("utf-8")) <= 5
            assert chunk["offset"] == offset
            pieces.append(chunk["text"])
            if chunk["eof"]:
                assert chunk["next_offset"] == len(original.encode("utf-8"))
                break
            assert chunk["next_offset"] > offset
            offset = chunk["next_offset"]
        assert "".join(pieces) == original
        verified = await fresh.call("artifact_manifest_verify", client, reference(pinned))
        assert verified["data"] == {"verified": True, "manifest_id": pinned["manifest_id"],
                                    "sha256": pinned["sha256"], "artifacts": [artifact]}
        for _ in range(2):
            assert (await fresh.call("artifact_manifest_release", client, reference(pinned)))["data"]["released"]
    finally:
        await fresh.close()

    restarted = ArtifactService(directory)
    try:
        for name in ("artifact_manifest_get", "artifact_manifest_verify"):
            assert (await restarted.call(name, client, reference(pinned)))["error"]["code"] == "artifact_unavailable"
        # Release revokes authorization, rather than deleting durable content.
        replacement = await manifest_for(restarted, client, artifact)
        assert (await restarted.call("artifact_read", client,
            {**reference(replacement), "artifact_id": artifact["artifact_id"]}))["data"]["text"] == original
    finally:
        await restarted.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", ["workspace_required", "workspace_invalid", "workspace_unsafe"])
async def test_workspace_refusal_does_not_touch_storage(tmp_path, code):
    service = ArtifactService(tmp_path / "must-not-exist")
    client = ClientContext("request", "http", error=code + ": private-path")
    try:
        result = await service.call("artifact_publish", client, {"name": "x", "content": "private-body"})
        assert result["error"]["code"] == code
        assert "private" not in json.dumps(result)
        assert not service.directory.exists()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_manifest_cannot_cross_workspaces_and_capture_refuses_escaping_paths(tmp_path):
    service = ArtifactService((tmp_path / "store").resolve())
    client, other = workspace(tmp_path), workspace(tmp_path, "other")
    try:
        artifact = await publish(service, client)
        pinned = await manifest_for(service, client, artifact)
        denied = await service.call("artifact_read", other,
            {**reference(pinned), "artifact_id": artifact["artifact_id"]})
        assert denied["error"]["code"] == "artifact_unavailable"
        for path in ("../private.txt", str(client.root / "private.txt"), "a/../private.txt"):
            result = await service.call("artifact_publish", client, {"name": "x", "source_path": path})
            assert result["error"]["code"] == ("permission_denied" if os.name == "posix" else "unsupported")
            assert path not in json.dumps(result)
        if os.name == "posix":
            outside = tmp_path / "private.txt"
            outside.write_text("must not capture")
            (client.root / "linked.txt").symlink_to(outside)
            denied = await service.call("artifact_publish", client, {"name": "x", "source_path": "linked.txt"})
            assert denied["error"]["code"] == "permission_denied"
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("arguments", [
    {"name": "x", "content": {"sentinel-private": True}},
    {"name": "x", "content": "secret", "source_path": "secret"},
    {"name": "x", "content": "secret", "unexpected": "sentinel-private"},
    {"name": True, "content": "sentinel-private"},
])
async def test_service_rejects_raw_types_and_unknown_fields_without_echo(tmp_path, arguments):
    service = ArtifactService(tmp_path / "store")
    try:
        with pytest.raises(ValueError, match=r"^Invalid artifact arguments\.$") as caught:
            await service.call("artifact_publish", workspace(tmp_path), arguments)
        assert "sentinel-private" not in str(caught.value)
        assert not service.directory.exists()
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_storage_failures_do_not_echo_paths_content_or_credentials(tmp_path, monkeypatch, caplog):
    client = workspace(tmp_path)
    service = ArtifactService((tmp_path / "store").resolve())
    secret = "sentinel-content-path-and-credential"
    def unavailable(*args, **kwargs):
        raise OSError(secret)
    monkeypatch.setattr(service.store, "publish", unavailable)
    caplog.set_level(logging.DEBUG)
    try:
        result = await service.call("artifact_publish", client, {"name": "x", "content": secret})
        assert result["error"]["code"] == "artifact_unavailable"
        assert secret not in json.dumps(result) + caplog.text
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_hydration_reads_all_utf8_chunks_and_accounts_for_framing(tmp_path):
    service = ArtifactService((tmp_path / "store").resolve())
    client = workspace(tmp_path)
    body = "é🙂exact\n" * 9000
    try:
        artifact = await publish(service, client, body)
        pinned = await manifest_for(service, client, artifact)
        expected = (f"\nArtifact {artifact['artifact_id']} SHA256 {artifact['sha256']} ({artifact['media_type']}):\n" + body)
        allowance = len(expected.encode("utf-8"))
        assert await service.hydrate(client, pinned, allowance) == expected
        with pytest.raises(ArtifactError) as caught:
            await service.hydrate(client, pinned, allowance - 1)
        assert caught.value.code == "input_too_large"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_cancelled_file_waiters_retain_slots_until_actual_work_finishes(tmp_path):
    service = ArtifactService(tmp_path / "store")
    loop = asyncio.get_running_loop()
    started = [asyncio.Event() for _ in range(5)]
    release = threading.Event()
    def blocking(index):
        loop.call_soon_threadsafe(started[index].set)
        if not release.wait(timeout=5):
            raise AssertionError("Test did not release admitted file work")
        return index
    tasks = [asyncio.create_task(service.run(blocking, index)) for index in range(4)]
    extra = None
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started[:4])), 2)
        assert service.pending == 4
        tasks[0].cancel()
        with pytest.raises(asyncio.CancelledError):
            await tasks[0]
        assert service.pending == 4
        extra = asyncio.create_task(service.run(blocking, 4))
        await asyncio.sleep(0)  # Scheduling barrier only; file completion is event-controlled.
        assert not started[4].is_set() and service.pending == 4
        extra.cancel()
        with pytest.raises(asyncio.CancelledError):
            await extra
        assert service.pending == 4 and not started[4].is_set()
    finally:
        release.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        if extra is not None:
            await asyncio.gather(extra, return_exceptions=True)
        await service.close()
    assert service.pending == 0
    with pytest.raises(ArtifactError) as caught:
        await service.run(lambda: pytest.fail("Closed service dispatched work"))
    assert caught.value.code == "artifact_unavailable"
