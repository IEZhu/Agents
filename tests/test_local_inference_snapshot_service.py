"""Snapshots are part of the serialized transaction, never generation retries."""
import asyncio
from dataclasses import replace

import pytest

from src.local_inference.config import RedisConfig
from src.local_inference.snapshots import RestoreFailure, RestoreOutcome, SaveOutcome
from tests.test_local_inference_cache import completion
from tests.test_local_inference_service import make, arguments, poll


class FakeSnapshots:
    def __init__(self):
        self.calls = []
        self.states = {}
        self.fail_restore = False
        self.fail_save = False
        self.restore_reuse = True
        self.save_started = asyncio.Event()
        self.allow_save = asyncio.Event(); self.allow_save.set()
        self.on_save = None
        self.closed = False

    def key_for(self, record, profile):
        return (record.context_id, profile)  # opaque stand-in, no prompt bytes

    async def restore(self, record, info, backend, *, deadline):
        self.calls.append("restore")
        if self.fail_restore:
            backend.saved = (123,)
            raise RestoreFailure("snapshot_native_protocol", {"snapshot_restore": .1})
        state = self.states.get(record.context_id)
        backend.saved = state if state and self.restore_reuse else ()
        return RestoreOutcome(state, timings_ms={"snapshot_lookup": .1})

    async def save(self, record, info, backend, value, prepared, *, deadline):
        self.calls.append("save")
        self.save_started.set()
        await self.allow_save.wait()
        if self.on_save:
            self.on_save()
        self.states[record.context_id] = value.saved_token_state
        return SaveOutcome("snapshot_unavailable" if self.fail_save else None,
                           {"snapshot_save": .1}, self.fail_save)

    async def delete_key(self, ref):
        self.calls.append(("delete", ref))

    async def close(self):
        self.closed = True


async def setup(tmp_path):
    service, backend, client, context = await make(tmp_path, redis=RedisConfig(enabled=True))
    backend.info = replace(backend.info, snapshot="supported", native_write_limit=True)
    snapshots = service.snapshots = FakeSnapshots()
    return service, backend, client, context, snapshots


@pytest.mark.asyncio
async def test_restored_hit_requires_positive_actual_counters(tmp_path):
    service, backend, client, context, snapshots = await setup(tmp_path)
    try:
        await completion(service, client, context)
        for reuse, expected in ((True, "restored_hit"), (False, "miss")):
            service._resident = None
            snapshots.restore_reuse = reuse
            data = (await completion(service, client, context))["data"]
            assert data["cache"]["state"] == expected
            assert (data["reused_input_tokens"] > 0) is reuse
        assert len(backend.calls) == 3
    finally:
        await service.close()
    assert snapshots.closed


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,support,write_limit", [("off", "supported", True),
    ("auto", "unknown", True), ("auto", "unsupported", True), ("auto", "supported", False)])
async def test_disabled_or_unproven_cache_performs_no_snapshot_operations(tmp_path, mode, support, write_limit):
    service, backend, client, context, snapshots = await setup(tmp_path)
    try:
        backend.info = replace(backend.info, snapshot=support, native_write_limit=write_limit)
        assert (await completion(service, client, context, cache_mode=mode))["status"] == "ok"
        assert snapshots.calls == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_touched_restore_is_erased_before_one_cold_generation(tmp_path):
    service, backend, client, context, snapshots = await setup(tmp_path)
    try:
        snapshots.fail_restore = True
        data = (await completion(service, client, context))["data"]
        assert len(backend.calls) == 1 and backend.calls[0][1] is False
        assert data["cache_warning"] == "snapshot_native_protocol"
        assert data["reused_input_tokens"] == 0
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_failed_erase_reaps_and_restarts_before_one_cold_generation(tmp_path):
    service, backend, client, context, snapshots = await setup(tmp_path)
    async def failed_erase():
        raise ValueError("damaged slot")
    backend.erase = failed_erase
    try:
        snapshots.fail_restore = True
        assert (await completion(service, client, context))["status"] == "ok"
        assert backend.fence_count == 1 and len(backend.calls) == 1
        assert backend.calls[0][1] is False
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_failed_save_retains_answer_and_never_replays(tmp_path):
    service, backend, client, context, snapshots = await setup(tmp_path)
    try:
        snapshots.fail_save = True
        data = (await completion(service, client, context))["data"]
        assert data["output_text"] == "ok" and data["cache_warning"] == "snapshot_unavailable"
        assert len(backend.calls) == 1 and backend.fence_count == 1 and service._resident is None
        assert data["remaining_budgets"]["context"]["requests"] == 9
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_save_holds_exclusive_queue_and_release_clears_text_then_scoped_deletion(tmp_path):
    service, backend, client, context, snapshots = await setup(tmp_path)
    try:
        snapshots.allow_save.clear()
        job = (await service.call("local_generate", client, arguments(context)))["data"]
        await asyncio.wait_for(snapshots.save_started.wait(), 1)
        other = (await service.call("local_context_create", client, {"common_system": "rules", "shared_context": "other"}))["data"]
        second = (await service.call("local_generate", client, arguments(other)))["data"]
        await asyncio.sleep(.01)
        assert len(backend.calls) == 1 and service.pending == 2
        old = service.registry.contexts[context["context_id"]]
        released = await service.call("local_context_release", client,
            {"context_id": context["context_id"], "owner_token": context["owner_token"]})
        assert released["data"]["cleanup_state"] == "cancelling"
        assert old.captured.common_system == old.captured.shared_context == ""
        assert not old.jobs and not old.captured.sources
        snapshots.allow_save.set()
        assert (await poll(service, client, context, job))["status"] == "error"
        assert (await poll(service, client, other, second))["data"]["state"] == "succeeded"
        assert ("delete", (context["context_id"], backend.info.profile_hash)) in snapshots.calls
        assert backend.peak == 1
    finally:
        snapshots.allow_save.set()
        await service.close()


@pytest.mark.asyncio
async def test_revocation_during_save_suppresses_answer_without_second_model_call(tmp_path):
    service, backend, client, context, snapshots = await setup(tmp_path)
    try:
        grant = (await service.call("local_context_grant", client,
            {"context_id": context["context_id"], "owner_token": context["owner_token"],
             "label": "child", "max_requests": 2, "max_total_output_tokens": 50}))["data"]
        snapshots.on_save = lambda: setattr(service.registry.contexts[context["context_id"]].grants[grant["grant_id"]], "revoked", True)
        result = await completion(service, client, context, access_token=grant["access_token"])
        assert result["error"]["code"] == "permission_denied" and "output_text" not in str(result)
        assert len(backend.calls) == 1 and result["consumed_budget"] == {"model_requests": 1, "output_tokens": 2}
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_repeated_release_reports_pending_until_snapshot_delete_completes(tmp_path):
    service, backend, client, context, snapshots = await setup(tmp_path)
    started, allowed = asyncio.Event(), asyncio.Event()
    async def slow_delete(ref):
        started.set()
        await allowed.wait()
    snapshots.delete_key = slow_delete
    try:
        await completion(service, client, context)
        args = {"context_id": context["context_id"], "owner_token": context["owner_token"]}
        assert (await service.call("local_context_release", client, args))["data"]["cleanup_state"] == "cancelling"
        await asyncio.wait_for(started.wait(), 1)
        assert (await service.call("local_context_release", client, args))["data"]["cleanup_state"] == "cancelling"
        allowed.set()
        for _ in range(100):
            await asyncio.sleep(.001)
            if not service._cleanup:
                break
        assert (await service.call("local_context_release", client, args))["data"]["cleanup_state"] == "done"
    finally:
        allowed.set()
        await service.close()
