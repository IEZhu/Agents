"""Opt-in acceptance against an owner-selected dedicated loopback Redis.

Run explicitly with ``AGENTS_RUN_REDIS_LIVE_TESTS=1`` and
``AGENTS_REDIS_LIVE_TEST_URL=redis://127.0.0.1:<owned-port>/0``. The production
store constructs its real redis-py client. Every test owns random installation
namespaces; teardown deletes only those keys and never flushes a database.
The greater-than-512-MiB case also requires ``AGENTS_RUN_REDIS_LIVE_LARGE=1``.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
from pathlib import Path
import secrets
import time
from types import SimpleNamespace

import pytest

if os.environ.get("AGENTS_RUN_REDIS_LIVE_TESTS") != "1":
    pytest.skip("set AGENTS_RUN_REDIS_LIVE_TESTS=1 to opt into real Redis", allow_module_level=True)

import pytest_asyncio

from src.local_inference.backend import BackendInfo
from src.local_inference.capabilities import MAPPING_VERSION
from src.local_inference.config import OwnerConfig, RedisConfig
from src.local_inference.profile import canonical_hash, canonical_json
from src.local_inference.redis_store import BINDING_FIELDS, CacheFailure, RedisSnapshotStore, packed_tokens
from src.local_inference.snapshots import SnapshotManager


class LiveRedis:
    def __init__(self, url: str):
        self.url = url
        self.config = RedisConfig(enabled=True, chunk_bytes=128, max_snapshot_bytes=16384,
                                  max_namespace_bytes=131072, max_spool_bytes=32768,
                                  max_pipeline_chunks=2, ttl_seconds=60)
        self.stores = []
        self.prefixes = set()
        self.admin = self.store()

    def track(self, store):
        self.stores.append(store)
        self.prefixes.add(store.prefix)
        return store

    def store(self, *, config=None, installation_id=None, signing_key=None):
        config = self.config if config is None else config
        return self.track(RedisSnapshotStore(
            config, installation_id or secrets.token_hex(16), signing_key or secrets.token_bytes(32),
            environ={config.url_env: self.url}))

    @property
    def client(self):
        return self.admin._get_client()

    async def keys(self, prefix):
        return {key async for key in self.client.scan_iter(match=prefix + "*", count=128)}

    async def namespace_bytes(self, prefix):
        return sum([await self.client.strlen(key) for key in await self.keys(prefix)])

    async def cleanup(self):
        try:
            for prefix in self.prefixes:
                keys = await self.keys(prefix)
                if keys:
                    await self.client.delete(*keys)
                assert not await self.keys(prefix)
        finally:
            for store in reversed(self.stores):
                await store.close()


@pytest_asyncio.fixture
async def live_redis():
    url = os.environ.get("AGENTS_REDIS_LIVE_TEST_URL")
    if not url:
        pytest.fail("explicit live Redis selection requires AGENTS_REDIS_LIVE_TEST_URL")
    runtime = LiveRedis(url)
    try:
        assert type(runtime.client).__module__.startswith("redis.")
        assert await runtime.client.ping() is True
        yield runtime
    finally:
        await runtime.cleanup()


def snapshot(tmp_path: Path, *, payload=None, bindings=None, lifetime=60.0):
    payload = bytes(range(256)) * 3 + b"\x00\xff\x80binary-tail" if payload is None else payload
    source = tmp_path / (secrets.token_hex(8) + ".bin")
    source.write_bytes(payload)
    tokens = (0, 1, 255, 65536, 0xffffffff)
    sidecar = packed_tokens(tokens)
    digest = hashlib.sha256(sidecar).hexdigest()
    created = time.time()
    manifest = {field: canonical_hash(field) for field in BINDING_FIELDS}
    if bindings is not None:
        manifest.update(bindings)
    manifest.update(snapshot_id=secrets.token_hex(16), created_at=created,
                    expires_at=created + lifetime, byte_length=len(payload), token_count=len(tokens),
                    token_state_sha256=digest, sidecar_bytes=len(sidecar), sidecar_sha256=digest,
                    rendered_input_sha256=canonical_hash("rendered input"),
                    rendered_tokens_sha256=canonical_hash("rendered tokens"), mapping_version=MAPPING_VERSION)
    return source, manifest, sidecar, tokens


async def save_snapshot(store, tmp_path, *, key=None, **kwargs):
    key = canonical_hash(secrets.token_hex(16)) if key is None else key
    source, draft, sidecar, tokens = snapshot(tmp_path, **kwargs)
    manifest = await store.save(key, source, draft, token_state=sidecar)
    return key, source, manifest, sidecar, tokens


async def wait_until_missing(client, key, *, timeout=5.0):
    deadline = time.monotonic() + timeout
    while await client.exists(key):
        if time.monotonic() >= deadline:
            pytest.fail("owned Redis key did not expire before the bounded deadline")
        await asyncio.sleep(0.025)


@pytest.mark.asyncio
async def test_live_binary_manifest_chunk_and_sidecar_roundtrip(live_redis, tmp_path):
    store = live_redis.store()
    key, source, manifest, sidecar, tokens = await save_snapshot(store, tmp_path)
    client = live_redis.client
    assert manifest["chunk_count"] > store.config.max_pipeline_chunks
    assert await store.lookup(key) == manifest
    assert json.loads(await client.get(store._key(key))) == manifest
    data_keys = store._data_keys(manifest)
    payload = source.read_bytes()
    chunks = [await client.get(key) for key in data_keys[:-1]]
    assert all(type(chunk) is bytes for chunk in chunks)
    assert b"".join(chunks) == payload
    assert max(map(len, chunks)) == store.config.chunk_bytes
    assert await client.get(data_keys[-1]) == sidecar
    destination = tmp_path / "restored.bin"
    assert await store.load(manifest, destination) == tokens
    assert destination.read_bytes() == payload
    assert destination.stat().st_mode & 0o077 == 0
    assert store.namespace_bytes == await live_redis.namespace_bytes(store.prefix)


@pytest.mark.asyncio
async def test_live_manifest_expiry_and_data_cleanup_grace(live_redis, tmp_path):
    store = live_redis.store()
    key, _, manifest, _, _ = await save_snapshot(store, tmp_path, lifetime=3.5)
    client = live_redis.client
    manifest_ttl = await client.pttl(store._key(key))
    assert 0 < manifest_ttl <= 3500
    for data_key in store._data_keys(manifest):
        assert 59000 <= await client.pttl(data_key) - manifest_ttl <= 61000
    await wait_until_missing(client, store._key(key))
    assert await store.lookup(key) is None
    assert all([await client.exists(data_key) for data_key in store._data_keys(manifest)])
    await asyncio.sleep(max(0, manifest["expires_at"] - time.time()) + 0.025)
    destination = tmp_path / "expired.bin"
    with pytest.raises(CacheFailure, match="snapshot_manifest"):
        await store.load(manifest, destination)
    assert not destination.exists()


@pytest.mark.parametrize("damage", ["missing_chunk", "corrupt_chunk", "oversized_chunk",
                                    "truncated_chunk", "missing_sidecar", "corrupt_sidecar"])
@pytest.mark.asyncio
async def test_live_missing_or_corrupt_binary_data_is_refused(live_redis, tmp_path, damage):
    store = live_redis.store()
    _, _, manifest, _, _ = await save_snapshot(store, tmp_path)
    data_keys = store._data_keys(manifest)
    damaged_key = data_keys[-1] if "sidecar" in damage else data_keys[1]
    client = live_redis.client
    if damage.startswith("missing"):
        assert await client.delete(damaged_key) == 1
    else:
        original = await client.get(damaged_key)
        if damage == "oversized_chunk":
            changed = original + b"extra"
        elif damage == "truncated_chunk":
            changed = original[:-1]
        else:
            changed = bytes([original[0] ^ 0xff]) + original[1:]
        assert await client.set(damaged_key, changed, ex=60) is True
    destination = tmp_path / "refused.bin"
    expected = "snapshot_token_state" if "sidecar" in damage else "snapshot_integrity"
    with pytest.raises(CacheFailure, match=expected):
        await store.load(manifest, destination)
    assert not destination.exists()


@pytest.mark.parametrize("damage", ["missing", "invalid_json", "hmac", "extra_field", "schema"])
@pytest.mark.asyncio
async def test_live_missing_or_corrupt_manifest_is_refused(live_redis, tmp_path, damage):
    store = live_redis.store()
    key, _, manifest, _, _ = await save_snapshot(store, tmp_path)
    client = live_redis.client
    manifest_key = store._key(key)
    if damage == "missing":
        assert await client.delete(manifest_key) == 1
        assert await store.lookup(key) is None
        return
    changed = dict(manifest)
    if damage == "hmac":
        changed["hmac"] = "0" * 64
    elif damage == "extra_field":
        changed["untrusted"] = "extra"
    elif damage == "schema":
        changed["schema"] = "unrecognized-snapshot-version"
    raw = b"{invalid-json" if damage == "invalid_json" else canonical_json(changed)
    assert await client.set(manifest_key, raw, ex=60) is True
    with pytest.raises(CacheFailure, match="snapshot_manifest"):
        await store.lookup(key)


@pytest.mark.asyncio
async def test_live_signing_identity_is_required(live_redis, tmp_path):
    store = live_redis.store()
    key, _, _, _, _ = await save_snapshot(store, tmp_path)
    stranger = live_redis.store(installation_id=store.installation_id)
    with pytest.raises(CacheFailure, match="snapshot_manifest"):
        await stranger.lookup(key)


@pytest.mark.asyncio
async def test_live_authenticated_profile_mismatch_never_reaches_native_restore(live_redis, tmp_path):
    config = OwnerConfig(redis=live_redis.config)
    manager = SnapshotManager(config, tmp_path / "manager", environ={config.redis.url_env: live_redis.url})
    store = live_redis.track(manager._get_store())
    info = BackendInfo("a" * 64, "live-storage-test", snapshot="supported", native_write_limit=True)
    spool = tmp_path / "spool"
    spool.mkdir(mode=0o700)
    calls = []

    async def native_restore(*args):
        calls.append(args)
        raise AssertionError("an incompatible stored profile must not reach the native parser")

    backend = SimpleNamespace(info=info, spool=spool, slot_action=native_restore)
    captured = SimpleNamespace(context_id=secrets.token_hex(16), workspace_id="storage-live-test",
                               workspace_root=str(tmp_path), source_root_id="root", source_root=str(tmp_path),
                               source_root_identity=(1, 2), owner_config_revision=config.revision,
                               common_system="test", shared_context="test", sources=(),
                               expires_monotonic=time.monotonic() + 60)
    context = SimpleNamespace(captured=captured, profile_hash=info.profile_hash)
    ref = manager.key_for(context, info.profile_hash)
    bindings = dict(ref.bindings)
    bindings["profile_hash"] = "b" * 64
    try:
        await save_snapshot(store, tmp_path, key=ref.context_key, bindings=bindings)
        assert (await store.lookup(ref.context_key))["profile_hash"] == "b" * 64
        outcome = await manager.restore(context, info, backend, deadline=time.monotonic() + 5)
        assert outcome.warning == "snapshot_compatibility" and outcome.token_state is None
        assert not calls and not list(spool.iterdir())
    finally:
        await manager.close()


@pytest.mark.asyncio
async def test_live_replacement_and_delete_preserve_other_contexts_and_installations(live_redis, tmp_path):
    store = live_redis.store()
    key, _, old, _, _ = await save_snapshot(store, tmp_path)
    other_key, other_source, other, _, other_tokens = await save_snapshot(store, tmp_path)
    foreign = live_redis.store()
    foreign_key = foreign.prefix + "owned-test-sentinel"
    assert await live_redis.client.set(foreign_key, b"keep", ex=60) is True
    _, source, current, _, tokens = await save_snapshot(store, tmp_path, key=key, payload=b"new\x00\xff" * 100)
    assert current["snapshot_id"] != old["snapshot_id"]
    assert all([not await live_redis.client.exists(data_key) for data_key in store._data_keys(old)])
    assert await store.lookup(key) == current
    destination = tmp_path / "replacement.bin"
    assert await store.load(current, destination) == tokens
    assert destination.read_bytes() == source.read_bytes()
    await store.delete(key)
    assert await store.lookup(key) is None
    assert all([not await live_redis.client.exists(data_key) for data_key in store._data_keys(current)])
    assert await store.lookup(other_key) == other
    other_destination = tmp_path / "other.bin"
    assert await store.load(other, other_destination) == other_tokens
    assert other_destination.read_bytes() == other_source.read_bytes()
    assert await live_redis.client.get(foreign_key) == b"keep"
    assert store.namespace_bytes == await live_redis.namespace_bytes(store.prefix)


@pytest.mark.asyncio
async def test_live_restart_counts_orphan_bytes_and_releases_expired_capacity(live_redis, tmp_path):
    config = live_redis.config.model_copy(update={"max_snapshot_bytes":4096, "max_namespace_bytes":8192})
    first = live_redis.store(config=config)
    old_key, old_source, old, _, old_tokens = await save_snapshot(first, tmp_path)
    orphan = first.prefix + canonical_hash("orphan") + ":" + secrets.token_hex(16) + ":chunk:0"
    assert await live_redis.client.set(orphan, b"orphan" * 1400, ex=3) is True
    foreign = live_redis.store()
    foreign_key = foreign.prefix + "owned-test-sentinel"
    assert await live_redis.client.set(foreign_key, b"outside" * 1400, ex=60) is True
    await first.close()
    restarted = live_redis.store(config=config, installation_id=first.installation_id,
                                 signing_key=first._signing_key)
    key = canonical_hash("new-context")
    source, draft, sidecar, _ = snapshot(tmp_path)
    with pytest.raises(CacheFailure, match="snapshot_capacity"):
        await restarted.save(key, source, draft, token_state=sidecar)
    assert await restarted.lookup(key) is None
    assert restarted.namespace_bytes == await live_redis.namespace_bytes(first.prefix)
    assert restarted.namespace_bytes >= 8400
    assert await live_redis.client.exists(orphan) == 1
    assert await restarted.lookup(old_key) == old
    destination = tmp_path / "after-restart.bin"
    assert await restarted.load(old, destination) == old_tokens
    assert destination.read_bytes() == old_source.read_bytes()
    await wait_until_missing(live_redis.client, orphan)
    # _refresh confirms observed expiry before releasing conservatively counted bytes.
    await asyncio.sleep(1.025)
    current = await restarted.save(key, source, draft, token_state=sidecar)
    assert await restarted.lookup(key) == current
    assert restarted.namespace_bytes == await live_redis.namespace_bytes(first.prefix)
    assert await live_redis.client.get(foreign_key) == b"outside" * 1400


@pytest.mark.skipif(os.environ.get("AGENTS_RUN_REDIS_LIVE_LARGE") != "1",
                    reason="set AGENTS_RUN_REDIS_LIVE_LARGE=1 for the >512-MiB Redis roundtrip")
@pytest.mark.asyncio
async def test_live_sparse_snapshot_exceeding_single_redis_value_limit(live_redis, tmp_path):
    size = 512 * 1024 * 1024 + 1
    chunk_bytes = 4 * 1024 * 1024
    config = RedisConfig(enabled=True, chunk_bytes=chunk_bytes, max_snapshot_bytes=size,
                         max_namespace_bytes=size + 128 * 1024, max_spool_bytes=size * 2,
                         max_pipeline_chunks=8, ttl_seconds=900, operation_timeout_seconds=120.0)
    store = live_redis.store(config=config)
    source, draft, sidecar, tokens = snapshot(tmp_path, payload=b"", lifetime=900.0)
    destination = tmp_path / "large-restored.bin"
    key = canonical_hash(secrets.token_hex(16))
    manifest = None

    def streamed_sha256(path):
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            while block := stream.read(1024 * 1024):
                digest.update(block)
        return digest.hexdigest()

    try:
        with source.open("r+b") as stream:
            stream.truncate(size)
            stream.write(b"\x00\xfflarge-sparse-start\x80")
            tail = b"\x80large-sparse-end\xff\x00"
            stream.seek(size - len(tail))
            stream.write(tail)
        draft["byte_length"] = size
        assert source.stat().st_size == 536870913
        source_digest = streamed_sha256(source)
        manifest = await store.save(key, source, draft, token_state=sidecar)
        assert manifest["chunk_count"] == 129
        assert manifest["chunk_bytes"] == chunk_bytes
        assert manifest["payload_sha256"] == source_digest
        assert store.config.max_pipeline_chunks == 8
        assert await store.lookup(key) == manifest
        lengths = [await live_redis.client.strlen(chunk) for chunk in store._data_keys(manifest)[:-1]]
        assert lengths == [chunk_bytes] * 128 + [1]
        assert await store.load(manifest, destination) == tokens
        assert destination.stat().st_size == size
        assert streamed_sha256(destination) == source_digest
        assert store.namespace_bytes == await live_redis.namespace_bytes(store.prefix)
    finally:
        try:
            await store.delete(key)
            assert not await live_redis.keys(store.prefix)
        finally:
            source.unlink(missing_ok=True)
            destination.unlink(missing_ok=True)
