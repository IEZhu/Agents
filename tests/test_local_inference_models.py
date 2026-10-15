"""JSON trust boundaries are strict, bounded and reject secret-bearing extras."""
from uuid import uuid4

import pytest
from pydantic import ValidationError

from src.local_inference.models import (ContextCreateRequest, ContextGrantRequest,
    ContextReleaseRequest, ContextRevokeRequest, GenerateRequest, GenerationCancelRequest,
    GenerationResultRequest, LocalInferenceError, SCHEMA_VERSION)


def generate(**updates):
    return {"context_id": "ctx_example", "access_token": "sentinel-secret", "request_id": str(uuid4()),
            "task": "Review the source.", **updates}


@pytest.mark.parametrize("model,data", [
    (ContextCreateRequest, {"common_system": "", "shared_context": "data"}),
    (ContextGrantRequest, {"context_id": "ctx", "owner_token": "owner", "label": "child", "max_requests": 2, "max_total_output_tokens": 64}),
    (ContextRevokeRequest, {"context_id": "ctx", "owner_token": "owner", "grant_id": "grant"}),
    (GenerateRequest, generate()),
    (GenerationResultRequest, {"context_id": "ctx", "capability_token": "secret", "job_id": "job"}),
    (GenerationCancelRequest, {"context_id": "ctx", "capability_token": "secret", "job_id": "job"}),
    (ContextReleaseRequest, {"context_id": "ctx", "owner_token": "owner"}),
])
def test_all_public_requests_forbid_extras(model, data):
    model.model_validate(data)
    with pytest.raises(ValidationError):
        model.model_validate({**data, "workspace_root": "/arbitrary"})


@pytest.mark.parametrize("updates", [
    {"max_output_tokens": True}, {"max_output_tokens": "512"}, {"timeout_seconds": False},
    {"temperature": True}, {"temperature": float("nan")}, {"temperature": float("inf")},
    {"seed": 1.0}, {"seed": -1}, {"cache_mode": "custom"}, {"task": ""},
    {"task": "\ud800"}, {"request_id": str(uuid4()).upper()}, {"request_id": "not-a-uuid"},
])
def test_generate_strict_boundary(updates):
    with pytest.raises(ValidationError):
        GenerateRequest.model_validate(generate(**updates))


@pytest.mark.parametrize("paths", [("a",), "a", ["a"] * 33, ["a", "a"], [1], ["\ud800"]])
def test_source_collection_is_bounded_before_capture(paths):
    with pytest.raises(ValidationError):
        ContextCreateRequest(common_system="", shared_context="data", source_paths=paths)


def test_public_limits_and_secret_repr():
    with pytest.raises(ValidationError):
        GenerationResultRequest(context_id="ctx", capability_token="cap", job_id="job", wait_seconds=21)
    assert "sentinel-secret" not in repr(GenerateRequest.model_validate(generate()))
    assert "owner-sentinel" not in repr(ContextRevokeRequest(context_id="ctx", owner_token="owner-sentinel", grant_id="g"))
    error = LocalInferenceError("budget_exhausted", "No request credits remain.").envelope()
    assert error == {"schema": SCHEMA_VERSION, "status": "error", "error": {
        "code": "budget_exhausted", "message": "No request credits remain.",
        "retryable": False, "generation_dispatched": False}}
