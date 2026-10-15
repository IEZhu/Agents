"""One broker, one FIFO engine transaction, short and reconnectable job calls."""
from __future__ import annotations

import asyncio
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import copy
from pathlib import Path
import time

from pydantic import ValidationError

from .backend import Backend, Completion, PreparedPrompt, longest_common_prefix
from .config import OwnerConfig, load_owner_config
from .context import ContextRegistry, capture_sources
from .metrics import Metrics, timings
from .models import (ContextCreateRequest, ContextGrantRequest, ContextReleaseRequest,
                     ContextRevokeRequest, GenerateRequest, GenerationCancelRequest,
                     GenerationResultRequest, LocalInferenceError, success)
from .render import messages

REQUEST_MODELS = {
    "local_context_create": ContextCreateRequest,
    "local_context_grant": ContextGrantRequest,
    "local_context_revoke": ContextRevokeRequest,
    "local_generate": GenerateRequest,
    "local_generation_result": GenerationResultRequest,
    "local_generation_cancel": GenerationCancelRequest,
    "local_context_release": ContextReleaseRequest,
}


class LocalInferenceService:
    def __init__(self, directory: Path | str, *, config: OwnerConfig | None = None,
                 backend: Backend | None = None, registry: ContextRegistry | None = None,
                 backend_factory=None, snapshots=None, snapshot_factory=None, clock=time.monotonic,
                 artifact_service=None):
        self.directory = Path(directory)
        self.config = config
        self.registry = registry
        self.backend = backend
        self.backend_factory = backend_factory
        self.snapshots = snapshots
        self.snapshot_factory = snapshot_factory
        self.artifact_service = artifact_service
        self.clock = registry.clock if registry else clock
        self.metrics = Metrics()
        self._config_error = None
        self._queue = deque()
        self._active = None
        self._cleanup = {}  # context ID -> immutable snapshot reference, never captured text
        self._wake = asyncio.Event()
        self._worker_task = None
        self._files = None
        self._file_pending = set()
        self._file_slots = asyncio.Semaphore(2)
        self._draining = False
        self._closed = False
        self._unavailable = False
        self._resident = None  # (context_id, stable_profile, engine_epoch, exact token state)
        self._orphans = set()

    def _load(self):
        if self._config_error:
            raise self._config_error
        if self.config is None:
            try:
                self.config = load_owner_config()
            except LocalInferenceError as error:
                self._config_error = error
                raise
        if self.registry is None:
            self.registry = ContextRegistry(self.config, clock=self.clock)
        return self.config

    def _enabled(self):
        config = self._load()
        if not config.enabled:
            raise LocalInferenceError("disabled", "Local inference is disabled.")
        if self._closed:
            raise LocalInferenceError("backend_unavailable", "Local inference is stopping.")
        return config

    @property
    def queued(self):
        return sum(not item[3].done_event.is_set() for item in self._queue)

    @property
    def pending(self):
        return (self.queued + int(self._active is not None) + len(self._cleanup) +
                len(self._orphans) + len(self._file_pending))

    def health(self):
        # Ordinary daemon health never opens optional config or starts an engine.
        state = getattr(self.backend, "state", "not_started")
        return {"local_inference_pending": self.pending,
                "local_inference_ready": state == "ready" and not self._unavailable,
                "local_inference_state": state}

    def status(self):
        try:
            config = self._load()
        except LocalInferenceError as error:
            return error.envelope()
        info = getattr(self.backend, "info", None)
        state = "disabled" if not config.enabled else getattr(self.backend, "state", "not_started")
        if self._unavailable:
            state = "unavailable"
        return success({"enabled": config.enabled, "backend_state": state,
                        "model_label": config.model_label,
                        "profile_hash": info.profile_hash if info else None,
                        "capabilities": {"resident": info.resident if info else "unknown",
                                         "snapshot": info.snapshot if info else "unknown"},
                        "active": int(self._active is not None), "queued": self.queued,
                        "limits": {"effective_context_tokens": (config.context_tokens - info.context_headroom if info else None),
                                   "context_headroom": info.context_headroom if info else None,
                                   **{name: getattr(config, name) for name in (
                            "context_tokens", "max_contexts", "max_grants_per_context", "max_queue",
                            "max_context_bytes", "max_source_files", "max_source_file_bytes", "max_task_bytes",
                            "max_output_tokens", "max_context_requests", "max_context_output_tokens",
                            "max_context_ttl_seconds", "max_job_seconds", "max_context_jobs",
                            "max_release_tombstones")}}})

    async def call(self, name: str, client, arguments: dict):
        """The sole public SDK/MCP boundary. No tracing or automatic output logging."""
        try:
            if name == "local_inference_status":
                if arguments:
                    raise ValueError("Invalid local inference arguments.")
                if client.transport != "http":
                    raise LocalInferenceError("transport_unsupported", "Local inference requires the shared HTTP daemon.")
                return self.status()
            model = REQUEST_MODELS[name]
            try:
                request = model.model_validate(arguments)
            except ValidationError:
                raise ValueError("Invalid local inference arguments.") from None
            if client.transport != "http":
                raise LocalInferenceError("transport_unsupported", "Local inference requires the shared HTTP daemon.")
            self._enabled()
            method = {"local_context_create": self.create_context, "local_context_grant": self.grant,
                      "local_context_revoke": self.revoke, "local_generate": self.submit,
                      "local_generation_result": self.result, "local_generation_cancel": self.cancel,
                      "local_context_release": self.release}[name]
            return success(await method(client, request))
        except LocalInferenceError as error:
            return error.envelope()

    async def _file(self, function, *args):
        await self._file_slots.acquire()
        try:
            if self._closed:
                raise LocalInferenceError("backend_unavailable", "Local inference is stopping.")
            if self._files is None:
                self._files = ThreadPoolExecutor(max_workers=2, thread_name_prefix="local-inference-files")
            future = asyncio.get_running_loop().run_in_executor(self._files, function, *args)
        except BaseException:
            self._file_slots.release()
            raise
        self._file_pending.add(future)
        def finished(done):
            self._file_pending.discard(done)
            self._file_slots.release()
            if not done.cancelled():
                done.exception()
        future.add_done_callback(finished)
        # Cancelling the HTTP/coroutine waiter does not stop its file thread.
        # Retain admission until actual completion, so retries cannot grow the
        # executor queue beyond the two explicit file slots.
        return await asyncio.shield(future)

    async def create_context(self, client, request):
        config = self._enabled()
        if len(self._cleanup) >= config.max_contexts:
            raise LocalInferenceError("busy", "Context cleanup is at capacity.", retryable=True)
        if request.artifact_manifest is not None:
            from src.artifacts.store import ArtifactError
            if self.artifact_service is None:
                raise LocalInferenceError("artifact_unavailable", "Artifact storage is unavailable.")
            try:
                budget = config.max_context_bytes - len((request.common_system + request.shared_context).encode("utf-8"))
                hydrated = await self.artifact_service.hydrate(
                    client, request.artifact_manifest.model_dump(mode="json", by_alias=True), max(0, budget))
            except ArtifactError as error:
                raise LocalInferenceError(error.code, error.message, generation_dispatched=False) from None
            request = request.model_copy(update={"shared_context": request.shared_context + hydrated})
        capture = await self._file(capture_sources, config, client, request)
        self._enabled()  # shutdown may have happened during capture
        if len(self._cleanup) >= config.max_contexts:
            raise LocalInferenceError("busy", "Context cleanup is at capacity.", retryable=True)
        return self.registry.create(client, request, capture=capture)

    async def grant(self, client, request):
        self._enabled()
        record = self.registry.authenticate_owner(client, request.context_id, request.owner_token)
        await self._check_artifacts(record)
        return self.registry.grant(client, request)

    async def revoke(self, client, request):
        self._enabled()
        data = self.registry.revoke(client, request)
        self._wake.set()
        return data

    async def release(self, client, request):
        self._enabled()
        ref = None
        record = self.registry.contexts.get(request.context_id)
        if record is not None:
            record = self.registry.authenticate_owner(client, request.context_id, request.owner_token)
            if self.snapshots is not None and record.profile_hash:
                ref = self.snapshots.key_for(record, record.profile_hash)
        data = self.registry.release(client, request)
        needs_cleanup = (ref is not None or self._resident is not None and self._resident[0] == request.context_id
                         or self._active is not None and self._active[1].context_id == request.context_id)
        if record is not None and needs_cleanup:
            self._cleanup[request.context_id] = ref
            self.registry.mark_release_pending(request.context_id)
            self._start_worker()
            data["cleanup_state"] = "cancelling"
        return data

    async def submit(self, client, request):
        config = self._enabled()
        record, _ = self.registry.authenticate_access(client, request.context_id, request.access_token)
        await self._check_artifacts(record)
        # Prune terminal queued cancellations without evicting registry idempotency.
        self._queue = deque(item for item in self._queue if not item[3].done_event.is_set())
        available = (not self._draining and not self._unavailable and
                     (self.queued < config.max_queue or self._active is None and not self._queue))
        try:
            record, grant, job, duplicate = self.registry.admit_job(client, request, admission_available=available)
        except LocalInferenceError as error:
            if self._unavailable and error.code == "busy":
                raise LocalInferenceError("backend_unavailable", "The local backend could not be fenced.") from None
            raise
        if not duplicate:
            self._queue.append((client, record, grant, job))
            self.metrics.accepted += 1
            self._start_worker()
        return {"job_id": job.job_id, "request_id": job.request_id, "context_id": job.context_id,
                "state": job.state, "deadline_at": job.deadline_at.isoformat(), "deduplicated": duplicate}

    async def result(self, client, request):
        self._enabled()
        record, _, job = self.registry.get_job(client, request.context_id, request.capability_token, request.job_id)
        await self._check_artifacts(record)
        if request.wait_seconds and not job.done_event.is_set():
            try:
                await asyncio.wait_for(job.done_event.wait(), request.wait_seconds)
            except asyncio.TimeoutError:
                pass
        # Recheck after the wait: revoked/expired capabilities cannot retrieve output.
        record, _, job = self.registry.get_job(client, request.context_id, request.capability_token, request.job_id)
        await self._check_artifacts(record)
        # File verification yields: capability revocation/expiry can happen while
        # it runs. Authorize once more with no await before returning any output.
        record, _, job = self.registry.get_job(client, request.context_id, request.capability_token, request.job_id)
        return {"job_id": job.job_id, "state": job.state, "result": copy.deepcopy(job.terminal_result)}

    async def cancel(self, client, request):
        self._enabled()
        _, _, job = self.registry.get_job(client, request.context_id, request.capability_token, request.job_id)
        if not job.done_event.is_set():
            job.cancel_event.set()
            if self._active is not None and self._active[3] is job:
                job.state = "cancelling"
            else:
                self.registry.finish_job(job, self._error(job, "cancelled").envelope(), "cancelled")
                self.metrics.cancelled += 1
            self._wake.set()
        return {"job_id": job.job_id, "state": job.state}

    def begin_drain(self):
        self._draining = True

    def resume(self):
        if not self._closed:
            self._draining = False

    def _start_worker(self):
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker(), name="local-inference-worker")
        self._wake.set()

    async def _worker(self):
        while True:
            await self._wake.wait()
            self._wake.clear()
            while self._queue or self._cleanup:
                if self._cleanup:
                    context_id = next(iter(self._cleanup))
                    ref = self._cleanup[context_id]
                    if self._resident and self._resident[0] == context_id:
                        self._resident = None
                        try:
                            task = asyncio.create_task(self.backend.erase())
                            if not await self._settle(task, self.config.shutdown_grace_seconds):
                                self._unavailable = True
                                await self._fence()
                            else:
                                task.result()
                        except Exception:
                            await self._fence()
                    if ref is not None:
                        task = asyncio.create_task(self.snapshots.delete_key(ref))
                        try:
                            if await self._settle(task, self.config.redis.operation_timeout_seconds):
                                task.result()
                        except Exception:
                            pass  # signed matching blobs remain bounded by their TTL
                    self._cleanup.pop(context_id, None)
                    self.registry.mark_release_done(context_id)
                    continue
                item = self._queue.popleft()
                if item[3].done_event.is_set():
                    continue
                self._active = item
                try:
                    await self._run_job(*item)
                finally:
                    self._active = None
            if self._closed:
                return

    @staticmethod
    def _error(job, code):
        texts = {"cancelled": "The generation was cancelled.", "deadline_exceeded": "The generation deadline expired.",
                 "backend_unavailable": "The local backend is unavailable.",
                 "backend_protocol_error": "The local backend returned an invalid response."}
        return LocalInferenceError(code, texts[code], generation_dispatched=job.dispatched)

    async def _fence(self):
        self._resident = None
        if self.backend is None:
            return True
        task = asyncio.create_task(self.backend.fence())
        try:
            settled = await self._settle(task, self.config.shutdown_grace_seconds + 1)
            confirmed = task.result() if settled else False
        except (Exception, asyncio.CancelledError):
            confirmed = False
        if confirmed is not True:
            self._unavailable = True
        return confirmed

    def _track_orphan(self, task):
        self._orphans.add(task)
        def finished(done):
            self._orphans.discard(done)
            if not done.cancelled():
                done.exception()  # retrieve a late failure without logging prompt-bearing exceptions
        task.add_done_callback(finished)

    async def _settle(self, task, timeout):
        """Bound acknowledgement even when an injected backend suppresses cancellation."""
        try:
            done, _ = await asyncio.wait((task,), timeout=max(0, timeout))
        except asyncio.CancelledError:
            task.cancel()
            self._track_orphan(task)
            raise
        if task in done:
            return True
        task.cancel()
        self._track_orphan(task)
        return False

    async def _fresh(self, record, spans, job):
        started = self.clock()
        try:
            await self._file(self.registry.check_freshness, record)
            await self._check_artifacts(record, exclude_job_id=job.job_id)
        except LocalInferenceError as error:
            if not error.retryable:
                self.registry.invalidate_sources(record, exclude_job_id=job.job_id)
            raise
        finally:
            spans["source_check"] += max(0, self.clock() - started) * 1000

    async def _check_artifacts(self, record, *, exclude_job_id=None):
        manifest = record.captured.artifact_manifest
        if manifest is None:
            return
        from src.artifacts.store import ArtifactError
        try:
            if self.artifact_service is None:
                raise ArtifactError("artifact_unavailable", "Artifact storage is unavailable.")
            await self.artifact_service.verify_binding(record.captured.workspace_root, manifest)
        except ArtifactError as error:
            if error.code == "store_busy":
                raise LocalInferenceError("store_busy", "Artifact storage is busy; retry the request.", retryable=True) from None
            self.registry.invalidate_sources(record, exclude_job_id=exclude_job_id)
            raise LocalInferenceError("artifact_unavailable", "The pinned artifacts are unavailable.") from None
        except (OSError, ValidationError):
            self.registry.invalidate_sources(record, exclude_job_id=exclude_job_id)
            raise LocalInferenceError("artifact_unavailable", "The pinned artifacts are unavailable.") from None

    def _snapshots(self, info, request):
        if (request.cache_mode != "auto" or not self.config.redis.enabled or
                info.snapshot != "supported" or not info.native_write_limit):
            return None
        if self.snapshots is None:
            if self.snapshot_factory is None:
                from .snapshots import SnapshotManager
                self.snapshots = SnapshotManager(self.config, self.directory, clock=self.clock)
            else:
                self.snapshots = self.snapshot_factory(self.config, self.directory)
        return self.snapshots

    @staticmethod
    def _snapshot_spans(spans, values):
        for name, value in (values or {}).items():
            if (name not in spans or not name.startswith("snapshot_") or
                    type(value) not in (int, float) or not 0 <= value < float("inf")):
                raise LocalInferenceError("backend_protocol_error", "Snapshot accounting is invalid.")
            spans[name] += value

    async def _recover_restore(self, record, grant, job, spans):
        self._resident = None
        try:
            await self.backend.erase()
        except Exception:
            if not await self._fence():
                raise self._error(job, "backend_unavailable") from None
        info = await self.backend.ensure_ready()
        self.registry.bind_profile(record, info.profile_hash)
        await self._fresh(record, spans, job)
        self._authorize_active(record, grant, job)
        return info

    def _authorize_active(self, record, grant, job):
        now = self.clock()
        if job.cancel_event.is_set():
            raise self._error(job, "cancelled")
        if record.released or record.captured.expires_monotonic <= now:
            raise LocalInferenceError("context_unavailable", "The context is unavailable.", generation_dispatched=job.dispatched)
        if grant.revoked or grant.expires_monotonic <= now:
            raise LocalInferenceError("permission_denied", "The capability is not authorized.", generation_dispatched=job.dispatched)
        if job.deadline <= now:
            raise self._error(job, "deadline_exceeded")

    @staticmethod
    def _validate_completion(value: Completion, prompt: PreparedPrompt, request, reuse: bool, lcp: int):
        counts = (value.input_tokens, value.evaluated_input_tokens, value.reused_input_tokens, value.output_tokens)
        valid = (all(type(count) is int and count >= 0 for count in counts) and
                 value.input_tokens == len(prompt.tokens) and
                 value.reused_input_tokens + value.evaluated_input_tokens == value.input_tokens and
                 value.reused_input_tokens <= lcp and (reuse or value.reused_input_tokens == 0) and
                 value.output_tokens <= request.max_output_tokens and value.finish_reason in ("stop", "length") and
                 isinstance(value.output_text, str) and len(value.output_text.encode("utf-8")) <= request.max_output_tokens * 32 + 4096)
        if value.first_token_ms is not None:
            valid = valid and type(value.first_token_ms) in (float, int) and 0 <= value.first_token_ms < float("inf")
        if value.saved_token_state is not None:
            valid = valid and (value.saved_token_state[:len(prompt.tokens)] == prompt.tokens and
                              len(value.saved_token_state) <= len(prompt.tokens) + value.output_tokens and
                              all(type(i) is int and 0 <= i <= 0xffffffff for i in value.saved_token_state))
        if not valid:
            raise LocalInferenceError("backend_protocol_error", "The local backend returned invalid token accounting.", generation_dispatched=True)

    async def _pipeline(self, client, record, grant, job, spans, observed):
        request = job.request
        self._authorize_active(record, grant, job)
        if self._unavailable:
            raise self._error(job, "backend_unavailable")
        if self.backend is None:
            if self.backend_factory is None:
                from .llama import LlamaBackend
                self.backend = LlamaBackend(self.config, self.directory)
            else:
                self.backend = self.backend_factory(self.config, self.directory)
        started = self.clock()
        info = await self.backend.ensure_ready()
        spans["startup"] = max(0, self.clock() - started) * 1000
        self.registry.bind_profile(record, info.profile_hash)
        started = self.clock()
        prompt = await self.backend.prepare(messages(record.captured, request.task))
        spans["render_tokenize"] = max(0, self.clock() - started) * 1000
        if (not prompt.tokens or any(type(i) is not int or not 0 <= i <= 0xffffffff for i in prompt.tokens)):
            raise LocalInferenceError("backend_protocol_error", "The backend tokenizer returned invalid input.")
        if (type(info.context_headroom) is not int or not 0 <= info.context_headroom < self.config.context_tokens):
            raise LocalInferenceError("backend_protocol_error", "The backend context capacity is invalid.")
        if len(prompt.tokens) + request.max_output_tokens > self.config.context_tokens - info.context_headroom:
            raise LocalInferenceError("context_overflow", "The complete prompt and output allowance exceed the model context.")
        await self._fresh(record, spans, job)
        self._authorize_active(record, grant, job)
        eligible = (request.cache_mode == "auto" and info.resident != "unsupported" and self._resident is not None and
                    self._resident[:3] == (record.context_id, info.profile_hash, info.epoch))
        lcp = longest_common_prefix(self._resident[3], prompt.tokens) if eligible else 0
        snapshots = self._snapshots(info, request)
        restored = False
        warnings = []
        if snapshots is not None and not eligible:
            from .snapshots import RestoreFailure
            try:
                outcome = await snapshots.restore(record, info, self.backend, deadline=job.deadline)
                self._snapshot_spans(spans, outcome.timings_ms)
                if outcome.warning:
                    warnings.append(outcome.warning)
                if outcome.token_state is not None:
                    if any(type(i) is not int or not 0 <= i <= 0xffffffff for i in outcome.token_state):
                        raise RestoreFailure("snapshot_invalid", {}, native_touched=True)
                    eligible = restored = True
                    lcp = longest_common_prefix(outcome.token_state, prompt.tokens)
            except RestoreFailure as failure:
                self._snapshot_spans(spans, failure.timings_ms)
                warnings.append(failure.code)
                if failure.native_touched:
                    info = await self._recover_restore(record, grant, job, spans)
            await self._fresh(record, spans, job)
            self._authorize_active(record, grant, job)
        self._resident = None  # uncertainty from this point invalidates preceding residency
        self.registry.reserve_dispatch(record, grant, job)
        self.metrics.dispatched += 1
        started = self.clock()
        try:
            value = await self.backend.generate(prompt, request, cache_enabled=eligible)
        except LocalInferenceError as error:
            if error.generation_dispatched is False:
                self.registry.withdraw_undispatched(record, grant, job)
                self.metrics.dispatched -= 1
            raise
        spans["model"] = max(0, self.clock() - started) * 1000
        self._validate_completion(value, prompt, request, eligible, lcp)
        if value.first_token_ms is not None and value.first_token_ms > spans["model"] + 1:
            raise self._error(job, "backend_protocol_error")
        observed.append(value.output_tokens)
        if value.first_token_ms is not None:
            spans["first_token"] = max(0, started - job.accepted_at) * 1000 + value.first_token_ms
        await self._fresh(record, spans, job)
        self._authorize_active(record, grant, job)
        self.registry.reconcile_dispatch(record, grant, job, value.output_tokens)
        state = ("disabled" if request.cache_mode == "off" else "unsupported" if info.resident == "unsupported" else
                 "restored_hit" if restored and value.reused_input_tokens else
                 "resident_hit" if value.reused_input_tokens and value.reused_input_tokens == lcp else
                 "partial_hit" if value.reused_input_tokens else "miss" if eligible else "cold")
        if request.cache_mode == "auto" and value.saved_token_state is not None:
            self._resident = (record.context_id, info.profile_hash, info.epoch, value.saved_token_state)
            if snapshots is not None:
                outcome = await snapshots.save(record, info, self.backend, value, prompt, deadline=job.deadline)
                self._snapshot_spans(spans, outcome.timings_ms)
                if outcome.warning:
                    warnings.append(outcome.warning)
                if outcome.fence_required:
                    await self._fence()  # retain the valid answer; never replay this generation
                await self._fresh(record, spans, job)
                self._authorize_active(record, grant, job)
        self.metrics.evaluated_input_tokens += value.evaluated_input_tokens
        self.metrics.reused_input_tokens += value.reused_input_tokens
        self.metrics.output_tokens += value.output_tokens
        return {"request_id": job.request_id, "context_id": job.context_id, "output_text": value.output_text,
                "finish_reason": value.finish_reason, "model_label": self.config.model_label,
                "input_tokens": value.input_tokens, "evaluated_input_tokens": value.evaluated_input_tokens,
                "reused_input_tokens": value.reused_input_tokens, "output_tokens": value.output_tokens,
                "cache": {"requested_mode": request.cache_mode, "state": state, "profile_hash": info.profile_hash,
                          "epoch": info.epoch, "compatibility": info.resident},
                **({"cache_warning": warnings[-1]} if warnings else {}),
                "timings_ms": spans, "remaining_budgets": self.registry.remaining_budgets(record, grant)}

    async def _run_job(self, client, record, grant, job):
        spans = timings()
        spans["queue"] = max(0, self.clock() - job.accepted_at) * 1000
        job.state = "running"
        observed = []
        pipeline = asyncio.create_task(self._pipeline(client, record, grant, job, spans, observed))
        cancelled = asyncio.create_task(job.cancel_event.wait())
        error = None
        data = None
        try:
            remaining = max(0, job.deadline - self.clock())
            done, _ = await asyncio.wait((pipeline, cancelled), timeout=remaining, return_when=asyncio.FIRST_COMPLETED)
            if cancelled in done:
                error = self._error(job, "cancelled")
            elif pipeline in done:
                data = pipeline.result()
            else:
                error = self._error(job, "deadline_exceeded")
        except LocalInferenceError as failure:
            failure.generation_dispatched = job.dispatched
            error = failure
        except asyncio.CancelledError:
            error = self._error(job, "cancelled")
        except Exception:
            error = self._error(job, "backend_protocol_error")
        finally:
            cancelled.cancel()
            if not pipeline.done():
                pipeline.cancel()
        if error is not None:
            # Active errors may leave native evaluation running despite HTTP cancellation.
            job.cancel_event.set()
            await self._fence()
            if not await self._settle(pipeline, self.config.shutdown_grace_seconds):
                self._unavailable = True
            elif not pipeline.cancelled():
                pipeline.exception()
            self.registry.reconcile_dispatch(record, grant, job, observed[-1] if observed else None)
            envelope = error.envelope()
            envelope["consumed_budget"] = {"model_requests": int(job.dispatched),
                                           "output_tokens": observed[-1] if observed else job.reserved_output_tokens}
            terminal = "cancelled" if error.code == "cancelled" else "failed"
        else:
            envelope, terminal = success(data), "succeeded"
        await self._settle(cancelled, self.config.shutdown_grace_seconds)
        spans["total"] = max(0, self.clock() - job.accepted_at) * 1000
        self.registry.finish_job(job, envelope, terminal)
        setattr(self.metrics, terminal, getattr(self.metrics, terminal) + 1)

    async def close(self):
        if self._closed:
            return
        self._closed = self._draining = True
        grace = self.config.shutdown_grace_seconds if self.config else 0
        deadline = asyncio.get_running_loop().time() + grace + 2
        for _, _, _, job in self._queue:
            if not job.done_event.is_set():
                job.cancel_event.set()
                self.registry.finish_job(job, self._error(job, "cancelled").envelope(), "cancelled")
        if self._active:
            self._active[3].cancel_event.set()
        self._wake.set()
        if self._worker_task:
            if not await self._settle(self._worker_task, deadline - asyncio.get_running_loop().time()):
                self._unavailable = True
        if self.backend:
            task = asyncio.create_task(self.backend.close())
            try:
                if not await self._settle(task, deadline - asyncio.get_running_loop().time()):
                    self._unavailable = True
                elif not task.cancelled():
                    task.result()
            except (Exception, asyncio.CancelledError):
                self._unavailable = True
        if self.snapshots:
            task = asyncio.create_task(self.snapshots.close())
            try:
                if await self._settle(task, deadline - asyncio.get_running_loop().time()):
                    task.result()
            except (Exception, asyncio.CancelledError):
                pass
        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            await self._settle(self._worker_task, deadline - asyncio.get_running_loop().time())
        if self._files:
            self._files.shutdown(wait=False, cancel_futures=True)
