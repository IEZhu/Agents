"""Private spool bridge called only inside the service's exclusive slot transaction."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import math
import os
from pathlib import Path
import re
import secrets
import stat
import time

from .backend import BackendInfo, Completion, PreparedPrompt
from .capabilities import MAPPING_VERSION
from .config import OwnerConfig
from .process import (MAX_SPOOL_ENTRIES, SpoolStateError, bounded_spool_entries, bounded_wait,
                      native_spool_bytes, private_directory)
from .profile import canonical_hash, content_fingerprint
from .redis_store import (BINDING_FIELDS, CacheFailure, MAX_SIDECAR_BYTES, NullSnapshotStore,
                          RedisSnapshotStore, packed_tokens)

_NAME = re.compile(r"^snapshot-[a-f0-9]{32}\.bin$")


@dataclass(frozen=True)
class SnapshotRef:
    context_key: str
    bindings: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class RestoreOutcome:
    token_state: tuple[int, ...] | None = None
    warning: str | None = None
    timings_ms: dict | None = None


@dataclass(frozen=True)
class SaveOutcome:
    warning: str | None = None
    timings_ms: dict | None = None
    fence_required: bool = False


class RestoreFailure(CacheFailure):
    def __init__(self, code, timings_ms, *, native_touched=True):
        super().__init__(code)
        self.native_touched = native_touched
        self.timings_ms = timings_ms


def _private_bytes(path: Path, size: int) -> bytes:
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as stream:
            stream.write(secrets.token_bytes(size))
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077 or info.st_size != size):
            raise CacheFailure("snapshot_private_state")
        value = os.read(fd, size + 1)
        if len(value) != size:
            raise CacheFailure("snapshot_private_state")
        return value
    finally:
        os.close(fd)


class SnapshotManager:
    def __init__(self, config: OwnerConfig, directory: Path | str, *, store_factory=None,
                 environ=None, clock=time.monotonic, wall_clock=time.time):
        self.config = config
        self.directory = Path(directory) / "snapshots"
        self.store_factory = store_factory
        self.environ = environ
        self.clock, self.wall_clock = clock, wall_clock
        self._store = None
        self._lock = asyncio.Lock()
        self._spool_identity = None
        self._reserved = 0
        self._closed = False

    def key_for(self, context, profile_hash: str) -> SnapshotRef:
        """Pure opaque deletion reference: never retains prompt/source/token bytes."""
        captured = getattr(context, "captured", context)
        bindings = {
            "workspace_identity": canonical_hash({"id": captured.workspace_id, "root": captured.workspace_root}),
            "source_root_identity": canonical_hash({"id": captured.source_root_id, "root": captured.source_root,
                                                      "file_identity": captured.source_root_identity}),
            "owner_config_revision": captured.owner_config_revision, "profile_hash": profile_hash,
            "content_fingerprint": content_fingerprint(captured, profile_hash),
            "context_instance_identity": canonical_hash(captured.context_id)}
        key = canonical_hash({"schema": "agents-core/local-kv-context/1", **bindings})
        return SnapshotRef(key, tuple(sorted(bindings.items())))

    def _allowed(self, info: BackendInfo, backend) -> bool:
        return (not self._closed and self.config.redis.enabled and info.snapshot == "supported"
                and info.native_write_limit is True and backend.info is not None
                and backend.info.profile_hash == info.profile_hash and backend.info.epoch == info.epoch
                and backend.info.snapshot == "supported" and backend.info.native_write_limit is True
                and backend.spool is not None)

    def _get_store(self):
        if self._store is None:
            if not self.config.redis.enabled or self._closed:
                self._store = NullSnapshotStore()
            else:
                private_directory(self.directory)
                installation = _private_bytes(self.directory / "installation.id", 16).hex()
                signing_key = _private_bytes(self.directory / "signing.key", 32)
                factory = self.store_factory or RedisSnapshotStore
                self._store = factory(self.config.redis, installation, signing_key, environ=self.environ,
                                      clock=self.clock, wall_clock=self.wall_clock)
        return self._store

    def _deadline(self, deadline):
        return min(deadline, self.clock() + self.config.redis.operation_timeout_seconds)

    def _spool(self, backend) -> Path:
        path = Path(backend.spool)
        info = path.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o077):
            raise CacheFailure("snapshot_spool")
        identity = (info.st_dev, info.st_ino)
        try:
            entries = list(bounded_spool_entries(path, [MAX_SPOOL_ENTRIES]))
        except SpoolStateError:
            raise CacheFailure("snapshot_capacity") from None
        if identity != self._spool_identity:
            # This is the exact current owned slot, never an old run. Old runs
            # require an authenticated inactive lease before startup reclamation.
            for entry, item in entries:
                if (_NAME.fullmatch(entry.name) and stat.S_ISREG(item.st_mode)
                        and item.st_uid == os.geteuid() and item.st_nlink == 1
                        and not stat.S_IMODE(item.st_mode) & 0o022):
                    entry.unlink()
            self._spool_identity = identity
        return path

    def _reserve(self, spool: Path, amount: int):
        try:
            total = self._reserved + native_spool_bytes(self.directory.parent / "native", spool)
        except (OSError, SpoolStateError):
            # Live old writers, legacy state, unsafe files and exhausted scan
            # bounds cannot prove capacity. Snapshot warnings leave generation usable.
            raise CacheFailure("snapshot_spool_capacity") from None
        if total + amount > self.config.redis.max_spool_bytes:
            raise CacheFailure("snapshot_spool_capacity")
        self._reserved += amount

    async def _native(self, backend, action, name, deadline):
        try:
            return await bounded_wait(backend.slot_action(action, name), max(0, min(
                self.config.redis.io_timeout_seconds, deadline - self.clock())))
        except asyncio.TimeoutError:
            raise CacheFailure("snapshot_timeout") from None

    @staticmethod
    def _regular_file(path: Path, maximum: int):
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            value = os.fstat(fd)
            if not stat.S_ISREG(value.st_mode) or value.st_size < 1 or value.st_size > maximum:
                raise CacheFailure("snapshot_file")
            return value
        finally:
            os.close(fd)

    @staticmethod
    def _native_counts(response, action, tokens, size):
        names = ("n_saved", "n_written", "save_ms") if action == "save" else ("n_restored", "n_read", "restore_ms")
        try:
            if (type(response) is not dict or type(response["id_slot"]) is not int or response["id_slot"] != 0
                    or type(response[names[0]]) is not int or response[names[0]] != tokens
                    or type(response[names[1]]) is not int or response[names[1]] != size):
                raise ValueError()
            elapsed = response["timings"][names[2]]
            if type(elapsed) not in (float, int) or not math.isfinite(elapsed) or elapsed < 0:
                raise ValueError()
        except (KeyError, ValueError, TypeError):
            raise CacheFailure("snapshot_native_protocol") from None

    async def restore(self, context, info: BackendInfo, backend, *, deadline: float) -> RestoreOutcome:
        spans = {"snapshot_lookup": 0.0, "snapshot_load": 0.0, "snapshot_restore": 0.0}
        if not self._allowed(info, backend):
            return RestoreOutcome(timings_ms=spans)
        async with self._lock:
            deadline = self._deadline(deadline)
            if deadline <= self.clock():
                return RestoreOutcome(warning="snapshot_timeout", timings_ms=spans)
            ref = self.key_for(context, info.profile_hash)
            path, reserved, native_touched = None, 0, False
            stage, started = "snapshot_lookup", self.clock()
            try:
                store = self._get_store()
                manifest = await store.lookup(ref.context_key, deadline=deadline)
                spans[stage] = max(0, self.clock() - started) * 1000
                started = self.clock()
                if manifest is None:
                    return RestoreOutcome(timings_ms=spans)
                if any(manifest[field] != expected for field, expected in ref.bindings):
                    raise CacheFailure("snapshot_compatibility")
                captured = getattr(context, "captured", context)
                if captured.expires_monotonic <= self.clock():
                    raise CacheFailure("snapshot_expired")
                stage, started = "snapshot_load", self.clock()
                spool = self._spool(backend)
                reserved = manifest["byte_length"] + manifest["sidecar_bytes"]
                self._reserve(spool, reserved)
                path = spool / f"snapshot-{secrets.token_hex(16)}.bin"
                tokens = await store.load(manifest, path, deadline=deadline)
                self._regular_file(path, manifest["byte_length"])
                spans[stage] = max(0, self.clock() - started) * 1000
                started = self.clock()
                if not self._allowed(info, backend) or deadline <= self.clock():
                    raise CacheFailure("snapshot_timeout")
                stage, started = "snapshot_restore", self.clock()
                native_touched = True
                response = await self._native(backend, "restore", path.name, deadline)
                self._native_counts(response, "restore", len(tokens), manifest["byte_length"])
                spans[stage] = max(0, self.clock() - started) * 1000
                return RestoreOutcome(tokens, timings_ms=spans)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                spans[stage] += max(0, self.clock() - started) * 1000
                warning = error.code if isinstance(error, CacheFailure) else "snapshot_unavailable"
                if native_touched:
                    raise RestoreFailure(warning, spans) from None
                return RestoreOutcome(warning=warning, timings_ms=spans)
            finally:
                if path is not None:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                self._reserved = max(0, self._reserved - reserved)

    async def save(self, context, info: BackendInfo, backend, completion: Completion, prepared: PreparedPrompt,
                   *, deadline: float) -> SaveOutcome:
        spans = {"snapshot_save": 0.0, "snapshot_store": 0.0}
        if not self._allowed(info, backend) or completion.saved_token_state is None:
            return SaveOutcome(timings_ms=spans)
        async with self._lock:
            deadline = self._deadline(deadline)
            if deadline <= self.clock():
                return SaveOutcome(warning="snapshot_timeout", timings_ms=spans)
            path, reserved, native_pending = None, 0, False
            stage, started = "snapshot_store", self.clock()
            try:
                captured = getattr(context, "captured", context)
                state = completion.saved_token_state
                if (state[:len(prepared.tokens)] != prepared.tokens or completion.input_tokens != len(prepared.tokens)
                        or len(completion.output_ids) != completion.output_tokens
                        or state != prepared.tokens + completion.output_ids[:-1]):
                    raise CacheFailure("snapshot_token_state")
                sidecar = packed_tokens(state)
                rendered_tokens = packed_tokens(prepared.tokens)
                remaining = min(captured.expires_monotonic - self.clock(), self.config.redis.ttl_seconds)
                if remaining < 1:
                    raise CacheFailure("snapshot_expired")
                ref, store = self.key_for(context, info.profile_hash), self._get_store()
                spool = self._spool(backend)
                reserved = self.config.redis.max_snapshot_bytes + len(sidecar)
                self._reserve(spool, reserved)
                snapshot_id = secrets.token_hex(16)
                path = spool / f"snapshot-{snapshot_id}.bin"
                spans[stage] += max(0, self.clock() - started) * 1000
                stage, started = "snapshot_save", self.clock()
                native_pending = True
                response = await self._native(backend, "save", path.name, deadline)
                file_info = self._regular_file(path, self.config.redis.max_snapshot_bytes)
                self._native_counts(response, "save", len(state), file_info.st_size)
                if not self._allowed(info, backend):
                    raise CacheFailure("snapshot_native_protocol")
                native_pending = False
                spans[stage] += max(0, self.clock() - started) * 1000
                stage, started = "snapshot_store", self.clock()
                created = self.wall_clock()
                remaining = min(captured.expires_monotonic - self.clock(), self.config.redis.ttl_seconds)
                if remaining < 1:
                    raise CacheFailure("snapshot_expired")
                digest = hashlib.sha256(sidecar).hexdigest()
                metadata = {**dict(ref.bindings), "context_key": ref.context_key, "snapshot_id": snapshot_id,
                            "created_at": created, "expires_at": created + remaining, "byte_length": file_info.st_size,
                            "token_count": len(state), "token_state_sha256": digest, "sidecar_bytes": len(sidecar),
                            "sidecar_sha256": digest, "rendered_input_sha256": hashlib.sha256(prepared.text.encode()).hexdigest(),
                            "rendered_tokens_sha256": hashlib.sha256(rendered_tokens).hexdigest(),
                            "mapping_version": MAPPING_VERSION}
                await store.save(ref.context_key, path, metadata, token_state=sidecar, deadline=deadline)
                spans[stage] += max(0, self.clock() - started) * 1000
                return SaveOutcome(timings_ms=spans)
            except asyncio.CancelledError:
                raise
            except Exception as error:
                spans[stage] += max(0, self.clock() - started) * 1000
                warning = error.code if isinstance(error, CacheFailure) else "snapshot_unavailable"
                return SaveOutcome(warning, spans, native_pending)
            finally:
                if path is not None:
                    try:
                        path.unlink(missing_ok=True)
                    except OSError:
                        pass
                self._reserved = max(0, self._reserved - reserved)

    async def delete_key(self, ref: SnapshotRef):
        if not self.config.redis.enabled or self._closed:
            return None
        async with self._lock:
            try:
                if type(ref) is not SnapshotRef:
                    raise CacheFailure("snapshot_compatibility")
                await self._get_store().delete(ref.context_key, deadline=self._deadline(math.inf))
                return None
            except Exception:
                return "snapshot_delete_failed"

    async def delete(self, context, profile_hash):
        return await self.delete_key(self.key_for(context, profile_hash))

    async def check_store(self, *, deadline: float):
        """Owner diagnostic only: confirm an actual bounded store command succeeds."""
        if not self.config.redis.enabled or self._closed:
            return "snapshot_disabled"
        async with self._lock:
            try:
                await self._get_store().check(deadline=self._deadline(deadline))
                return None
            except CacheFailure as error:
                return error.code
            except Exception:
                return "snapshot_unavailable"

    async def close(self):
        self._closed = True
        if self._store is not None:
            await self._store.close()
