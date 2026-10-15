import asyncio
from dataclasses import replace
from pathlib import Path
import uuid

import pytest

from src.daemon.workspaces import ClientContext
from src.local_inference.config import OwnerConfig
from src.local_inference.context import ContextRegistry
from src.local_inference.models import LocalInferenceError
from src.local_inference.service import LocalInferenceService
from tests.local_inference_fake import FakeBackend


async def make(tmp_path, **settings):
    root = tmp_path / "repo"; root.mkdir(exist_ok=True)
    client = ClientContext("transport-request", "http", "workspace-1", root)
    config = OwnerConfig(enabled=True, llama_server_binary="/fake/engine", model_path="/fake/model",
                         shutdown_grace_seconds=0.1, **settings)
    backend = FakeBackend()
    service = LocalInferenceService(tmp_path / "state", config=config, backend=backend)
    context = (await service.call("local_context_create", client,
               {"common_system": "rules", "shared_context": "shared data" * 30})) ["data"]
    return service, backend, client, context


def arguments(context, **values):
    return {"context_id": context["context_id"], "access_token": context["access_token"],
            "request_id": str(uuid.uuid4()), "task": "task", "max_output_tokens": 8, **values}


async def poll(service, client, context, job, **values):
    return await service.call("local_generation_result", client,
                             {"context_id": context["context_id"], "capability_token": context["owner_token"],
                              "job_id": job["job_id"], "wait_seconds": 1, **values})


async def cancel(service, client, context, job):
    return await service.call("local_generation_cancel", client,
                             {"context_id": context["context_id"], "capability_token": context["owner_token"],
                              "job_id": job["job_id"]})


@pytest.mark.asyncio
async def test_fifo_multiple_grants_one_transaction_and_reconnect(tmp_path):
    service, backend, client, context = await make(tmp_path)
    try:
        backend.allow.clear()
        first_args = arguments(context)
        first = (await service.call("local_generate", client, first_args))["data"]
        await asyncio.wait_for(backend.started.wait(), 1)
        grant = (await service.call("local_context_grant", client,
                 {"context_id": context["context_id"], "owner_token": context["owner_token"],
                  "label": "reviewer", "max_requests": 3, "max_total_output_tokens": 100}))["data"]
        second_args = arguments(context, access_token=grant["access_token"], task="review task")
        second = (await service.call("local_generate", client, second_args))["data"]
        dropped = asyncio.create_task(poll(service, client, context, first))
        await asyncio.sleep(0)
        dropped.cancel(); await asyncio.gather(dropped, return_exceptions=True)
        duplicate = await service.call("local_generate", client, first_args)
        assert duplicate["data"]["deduplicated"] and duplicate["data"]["job_id"] == first["job_id"]
        assert len(backend.calls) == 1 and backend.live == 1
        assert service.status()["data"]["queued"] == 1
        backend.allow.set()
        assert (await poll(service, client, context, first))["data"]["state"] == "succeeded"
        result = (await poll(service, client, context, second))["data"]["result"]["data"]
        assert backend.peak == 1 and [c[0] for c in backend.calls] == [first_args["request_id"], second_args["request_id"]]
        assert result["reused_input_tokens"] > 0
        assert result["input_tokens"] == result["reused_input_tokens"] + result["evaluated_input_tokens"]
        assert result["remaining_budgets"]["context"]["requests"] == 8
        assert set(result["timings_ms"]) == {"total", "queue", "startup", "render_tokenize", "source_check",
             "snapshot_lookup", "snapshot_load", "snapshot_restore", "model", "first_token", "snapshot_save", "snapshot_store"}
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_full_queue_duplicates_and_queued_cancel_preserve_credits_and_job_cap(tmp_path):
    service, backend, client, context = await make(tmp_path, max_queue=1, max_context_jobs=3)
    try:
        backend.allow.clear()
        first_args = arguments(context)
        first = (await service.call("local_generate", client, first_args))["data"]
        await backend.started.wait()
        second = (await service.call("local_generate", client, arguments(context)))["data"]
        assert (await service.call("local_generate", client, arguments(context)))["error"]["code"] == "busy"
        assert (await service.call("local_generate", client, first_args))["data"]["deduplicated"]
        assert (await cancel(service, client, context, second))["data"]["state"] == "cancelled"
        terminal = (await poll(service, client, context, second))["data"]["result"]
        assert terminal["consumed_budget"] == {"model_requests": 0, "output_tokens": 0}
        assert (await cancel(service, client, context, second))["data"]["state"] == "cancelled"
        third = (await service.call("local_generate", client, arguments(context)))["data"]
        await cancel(service, client, context, third)
        assert (await service.call("local_generate", client, arguments(context)))["error"]["code"] == "job_limit_reached"
        record = service.registry.contexts[context["context_id"]]
        assert record.remaining_requests == 9
        backend.allow.set()
        await poll(service, client, context, first)
        assert len(backend.calls) == 1 and record.remaining_output_tokens == 4094
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cancel", "revoke", "release"])
async def test_active_cancel_revoke_release_hold_slot_until_reaped(tmp_path, operation):
    service, backend, client, context = await make(tmp_path)
    try:
        grant = (await service.call("local_context_grant", client,
                 {"context_id": context["context_id"], "owner_token": context["owner_token"], "label": "child",
                  "max_requests": 3, "max_total_output_tokens": 100}))["data"]
        backend.allow.clear(); backend.reap_allowed.clear()
        first = (await service.call("local_generate", client,
                 arguments(context, access_token=grant["access_token"])))["data"]
        await backend.started.wait()
        other = (await service.call("local_context_create", client,
                 {"common_system": "", "shared_context": "other context"}))["data"]
        second = (await service.call("local_generate", client, arguments(other)))["data"]
        if operation == "cancel":
            assert (await cancel(service, client, context, first))["data"]["state"] == "cancelling"
        elif operation == "revoke":
            response = await service.call("local_context_revoke", client,
                {"context_id": context["context_id"], "owner_token": context["owner_token"], "grant_id": grant["grant_id"]})
            assert response["data"]["cleanup_state"] == "cancelling"
            denied = await service.call("local_generation_result", client,
                {"context_id": context["context_id"], "capability_token": grant["access_token"], "job_id": first["job_id"]})
            assert denied["error"]["code"] == "permission_denied"
        else:
            response = await service.call("local_context_release", client,
                {"context_id": context["context_id"], "owner_token": context["owner_token"]})
            assert response["data"]["cleanup_state"] == "cancelling"
        await asyncio.wait_for(backend.fencing.wait(), 1)
        await asyncio.sleep(0)
        assert len(backend.calls) == 1 and backend.live == 1 and service.pending >= 2
        backend.allow.set(); backend.reap_allowed.set()
        assert (await poll(service, client, other, second))["data"]["state"] == "succeeded"
        assert backend.peak == 1 and backend.fence_count >= 1
        if operation != "release":
            terminal = (await poll(service, client, context, first))["data"]["result"]
            assert terminal["error"]["generation_dispatched"] and terminal["consumed_budget"]["output_tokens"] == 8
        else:
            assert (await poll(service, client, context, first))["error"]["code"] == "context_unavailable"
    finally:
        backend.reap_allowed.set(); await service.close()


@pytest.mark.asyncio
async def test_active_deadline_fences_and_queued_expiry_never_dispatches(tmp_path):
    service, backend, client, context = await make(tmp_path)
    try:
        now = [100.0]
        service.registry.clock = service.clock = lambda: now[0]
        backend.allow.clear()
        first = (await service.call("local_generate", client, arguments(context, timeout_seconds=1)))["data"]
        await backend.started.wait()
        second = (await service.call("local_generate", client, arguments(context, timeout_seconds=1)))["data"]
        now[0] += 2
        # Wake the active HTTP waiter so post-generation authorization enforces the total deadline.
        backend.allow.set()
        one, two = await poll(service, client, context, first), await poll(service, client, context, second)
        assert one["data"]["result"]["error"]["code"] == "deadline_exceeded"
        assert two["data"]["result"]["error"]["code"] == "deadline_exceeded"
        assert len(backend.calls) == 1 and backend.fence_count >= 1
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("when", ["queued", "active"])
async def test_source_freshness_before_dispatch_and_after_generation(tmp_path, when):
    root = tmp_path / "repo"; root.mkdir()
    path = root / "source.py"; path.write_text("original")
    service, backend, client, ordinary = await make(tmp_path,
        source_roots={"source": {"path": str(root), "workspace_root": str(root)}})
    try:
        context = (await service.call("local_context_create", client,
                   {"common_system": "", "shared_context": "", "source_root_id": "source", "source_paths": ["source.py"]}))["data"]
        backend.allow.clear()
        if when == "queued":
            first = (await service.call("local_generate", client, arguments(ordinary)))["data"]
            await backend.started.wait()
            job = (await service.call("local_generate", client, arguments(context)))["data"]
            path.write_text("changed")
        else:
            job = (await service.call("local_generate", client, arguments(context)))["data"]
            await backend.started.wait(); path.write_text("changed")
        backend.allow.set()
        result = (await poll(service, client, context, job))["data"]["result"]
        assert result["error"]["code"] == "source_changed" and "output_text" not in str(result)
        assert len(backend.calls) == 1
        assert all(g.revoked for g in service.registry.contexts[context["context_id"]].grants.values())
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_drain_resume_shutdown_and_zero_queue(tmp_path):
    service, backend, client, context = await make(tmp_path, max_queue=0)
    backend.allow.clear()
    first = (await service.call("local_generate", client, arguments(context)))["data"]
    await backend.started.wait()
    assert (await service.call("local_generate", client, arguments(context)))["error"]["code"] == "busy"
    service.begin_drain()
    backend.allow.set(); await poll(service, client, context, first)
    assert (await service.call("local_generate", client, arguments(context)))["error"]["code"] == "busy"
    service.resume()
    backend.allow.clear(); backend.started.clear()
    second = (await service.call("local_generate", client, arguments(context)))["data"]
    await backend.started.wait()
    await asyncio.wait_for(service.close(), 2)
    assert service.pending == 0 and backend.live == 0
    assert service.registry.contexts[context["context_id"]].jobs[second["job_id"]].state == "cancelled"


@pytest.mark.asyncio
async def test_grant_expiry_during_generation_suppresses_output_and_no_budget_refund(tmp_path):
    service, backend, client, context = await make(tmp_path)
    try:
        now = [service.clock()]
        service.registry.clock = service.clock = lambda: now[0]
        grant = (await service.call("local_context_grant", client,
                 {"context_id": context["context_id"], "owner_token": context["owner_token"], "label": "short",
                  "ttl_seconds": 1, "max_requests": 1, "max_total_output_tokens": 8}))["data"]
        backend.allow.clear()
        job = (await service.call("local_generate", client, arguments(context, access_token=grant["access_token"])))["data"]
        await backend.started.wait(); now[0] += 2; backend.allow.set()
        result = (await poll(service, client, context, job))["data"]["result"]
        assert result["status"] == "error" and "output_text" not in str(result)
        assert service.registry.contexts[context["context_id"]].remaining_requests == 9
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_real_deadline_cancels_http_waiter_but_fences_native_process(tmp_path):
    service, backend, client, context = await make(tmp_path)
    try:
        backend.allow.clear(); backend.reap_allowed.clear()
        first = (await service.call("local_generate", client, arguments(context, timeout_seconds=1)))["data"]
        await backend.started.wait()
        second = (await service.call("local_generate", client, arguments(context)))["data"]
        await asyncio.wait_for(backend.fencing.wait(), 1.1)
        assert backend.live == 1 and len(backend.calls) == 1
        backend.reap_allowed.set(); backend.allow.set()
        result = (await poll(service, client, context, first))["data"]["result"]
        assert result["error"]["code"] == "deadline_exceeded"
        assert result["consumed_budget"] == {"model_requests": 1, "output_tokens": 8}
        assert (await poll(service, client, context, second))["data"]["state"] == "succeeded"
        assert backend.peak == 1
    finally:
        backend.reap_allowed.set(); await service.close()


@pytest.mark.asyncio
async def test_unconfirmed_fence_refuses_further_generation(tmp_path):
    service, backend, client, context = await make(tmp_path)
    try:
        async def unconfirmed():
            return False
        backend.fence = unconfirmed
        backend.allow.clear()
        job = (await service.call("local_generate", client, arguments(context)))["data"]
        await backend.started.wait()
        await cancel(service, client, context, job)
        await poll(service, client, context, job)
        assert service.status()["data"]["backend_state"] == "unavailable"
        assert (await service.call("local_generate", client, arguments(context)))["error"]["code"] == "backend_unavailable"
        assert len(backend.calls) == 1
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("count,headroom,expected", [(247, 0, "ok"), (248, 0, "ok"), (249, 0, "error"),
                                                  (247, 1, "ok"), (248, 1, "error")])
async def test_validated_effective_context_capacity(tmp_path, count, headroom, expected):
    from src.local_inference.backend import PreparedPrompt
    service, backend, client, context = await make(tmp_path, context_tokens=256, max_output_tokens=8)
    try:
        backend.info = replace(backend.info, context_headroom=headroom)
        async def prepared(_):
            return PreparedPrompt("input", tuple(range(count)))
        backend.prepare = prepared
        job = (await service.call("local_generate", client, arguments(context)))["data"]
        terminal = (await poll(service, client, context, job))["data"]["result"]
        assert terminal["status"] == expected
        if expected == "error":
            assert terminal["error"]["code"] == "context_overflow" and not backend.calls
        assert service.status()["data"]["limits"]["effective_context_tokens"] == 256 - headroom
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_shutdown_bounds_cancellation_resistant_prepare(tmp_path):
    service, backend, client, context = await make(tmp_path)
    release = asyncio.Event()
    started = asyncio.Event()
    original = backend.prepare
    async def stubborn(data):
        started.set()
        while not release.is_set():
            try:
                await release.wait()
            except asyncio.CancelledError:
                continue
        return await original(data)
    backend.prepare = stubborn
    try:
        job = (await service.call("local_generate", client, arguments(context)))["data"]
        await started.wait()
        await asyncio.wait_for(service.close(), .8)
        assert not backend.calls and service.status()["data"]["backend_state"] == "unavailable"
        assert service.registry.contexts[context["context_id"]].jobs[job["job_id"]].terminal_result["consumed_budget"] == {
            "model_requests": 0, "output_tokens": 0}
    finally:
        release.set()
        await asyncio.gather(*list(service._orphans), return_exceptions=True)
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("dispatched", [False, True])
async def test_known_refusal_before_native_http_attempt_returns_entire_reservation(tmp_path, dispatched):
    service, backend, client, context = await make(tmp_path)
    async def refused(prompt, request, *, cache_enabled):
        raise LocalInferenceError("backend_unavailable", "Owned child exited.", generation_dispatched=dispatched)
    backend.generate = refused
    try:
        job = (await service.call("local_generate", client, arguments(context)))["data"]
        value = (await poll(service, client, context, job))["data"]["result"]
        assert value["error"]["generation_dispatched"] is dispatched
        assert value["consumed_budget"] == {"model_requests": int(dispatched), "output_tokens": 8 if dispatched else 0}
        record = service.registry.contexts[context["context_id"]]
        assert record.remaining_requests == 10 - int(dispatched)
        assert record.remaining_output_tokens == 4096 - (8 if dispatched else 0)
        assert service.metrics.dispatched == int(dispatched) and not backend.calls
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_unused_release_loops_do_not_accumulate_cleanup_behind_active_job(tmp_path):
    service, backend, client, context = await make(tmp_path, max_contexts=2, max_release_tombstones=2)
    try:
        backend.allow.clear()
        job = (await service.call("local_generate", client, arguments(context)))["data"]
        await backend.started.wait()
        for _ in range(20):
            unused = (await service.call("local_context_create", client,
                {"common_system": "rules", "shared_context": "unused"}))["data"]
            value = await service.call("local_context_release", client,
                {"context_id": unused["context_id"], "owner_token": unused["owner_token"]})
            assert value["data"]["cleanup_state"] == "done"
            assert not service._cleanup and len(service.registry.release_tombstones) <= 2
        backend.allow.set()
        assert (await poll(service, client, context, job))["data"]["state"] == "succeeded"
    finally:
        backend.allow.set()
        await service.close()


@pytest.mark.asyncio
async def test_cancelled_file_waiters_retain_admission_until_threads_finish(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    service, backend, client, context = await make(tmp_path)
    service._files.shutdown()
    class CountedExecutor(ThreadPoolExecutor):
        submitted = 0
        def submit(self, *args, **kwargs):
            self.submitted += 1
            return super().submit(*args, **kwargs)
    executor = service._files = CountedExecutor(max_workers=2)
    allowed = threading.Event()
    started = [threading.Event(), threading.Event()]
    def work(index):
        started[index].set()
        allowed.wait()
    tasks = [asyncio.create_task(service._file(work, index)) for index in range(2)]
    third = None
    try:
        for _ in range(1000):
            if all(event.is_set() for event in started):
                break
            await asyncio.sleep(.001)
        assert all(event.is_set() for event in started)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        third = asyncio.create_task(service._file(lambda: "third"))
        await asyncio.sleep(.01)
        assert executor.submitted == 2 and service.pending == 2
        allowed.set()
        assert await asyncio.wait_for(third, 1) == "third"
        assert executor.submitted == 3
    finally:
        allowed.set()
        await asyncio.gather(*tasks, return_exceptions=True)
        if third is not None:
            await asyncio.gather(third, return_exceptions=True)
        await service.close()
