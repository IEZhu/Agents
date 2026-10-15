"""Snapshot integrity/capacity is exercised with fake binary storage, never Redis."""
import asyncio
from dataclasses import replace
import hashlib
import importlib
import json
import math
import os
from pathlib import Path
import struct
import time
from types import SimpleNamespace

import pytest

from src.local_inference.backend import BackendInfo, Completion, PreparedPrompt
from src.local_inference.config import OwnerConfig, RedisConfig
from src.local_inference.redis_store import (CacheFailure, FakeSnapshotStore, MemoryBinaryClient,
                                              RedisSnapshotStore, packed_tokens, redis_options)
from src.local_inference.snapshots import RestoreFailure, SnapshotManager


class Clock:
    def __init__(self): self.value = 100.0
    def __call__(self): return self.value
    def advance(self, seconds): self.value += seconds


class Native:
    def __init__(self, directory):
        self.info = BackendInfo("a" * 64, "epoch-1", "supported", "supported", True)
        self.spool = directory / "spool"; self.spool.mkdir(mode=0o700)
        self.payload = b"opaque sentinel snapshot bytes\0" * 6
        self.tokens = (1, 2, 3, 10)
        self.calls = []
        self.fail_save = self.fail_restore = False
        self.saved_count = self.restored_count = None
        self.restored_payload = None
        self.on_save = None

    async def slot_action(self, action, filename):
        self.calls.append((action, filename))
        path = self.spool / filename
        if action == "save":
            if self.on_save: self.on_save()
            if self.fail_save: raise RuntimeError("private-credential-sentinel")
            path.write_bytes(self.payload)
            return {"id_slot": 0, "filename": filename,
                    "n_saved": len(self.tokens) if self.saved_count is None else self.saved_count,
                    "n_written": len(self.payload), "timings": {"save_ms": 1.0}}
        if self.fail_restore: raise RuntimeError("private-credential-sentinel")
        self.restored_payload = path.read_bytes()
        return {"id_slot": 0, "filename": filename,
                "n_restored": len(self.tokens) if self.restored_count is None else self.restored_count,
                "n_read": len(self.restored_payload), "timings": {"restore_ms": 1.0}}


def context(config, clock, *, context_id="ctx-1", source_root="/authorized/root", profile=None):
    captured = SimpleNamespace(context_id=context_id, workspace_id="workspace-1", workspace_root="/workspace",
                               source_root_id="root", source_root=source_root, source_root_identity=(1, 2),
                               owner_config_revision=config.revision, common_system="private system sentinel",
                               shared_context="private shared sentinel", sources=(), expires_monotonic=clock() + 60)
    return SimpleNamespace(captured=captured, profile_hash="a" * 64 if profile is None else profile)


def completion():
    return Completion("private output sentinel", "stop", 3, 3, 0, 2, 0.1, (10, 11), (1, 2, 3, 10))


def setup(tmp_path, *, redis=None, client=None):
    tmp_path.mkdir(parents=True, exist_ok=True)
    clock = Clock()
    cfg = OwnerConfig(redis=redis or RedisConfig(enabled=True, chunk_bytes=64, max_snapshot_bytes=256,
                                                max_namespace_bytes=65536, max_spool_bytes=1024,
                                                max_pipeline_chunks=2, ttl_seconds=60))
    client = MemoryBinaryClient(wall_clock=clock) if client is None else client
    stores = []
    def factory(config, installation_id, key, **kwargs):
        value = FakeSnapshotStore(config, installation_id, key, client=client, **kwargs)
        stores.append(value)
        return value
    manager = SnapshotManager(cfg, tmp_path / "state", store_factory=factory, clock=clock, wall_clock=clock)
    native = Native(tmp_path)
    record = context(cfg, clock)
    prepared = PreparedPrompt("authoritative rendered input sentinel", (1, 2, 3))
    return manager, native, record, prepared, client, clock, stores


@pytest.mark.asyncio
async def test_roundtrip_exact_sidecar_manifest_and_private_spool(tmp_path):
    manager, native, record, prepared, client, clock, stores = setup(tmp_path)
    native.on_save = lambda: assert_reservation(manager, native)
    try:
        saved = await manager.save(record, native.info, native, completion(), prepared, deadline=clock() + 10)
        assert saved.warning is None and not saved.fence_required
        assert not list(native.spool.iterdir()) and manager._reserved == 0
        ref = manager.key_for(record, native.info.profile_hash)
        manifest = await stores[0].lookup(ref.context_key)
        token_key = stores[0]._generation(manifest) + "tokens"
        assert client.values[token_key] == struct.pack("<4I", 1, 2, 3, 10)
        assert manifest["token_count"] == 4 and manifest["sidecar_bytes"] == 16
        assert manifest["rendered_input_sha256"] == hashlib.sha256(prepared.text.encode()).hexdigest()
        assert manifest["byte_length"] == len(native.payload) and manifest["chunk_count"] > 1
        assert client.max_batch <= 2
        set_keys = [call[1] for call in client.calls if call[0] == "set"]
        assert set_keys[-1].endswith(":manifest") and set_keys[-2].endswith(":tokens")
        restored = await manager.restore(record, native.info, native, deadline=clock() + 10)
        assert restored.token_state == (1, 2, 3, 10) and restored.warning is None
        assert native.restored_payload == native.payload
        assert not list(native.spool.iterdir()) and manager._reserved == 0
        assert all(value >= 0 for value in restored.timings_ms.values())
        assert all("sentinel" not in value for _, value in ref.bindings)
    finally:
        await manager.close()


def assert_reservation(manager, native):
    assert manager._reserved >= manager.config.redis.max_snapshot_bytes + 16
    assert native.spool.stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("damage", ["hmac", "extra", "version", "chunk", "missing_chunk", "sidecar", "missing_sidecar",
                                     "oversized_chunk", "inconsistent_count"])
@pytest.mark.asyncio
async def test_corruption_never_reaches_native_parser(tmp_path, damage):
    manager, native, record, prepared, client, clock, stores = setup(tmp_path)
    try:
        assert (await manager.save(record, native.info, native, completion(), prepared, deadline=clock() + 10)).warning is None
        ref = manager.key_for(record, native.info.profile_hash)
        store = stores[0]
        key = store._key(ref.context_key)
        manifest = json.loads(client.values[key])
        keys = store._data_keys(manifest)
        if damage in {"hmac", "extra", "version", "inconsistent_count"}:
            if damage == "hmac": manifest["hmac"] = "0" * 64
            if damage == "extra": manifest["untrusted"] = "value"
            if damage == "version": manifest["schema"] = "unknown"
            if damage == "inconsistent_count": manifest["chunk_count"] = 999
            client.values[key] = json.dumps(manifest).encode()
        elif damage == "chunk": client.values[keys[0]] = b"x" * len(client.values[keys[0]])
        elif damage == "missing_chunk": client.values.pop(keys[0])
        elif damage == "sidecar": client.values[keys[-1]] = b"x" * manifest["sidecar_bytes"]
        elif damage == "missing_sidecar": client.values.pop(keys[-1])
        elif damage == "oversized_chunk": client.values[keys[0]] += b"tail"
        restored = await manager.restore(record, native.info, native, deadline=clock() + 10)
        assert restored.token_state is None and restored.warning is not None
        assert all(action != "restore" for action, _ in native.calls)
        assert not list(native.spool.iterdir()) and manager._reserved == 0
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_ttl_and_independent_context_source_profile_bindings(tmp_path):
    manager, native, record, prepared, client, clock, stores = setup(tmp_path)
    try:
        assert (await manager.save(record, native.info, native, completion(), prepared, deadline=clock() + 10)).warning is None
        for changed in [context(manager.config, clock, context_id="ctx-2"),
                        context(manager.config, clock, source_root="/another/authorized/root")]:
            result = await manager.restore(changed, native.info, native, deadline=clock() + 10)
            assert result.token_state is None
        other_profile = replace(native.info, profile_hash="b" * 64)
        native.info = other_profile
        result = await manager.restore(record, other_profile, native, deadline=clock() + 10)
        assert result.token_state is None
        native.info = replace(native.info, profile_hash="a" * 64)
        clock.advance(61)
        result = await manager.restore(record, native.info, native, deadline=clock() + 10)
        assert result.token_state is None
        assert all(action != "restore" for action, _ in native.calls)
    finally:
        await manager.close()


@pytest.mark.parametrize("count", [None, True, 3])
@pytest.mark.asyncio
async def test_native_restore_failure_requires_recovery(tmp_path, count):
    manager, native, record, prepared, _, clock, _ = setup(tmp_path)
    try:
        await manager.save(record, native.info, native, completion(), prepared, deadline=clock() + 10)
        native.fail_restore = count is None
        native.restored_count = count
        with pytest.raises(RestoreFailure) as caught:
            await manager.restore(record, native.info, native, deadline=clock() + 10)
        assert caught.value.native_touched is True and caught.value.timings_ms["snapshot_restore"] >= 0
        assert "private-credential-sentinel" not in str(caught.value)
        assert not list(native.spool.iterdir()) and manager._reserved == 0
    finally:
        await manager.close()


@pytest.mark.parametrize("failure", ["native", "counter", "upload", "publish"])
@pytest.mark.asyncio
async def test_failed_save_preserves_answer_with_warning(tmp_path, failure):
    manager, native, record, prepared, client, clock, _ = setup(tmp_path)
    value = completion()
    if failure == "native": native.fail_save = True
    if failure == "counter": native.saved_count = 999
    if failure in ("upload", "publish"):
        def fail(name, args):
            if name == "set" and ((failure == "publish" and args[0].endswith(":manifest"))
                                  or (failure == "upload" and ":chunk:" in args[0])):
                raise RuntimeError("private-credential-sentinel")
        client.fail = fail
    try:
        result = await manager.save(record, native.info, native, value, prepared, deadline=clock() + 10)
        assert result.warning is not None and value.output_text == "private output sentinel"
        assert result.fence_required == (failure in ("native", "counter"))
        assert "sentinel" not in result.warning and not list(native.spool.iterdir())
        assert sum(action == "save" for action, _ in native.calls) == 1
        assert not any(key.endswith(":manifest") for key in client.values)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_replacement_manifest_last_and_scoped_deletion(tmp_path):
    manager, native, record, prepared, client, clock, stores = setup(tmp_path)
    client.values["unrelated-key"] = b"keep"
    try:
        await manager.save(record, native.info, native, completion(), prepared, deadline=clock() + 10)
        ref = manager.key_for(record, native.info.profile_hash)
        old = await stores[0].lookup(ref.context_key)
        other = context(manager.config, clock, context_id="ctx-other")
        await manager.save(other, native.info, native, completion(), prepared, deadline=clock() + 10)
        other_ref = manager.key_for(other, native.info.profile_hash)
        native.payload = b"new payload" * 15
        await manager.save(record, native.info, native, completion(), prepared, deadline=clock() + 10)
        current = await stores[0].lookup(ref.context_key)
        assert old["snapshot_id"] != current["snapshot_id"]
        assert all(key not in client.values for key in stores[0]._data_keys(old))
        assert await manager.delete_key(ref) is None
        assert await stores[0].lookup(ref.context_key) is None
        assert await stores[0].lookup(other_ref.context_key) is not None
        assert client.values["unrelated-key"] == b"keep"
        assert all(call[1] != "unrelated-key" for call in client.calls if call[0] == "delete")
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_failed_replacement_keeps_preceding_published_snapshot(tmp_path):
    manager, native, record, prepared, client, clock, stores = setup(tmp_path)
    try:
        await manager.save(record, native.info, native, completion(), prepared, deadline=clock() + 10)
        ref = manager.key_for(record, native.info.profile_hash)
        old = await stores[0].lookup(ref.context_key)
        def fail(name, args):
            if name == "set" and ":chunk:1" in args[0]: raise RuntimeError()
        client.fail = fail
        result = await manager.save(record, native.info, native, completion(), prepared, deadline=clock() + 10)
        assert result.warning is not None
        client.fail = None
        current = await stores[0].lookup(ref.context_key)
        assert current == old
        assert all(key in client.values for key in stores[0]._data_keys(old))
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_restart_reconciliation_counts_orphans_and_capacity(tmp_path):
    redis = RedisConfig(enabled=True, chunk_bytes=64, max_snapshot_bytes=256,
                        max_namespace_bytes=4096, max_spool_bytes=1024, ttl_seconds=60)
    manager, native, record, prepared, client, clock, stores = setup(tmp_path, redis=redis)
    try:
        await manager.save(record, native.info, native, completion(), prepared, deadline=clock() + 10)
        first = stores[0]
        orphan = first.prefix + "f" * 64 + ":" + "e" * 32 + ":chunk:0"
        await client.set(orphan, b"x" * 2500, ex=120)
        restarted = FakeSnapshotStore(redis, first.installation_id, first._signing_key,
                                      client=client, clock=clock, wall_clock=clock)
        manager._store = restarted
        new_context = context(manager.config, clock, context_id="new-broker-context")
        result = await manager.save(new_context, native.info, native, completion(), prepared, deadline=clock() + 10)
        assert result.warning == "snapshot_capacity"
        assert restarted.namespace_bytes >= 2500
        restored = await manager.restore(new_context, native.info, native, deadline=clock() + 10)
        assert restored.token_state is None
        await first.close()
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_spool_reservation_cleanup_and_unrelated_file_capacity(tmp_path):
    manager, native, record, prepared, _, clock, _ = setup(tmp_path)
    stale = native.spool / ("snapshot-" + "f" * 32 + ".bin")
    stale.write_bytes(b"stale")
    unrelated = native.spool / "unrelated.bin"; unrelated.write_bytes(b"x" * 900)
    try:
        result = await manager.save(record, native.info, native, completion(), prepared, deadline=clock() + 10)
        assert result.warning == "snapshot_spool_capacity" and not native.calls
        assert not stale.exists() and unrelated.stat().st_size == 900
        assert manager._reserved == 0
    finally:
        await manager.close()


@pytest.mark.parametrize("unsupported", ["unknown", "no_limit", "missing_ids", "disabled"])
@pytest.mark.asyncio
async def test_unsupported_or_disabled_snapshots_do_no_io(tmp_path, unsupported):
    manager, native, record, prepared, client, clock, stores = setup(tmp_path)
    value = completion()
    if unsupported == "unknown": native.info = replace(native.info, snapshot="unknown")
    if unsupported == "no_limit": native.info = replace(native.info, native_write_limit=False)
    if unsupported == "missing_ids": value = replace(value, saved_token_state=None)
    if unsupported == "disabled": manager.config = manager.config.model_copy(update={"redis": RedisConfig()})
    try:
        result = await manager.save(record, native.info, native, value, prepared, deadline=clock() + 10)
        assert result.warning is None and not native.calls and not stores and not client.calls
        assert not manager.directory.exists()
    finally:
        await manager.close()


@pytest.mark.parametrize("url", ["redis://remote.example/0", "redis://127.0.0.2/0", "unix:///tmp/redis.sock",
                                 "redis://localhost/0?socket_timeout=999", "redis://localhost/-1",
                                 "redis://localhost:99999/0", "redis://localhost/0#fragment", " redis://localhost/0"])
def test_redis_url_rejects_nonloopback_or_option_overrides(url):
    with pytest.raises(CacheFailure) as caught:
        redis_options(url, RedisConfig())
    assert caught.value.code == "snapshot_configuration"


def test_loopback_options_do_not_inherit_query_or_decoder_changes():
    value = redis_options("rediss://user:secret@localhost:6380/2", RedisConfig())
    assert value["host"] == "127.0.0.1" and value["db"] == 2 and value["ssl"]
    assert value["decode_responses"] is False and value["socket_timeout"] == 5


@pytest.mark.asyncio
async def test_missing_optional_dependency_and_connection_failure_are_warnings(tmp_path, monkeypatch):
    config = OwnerConfig(redis=RedisConfig(enabled=True))
    manager = SnapshotManager(config, tmp_path, environ={"AGENTS_LOCAL_KV_REDIS_URL": "redis://127.0.0.1/0"})
    real = importlib.import_module
    def unavailable(name, *args, **kwargs):
        if name.startswith("redis"): raise ImportError()
        return real(name, *args, **kwargs)
    monkeypatch.setattr(importlib, "import_module", unavailable)
    assert await manager.check_store(deadline=time.monotonic() + 2) == "snapshot_unavailable"
    await manager.close()
    manager, native, record, prepared, client, clock, _ = setup(tmp_path / "other")
    client.fail = lambda *_: (_ for _ in ()).throw(RuntimeError("credential"))
    try:
        assert await manager.check_store(deadline=clock() + 10) == "snapshot_unavailable"
        value = await manager.restore(record, native.info, native, deadline=clock() + 10)
        assert value.warning == "snapshot_unavailable" and not native.calls
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_owner_check_store_executes_real_store_boundary_only(tmp_path):
    manager, _, _, _, client, clock, _ = setup(tmp_path)
    try:
        assert await manager.check_store(deadline=clock() + 10) is None
        assert [call[0] for call in client.calls] == ["getrange"]
    finally:
        await manager.close()


@pytest.mark.parametrize("tokens", [[True], [-1], [2**32], [], [1, "2"]])
def test_exact_packed_sidecar_rejects_bad_ids(tokens):
    with pytest.raises(CacheFailure): packed_tokens(tokens)


@pytest.mark.asyncio
async def test_stream_sparse_payload_larger_than_default_redis_single_value_limit(tmp_path):
    size = 512 * 1024 * 1024 + 1
    redis = RedisConfig(enabled=True, chunk_bytes=8 * 1024 * 1024, max_snapshot_bytes=size,
                        max_namespace_bytes=size * 2, max_spool_bytes=size * 2, max_pipeline_chunks=2,
                        operation_timeout_seconds=30.0)
    manager, native, record, prepared, _, clock, _ = setup(tmp_path, redis=redis)
    class SparseClient(MemoryBinaryClient):
        def __init__(self): super().__init__(wall_clock=clock); self.lengths = {}; self.max_block = 0
        async def set(self, key, value, *, ex, nx=False):
            self.max_block = max(self.max_block, len(value))
            if ":chunk:" in key:
                self._record("set", key, len(value))
                assert not any(value)
                self.lengths[key] = len(value); self.values[key] = b"sparse-zero-chunk"; self.expiry[key] = clock() + ex
                return True
            return await super().set(key, value, ex=ex, nx=nx)
        async def strlen(self, key): return self.lengths.get(key, await super().strlen(key))
    sparse = SparseClient()
    def factory(config, installation_id, key, **kwargs):
        return FakeSnapshotStore(config, installation_id, key, client=sparse, **kwargs)
    manager.store_factory = factory
    async def save_sparse(action, filename):
        assert action == "save"
        with (native.spool / filename).open("wb") as stream: stream.truncate(size)
        return {"id_slot": 0, "n_saved": 4, "n_written": size, "timings": {"save_ms": 0.0}}
    native.slot_action = save_sparse
    try:
        result = await manager.save(record, native.info, native, completion(), prepared, deadline=clock() + 30)
        assert result.warning is None
        assert sum(sparse.lengths.values()) == size and len(sparse.lengths) == 65
        assert sparse.max_block <= redis.chunk_bytes and sparse.max_batch <= 2
        assert not list(native.spool.iterdir())
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_io_and_native_operation_deadlines_are_bounded(tmp_path):
    redis = RedisConfig(enabled=True, chunk_bytes=64, max_snapshot_bytes=256, max_spool_bytes=1024,
                        connect_timeout_seconds=0.01, io_timeout_seconds=0.01, operation_timeout_seconds=0.05)
    manager, native, record, prepared, client, clock, _ = setup(tmp_path, redis=redis)
    async def silent(*args, **kwargs): await asyncio.sleep(10)
    client.getrange = silent
    try:
        started = time.monotonic()
        result = await manager.restore(record, native.info, native, deadline=clock() + 10)
        assert result.warning == "snapshot_timeout" and time.monotonic() - started < 0.2
        assert not native.calls
    finally:
        await manager.close()
    manager, native, record, prepared, _, clock, _ = setup(tmp_path / "native", redis=redis)
    native.slot_action = silent
    try:
        started = time.monotonic()
        result = await manager.save(record, native.info, native, completion(), prepared, deadline=clock() + 10)
        assert result.warning == "snapshot_timeout" and result.fence_required
        assert time.monotonic() - started < 0.2 and manager._reserved == 0
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_publish_reply_failure_keeps_immutable_data_for_possible_commit(tmp_path):
    manager, native, record, prepared, client, clock, stores = setup(tmp_path)
    original = client.set
    async def ambiguous(key, value, **kwargs):
        result = await original(key, value, **kwargs)
        if key.endswith(":manifest"):
            raise RuntimeError("reply lost after successful SET")
        return result
    client.set = ambiguous
    try:
        saved = await manager.save(record, native.info, native, completion(), prepared, deadline=clock() + 10)
        assert saved.warning == "snapshot_unavailable" and not saved.fence_required
        client.set = original
        ref = manager.key_for(record, native.info.profile_hash)
        manifest = await stores[0].lookup(ref.context_key)
        assert all(key in client.values for key in stores[0]._data_keys(manifest))
        restored = await manager.restore(record, native.info, native, deadline=clock() + 10)
        assert restored.token_state == (1, 2, 3, 10)
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_regular_file_and_source_mutation_checks_prevent_publication(tmp_path):
    manager, native, record, prepared, client, clock, _ = setup(tmp_path)
    def mutate(name, args):
        if name == "set" and args[0].endswith(":chunk:0"):
            (native.spool / native.calls[-1][1]).write_bytes(b"changed")
    client.fail = mutate
    try:
        result = await manager.save(record, native.info, native, completion(), prepared, deadline=clock() + 10)
        assert result.warning == "snapshot_file" and not result.fence_required
        assert not any(key.endswith(":manifest") for key in client.values)
        assert not list(native.spool.iterdir())
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_uncertain_installation_spool_warns_without_changing_native_state(tmp_path):
    from src.local_inference.process import private_directory

    manager, native, record, prepared, _, clock, _ = setup(tmp_path)
    try:
        assert (await manager.save(record, native.info, native, completion(), prepared,
                                   deadline=clock() + 10)).warning is None
        native.calls.clear()
        root = manager.directory.parent / "native"
        private_directory(root)
        legacy = root / "engine-legacy"
        legacy.mkdir(mode=0o700)
        slots = legacy / "slots"
        slots.mkdir(mode=0o700)
        partial = slots / ("snapshot-" + "f" * 32 + ".bin")
        partial.write_bytes(b"x" * 900)
        saved = await manager.save(record, native.info, native, completion(), prepared,
                                   deadline=clock() + 10)
        restored = await manager.restore(record, native.info, native, deadline=clock() + 10)
        assert saved.warning == restored.warning == "snapshot_spool_capacity"
        assert not saved.fence_required and restored.token_state is None and not native.calls
        assert partial.stat().st_size == 900 and manager._reserved == 0
        assert not list(native.spool.iterdir())
    finally:
        await manager.close()
