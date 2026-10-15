"""Optional opaque binary snapshots: bounded chunks, authenticated manifest last."""
from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import fnmatch
import hashlib
import hmac
import importlib
import json
import math
import os
from pathlib import Path
import re
import stat
import struct
import threading
import time
from urllib.parse import unquote, urlsplit

from .capabilities import MAPPING_VERSION
from .config import RedisConfig
from .process import bounded_wait
from .profile import canonical_json

MANIFEST_SCHEMA = "agents-core/local-kv-snapshot/1"
MAX_MANIFEST_BYTES = 65536
MAX_SIDECAR_BYTES = 1024 * 1024
MAX_NAMESPACE_KEYS = 4096
MAX_SCAN_PAGES = 128
HEX64 = re.compile(r"^[a-f0-9]{64}$")
HEX32 = re.compile(r"^[a-f0-9]{32}$")
BINDING_FIELDS = ("workspace_identity", "source_root_identity", "owner_config_revision", "profile_hash",
                  "content_fingerprint", "context_instance_identity")
MANIFEST_FIELDS = {"schema", "installation_id", "context_key", *BINDING_FIELDS, "snapshot_id", "created_at",
                   "expires_at", "byte_length", "chunk_bytes", "chunk_count", "chunk_sha256", "payload_sha256",
                   "token_count", "token_state_sha256", "sidecar_bytes", "sidecar_sha256",
                   "rendered_input_sha256", "rendered_tokens_sha256", "mapping_version", "hmac"}


class CacheFailure(ValueError):
    def __init__(self, code="snapshot_unavailable"):
        super().__init__(code)
        self.code = code


def packed_tokens(tokens) -> bytes:
    if (type(tokens) not in (tuple, list) or not tokens or len(tokens) > MAX_SIDECAR_BYTES // 4
            or any(type(token) is not int or not 0 <= token <= 0xffffffff for token in tokens)):
        raise CacheFailure("snapshot_token_state")
    return struct.pack(f"<{len(tokens)}I", *tokens)


def _object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise CacheFailure("snapshot_manifest")
        value[key] = item
    return value


def validate_manifest(raw: bytes | dict, config: RedisConfig, installation: str, key: bytes,
                      context_key: str, now: float, *, allow_expired=False) -> dict:
    try:
        if isinstance(raw, bytes):
            if len(raw) > MAX_MANIFEST_BYTES:
                raise ValueError()
            value = json.loads(raw.decode("utf-8"), object_pairs_hook=_object,
                               parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
        else:
            value = dict(raw)
        if set(value) != MANIFEST_FIELDS:
            raise ValueError()
        if (value["schema"] != MANIFEST_SCHEMA or value["installation_id"] != installation
                or value["context_key"] != context_key or not HEX32.fullmatch(value["snapshot_id"])
                or value["mapping_version"] != MAPPING_VERSION):
            raise ValueError()
        for field in (*BINDING_FIELDS, "context_key", "payload_sha256", "token_state_sha256", "sidecar_sha256",
                      "rendered_input_sha256", "rendered_tokens_sha256", "hmac"):
            if type(value[field]) is not str or not HEX64.fullmatch(value[field]):
                raise ValueError()
        for field in ("created_at", "expires_at"):
            if type(value[field]) not in (float, int) or not math.isfinite(value[field]):
                raise ValueError()
        if (not 0 < value["created_at"] <= now or value["expires_at"] <= value["created_at"]
                or value["expires_at"] - value["created_at"] > config.ttl_seconds
                or (not allow_expired and value["expires_at"] <= now)):
            raise ValueError()
        for field in ("byte_length", "chunk_bytes", "chunk_count", "token_count", "sidecar_bytes"):
            if type(value[field]) is not int or value[field] <= 0:
                raise ValueError()
        if (value["byte_length"] > config.max_snapshot_bytes or value["chunk_bytes"] != config.chunk_bytes
                or value["chunk_count"] != (value["byte_length"] + config.chunk_bytes - 1) // config.chunk_bytes
                or value["chunk_count"] > 256 or value["sidecar_bytes"] != value["token_count"] * 4
                or value["sidecar_bytes"] > MAX_SIDECAR_BYTES
                or value["token_state_sha256"] != value["sidecar_sha256"]):
            raise ValueError()
        hashes = value["chunk_sha256"]
        if (type(hashes) is not list or len(hashes) != value["chunk_count"]
                or any(type(item) is not str or not HEX64.fullmatch(item) for item in hashes)):
            raise ValueError()
        unsigned = {field: item for field, item in value.items() if field != "hmac"}
        signature = hmac.new(key, canonical_json(unsigned), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, value["hmac"]):
            raise ValueError()
        return value
    except (ValueError, UnicodeError, TypeError, KeyError):
        raise CacheFailure("snapshot_manifest") from None


def redis_options(url: str, config: RedisConfig) -> dict:
    """Reject URL options that could override our timeouts, host or decoder."""
    try:
        if type(url) is not str or url != url.strip() or any(ord(char) < 32 for char in url):
            raise ValueError()
        parsed = urlsplit(url)
        if (parsed.scheme not in ("redis", "rediss") or parsed.hostname not in ("127.0.0.1", "::1", "localhost")
                or parsed.query or parsed.fragment or not re.fullmatch(r"/?[0-9]*", parsed.path)):
            raise ValueError()
        db = int(parsed.path.lstrip("/") or "0")
        if db > 65535 or not 1 <= (parsed.port or 6379) <= 65535:
            raise ValueError()
        return {"host": "127.0.0.1" if parsed.hostname == "localhost" else parsed.hostname,
                "port": parsed.port or 6379, "db": db,
                "username": None if parsed.username is None else unquote(parsed.username, errors="strict"),
                "password": None if parsed.password is None else unquote(parsed.password, errors="strict"),
                "ssl": parsed.scheme == "rediss", "decode_responses": False,
                "socket_connect_timeout": config.connect_timeout_seconds,
                "socket_timeout": config.io_timeout_seconds, "max_connections": 1,
                "health_check_interval": 0, "protocol": 2}
    except (ValueError, UnicodeError):
        raise CacheFailure("snapshot_configuration") from None


class NullSnapshotStore:
    async def lookup(self, key, **kwargs): return None
    async def load(self, manifest, destination, **kwargs): raise CacheFailure("snapshot_disabled")
    async def save(self, key, source, manifest, **kwargs): return None
    async def delete(self, key, **kwargs): return None
    async def close(self): return None


class RedisSnapshotStore:
    """One broker writer. Every public method serializes its bounded store job."""
    def __init__(self, config: RedisConfig, installation_id: str, signing_key: bytes, *, client=None,
                 environ=None, clock=time.monotonic, wall_clock=time.time):
        if not HEX32.fullmatch(installation_id) or type(signing_key) is not bytes or len(signing_key) != 32:
            raise ValueError("invalid private snapshot identity")
        self.config, self.installation_id = config, installation_id
        self._signing_key = signing_key
        self.prefix = f"agents-kv:v1:{installation_id}:"
        self._client = client
        self._environ = os.environ if environ is None else environ
        self.clock, self.wall_clock = clock, wall_clock
        self._lock = asyncio.Lock()
        self._sizes = {}
        self._known = False
        self._pending = 0
        self._blocked = False
        self._executor = None
        self._closed = False

    @property
    def namespace_bytes(self):
        return sum(item[0] for item in self._sizes.values()) + self._pending

    def _key(self, context_key):
        if type(context_key) is not str or not HEX64.fullmatch(context_key):
            raise CacheFailure("snapshot_manifest")
        return self.prefix + context_key + ":manifest"

    def _generation(self, manifest):
        return self.prefix + manifest["context_key"] + ":" + manifest["snapshot_id"] + ":"

    def _data_keys(self, manifest):
        base = self._generation(manifest)
        return [base + f"chunk:{index}" for index in range(manifest["chunk_count"])] + [base + "tokens"]

    def _get_client(self):
        if self._closed or self._blocked:
            raise CacheFailure()
        if self._client is None:
            if not self.config.enabled:
                raise CacheFailure("snapshot_disabled")
            options = redis_options(self._environ.get(self.config.url_env), self.config)
            try:
                redis = importlib.import_module("redis.asyncio")
                retry = importlib.import_module("redis.asyncio.retry")
                backoff = importlib.import_module("redis.backoff")
                options["retry"] = retry.Retry(backoff=backoff.ExponentialWithJitterBackoff(base=1, cap=10), retries=0)
                self._client = redis.Redis(**options)
            except (ImportError, TypeError, ValueError):
                raise CacheFailure("snapshot_unavailable") from None
        return self._client

    def _deadline(self, deadline):
        return min(deadline if deadline is not None else math.inf, self.clock() + self.config.operation_timeout_seconds)

    async def _io(self, awaitable, deadline):
        remaining = min(self.config.io_timeout_seconds, deadline - self.clock())
        if remaining <= 0:
            if hasattr(awaitable, "close"):
                awaitable.close()
            raise CacheFailure("snapshot_timeout")
        try:
            return await bounded_wait(awaitable, remaining)
        except asyncio.CancelledError:
            self._known = False
            self._blocked = True
            raise
        except asyncio.TimeoutError:
            self._known = False
            self._blocked = True  # an unacknowledged command may still be running
            raise CacheFailure("snapshot_timeout") from None
        except Exception:
            self._known = False
            raise CacheFailure("snapshot_unavailable") from None

    async def _file(self, function, deadline, *, fd):
        if self._executor is None:
            self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-snapshot-files")
        own_fd = os.dup(fd)
        stop = threading.Event()
        def work():
            try:
                return function(own_fd, stop)
            finally:
                os.close(own_fd)
        pending = self._executor.submit(work)
        pending.add_done_callback(lambda value: os.close(own_fd) if value.cancelled() else None)
        future = asyncio.wrap_future(pending)
        try:
            return await bounded_wait(future, max(0, deadline - self.clock()))
        except asyncio.CancelledError:
            stop.set(); pending.cancel(); self._blocked = True
            raise
        except asyncio.TimeoutError:
            stop.set(); pending.cancel(); self._blocked = True
            raise CacheFailure("snapshot_timeout") from None

    async def _pipeline(self, commands, deadline):
        if len(commands) > self.config.max_pipeline_chunks:
            raise CacheFailure("snapshot_capacity")
        pipe = self._get_client().pipeline(transaction=False)
        try:
            for name, args, kwargs in commands:
                getattr(pipe, name)(*args, **kwargs)
            return await self._io(pipe.execute(), deadline)
        finally:
            try:
                await bounded_wait(pipe.aclose(), min(0.1, max(0.001, deadline - self.clock())))
            except Exception:
                self._known = False

    async def _read_manifest(self, context_key, deadline, *, expired=False):
        raw = await self._io(self._get_client().getrange(self._key(context_key), 0, MAX_MANIFEST_BYTES), deadline)
        if raw == b"":
            return None
        if type(raw) is not bytes:
            raise CacheFailure("snapshot_manifest")
        return validate_manifest(raw, self.config, self.installation_id, self._signing_key, context_key,
                                 self.wall_clock(), allow_expired=expired)

    async def _reconcile(self, deadline):
        client = self._get_client()
        sizes, cursor = {}, 0
        for _ in range(MAX_SCAN_PAGES):
            cursor, keys = await self._io(client.scan(cursor=cursor, match=self.prefix + "*", count=128), deadline)
            if type(keys) not in (list, tuple) or len(keys) > MAX_NAMESPACE_KEYS:
                raise CacheFailure("snapshot_capacity")
            for key in keys:
                if type(key) is bytes:
                    key = key.decode("ascii")
                if type(key) is not str or len(key) > 256 or not key.startswith(self.prefix):
                    raise CacheFailure("snapshot_manifest")
                if key in sizes:
                    continue
                if len(sizes) >= MAX_NAMESPACE_KEYS:
                    raise CacheFailure("snapshot_capacity")
                size = await self._io(client.strlen(key), deadline)
                ttl = await self._io(client.ttl(key), deadline)
                if type(size) is not int or size < 0 or type(ttl) is not int:
                    raise CacheFailure("snapshot_manifest")
                if ttl == -2:
                    continue
                sizes[key] = (size, self.wall_clock() + ttl if ttl >= 0 else math.inf)
                if key.endswith(":manifest"):
                    # Read only our bounded authenticated metadata during restart.
                    context_key = key[len(self.prefix):-len(":manifest")]
                    if HEX64.fullmatch(context_key):
                        try:
                            await self._read_manifest(context_key, deadline, expired=True)
                        except CacheFailure as error:
                            if error.code != "snapshot_manifest":
                                raise
                            # invalid manifests/opaque orphans still count
            if int(cursor) == 0:
                self._sizes, self._known = sizes, True
                self._pending = 0
                return
        raise CacheFailure("snapshot_capacity")

    async def _refresh(self, deadline):
        if not self._known:
            await self._reconcile(deadline)
        client = self._get_client()
        for key, (_, expiry) in list(self._sizes.items()):
            if expiry <= self.wall_clock():
                ttl = await self._io(client.ttl(key), deadline)
                if ttl == -2:
                    self._sizes.pop(key, None)
                else:
                    size = await self._io(client.strlen(key), deadline)
                    self._sizes[key] = (size, self.wall_clock() + ttl if ttl >= 0 else math.inf)

    async def lookup(self, key: str, *, deadline=None):
        async with self._lock:
            return await self._read_manifest(key, self._deadline(deadline))

    async def check(self, *, deadline=None):
        async with self._lock:
            raw = await self._io(self._get_client().getrange(self._key("0" * 64), 0, MAX_MANIFEST_BYTES),
                                 self._deadline(deadline))
            if type(raw) is not bytes or len(raw) > MAX_MANIFEST_BYTES + 1:
                raise CacheFailure()

    async def _remove_keys(self, keys, deadline):
        for start in range(0, len(keys), self.config.max_pipeline_chunks):
            batch = keys[start:start + self.config.max_pipeline_chunks]
            await self._io(self._get_client().delete(*batch), deadline)
            for key in batch:
                self._sizes.pop(key, None)

    async def save(self, key: str, source: Path, manifest: dict, *, token_state: bytes, deadline=None):
        async with self._lock:
            deadline = self._deadline(deadline)
            self._key(key)
            if (type(token_state) is not bytes or len(token_state) > MAX_SIDECAR_BYTES
                    or len(token_state) != manifest["sidecar_bytes"]
                    or hashlib.sha256(token_state).hexdigest() != manifest["sidecar_sha256"]):
                raise CacheFailure("snapshot_token_state")
            draft = dict(manifest)
            size = draft["byte_length"]
            count = (size + self.config.chunk_bytes - 1) // self.config.chunk_bytes
            draft.update(schema=MANIFEST_SCHEMA, installation_id=self.installation_id, context_key=key,
                         chunk_bytes=self.config.chunk_bytes, chunk_count=count,
                         chunk_sha256=["0" * 64] * count, payload_sha256="0" * 64)
            unsigned = {field: value for field, value in draft.items() if field != "hmac"}
            unsigned["hmac"] = hmac.new(self._signing_key, canonical_json(unsigned), hashlib.sha256).hexdigest()
            validate_manifest(unsigned, self.config, self.installation_id, self._signing_key, key, self.wall_clock())
            await self._refresh(deadline)
            reserve = size + len(token_state) + len(canonical_json(unsigned))
            if self.namespace_bytes + reserve > self.config.max_namespace_bytes:
                raise CacheFailure("snapshot_capacity")
            try:
                old = await self._read_manifest(key, deadline, expired=True)
            except CacheFailure:
                old = None
            self._pending += reserve
            attempted, published = [], False
            fd = None
            try:
                fd = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
                before = os.fstat(fd)
                if not stat.S_ISREG(before.st_mode) or before.st_size != size:
                    raise CacheFailure("snapshot_file")
                digest, hashes, batch = hashlib.sha256(), [], []
                generation = self._generation(unsigned)
                for index in range(count):
                    length = min(self.config.chunk_bytes, size - index * self.config.chunk_bytes)
                    block = await self._file(lambda own_fd, stop, length=length: os.read(own_fd, length), deadline, fd=fd)
                    if len(block) != length:
                        raise CacheFailure("snapshot_file")
                    digest.update(block); hashes.append(hashlib.sha256(block).hexdigest())
                    chunk_key = generation + f"chunk:{index}"
                    ttl = math.floor(draft["expires_at"] - self.wall_clock())
                    if ttl < 1:
                        raise CacheFailure("snapshot_expired")
                    batch.append(("set", (chunk_key, block), {"ex": ttl + 60, "nx": True}))
                    if len(batch) == self.config.max_pipeline_chunks or index == count - 1:
                        for _, args, _ in batch:
                            attempted.append(args[0])
                            self._sizes[args[0]] = (len(args[1]), draft["expires_at"] + 60)
                            self._pending -= len(args[1])
                        results = await self._pipeline(batch, deadline)
                        if type(results) is not list or len(results) != len(batch) or any(item is not True for item in results):
                            raise CacheFailure("snapshot_atomic_write")
                        batch = []
                after = os.fstat(fd)
                path_info = os.stat(source, follow_symlinks=False)
                identity = lambda item: (item.st_dev, item.st_ino, item.st_size, item.st_mtime_ns, item.st_ctime_ns)
                if identity(before) != identity(after) or identity(before) != identity(path_info):
                    raise CacheFailure("snapshot_file")
                token_key = generation + "tokens"
                attempted.append(token_key)
                self._sizes[token_key] = (len(token_state), draft["expires_at"] + 60)
                self._pending -= len(token_state)
                ttl = math.floor(draft["expires_at"] - self.wall_clock())
                if ttl < 1 or await self._io(self._get_client().set(token_key, token_state, ex=ttl + 60, nx=True), deadline) is not True:
                    raise CacheFailure("snapshot_atomic_write")
                unsigned["chunk_sha256"], unsigned["payload_sha256"] = hashes, digest.hexdigest()
                unsigned.pop("hmac")
                unsigned["hmac"] = hmac.new(self._signing_key, canonical_json(unsigned), hashlib.sha256).hexdigest()
                encoded = canonical_json(unsigned)
                validate_manifest(encoded, self.config, self.installation_id, self._signing_key, key, self.wall_clock())
                manifest_key = self._key(key)
                published = True  # uncertain publish must retain its referenced immutable data
                if await self._io(self._get_client().set(manifest_key, encoded, ex=ttl), deadline) is not True:
                    raise CacheFailure("snapshot_atomic_write")
                self._sizes[manifest_key] = (len(encoded), draft["expires_at"])
                self._pending = 0
                if old is not None and old["snapshot_id"] != unsigned["snapshot_id"]:
                    try:
                        await self._remove_keys(self._data_keys(old), deadline)
                    except CacheFailure:
                        self._known = False
                return unsigned
            except BaseException:
                if not published:
                    try:
                        await self._remove_keys(attempted, deadline)
                    except (CacheFailure, asyncio.CancelledError):
                        self._known = False
                    self._pending = 0  # attempted orphans remain conservatively in _sizes
                else:
                    self._known = False
                raise
            finally:
                if fd is not None:
                    os.close(fd)

    async def load(self, manifest: dict, destination: Path, *, deadline=None) -> tuple[int, ...]:
        async with self._lock:
            deadline = self._deadline(deadline)
            value = validate_manifest(manifest, self.config, self.installation_id, self._signing_key,
                                      manifest["context_key"], self.wall_clock())
            keys = self._data_keys(value)
            sidecar = await self._io(self._get_client().getrange(keys[-1], 0, value["sidecar_bytes"]), deadline)
            if (type(sidecar) is not bytes or len(sidecar) != value["sidecar_bytes"]
                    or hashlib.sha256(sidecar).hexdigest() != value["sidecar_sha256"]):
                raise CacheFailure("snapshot_token_state")
            tokens = struct.unpack(f"<{value['token_count']}I", sidecar)
            fd, complete = None, False
            try:
                fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
                digest, written = hashlib.sha256(), 0
                for start in range(0, value["chunk_count"], self.config.max_pipeline_chunks):
                    commands = []
                    for index in range(start, min(start + self.config.max_pipeline_chunks, value["chunk_count"])):
                        length = min(self.config.chunk_bytes, value["byte_length"] - index * self.config.chunk_bytes)
                        commands.append(("getrange", (keys[index], 0, length), {}))
                    blocks = await self._pipeline(commands, deadline)
                    if type(blocks) is not list or len(blocks) != len(commands):
                        raise CacheFailure("snapshot_integrity")
                    for offset, block in enumerate(blocks):
                        index = start + offset
                        length = min(self.config.chunk_bytes, value["byte_length"] - index * self.config.chunk_bytes)
                        if (type(block) is not bytes or len(block) != length
                                or hashlib.sha256(block).hexdigest() != value["chunk_sha256"][index]):
                            raise CacheFailure("snapshot_integrity")
                        digest.update(block)
                        def write_block(own_fd, stop, block=block):
                            view = memoryview(block)
                            while view:
                                if stop.is_set():
                                    raise OSError("snapshot write cancelled")
                                count = os.write(own_fd, view[:1024 * 1024])
                                if count <= 0:
                                    raise OSError("snapshot write made no progress")
                                view = view[count:]
                        await self._file(write_block, deadline, fd=fd)
                        written += length
                if written != value["byte_length"] or digest.hexdigest() != value["payload_sha256"]:
                    raise CacheFailure("snapshot_integrity")
                if os.fstat(fd).st_size != written:
                    raise CacheFailure("snapshot_file")
                complete = True
                return tokens
            finally:
                if fd is not None:
                    os.close(fd)
                    if not complete:
                        destination.unlink(missing_ok=True)

    async def delete(self, key: str, *, deadline=None):
        async with self._lock:
            deadline = self._deadline(deadline)
            try:
                manifest = await self._read_manifest(key, deadline, expired=True)
            except CacheFailure:
                manifest = None
            await self._remove_keys([self._key(key)], deadline)
            if manifest is not None:
                await self._remove_keys(self._data_keys(manifest), deadline)

    async def close(self):
        self._closed = True
        if self._client is not None:
            try:
                await bounded_wait(self._client.aclose(), 0.2)
            except Exception:
                pass
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)
            self._executor = None


class MemoryBinaryClient:
    """Small deterministic fake RESP boundary; never imported from redis-py."""
    def __init__(self, *, wall_clock=time.time):
        self.values, self.expiry = {}, {}
        self.wall_clock = wall_clock
        self.calls = []
        self.max_batch = 0
        self.fail = None

    def _record(self, name, *args):
        self.calls.append((name, *args))
        if self.fail is not None:
            self.fail(name, args)

    def _present(self, key):
        if self.expiry.get(key, math.inf) <= self.wall_clock():
            self.values.pop(key, None); self.expiry.pop(key, None)
        return key in self.values

    async def set(self, key, value, *, ex, nx=False):
        self._record("set", key, len(value))
        if nx and self._present(key): return None
        self.values[key] = bytes(value); self.expiry[key] = self.wall_clock() + ex
        return True

    async def getrange(self, key, start, end):
        self._record("getrange", key, start, end)
        return self.values[key][start:end + 1] if self._present(key) else b""

    async def strlen(self, key):
        self._record("strlen", key)
        return len(self.values[key]) if self._present(key) else 0

    async def ttl(self, key):
        self._record("ttl", key)
        if not self._present(key): return -2
        return math.floor(self.expiry[key] - self.wall_clock()) if key in self.expiry else -1

    async def delete(self, *keys):
        self._record("delete", *keys)
        count = 0
        for key in keys:
            count += int(self._present(key)); self.values.pop(key, None); self.expiry.pop(key, None)
        return count

    async def scan(self, *, cursor=0, match, count):
        self._record("scan", match)
        keys = sorted(key for key in list(self.values) if self._present(key) and fnmatch.fnmatchcase(key, match))
        start = int(cursor); end = start + count
        return (end if end < len(keys) else 0), keys[start:end]

    def pipeline(self, *, transaction=False):
        client = self
        class Pipeline:
            def __init__(self): self.commands = []
            def set(self, *args, **kwargs): self.commands.append(("set", args, kwargs)); return self
            def getrange(self, *args, **kwargs): self.commands.append(("getrange", args, kwargs)); return self
            async def execute(self):
                client.max_batch = max(client.max_batch, len(self.commands))
                return [await getattr(client, name)(*args, **kwargs) for name, args, kwargs in self.commands]
            async def aclose(self): self.commands = []
        return Pipeline()

    async def aclose(self): return None


class FakeSnapshotStore(RedisSnapshotStore):
    def __init__(self, config, installation_id, signing_key, *, client=None, wall_clock=time.time, **kwargs):
        client = MemoryBinaryClient(wall_clock=wall_clock) if client is None else client
        super().__init__(config, installation_id, signing_key, client=client, wall_clock=wall_clock, **kwargs)
