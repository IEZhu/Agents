"""Hits are observed counters bounded by an exact native token prefix."""
from dataclasses import replace
import math

import pytest

from tests.test_local_inference_service import make, arguments, poll


async def completion(service, client, context, **values):
    job = (await service.call("local_generate", client, arguments(context, **values)))["data"]
    return (await poll(service, client, context, job))["data"]["result"]


@pytest.mark.asyncio
@pytest.mark.parametrize("reuse,state", [(None, "resident_hit"), (3, "partial_hit"), (0, "miss")])
async def test_cold_and_actual_resident_partial_zero_hit_counters(tmp_path, reuse, state):
    service, backend, client, context = await make(tmp_path)
    try:
        cold = await completion(service, client, context)
        assert cold["data"]["cache"]["state"] == "cold" and cold["data"]["reused_input_tokens"] == 0
        backend.override_reuse = reuse
        warm = (await completion(service, client, context))["data"]
        assert warm["cache"]["state"] == state
        assert warm["reused_input_tokens"] + warm["evaluated_input_tokens"] == warm["input_tokens"]
        assert backend.calls[-1][1] is True
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_observed_hit_does_not_upgrade_unknown_capability(tmp_path):
    service, backend, client, context = await make(tmp_path)
    try:
        backend.info = replace(backend.info, resident="unknown")
        await completion(service, client, context)
        hit = (await completion(service, client, context))["data"]
        assert hit["reused_input_tokens"] > 0 and hit["cache"]["compatibility"] == "unknown"
        assert service.status()["data"]["capabilities"]["resident"] == "unknown"
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_off_control_does_not_warm_next_auto_and_unsupported_is_truthful(tmp_path):
    service, backend, client, context = await make(tmp_path)
    try:
        await completion(service, client, context)
        off = (await completion(service, client, context, cache_mode="off"))["data"]
        assert off["cache"]["state"] == "disabled" and off["reused_input_tokens"] == 0
        auto = (await completion(service, client, context))["data"]
        assert auto["cache"]["state"] == "cold" and backend.calls[-1][1] is False
        backend.info = replace(backend.info, resident="unsupported")
        unsupported = (await completion(service, client, context))["data"]
        assert unsupported["cache"]["state"] == "unsupported" and unsupported["reused_input_tokens"] == 0
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_unknown_token_state_and_engine_epoch_clear_residency(tmp_path):
    service, backend, client, context = await make(tmp_path)
    try:
        backend.lose_ids = True
        await completion(service, client, context)
        backend.lose_ids = False
        assert (await completion(service, client, context))["data"]["cache"]["state"] == "cold"
        backend.info = replace(backend.info, epoch="restarted")
        assert (await completion(service, client, context))["data"]["cache"]["state"] == "cold"
        assert backend.calls[-1][1] is False
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_profile_change_requires_new_context_and_independent_identical_context_is_cold(tmp_path):
    service, backend, client, context = await make(tmp_path)
    try:
        await completion(service, client, context)
        other = (await service.call("local_context_create", client,
                 {"common_system": "rules", "shared_context": "shared data" * 30}))["data"]
        assert (await completion(service, client, other))["data"]["cache"]["state"] == "cold"
        backend.info = replace(backend.info, profile_hash="a" * 64)
        refusal = await completion(service, client, other)
        assert refusal["error"]["code"] == "context_unavailable"
        assert len(backend.calls) == 2
        new = (await service.call("local_context_create", client,
               {"common_system": "rules", "shared_context": "shared data" * 30}))["data"]
        assert (await completion(service, client, new))["data"]["cache"]["state"] == "cold"
    finally:
        await service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", [{"input_tokens": True}, {"evaluated_input_tokens": -1},
    {"output_tokens": 9}, {"first_token_ms": math.nan}, {"first_token_ms": math.inf},
    {"first_token_ms": 1e9}, {"finish_reason": "unknown"}, {"saved_token_state": (1, 2)}])
async def test_invalid_accounting_cannot_publish_or_create_residency(tmp_path, corrupt):
    service, backend, client, context = await make(tmp_path)
    try:
        backend.corrupt = corrupt
        result = await completion(service, client, context)
        assert result["error"]["code"] == "backend_protocol_error" and "output_text" not in str(result)
        assert result["consumed_budget"] == {"model_requests": 1, "output_tokens": 8}
        assert service._resident is None and backend.fence_count == 1
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_native_reuse_may_not_exceed_verified_lcp(tmp_path):
    service, backend, client, context = await make(tmp_path)
    try:
        first = (await completion(service, client, context, task="aaaaaaaaaa"))["data"]
        backend.override_reuse = first["input_tokens"]
        result = await completion(service, client, context, task="bbbbbbbbbb")
        assert result["error"]["code"] == "backend_protocol_error" and service._resident is None
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_returned_sdk_terminal_data_cannot_mutate_retained_result(tmp_path):
    service, backend, client, context = await make(tmp_path)
    try:
        job = (await service.call("local_generate", client, arguments(context)))["data"]
        first = await poll(service, client, context, job)
        first["data"]["result"]["data"]["output_text"] = "tampered"
        second = await poll(service, client, context, job)
        assert second["data"]["result"]["data"]["output_text"] == "ok"
        assert len(backend.calls) == 1
    finally:
        await service.close()
