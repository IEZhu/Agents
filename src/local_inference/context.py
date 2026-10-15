"""Immutable capture plus event-loop-owned capability/accounting registry.

Registry mutations are synchronous and contain no await: the scheduler calls them
on its own event loop. ``capture_sources`` and ``check_freshness`` are read-only
and may run in a worker thread. No model, network or Redis dependency exists here.
"""
from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import stat
import time
import uuid
from collections import OrderedDict
import copy
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

from .config import OwnerConfig
from .models import (CapturedContext, ContextCreateRequest, ContextGrantRequest,
                     ContextRecord, ContextReleaseRequest, ContextRevokeRequest,
                     GenerateRequest, GrantRecord, JobRecord, LocalInferenceError, SourceFile)
from .profile import canonical_hash


@dataclass(frozen=True)
class SourceCapture:
    workspace_id: str
    workspace_root: str
    source_root_id: str | None
    source_root: str | None
    root_identity: tuple[int, int] | None
    sources: tuple[SourceFile, ...]


@dataclass(frozen=True)
class ReleaseTombstone:
    workspace_id: str
    workspace_root: str
    owner_digest: str
    expires_monotonic: float
    cleanup_state: str = "done"


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _bytes(text: str) -> bytes:
    try:
        return text.encode("utf-8")
    except UnicodeEncodeError:
        raise LocalInferenceError("input_too_large", "Text must contain valid UTF-8 characters.") from None


def _workspace(client) -> tuple[str, str]:
    if client.transport != "http":
        raise LocalInferenceError("transport_unsupported", "Local inference requires the shared HTTP daemon.")
    if client.error or client.root is None or not client.workspace_id:
        code = "workspace_invalid" if client.error and "invalid" in client.error else "workspace_required"
        raise LocalInferenceError(code, "A valid registered HTTP workspace is required.")
    try:
        root = Path(client.root).resolve(strict=True)
        if not root.is_dir():
            raise ValueError()
    except (OSError, ValueError, RuntimeError):
        raise LocalInferenceError("workspace_invalid", "The registered workspace is invalid.") from None
    return client.workspace_id, str(root)


def _parts(path: str) -> list[str]:
    # Reject instead of normalizing ambiguous spellings or traversal.
    if not path or path.startswith("/") or "\x00" in path or "\\" in path:
        raise LocalInferenceError("permission_denied", "A source path is not an allowed relative path.")
    parts = path.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise LocalInferenceError("permission_denied", "A source path is not an allowed relative path.")
    return parts


def _open_root(root: str) -> int:
    """Anchor every absolute component using no-follow directory descriptors."""
    if not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        raise LocalInferenceError("unsupported", "Safe source capture requires POSIX directory descriptors.")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in Path(root).parts[1:]:
            new_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = new_fd
        return fd
    except BaseException:
        os.close(fd)
        raise


def _file_identity(info) -> tuple[int, int, int, int, int]:
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _read_source(root_fd: int, path: str, limit: int) -> SourceFile:
    parts = _parts(path)
    directory = os.dup(root_fd)
    file_fd = None
    try:
        for part in parts[:-1]:
            new_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=directory)
            os.close(directory)
            directory = new_fd
        file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        before = os.fstat(file_fd)
        if not stat.S_ISREG(before.st_mode):
            raise LocalInferenceError("permission_denied", "Sources must be regular UTF-8 files.")
        if before.st_size > limit:
            raise LocalInferenceError("input_too_large", "A captured source exceeds the byte limit.")
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(file_fd, min(65536, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(file_fd)
        if len(data) > limit:
            raise LocalInferenceError("input_too_large", "A captured source exceeds the byte limit.")
        if _file_identity(before) != _file_identity(after) or len(data) != before.st_size:
            raise LocalInferenceError("source_changed", "A source changed during capture.")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError:
            raise LocalInferenceError("permission_denied", "Sources must be regular UTF-8 files.") from None
        return SourceFile(path, hashlib.sha256(data).hexdigest(), content, len(data),
                          before.st_dev, before.st_ino, before.st_mtime_ns)
    except OSError:
        raise LocalInferenceError("permission_denied", "A source could not be captured safely.") from None
    finally:
        if file_fd is not None:
            os.close(file_fd)
        os.close(directory)


def capture_sources(config: OwnerConfig, client, request: ContextCreateRequest) -> SourceCapture:
    """Bounded safe filesystem capture, without registry mutation."""
    workspace_id, workspace_root = _workspace(client)
    if len(request.source_paths) > config.max_source_files:
        raise LocalInferenceError("input_too_large", "Too many source files were requested.")
    total = len(_bytes(request.common_system)) + len(_bytes(request.shared_context))
    if total > config.max_context_bytes:
        raise LocalInferenceError("input_too_large", "The shared context exceeds the byte limit.")
    if not request.source_paths and request.source_root_id is None:
        if not request.shared_context:
            raise LocalInferenceError("input_too_large", "Shared context or captured source data is required.")
        return SourceCapture(workspace_id, workspace_root, None, None, None, ())
    binding = config.source_roots.get(request.source_root_id or "")
    if binding is None:
        raise LocalInferenceError("permission_denied", "An owner-authorized source root is required.")
    try:
        expected = str(Path(binding.workspace_root).resolve(strict=True))
        root = str(Path(binding.path).resolve(strict=True))
        if expected != workspace_root:
            raise LocalInferenceError("permission_denied", "The source root is not authorized for this workspace.")
        fd = _open_root(root)
    except (OSError, ValueError, RuntimeError):
        raise LocalInferenceError("permission_denied", "The configured source root is unavailable.") from None
    try:
        info = os.fstat(fd)
        sources = []
        for path in request.source_paths:
            remaining = config.max_context_bytes - total
            source = _read_source(fd, path, min(config.max_source_file_bytes, remaining))
            sources.append(source)
            total += source.size
        if not request.shared_context and not any(source.size for source in sources):
            raise LocalInferenceError("input_too_large", "Shared context or captured source data is required.")
        return SourceCapture(workspace_id, workspace_root, request.source_root_id, root,
                             (info.st_dev, info.st_ino), tuple(sources))
    finally:
        os.close(fd)


class ContextRegistry:
    def __init__(self, config: OwnerConfig, *, clock: Callable[[], float] = time.monotonic,
                 wall_clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
                 token_factory: Callable[[], str] = lambda: secrets.token_urlsafe(32),
                 id_factory: Callable[[], str] = lambda: str(uuid.uuid4())):
        self.config = config
        self.owner_config_revision = config.revision
        self.clock = clock
        self.wall_clock = wall_clock
        self.token_factory = token_factory
        self.id_factory = id_factory
        self.contexts: dict[str, ContextRecord] = {}
        self.release_tombstones: OrderedDict[str, ReleaseTombstone] = OrderedDict()

    def _enabled(self):
        if not self.config.enabled:
            raise LocalInferenceError("disabled", "Local inference is disabled.")

    def _purge(self):
        now = self.clock()
        for context_id, record in list(self.contexts.items()):
            if record.captured.expires_monotonic <= now:
                self._invalidate(record)
                del self.contexts[context_id]
        for context_id, tombstone in list(self.release_tombstones.items()):
            if tombstone.expires_monotonic <= now:
                del self.release_tombstones[context_id]

    def _lookup(self, client, context_id: str) -> ContextRecord:
        self._enabled()
        workspace_id, root = _workspace(client)
        self._purge()
        record = self.contexts.get(context_id)
        if (record is None or record.released or record.captured.workspace_id != workspace_id
                or record.captured.workspace_root != root
                or record.captured.owner_config_revision != self.owner_config_revision):
            raise LocalInferenceError("context_unavailable", "The context is unavailable.")
        return record

    def create(self, client, request: ContextCreateRequest, *, capture: SourceCapture | None = None) -> dict:
        self._enabled()
        self._purge()
        if len(self.contexts) >= self.config.max_contexts:
            raise LocalInferenceError("busy", "The context registry is full.", retryable=True)
        if (request.ttl_seconds > self.config.max_context_ttl_seconds
                or request.max_requests > self.config.max_context_requests
                or request.max_total_output_tokens > self.config.max_context_output_tokens):
            raise LocalInferenceError("budget_exhausted", "Requested context limits exceed owner limits.")
        captured_sources = capture or capture_sources(self.config, client, request)
        if (captured_sources.workspace_id, captured_sources.workspace_root) != _workspace(client):
            raise LocalInferenceError("workspace_invalid", "Captured sources belong to a different workspace.")
        now, wall = self.clock(), self.wall_clock()
        context_id, grant_id = "ctx_" + self.id_factory(), "grant_" + self.id_factory()
        owner_token, access_token = self.token_factory(), self.token_factory()
        captured = CapturedContext(context_id, captured_sources.workspace_id,
                                   captured_sources.workspace_root, captured_sources.source_root_id,
                                   captured_sources.source_root, captured_sources.root_identity,
                                   request.common_system, request.shared_context, captured_sources.sources,
                                   self.owner_config_revision, wall, wall + timedelta(seconds=request.ttl_seconds),
                                   now + request.ttl_seconds, request.max_requests,
                                   request.max_total_output_tokens,
                                   request.artifact_manifest.model_dump(mode="json", by_alias=True)
                                   if request.artifact_manifest is not None else None)
        owner_grant = GrantRecord(grant_id, _digest(access_token), "owner", captured.expires_monotonic,
                                  request.max_requests, request.max_total_output_tokens,
                                  request.max_requests, request.max_total_output_tokens, owner_access=True)
        record = ContextRecord(captured, _digest(owner_token), {grant_id: owner_grant},
                               remaining_requests=request.max_requests,
                               remaining_output_tokens=request.max_total_output_tokens)
        self.contexts[context_id] = record
        return {"context_id": context_id, "owner_token": owner_token, "access_token": access_token,
                "grant_id": grant_id, "expires_at": captured.expires_at.isoformat(),
                "max_requests": request.max_requests, "max_total_output_tokens": request.max_total_output_tokens,
                "source_root_id": captured.source_root_id, "source_root": captured.source_root,
                "sources": [{"path": source.path, "sha256": source.sha256} for source in captured.sources],
                "state": "registered"}

    def authenticate_owner(self, client, context_id: str, owner_token: str) -> ContextRecord:
        record = self._lookup(client, context_id)
        if not hmac.compare_digest(record.owner_digest, _digest(owner_token)):
            raise LocalInferenceError("permission_denied", "The capability is not authorized.")
        return record

    def authenticate_access(self, client, context_id: str, access_token: str) -> tuple[ContextRecord, GrantRecord]:
        record = self._lookup(client, context_id)
        digest = _digest(access_token)
        for grant in record.grants.values():
            if hmac.compare_digest(grant.capability_digest, digest):
                if grant.revoked or grant.expires_monotonic <= self.clock():
                    break
                return record, grant
        raise LocalInferenceError("permission_denied", "The capability is not authorized.")

    def grant(self, client, request: ContextGrantRequest) -> dict:
        record = self.authenticate_owner(client, request.context_id, request.owner_token)
        if record.stale:
            raise LocalInferenceError("source_changed", "The captured sources have changed.")
        if len(record.grants) >= self.config.max_grants_per_context:
            raise LocalInferenceError("busy", "The context grant limit has been reached.")
        now = self.clock()
        remaining_ttl = record.captured.expires_monotonic - now
        ttl = remaining_ttl if request.ttl_seconds is None else request.ttl_seconds
        if (ttl > remaining_ttl or request.max_requests > record.remaining_requests
                or request.max_total_output_tokens > record.remaining_output_tokens):
            raise LocalInferenceError("budget_exhausted", "Requested grant limits exceed the remaining context limits.")
        grant_id, access_token = "grant_" + self.id_factory(), self.token_factory()
        grant = GrantRecord(grant_id, _digest(access_token), request.label, min(record.captured.expires_monotonic, now + ttl),
                            request.max_requests, request.max_total_output_tokens,
                            request.max_requests, request.max_total_output_tokens)
        record.grants[grant_id] = grant
        return {"grant_id": grant_id, "access_token": access_token, "max_requests": request.max_requests,
                "max_total_output_tokens": request.max_total_output_tokens,
                "expires_at": (self.wall_clock() + timedelta(seconds=ttl)).isoformat()}

    def revoke(self, client, request: ContextRevokeRequest) -> dict:
        record = self.authenticate_owner(client, request.context_id, request.owner_token)
        grant = record.grants.get(request.grant_id)
        if grant is None or grant.owner_access:
            raise LocalInferenceError("permission_denied", "The grant cannot be revoked.")
        grant.revoked = True
        cancelling = self._cancel_jobs(record, grant.grant_id)
        return {"grant_id": grant.grant_id, "cleanup_state": "cancelling" if cancelling else "done"}

    def admit_job(self, client, request: GenerateRequest, *, admission_available: bool = True
                  ) -> tuple[ContextRecord, GrantRecord, JobRecord, bool]:
        record, grant = self.authenticate_access(client, request.context_id, request.access_token)
        arguments = request.model_dump(exclude={"access_token"})
        arguments["grant_id"] = grant.grant_id
        argument_hash = canonical_hash(arguments)
        existing_id = record.requests.get(request.request_id)
        if existing_id is not None:
            job = record.jobs[existing_id]
            if job.grant_id != grant.grant_id:
                raise LocalInferenceError("permission_denied", "The request ID belongs to another grant.")
            if not hmac.compare_digest(job.argument_hash, argument_hash):
                raise LocalInferenceError("request_conflict", "The request ID has different arguments.")
            return record, grant, job, True
        if record.stale:
            raise LocalInferenceError("source_changed", "The captured sources have changed.")
        if len(record.jobs) >= self.config.max_context_jobs:
            raise LocalInferenceError("job_limit_reached", "The context retained-job limit has been reached.")
        if not admission_available:
            raise LocalInferenceError("busy", "The generation queue is full.", retryable=True)
        if len(_bytes(request.task)) > self.config.max_task_bytes:
            raise LocalInferenceError("input_too_large", "The task exceeds the byte limit.")
        if request.max_output_tokens > self.config.max_output_tokens or request.timeout_seconds > self.config.max_job_seconds:
            raise LocalInferenceError("budget_exhausted", "Generation limits exceed the owner limits.")
        self._check_budget(record, grant, request.max_output_tokens)
        now = self.clock()
        deadline = min(now + request.timeout_seconds, record.captured.expires_monotonic, grant.expires_monotonic)
        # Discard the bearer token immediately; argument hashes already omit it.
        private_request = request.model_copy(update={"access_token": "redacted"})
        job = JobRecord("job_" + self.id_factory(), request.request_id, record.context_id, grant.grant_id,
                        argument_hash, now, deadline, self.wall_clock() + timedelta(seconds=deadline-now), private_request)
        record.jobs[job.job_id] = job
        record.requests[job.request_id] = job.job_id
        return record, grant, job, False

    def get_job(self, client, context_id: str, capability_token: str, job_id: str
                ) -> tuple[ContextRecord, GrantRecord | None, JobRecord]:
        record = self._lookup(client, context_id)
        owner = hmac.compare_digest(record.owner_digest, _digest(capability_token))
        grant = None
        if not owner:
            record, grant = self.authenticate_access(client, context_id, capability_token)
        job = record.jobs.get(job_id)
        if job is None:
            raise LocalInferenceError("context_unavailable", "The job is unavailable.")
        if not owner and job.grant_id != grant.grant_id:
            raise LocalInferenceError("permission_denied", "The capability cannot inspect this job.")
        return record, grant, job

    @staticmethod
    def _check_budget(record: ContextRecord, grant: GrantRecord, output: int):
        if min(record.remaining_requests, grant.remaining_requests) < 1:
            raise LocalInferenceError("budget_exhausted", "The context or grant has no model requests remaining.")
        if min(record.remaining_output_tokens, grant.remaining_output_tokens) < output:
            raise LocalInferenceError("budget_exhausted", "The context or grant has insufficient output budget.")

    def reserve_dispatch(self, record: ContextRecord, grant: GrantRecord, job: JobRecord):
        now = self.clock()
        if record.released or record.captured.expires_monotonic <= now:
            raise LocalInferenceError("context_unavailable", "The context is unavailable.")
        if grant.revoked or grant.expires_monotonic <= now:
            raise LocalInferenceError("permission_denied", "The capability is not authorized.")
        if record.stale:
            raise LocalInferenceError("source_changed", "The captured sources have changed.")
        if job.cancel_event.is_set():
            raise LocalInferenceError("cancelled", "The generation was cancelled.")
        if job.deadline <= now:
            raise LocalInferenceError("deadline_exceeded", "The generation deadline has expired.")
        if job.dispatched or job.request is None or job.state not in ("queued", "running"):
            raise LocalInferenceError("backend_protocol_error", "The job cannot be dispatched again.")
        if job.context_id != record.context_id or job.grant_id != grant.grant_id:
            raise LocalInferenceError("permission_denied", "The job does not belong to this context and grant.")
        output = job.request.max_output_tokens
        self._check_budget(record, grant, output)
        record.remaining_requests -= 1
        grant.remaining_requests -= 1
        record.remaining_output_tokens -= output
        grant.remaining_output_tokens -= output
        job.dispatched, job.reserved_output_tokens = True, output
        job.state = "running"

    def reconcile_dispatch(self, record: ContextRecord, grant: GrantRecord, job: JobRecord,
                           observed_output_tokens: int | None = None):
        """Unknown/ambiguous outcomes consume the entire output reservation."""
        if not job.dispatched or job.reconciled:
            return
        used = job.reserved_output_tokens if observed_output_tokens is None else observed_output_tokens
        if type(used) is not int or not 0 <= used <= job.reserved_output_tokens:
            raise LocalInferenceError("backend_protocol_error", "Generated token accounting is invalid.",
                                      generation_dispatched=True)
        refund = job.reserved_output_tokens - used
        record.remaining_output_tokens += refund
        grant.remaining_output_tokens += refund
        job.reconciled = True
        job.consumed_output_tokens = used

    @staticmethod
    def withdraw_undispatched(record: ContextRecord, grant: GrantRecord, job: JobRecord):
        """Return a reservation only when the adapter proves no HTTP attempt occurred."""
        if not job.dispatched or job.reconciled:
            raise LocalInferenceError("backend_protocol_error", "The dispatch reservation cannot be withdrawn.")
        record.remaining_requests += 1
        grant.remaining_requests += 1
        record.remaining_output_tokens += job.reserved_output_tokens
        grant.remaining_output_tokens += job.reserved_output_tokens
        job.dispatched = False
        job.reserved_output_tokens = 0
        job.consumed_output_tokens = 0
        job.reconciled = True

    def finish_job(self, job: JobRecord, envelope: dict, state: str):
        if job.done_event.is_set():
            return
        if state not in ("succeeded", "failed", "cancelled"):
            raise ValueError("terminal job state required")
        # Scheduler supplies its validated bounded result schema.
        from .profile import canonical_json
        max_result_bytes = self.config.max_output_tokens * 32 + 65536
        if len(canonical_json(envelope)) > max_result_bytes:
            envelope = LocalInferenceError("backend_protocol_error", "The backend result exceeds its limit.",
                                           generation_dispatched=job.dispatched).envelope()
            state = "failed"
        if envelope.get("status") == "error":
            envelope = {**envelope, "consumed_budget": envelope.get("consumed_budget", {
                "model_requests": int(job.dispatched),
                "output_tokens": (job.consumed_output_tokens if job.consumed_output_tokens is not None
                                  else job.reserved_output_tokens)})}
        job.terminal_result = copy.deepcopy(envelope)
        job.state = state
        job.request = None
        job.done_event.set()

    def bind_profile(self, record: ContextRecord, stable_profile_hash: str):
        if len(stable_profile_hash) != 64 or any(ch not in "0123456789abcdef" for ch in stable_profile_hash):
            raise ValueError("profile hash must be lowercase SHA256")
        if record.profile_hash is not None and record.profile_hash != stable_profile_hash:
            raise LocalInferenceError("context_unavailable", "A new context is required for the changed model profile.")
        record.profile_hash = stable_profile_hash

    def check_freshness(self, record: ContextRecord):
        """Read-only safe-open/hash check; call invalidate_sources on the owner loop after failure."""
        captured = record.captured
        if captured.source_root is None:
            return
        fd = None
        try:
            fd = _open_root(captured.source_root)
            info = os.fstat(fd)
            if (info.st_dev, info.st_ino) != captured.source_root_identity:
                raise ValueError()
            remaining = self.config.max_context_bytes
            for source in captured.sources:
                current = _read_source(fd, source.path, min(self.config.max_source_file_bytes, remaining))
                if current.sha256 != source.sha256:
                    raise ValueError()
                remaining -= current.size
        except (OSError, ValueError, LocalInferenceError):
            raise LocalInferenceError("source_changed", "The captured sources have changed.") from None
        finally:
            if fd is not None:
                os.close(fd)

    def invalidate_sources(self, record: ContextRecord, *, exclude_job_id: str | None = None):
        record.stale = True
        for grant in record.grants.values():
            grant.revoked = True
        self._cancel_jobs(record, exclude_job_id=exclude_job_id)

    def _cancel_jobs(self, record: ContextRecord, grant_id: str | None = None,
                     *, exclude_job_id: str | None = None) -> bool:
        cancelling = False
        for job in record.jobs.values():
            if job.job_id == exclude_job_id:
                continue
            if (grant_id is not None and job.grant_id != grant_id) or job.done_event.is_set():
                continue
            job.cancel_event.set()
            if job.dispatched:
                job.state = "cancelling"
                cancelling = True
            else:
                self.finish_job(job, LocalInferenceError("cancelled", "The generation was cancelled.").envelope(), "cancelled")
        return cancelling

    def _invalidate(self, record: ContextRecord):
        record.released = True
        for grant in record.grants.values():
            grant.revoked = True
        self._cancel_jobs(record)
        for job in record.jobs.values():
            job.request = None
            job.terminal_result = None
        record.jobs.clear()
        record.requests.clear()
        record.captured = replace(record.captured, common_system="", shared_context="", sources=())

    def release(self, client, request: ContextReleaseRequest) -> dict:
        self._enabled()
        self._purge()
        workspace_id, root = _workspace(client)
        tombstone = self.release_tombstones.get(request.context_id)
        if tombstone is not None:
            if (tombstone.workspace_id != workspace_id or tombstone.workspace_root != root):
                raise LocalInferenceError("context_unavailable", "The context is unavailable.")
            if not hmac.compare_digest(tombstone.owner_digest, _digest(request.owner_token)):
                raise LocalInferenceError("permission_denied", "The capability is not authorized.")
            return {"cleanup_state": tombstone.cleanup_state}
        record = self.authenticate_owner(client, request.context_id, request.owner_token)
        cancelling = any(job.dispatched and not job.done_event.is_set() for job in record.jobs.values())
        self._invalidate(record)
        del self.contexts[record.context_id]
        self.release_tombstones[record.context_id] = ReleaseTombstone(
            workspace_id, root, record.owner_digest, self.clock() + self.config.max_context_ttl_seconds,
            "cancelling" if cancelling else "done")
        while len(self.release_tombstones) > self.config.max_release_tombstones:
            self.release_tombstones.popitem(last=False)
        return {"cleanup_state": "cancelling" if cancelling else "done"}

    def mark_release_pending(self, context_id: str):
        tombstone = self.release_tombstones.get(context_id)
        if tombstone is not None:
            self.release_tombstones[context_id] = replace(tombstone, cleanup_state="cancelling")

    def mark_release_done(self, context_id: str):
        tombstone = self.release_tombstones.get(context_id)
        if tombstone is not None:
            self.release_tombstones[context_id] = replace(tombstone, cleanup_state="done")

    @staticmethod
    def remaining_budgets(record: ContextRecord, grant: GrantRecord) -> dict:
        return {"context": {"requests": record.remaining_requests, "output_tokens": record.remaining_output_tokens},
                "grant": {"requests": grant.remaining_requests, "output_tokens": grant.remaining_output_tokens}}
