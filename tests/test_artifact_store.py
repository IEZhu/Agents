"""Real filesystem coverage for durable artifact identity and trust boundaries."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import multiprocessing
import os
from pathlib import Path
import stat
import time
from types import SimpleNamespace

import pytest

from src.artifacts import ArtifactError, ArtifactStore
import src.artifacts.store as storage


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    return str(root.resolve())


def _entry(artifact, **changes):
    return {"artifact_id": artifact["artifact_id"], "sha256": artifact["sha256"],
            "required": True, "purpose": "Read the pinned task input.", **changes}


def _manifest(store, workspace, artifact, **kwargs):
    return store.create_manifest(workspace, "task-1", [_entry(artifact)], **kwargs)


def _namespace(store, workspace):
    return store.directory / hashlib.sha256(workspace.encode()).hexdigest()


def _artifact_path(store, workspace, artifact):
    return _namespace(store, workspace) / "artifacts" / artifact["artifact_id"]


def _retry(function):
    deadline = time.monotonic() + 10
    while True:
        try:
            return function()
        except ArtifactError as error:
            if error.code != "store_busy" or time.monotonic() >= deadline:
                raise
            time.sleep(0.01)


def _process_publish(directory, workspace, content, queue):
    try:
        result = _retry(lambda: ArtifactStore(directory).publish(workspace, content, name="worker.txt",
                                                                  media_type="text/plain"))
        queue.put(("ok", result))
    except Exception as error:
        queue.put(("error", repr(error)))


def test_persisted_identity_manifest_and_private_modes(tmp_path, workspace):
    first = ArtifactStore(tmp_path / "store")
    artifact = first.publish(workspace, b"persisted text", name="original.md", media_type="text/markdown")
    manifest = _manifest(first, workspace, artifact)

    restarted = ArtifactStore(first.directory)
    assert restarted.publish(workspace, b"persisted text", name="renamed.txt", media_type="text/plain") == artifact
    assert restarted.get_manifest(workspace, manifest["manifest_id"], manifest["sha256"]) == manifest
    assert restarted.verify(workspace, manifest["manifest_id"], manifest["sha256"]) == {
        "verified": True, "manifest_id": manifest["manifest_id"], "sha256": manifest["sha256"],
        "artifacts": [artifact],
    }
    assert restarted.read(workspace, manifest["manifest_id"], manifest["sha256"], artifact["artifact_id"])["text"] == "persisted text"
    paths = [first.directory, *first.directory.rglob("*")]
    if os.name == "posix":
        for path in paths:
            assert stat.S_IMODE(path.stat().st_mode) == (0o700 if path.is_dir() else 0o600)
    elif os.name == "nt":
        from src.daemon import acl
        for path in paths:
            assert acl.owned_by_user_or_admins(path)
            assert acl.is_private(path, inherited_below=path.is_dir())


def test_binary_storage_preserves_utf8_control_bytes(tmp_path, workspace):
    store = ArtifactStore(tmp_path / "store")
    content = b"first\r\nsecond\x1afinal"
    artifact = store.publish(workspace, content, name="input", media_type="text/plain")
    manifest = _manifest(store, workspace, artifact)
    result = ArtifactStore(store.directory).read(workspace, manifest["manifest_id"], manifest["sha256"], artifact["artifact_id"])
    assert result["text"].encode("utf-8") == content
    assert result["artifact"]["sha256"] == hashlib.sha256(content).hexdigest()


def test_workspace_isolation_and_membership(tmp_path, workspace):
    store = ArtifactStore(tmp_path / "store")
    member = store.publish(workspace, b"member", name="one", media_type="text/plain")
    other = store.publish(workspace, b"other", name="two", media_type="text/plain")
    manifest = _manifest(store, workspace, member)
    for operation in (
        lambda: store.get_manifest(str(tmp_path / "foreign"), manifest["manifest_id"]),
        lambda: store.verify(str(tmp_path / "foreign"), manifest["manifest_id"], manifest["sha256"]),
        lambda: store.read(workspace, manifest["manifest_id"], manifest["sha256"], other["artifact_id"]),
    ):
        with pytest.raises(ArtifactError) as caught:
            operation()
        assert caught.value.code == "artifact_unavailable"
        assert workspace not in str(caught.value)


def test_manifest_pins_content_hash_and_original_descriptor(tmp_path, workspace):
    store = ArtifactStore(tmp_path / "store")
    artifact = store.publish(workspace, b"original", name="input", media_type="text/plain")
    with pytest.raises(ArtifactError) as caught:
        store.create_manifest(workspace, "task", [_entry(artifact, sha256="0" * 64)])
    assert caught.value.code == "integrity_error"
    manifest = _manifest(store, workspace, artifact)
    metadata_path = _artifact_path(store, workspace, artifact) / "metadata.json"
    metadata = json.loads(metadata_path.read_bytes())
    metadata["name"] = "changed"
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    with pytest.raises(ArtifactError) as caught:
        store.verify(workspace, manifest["manifest_id"], manifest["sha256"])
    assert caught.value.code == "integrity_error"


def test_even_small_reads_verify_the_entire_artifact(tmp_path, workspace):
    store = ArtifactStore(tmp_path / "store")
    artifact = store.publish(workspace, b"good content", name="input", media_type="text/plain")
    manifest = _manifest(store, workspace, artifact)
    (_artifact_path(store, workspace, artifact) / "content").write_bytes(b"good corrup!")
    for operation in (
        lambda: store.verify(workspace, manifest["manifest_id"], manifest["sha256"]),
        lambda: store.read(workspace, manifest["manifest_id"], manifest["sha256"], artifact["artifact_id"], max_bytes=4),
        lambda: store.publish(workspace, b"good content", name="input", media_type="text/plain"),
    ):
        with pytest.raises(ArtifactError) as caught:
            operation()
        assert caught.value.code == "integrity_error"


def test_new_publication_accounts_for_metadata_without_opening_unrelated_content(tmp_path, workspace, monkeypatch):
    store = ArtifactStore(tmp_path / "store")
    artifacts = [store.publish(workspace, content, name=f"input-{index}", media_type="text/plain")
                 for index, content in enumerate((b"first", b"second", b"third"))]
    content_paths = {_artifact_path(store, workspace, artifact) / "content" for artifact in artifacts}
    metadata_paths = {_artifact_path(store, workspace, artifact) / "metadata.json" for artifact in artifacts}
    opened_metadata = set()
    real_open = storage.os.open
    def guard_unrelated_content(path, flags, *args, **kwargs):
        path = Path(path)
        assert path not in content_paths, "quota accounting must not open unrelated artifact content"
        if path in metadata_paths:
            opened_metadata.add(path)
        return real_open(path, flags, *args, **kwargs)
    monkeypatch.setattr(storage.os, "open", guard_unrelated_content)

    published = ArtifactStore(store.directory).publish(workspace, b"new text", name="new", media_type="text/plain")
    assert published["sha256"] == hashlib.sha256(b"new text").hexdigest()
    assert opened_metadata == metadata_paths


@pytest.mark.parametrize("corruption", [
    "metadata_json", "metadata_extra", "metadata_hash", "metadata_id", "metadata_oversized",
    "metadata_size", "missing_content", "missing_metadata", "extra_file", "content_symlink",
    "content_hardlink", "content_directory", "content_fifo", "content_mode", "metadata_mode",
    "content_shorter", "content_longer",
])
def test_new_publication_rejects_invalid_existing_artifact_accounting(tmp_path, workspace, corruption):
    if os.name != "posix" and corruption in {"content_fifo", "content_mode", "metadata_mode"}:
        pytest.skip("POSIX special-file and mode policy; portable ACL and reparse checks have separate coverage")
    store = ArtifactStore(tmp_path / "store")
    artifact = store.publish(workspace, b"stored", name="existing", media_type="text/plain")
    directory = _artifact_path(store, workspace, artifact)
    metadata_path = directory / "metadata.json"
    content_path = directory / "content"
    outside = tmp_path / "outside"
    _private_bytes(outside, b"stored")
    if corruption == "metadata_json":
        metadata_path.write_bytes(b'{"artifact_id":')
    elif corruption == "metadata_oversized":
        metadata_path.write_bytes(b" " * (storage._MAX_METADATA_BYTES + 1))
    elif corruption.startswith("metadata_") and corruption != "metadata_mode":
        metadata = json.loads(metadata_path.read_bytes())
        if corruption == "metadata_extra":
            metadata["unexpected"] = "field"
        elif corruption == "metadata_hash":
            metadata["sha256"] = "0" * 64
        elif corruption == "metadata_id":
            # The descriptor is internally consistent but belongs to another ID.
            metadata["sha256"] = "0" * 64
            metadata["artifact_id"] = "art_" + "0" * 64
        elif corruption == "metadata_size":
            metadata["size_bytes"] += 1
        metadata_path.write_bytes(storage._canonical(metadata))
    elif corruption == "missing_metadata":
        metadata_path.unlink()
    elif corruption == "extra_file":
        _private_bytes(directory / "unexpected", b"extra")
    elif corruption == "content_mode":
        content_path.chmod(0o644)
    elif corruption == "metadata_mode":
        metadata_path.chmod(0o644)
    else:
        content_path.unlink()
        if corruption == "content_symlink":
            _symlink_or_skip(content_path, outside)
        elif corruption == "content_hardlink":
            os.link(outside, content_path)
        elif corruption == "content_directory":
            content_path.mkdir(mode=0o700)
        elif corruption == "content_fifo":
            os.mkfifo(content_path, mode=0o600)
        elif corruption == "content_shorter":
            _private_bytes(content_path, b"short")
        elif corruption == "content_longer":
            _private_bytes(content_path, b"longer than recorded")

    with pytest.raises(ArtifactError) as caught:
        ArtifactStore(store.directory).publish(workspace, b"new text", name="new", media_type="text/plain")
    assert caught.value.code in {"integrity_error", "permission_denied", "quota_exceeded"}
    new_id = "art_" + hashlib.sha256(b"new text").hexdigest()
    assert not (_namespace(store, workspace) / "artifacts" / new_id).exists()
    assert outside.read_bytes() == b"stored"


def test_metadata_only_quota_scan_preserves_full_corruption_checks_on_consumption(tmp_path, workspace):
    store = ArtifactStore(tmp_path / "store")
    original = b"good content"
    corrupted = b"good corrup!"
    assert len(original) == len(corrupted)
    artifact = store.publish(workspace, original, name="existing", media_type="text/plain")
    manifest = _manifest(store, workspace, artifact)
    (_artifact_path(store, workspace, artifact) / "content").write_bytes(corrupted)

    # Quotas account for exact file sizes; byte integrity is checked when consumed.
    store.publish(workspace, b"unrelated", name="new", media_type="text/plain")
    restarted = ArtifactStore(store.directory)
    for operation in (
        lambda: restarted.read(workspace, manifest["manifest_id"], manifest["sha256"], artifact["artifact_id"]),
        lambda: restarted.verify(workspace, manifest["manifest_id"], manifest["sha256"]),
        lambda: restarted.publish(workspace, original, name="duplicate", media_type="text/plain"),
        lambda: _manifest(restarted, workspace, artifact),
    ):
        with pytest.raises(ArtifactError) as caught:
            operation()
        assert caught.value.code == "integrity_error"


def test_manifest_corruption_and_expected_hash_are_rejected(tmp_path, workspace):
    store = ArtifactStore(tmp_path / "store")
    artifact = store.publish(workspace, b"text", name="input", media_type="text/plain")
    manifest = _manifest(store, workspace, artifact)
    with pytest.raises(ArtifactError) as caught:
        store.get_manifest(workspace, manifest["manifest_id"], "0" * 64)
    assert caught.value.code == "integrity_error"
    path = _namespace(store, workspace) / "manifests" / (manifest["manifest_id"] + ".json")
    value = json.loads(path.read_bytes())
    value["task_id"] = "tampered"
    path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(ArtifactError) as caught:
        store.get_manifest(workspace, manifest["manifest_id"])
    assert caught.value.code == "integrity_error"


def test_utf8_byte_ranges_resume_without_splitting_characters(tmp_path, workspace):
    store = ArtifactStore(tmp_path / "store")
    text = "ab😀é漢z"
    artifact = store.publish(workspace, text.encode(), name="unicode", media_type="text/plain")
    manifest = _manifest(store, workspace, artifact)
    offset = 0
    pieces = []
    while True:
        result = store.read(workspace, manifest["manifest_id"], manifest["sha256"], artifact["artifact_id"], offset=offset, max_bytes=4)
        assert result["offset"] == offset
        assert len(result["text"].encode()) <= 4
        assert result["next_offset"] > offset
        pieces.append(result["text"])
        offset = result["next_offset"]
        if result["eof"]:
            break
    assert pieces == ["ab", "😀", "é", "漢z"]
    assert "".join(pieces) == text
    assert offset == artifact["size_bytes"]
    assert store.read(workspace, manifest["manifest_id"], manifest["sha256"], artifact["artifact_id"], offset=offset)["text"] == ""
    for kwargs in ({"offset": 3}, {"offset": offset + 1}, {"offset": -1}, {"offset": True}, {"max_bytes": 3}, {"max_bytes": 65537}):
        with pytest.raises(ArtifactError) as caught:
            store.read(workspace, manifest["manifest_id"], manifest["sha256"], artifact["artifact_id"], **kwargs)
        assert caught.value.code == "invalid_input"


def test_empty_text_is_a_valid_artifact(tmp_path, workspace):
    store = ArtifactStore(tmp_path / "store")
    artifact = store.publish(workspace, b"", name="empty", media_type="text/plain")
    manifest = _manifest(store, workspace, artifact)
    result = store.read(workspace, manifest["manifest_id"], manifest["sha256"], artifact["artifact_id"])
    assert (result["text"], result["next_offset"], result["eof"]) == ("", 0, True)


@pytest.mark.parametrize("content,media_type", [
    (b"\xff", "text/plain"), (b"NaN", "application/json"),
    (b"Infinity", "application/json"), (b"-Infinity", "application/json"),
    (b"1e9999", "application/json"), (b'{"a":}', "application/json"),
    (b"text", "application/octet-stream"), ("not bytes", "text/plain"),
])
def test_content_validation(tmp_path, workspace, content, media_type):
    with pytest.raises(ArtifactError) as caught:
        ArtifactStore(tmp_path / "store").publish(workspace, content, name="input", media_type=media_type)
    assert caught.value.code == "invalid_input"


@pytest.mark.parametrize("name", ["", " ", "\t", "\u00a0", "a" * 121, "a/b", "a\\b", "line\nfeed", "nul\x00", "\ud800", "a\u2028b", "a\u2029b"])
def test_descriptor_name_validation(tmp_path, workspace, name):
    with pytest.raises(ArtifactError) as caught:
        ArtifactStore(tmp_path / "store").publish(workspace, b"text", name=name, media_type="text/plain")
    assert caught.value.code == "invalid_input"


@pytest.mark.parametrize("name", ["ordinary.txt", " spaced name ", "format\u202ename", "joined\u200dname"])
def test_accepted_names_match_shared_artifact_schema(tmp_path, workspace, name):
    from src.schemas.artifacts import ArtifactDescriptor
    artifact = ArtifactStore(tmp_path / "store").publish(workspace, b"text", name=name, media_type="text/plain")
    assert ArtifactDescriptor.model_validate(artifact).name == name


@pytest.mark.parametrize("purpose", [" ", "\t\n", "\u00a0", "\u2028\u2029"])
def test_manifest_purpose_must_not_be_blank(tmp_path, workspace, purpose):
    store = ArtifactStore(tmp_path / "store")
    artifact = store.publish(workspace, b"text", name="input", media_type="text/plain")
    with pytest.raises(ArtifactError) as caught:
        store.create_manifest(workspace, "task", [_entry(artifact, purpose=purpose)])
    assert caught.value.code == "invalid_input"


def test_saved_blank_purpose_is_rejected_even_with_recomputed_hash(tmp_path, workspace):
    store = ArtifactStore(tmp_path / "store")
    artifact = store.publish(workspace, b"text", name="input", media_type="text/plain")
    manifest = _manifest(store, workspace, artifact)
    manifest["entries"][0]["purpose"] = " \t"
    manifest["sha256"] = hashlib.sha256(storage._canonical({key: value for key, value in manifest.items() if key != "sha256"})).hexdigest()
    path = _namespace(store, workspace) / "manifests" / (manifest["manifest_id"] + ".json")
    path.write_bytes(storage._canonical(manifest))
    with pytest.raises(ArtifactError) as caught:
        ArtifactStore(store.directory).get_manifest(workspace, manifest["manifest_id"], manifest["sha256"])
    assert caught.value.code == "integrity_error"


def test_manifest_entry_and_limit_validation(tmp_path, workspace):
    store = ArtifactStore(tmp_path / "store")
    artifact = store.publish(workspace, b"text", name="input", media_type="text/plain")
    for entries in ([], [_entry(artifact), _entry(artifact)], [_entry(artifact, required=1)],
                    [_entry(artifact, purpose="")], [_entry(artifact, extra="unexpected")]):
        with pytest.raises(ArtifactError) as caught:
            store.create_manifest(workspace, "task", entries)
        assert caught.value.code == "invalid_input"
    for ttl in (29, 604801, float("nan"), float("inf"), True, 10 ** 1000):
        with pytest.raises(ArtifactError) as caught:
            _manifest(store, workspace, artifact, ttl_seconds=ttl)
        assert caught.value.code == "invalid_input"


def test_byte_count_and_manifest_quotas_survive_restart(tmp_path, workspace):
    directory = tmp_path / "store"
    settings = dict(max_artifact_bytes=4, max_workspace_bytes=7, max_artifacts=2, max_manifests=1)
    store = ArtifactStore(directory, **settings)
    artifact = store.publish(workspace, b"1234", name="one", media_type="text/plain")
    with pytest.raises(ArtifactError) as caught:
        store.publish(workspace, b"12345", name="big", media_type="text/plain")
    assert caught.value.code == "artifact_too_large"
    with pytest.raises(ArtifactError) as caught:
        store.publish(workspace, b"abcd", name="overflow", media_type="text/plain")
    assert caught.value.code == "quota_exceeded"
    store.publish(workspace, b"abc", name="two", media_type="text/plain")
    restarted = ArtifactStore(directory, **settings)
    assert restarted.publish(workspace, b"1234", name="duplicate", media_type="text/plain") == artifact
    with pytest.raises(ArtifactError) as caught:
        restarted.publish(workspace, b"", name="three", media_type="text/plain")
    assert caught.value.code == "quota_exceeded"
    manifest = _manifest(restarted, workspace, artifact)
    restarted.release_manifest(workspace, manifest["manifest_id"], manifest["sha256"])
    with pytest.raises(ArtifactError) as caught:
        _manifest(ArtifactStore(directory, **settings), workspace, artifact)
    assert caught.value.code == "quota_exceeded"


def test_expiry_release_and_immutable_underlying_artifact(tmp_path, workspace):
    now = [1700000000.0]
    store = ArtifactStore(tmp_path / "store", clock=lambda: now[0])
    artifact = store.publish(workspace, b"keep this", name="input", media_type="text/plain")
    manifest = _manifest(store, workspace, artifact, ttl_seconds=30)
    now[0] += 30
    for operation in (
        lambda: store.get_manifest(workspace, manifest["manifest_id"]),
        lambda: store.read(workspace, manifest["manifest_id"], manifest["sha256"], artifact["artifact_id"]),
        lambda: store.verify(workspace, manifest["manifest_id"], manifest["sha256"]),
    ):
        with pytest.raises(ArtifactError) as caught:
            operation()
        assert caught.value.code == "artifact_unavailable"
    assert store.release_manifest(workspace, manifest["manifest_id"], manifest["sha256"])["released"] is True
    assert store.release_manifest(workspace, manifest["manifest_id"], manifest["sha256"])["released"] is True
    with pytest.raises(ArtifactError) as caught:
        store.release_manifest(workspace, manifest["manifest_id"], "0" * 64)
    assert caught.value.code == "integrity_error"
    replacement = _manifest(store, workspace, artifact)
    restarted = ArtifactStore(store.directory, clock=lambda: now[0])
    assert restarted.read(workspace, replacement["manifest_id"], replacement["sha256"], artifact["artifact_id"])["text"] == "keep this"


def test_release_is_persistent_and_idempotent(tmp_path, workspace):
    store = ArtifactStore(tmp_path / "store")
    artifact = store.publish(workspace, b"text", name="input", media_type="text/plain")
    manifest = _manifest(store, workspace, artifact)
    expected = {"manifest_id": manifest["manifest_id"], "released": True}
    assert store.release_manifest(workspace, manifest["manifest_id"], manifest["sha256"]) == expected
    restarted = ArtifactStore(store.directory)
    assert restarted.release_manifest(workspace, manifest["manifest_id"], manifest["sha256"]) == expected
    with pytest.raises(ArtifactError) as caught:
        restarted.get_manifest(workspace, manifest["manifest_id"])
    assert caught.value.code == "artifact_unavailable"
    assert restarted.publish(workspace, b"text", name="other", media_type="text/plain") == artifact


@pytest.mark.parametrize("clock_value", [float("nan"), float("inf"), 10 ** 1000, "bad", True])
def test_invalid_clock_is_a_generic_error(tmp_path, workspace, clock_value):
    store = ArtifactStore(tmp_path / "store", clock=lambda: clock_value)
    artifact = store.publish(workspace, b"text", name="input", media_type="text/plain")
    with pytest.raises(ArtifactError) as caught:
        _manifest(store, workspace, artifact)
    assert caught.value.code == "invalid_input"


def test_threaded_instances_publish_one_atomic_descriptor(tmp_path, workspace):
    directory = tmp_path / "store"
    def publish(index):
        return _retry(lambda: ArtifactStore(directory).publish(workspace, b"same text", name=f"worker-{index}", media_type="text/plain"))
    with ThreadPoolExecutor(max_workers=8) as pool:
        descriptors = list(pool.map(publish, range(16)))
    assert all(descriptor == descriptors[0] for descriptor in descriptors)
    assert len(list((_namespace(ArtifactStore(directory), workspace) / "artifacts").iterdir())) == 1


def test_concurrent_quota_accounting_is_atomic(tmp_path, workspace):
    directory = tmp_path / "store"
    def publish(content):
        try:
            return _retry(lambda: ArtifactStore(directory, max_workspace_bytes=4).publish(workspace, content, name="input", media_type="text/plain"))
        except ArtifactError as error:
            return error.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(publish, [b"1234", b"abcd"]))
    assert sum(isinstance(result, dict) for result in results) == 1
    assert results.count("quota_exceeded") == 1


def test_cross_process_publication_survives_new_instances(tmp_path, workspace):
    context = multiprocessing.get_context("spawn")
    queue = context.Queue()
    directory = str(tmp_path / "store")
    workers = [context.Process(target=_process_publish, args=(directory, workspace, b"shared", queue)) for _ in range(3)]
    try:
        for process in workers:
            process.start()
        results = [queue.get(timeout=20) for _ in workers]
        for process in workers:
            process.join(timeout=20)
        assert all(process.exitcode == 0 for process in workers)
        assert all(status == "ok" for status, _ in results), results
        assert all(result == results[0][1] for _, result in results)
        artifact = results[0][1]
        store = ArtifactStore(directory)
        manifest = _manifest(store, workspace, artifact)
        assert ArtifactStore(directory).verify(workspace, manifest["manifest_id"], manifest["sha256"])["verified"] is True
    finally:
        for process in workers:
            if process.is_alive():
                process.terminate()
                process.join(timeout=5)
        queue.close()


def test_lock_wait_is_bounded_and_reports_retry(tmp_path, workspace, monkeypatch):
    store = ArtifactStore(tmp_path / "store")
    moments = iter([0.0, 3.0])
    @contextmanager
    def held_lock(*args, **kwargs):
        assert kwargs["blocking"] is False
        raise BlockingIOError()
        yield  # pragma: no cover
    monkeypatch.setattr(storage, "file_lock", held_lock)
    monkeypatch.setattr(storage, "time", SimpleNamespace(monotonic=lambda: next(moments), sleep=lambda _: None))
    with pytest.raises(ArtifactError) as caught:
        store.publish(workspace, b"text", name="input", media_type="text/plain")
    assert caught.value.code == "store_busy"


def test_process_local_lock_fallback_fails_closed(tmp_path, workspace, monkeypatch):
    @contextmanager
    def local_lock(*args, **kwargs):
        yield None
    monkeypatch.setattr(storage, "file_lock", local_lock)
    with pytest.raises(ArtifactError) as caught:
        ArtifactStore(tmp_path / "store").publish(workspace, b"text", name="input", media_type="text/plain")
    assert caught.value.code == "unsupported"


def test_failed_atomic_publication_leaves_no_visible_artifact(tmp_path, workspace, monkeypatch):
    store = ArtifactStore(tmp_path / "store")
    replace = storage.os.replace
    def fail_publish(source, destination):
        if Path(source).is_dir():
            raise OSError("simulated rename failure")
        return replace(source, destination)
    monkeypatch.setattr(storage.os, "replace", fail_publish)
    with pytest.raises(ArtifactError) as caught:
        store.publish(workspace, b"text", name="input", media_type="text/plain")
    assert caught.value.code == "store_unavailable"
    assert list((_namespace(store, workspace) / "artifacts").iterdir()) == []


@pytest.mark.skipif(os.name != "posix", reason="Safe capture requires POSIX directory descriptors")
def test_capture_uses_trusted_root_and_copies_immutable_bytes(tmp_path, workspace):
    root = Path(workspace)
    (root / "nested").mkdir()
    source = root / "nested" / "source.txt"
    source.write_text("captured é", encoding="utf-8")
    store = ArtifactStore(tmp_path / "store")
    artifact = store.capture(workspace, root, "nested/source.txt", name="captured", media_type="text/plain")
    source.write_text("changed later", encoding="utf-8")
    manifest = _manifest(store, workspace, artifact)
    assert store.read(workspace, manifest["manifest_id"], manifest["sha256"], artifact["artifact_id"])["text"] == "captured é"


@pytest.mark.parametrize("path", ["../outside", "nested/../../outside", "/etc/passwd", "./source", "nested//source", "nested/./source", "", "source\x00", "nested\\source"])
def test_capture_rejects_traversal_and_ambiguous_paths(tmp_path, workspace, path):
    with pytest.raises(ArtifactError) as caught:
        ArtifactStore(tmp_path / "store").capture(workspace, Path(workspace), path, name="source", media_type="text/plain")
    assert caught.value.code in {"permission_denied", "unsupported"}


@pytest.mark.skipif(os.name != "posix", reason="Safe capture requires POSIX directory descriptors")
def test_capture_rejects_leaf_directory_root_symlinks_and_fifo(tmp_path, workspace):
    root = Path(workspace)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("secret", encoding="utf-8")
    (root / "leaf").symlink_to(outside / "secret")
    (root / "linked").symlink_to(outside, target_is_directory=True)
    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(root, target_is_directory=True)
    os.mkfifo(root / "fifo")
    store = ArtifactStore(tmp_path / "store")
    for source_root, source_path in ((root, "leaf"), (root, "linked/secret"), (linked_root, "leaf"), (root, "fifo")):
        with pytest.raises(ArtifactError) as caught:
            store.capture(workspace, source_root, source_path, name="source", media_type="text/plain")
        assert caught.value.code == "permission_denied"


@pytest.mark.skipif(os.name != "posix", reason="Safe capture requires POSIX directory descriptors")
def test_capture_detects_a_source_mutation(tmp_path, workspace, monkeypatch):
    source = Path(workspace) / "source"
    source.write_bytes(b"before")
    real_read = storage.os.read
    changed = [False]
    def read_and_change(fd, count):
        content = real_read(fd, count)
        if not changed[0]:
            source.write_bytes(b"changed and longer")
            changed[0] = True
        return content
    monkeypatch.setattr(storage.os, "read", read_and_change)
    with pytest.raises(ArtifactError) as caught:
        ArtifactStore(tmp_path / "store").capture(workspace, Path(workspace), "source", name="source", media_type="text/plain")
    assert caught.value.code == "source_changed"


@pytest.mark.skipif(os.name != "posix", reason="Safe capture requires POSIX directory descriptors")
def test_capture_enforces_size_and_utf8_before_publication(tmp_path, workspace):
    source = Path(workspace) / "source"
    source.write_bytes(b"12345")
    store = ArtifactStore(tmp_path / "store", max_artifact_bytes=4)
    with pytest.raises(ArtifactError) as caught:
        store.capture(workspace, Path(workspace), "source", name="source", media_type="text/plain")
    assert caught.value.code == "artifact_too_large"
    source.write_bytes(b"\xff")
    with pytest.raises(ArtifactError) as caught:
        store.capture(workspace, Path(workspace), "source", name="source", media_type="text/plain")
    assert caught.value.code == "invalid_input"
    assert not store.directory.exists()


def test_capture_unsupported_does_not_disable_published_artifacts(tmp_path, workspace, monkeypatch):
    monkeypatch.setattr(storage.os, "supports_dir_fd", set())
    store = ArtifactStore(tmp_path / "store")
    with pytest.raises(ArtifactError) as caught:
        store.capture(workspace, Path(workspace), "source", name="source", media_type="text/plain")
    assert caught.value.code == "unsupported"
    artifact = store.publish(workspace, b"text", name="input", media_type="text/plain")
    manifest = _manifest(store, workspace, artifact)
    assert store.verify(workspace, manifest["manifest_id"], manifest["sha256"])["verified"] is True


@pytest.mark.skipif(os.name != "posix", reason="This test changes POSIX mode bits and creates symlinks")
def test_store_symlinks_and_unsafe_file_modes_fail_closed(tmp_path, workspace):
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    link = tmp_path / "linked-store"
    link.symlink_to(real, target_is_directory=True)
    with pytest.raises(ArtifactError) as caught:
        ArtifactStore(link).publish(workspace, b"text", name="input", media_type="text/plain")
    assert caught.value.code == "permission_denied"
    store = ArtifactStore(tmp_path / "store")
    artifact = store.publish(workspace, b"text", name="input", media_type="text/plain")
    manifest = _manifest(store, workspace, artifact)
    path = _artifact_path(store, workspace, artifact) / "content"
    path.chmod(0o644)
    with pytest.raises(ArtifactError) as caught:
        store.verify(workspace, manifest["manifest_id"], manifest["sha256"])
    assert caught.value.code == "permission_denied"
    path.unlink()
    path.symlink_to(real / "unrelated")
    with pytest.raises(ArtifactError):
        store.read(workspace, manifest["manifest_id"], manifest["sha256"], artifact["artifact_id"])


def test_manifest_and_metadata_size_bounds_precede_parsing(tmp_path, workspace):
    store = ArtifactStore(tmp_path / "store")
    artifact = store.publish(workspace, b"text", name="input", media_type="text/plain")
    manifest = _manifest(store, workspace, artifact)
    metadata_path = _artifact_path(store, workspace, artifact) / "metadata.json"
    metadata_path.write_bytes(b" " * (storage._MAX_METADATA_BYTES + 1))
    with pytest.raises(ArtifactError) as caught:
        store.verify(workspace, manifest["manifest_id"], manifest["sha256"])
    assert caught.value.code == "integrity_error"
    manifest_path = _namespace(store, workspace) / "manifests" / (manifest["manifest_id"] + ".json")
    manifest_path.write_bytes(b" " * (storage._MAX_MANIFEST_BYTES + 1))
    with pytest.raises(ArtifactError) as caught:
        store.get_manifest(workspace, manifest["manifest_id"])
    assert caught.value.code == "integrity_error"


def _private_bytes(path, data):
    path.write_bytes(data)
    path.chmod(0o600)


def _symlink_or_skip(path, target, *, directory=False):
    try:
        path.symlink_to(target, target_is_directory=directory)
    except OSError as error:
        if os.name == "nt" and getattr(error, "winerror", None) == 1314:
            pytest.skip("Windows symlink privilege is unavailable; reparse rejection has portable boundary coverage")
        raise


@pytest.mark.parametrize("suffix", ["abcd123_", "a" * 32])
@pytest.mark.parametrize("partial_files", [(), ("content",), ("metadata.json",), ("content", "metadata.json")])
def test_restart_recovers_abandoned_artifact_publication(tmp_path, workspace, suffix, partial_files):
    directory = tmp_path / "store"
    original = ArtifactStore(directory, max_artifacts=2)
    artifact = original.publish(workspace, b"existing", name="existing", media_type="text/plain")
    manifest = _manifest(original, workspace, artifact)
    published_path = _artifact_path(original, workspace, artifact)
    published_bytes = {path.name: path.read_bytes() for path in published_path.iterdir()}
    staging = _namespace(original, workspace) / "artifacts" / (".tmp-" + suffix)
    staging.mkdir(mode=0o700)
    for name in partial_files:
        _private_bytes(staging / name, b"\xffpartial" if name == "content" else b'{"artifact_id":')

    restarted = ArtifactStore(directory, max_artifacts=2)
    replacement = restarted.publish(workspace, b"new", name="new", media_type="text/plain")
    assert replacement["artifact_id"] != artifact["artifact_id"]
    assert not staging.exists()
    assert restarted.read(workspace, manifest["manifest_id"], manifest["sha256"], artifact["artifact_id"])["text"] == "existing"
    assert {path.name: path.read_bytes() for path in published_path.iterdir()} == published_bytes
    assert len(list((_namespace(restarted, workspace) / "artifacts").iterdir())) == 2


@pytest.mark.parametrize("partial", [b"", b'{"manifest_id":'])
def test_restart_recovers_abandoned_json_without_changing_published_records(tmp_path, workspace, partial):
    original = ArtifactStore(tmp_path / "store")
    artifact = original.publish(workspace, b"existing", name="existing", media_type="text/plain")
    active = _manifest(original, workspace, artifact)
    released = _manifest(original, workspace, artifact)
    original.release_manifest(workspace, released["manifest_id"], released["sha256"])
    root = _namespace(original, workspace)
    published = [root / "manifests" / (manifest["manifest_id"] + ".json") for manifest in (active, released)]
    published.append(root / "revoked" / (released["manifest_id"] + ".json"))
    original_bytes = {path: path.read_bytes() for path in published}
    abandoned = [root / category / (".tmp-" + "a" * 32) for category in ("manifests", "revoked")]
    for path in abandoned:
        _private_bytes(path, partial)

    restarted = ArtifactStore(original.directory)
    assert restarted.read(workspace, active["manifest_id"], active["sha256"], artifact["artifact_id"])["text"] == "existing"
    assert all(not path.exists() for path in abandoned)
    assert {path: path.read_bytes() for path in published} == original_bytes
    restarted.publish(workspace, b"new", name="new", media_type="text/plain")
    with pytest.raises(ArtifactError) as caught:
        restarted.get_manifest(workspace, released["manifest_id"])
    assert caught.value.code == "artifact_unavailable"
    assert restarted.release_manifest(workspace, released["manifest_id"], released["sha256"])["released"] is True


@pytest.mark.parametrize("unsafe", [
    "stage_symlink", "child_symlink", "child_hardlink", "child_unknown", "child_directory",
    "child_mode", "child_oversized", "stage_unknown_name", "json_symlink", "json_hardlink",
    "json_unknown_name", "json_oversized", "json_mode", "namespace_unknown",
])
def test_reconciliation_refuses_unsafe_entries_without_deleting_other_staging(tmp_path, workspace, unsafe):
    if os.name != "posix" and unsafe in {"child_mode", "json_mode"}:
        pytest.skip("POSIX mode-bit policy; Windows ACL rejection has portable boundary coverage")
    store = ArtifactStore(tmp_path / "store", max_artifact_bytes=16)
    artifact = store.publish(workspace, b"existing", name="existing", media_type="text/plain")
    manifest = _manifest(store, workspace, artifact)
    root = _namespace(store, workspace)
    safe = root / "artifacts" / (".tmp-" + "a" * 32)
    safe.mkdir(mode=0o700)
    _private_bytes(safe / "content", b"partial")
    outside = tmp_path / "outside"
    _private_bytes(outside, b"must remain")
    bad = root / "artifacts" / (".tmp-" + "b" * 32)
    json_bad = root / "manifests" / (".tmp-" + "c" * 32)
    if unsafe == "stage_symlink":
        _symlink_or_skip(bad, Path(workspace), directory=True)
    elif unsafe.startswith("child_"):
        bad.mkdir(mode=0o700)
        child = bad / "content"
        if unsafe == "child_symlink":
            _symlink_or_skip(child, outside)
        elif unsafe == "child_hardlink":
            os.link(outside, child)
        elif unsafe == "child_unknown":
            _private_bytes(bad / "foreign", b"unknown")
        elif unsafe == "child_directory":
            child.mkdir(mode=0o700)
        elif unsafe == "child_mode":
            _private_bytes(child, b"partial")
            child.chmod(0o644)
        elif unsafe == "child_oversized":
            _private_bytes(child, b"x" * 17)
    elif unsafe == "stage_unknown_name":
        (root / "artifacts" / ".tmp-foreign-data").mkdir(mode=0o700)
    elif unsafe == "json_symlink":
        _symlink_or_skip(json_bad, outside)
    elif unsafe == "json_hardlink":
        os.link(outside, json_bad)
    elif unsafe == "json_unknown_name":
        _private_bytes(root / "manifests" / "foreign.json", b"unknown")
    elif unsafe == "json_oversized":
        _private_bytes(json_bad, b"x" * (storage._MAX_MANIFEST_BYTES + 1))
    elif unsafe == "json_mode":
        _private_bytes(json_bad, b"partial")
        json_bad.chmod(0o644)
    elif unsafe == "namespace_unknown":
        _private_bytes(root / "foreign", b"unknown")

    restarted = ArtifactStore(store.directory, max_artifact_bytes=16)
    with pytest.raises(ArtifactError) as caught:
        restarted.publish(workspace, b"new", name="new", media_type="text/plain")
    assert caught.value.code in {"integrity_error", "permission_denied", "quota_exceeded"}
    assert (safe / "content").read_bytes() == b"partial"
    assert outside.read_bytes() == b"must remain"
    assert (_artifact_path(store, workspace, artifact) / "content").read_bytes() == b"existing"
    assert (root / "manifests" / (manifest["manifest_id"] + ".json")).exists()


def test_staging_recovery_has_a_fixed_count_bound(tmp_path, workspace):
    store = ArtifactStore(tmp_path / "store")
    artifact = store.publish(workspace, b"existing", name="existing", media_type="text/plain")
    root = _namespace(store, workspace)
    abandoned = []
    for index in range(storage._MAX_STAGING_ENTRIES + 1):
        path = root / "artifacts" / (".tmp-" + f"{index:032x}")
        path.mkdir(mode=0o700)
        abandoned.append(path)
    with pytest.raises(ArtifactError) as caught:
        ArtifactStore(store.directory).publish(workspace, b"new", name="new", media_type="text/plain")
    assert caught.value.code == "quota_exceeded"
    assert all(path.exists() for path in abandoned)
    assert (_artifact_path(store, workspace, artifact) / "content").read_bytes() == b"existing"


@pytest.mark.parametrize("directory", [False, True])
def test_windows_private_policy_uses_acl_instead_of_posix_mode_bits(tmp_path, monkeypatch, directory):
    checked = []
    acl = SimpleNamespace(
        owned_by_user_or_admins=lambda path: checked.append(("owner", path)) or True,
        is_private=lambda path, inherited_below=False: checked.append(("acl", path, inherited_below)) or True,
    )
    monkeypatch.setattr(storage, "_IS_WINDOWS", True)
    monkeypatch.setattr(storage, "_windows_acl", lambda: acl)
    info = SimpleNamespace(st_mode=(stat.S_IFDIR if directory else stat.S_IFREG) | 0o777,
                           st_nlink=1, st_file_attributes=0)
    path = tmp_path / "entry"
    ArtifactStore._private(info, directory=directory, path=path)
    assert checked == [("owner", path), ("acl", path, directory)]


@pytest.mark.parametrize("unsafe", ["owner", "acl", "reparse", "hardlink"])
def test_windows_private_policy_fails_closed_at_portable_boundary(tmp_path, monkeypatch, unsafe):
    acl = SimpleNamespace(owned_by_user_or_admins=lambda path: unsafe != "owner",
                          is_private=lambda path, inherited_below=False: unsafe != "acl")
    monkeypatch.setattr(storage, "_IS_WINDOWS", True)
    monkeypatch.setattr(storage, "_windows_acl", lambda: acl)
    info = SimpleNamespace(st_mode=stat.S_IFREG | 0o666,
                           st_nlink=2 if unsafe == "hardlink" else 1,
                           st_file_attributes=0x400 if unsafe == "reparse" else 0)
    with pytest.raises(ArtifactError) as caught:
        ArtifactStore._private(info, directory=False, path=tmp_path / "entry")
    assert caught.value.code == "permission_denied"


@pytest.mark.skipif(os.name != "nt", reason="Windows must support published artifacts without POSIX capture")
def test_windows_publish_read_remain_available_when_capture_is_unsupported(tmp_path, workspace):
    store = ArtifactStore(tmp_path / "store")
    with pytest.raises(ArtifactError) as caught:
        store.capture(workspace, Path(workspace), "input", name="source", media_type="text/plain")
    assert caught.value.code == "unsupported"
    artifact = store.publish(workspace, b"portable", name="input", media_type="text/plain")
    manifest = _manifest(store, workspace, artifact)
    assert ArtifactStore(store.directory).read(workspace, manifest["manifest_id"], manifest["sha256"], artifact["artifact_id"])["text"] == "portable"
