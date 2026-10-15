"""Source boundaries, scoped capabilities, accounting and retained records."""
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import os
from uuid import uuid4

import pytest

from src.daemon.workspaces import ClientContext
from src.local_inference.config import OwnerConfig
from src.local_inference.context import ContextRegistry, capture_sources
from src.local_inference.models import (ContextCreateRequest, ContextGrantRequest, ContextReleaseRequest,
    ContextRevokeRequest, GenerateRequest, LocalInferenceError, success)


class Clock:
    now = 100.0

    def __call__(self):
        return self.now

    def wall(self):
        return datetime(2026, 1, 1, tzinfo=timezone.utc) + timedelta(seconds=self.now)


@pytest.fixture
def setup(tmp_path):
    workspace, source = tmp_path / "workspace", tmp_path / "worktree"
    workspace.mkdir()
    source.mkdir()
    (source / "a.py").write_text("print('captured')\n")
    client = ClientContext("request", "http", "workspace-1", workspace)
    clock = Clock()
    config = OwnerConfig(enabled=True, llama_server_binary="/fake/llama", model_path="/fake/model.gguf",
                         source_roots={"worktree": {"path": str(source), "workspace_root": str(workspace)}})
    return ContextRegistry(config, clock=clock, wall_clock=clock.wall), client, source, clock


def create(registry, client, **updates):
    return registry.create(client, ContextCreateRequest(common_system="common instructions",
        shared_context="shared data", max_requests=10, max_total_output_tokens=100, **updates))


def request(created, **updates):
    return GenerateRequest(context_id=created["context_id"], access_token=created["access_token"],
                           request_id=str(uuid4()), task="task", max_output_tokens=16, **updates)


def error(code, operation):
    with pytest.raises(LocalInferenceError) as caught:
        operation()
    assert caught.value.code == code


def test_disabled_and_transport_workspace_boundaries(setup):
    registry, client, _, _ = setup
    disabled = ContextRegistry(OwnerConfig())
    error("disabled", lambda: create(disabled, client))
    error("transport_unsupported", lambda: create(registry, replace(client, transport="stdio")))
    error("workspace_required", lambda: create(registry, replace(client, root=None)))
    owner = create(registry, client)
    foreign = replace(client, workspace_id="other")
    error("context_unavailable", lambda: registry.authenticate_owner(foreign, owner["context_id"], owner["owner_token"]))


def test_capture_immutable_worktree_identity_and_digest_only_capabilities(setup):
    registry, client, source, _ = setup
    owner = create(registry, client, source_root_id="worktree", source_paths=["a.py"])
    record, grant = registry.authenticate_access(client, owner["context_id"], owner["access_token"])
    assert owner["source_root"] == str(source)
    assert record.captured.workspace_root == str(client.root)
    assert record.captured.source_root != record.captured.workspace_root
    assert record.captured.sources[0].content == "print('captured')\n"
    assert owner["owner_token"] not in repr(record)
    assert owner["access_token"] not in repr(record)
    assert record.owner_digest != owner["owner_token"]
    assert grant.capability_digest != owner["access_token"]
    error("permission_denied", lambda: registry.authenticate_owner(client, owner["context_id"], owner["access_token"]))
    with pytest.raises(Exception):
        record.captured.common_system = "changed"


@pytest.mark.parametrize("path", ["/etc/passwd", "../a.py", "sub/../../a.py", "./a.py", "sub//a.py", "a.py/", "a\x00.py"])
def test_relative_source_traversal_is_rejected(setup, path):
    registry, client, _, _ = setup
    error("permission_denied", lambda: create(registry, client, source_root_id="worktree", source_paths=[path]))


def test_source_root_required_and_must_bind_http_workspace(setup, tmp_path):
    registry, client, _, _ = setup
    error("permission_denied", lambda: create(registry, client, source_paths=["a.py"]))
    error("permission_denied", lambda: create(registry, client, source_root_id="missing", source_paths=["a.py"]))
    another = tmp_path / "different-workspace"
    another.mkdir()
    error("permission_denied", lambda: create(registry, replace(client, root=another), source_root_id="worktree", source_paths=["a.py"]))


@pytest.mark.parametrize("kind", ["file", "directory", "internal"])
def test_every_source_symlink_component_is_rejected(setup, tmp_path, kind):
    registry, client, source, _ = setup
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "data").write_text("outside")
    if kind == "file":
        (source / "link").symlink_to(outside / "data")
        path = "link"
    elif kind == "internal":
        (source / "link").symlink_to(source / "a.py")
        path = "link"
    else:
        (source / "link").symlink_to(outside, target_is_directory=True)
        path = "link/data"
    error("permission_denied", lambda: create(registry, client, source_root_id="worktree", source_paths=[path]))


def test_fifo_is_rejected_without_blocking_and_invalid_utf8(setup):
    registry, client, source, _ = setup
    os.mkfifo(source / "fifo")
    error("permission_denied", lambda: create(registry, client, source_root_id="worktree", source_paths=["fifo"]))
    (source / "invalid").write_bytes(b"\xff")
    error("permission_denied", lambda: create(registry, client, source_root_id="worktree", source_paths=["invalid"]))


def test_byte_limits_include_multibyte_text_and_aggregate_sources(setup):
    registry, client, source, clock = setup
    raw = registry.config.model_dump(mode="json", by_alias=True)
    raw.update(max_context_bytes=64, max_source_file_bytes=40, max_task_bytes=4)
    bounded = ContextRegistry(OwnerConfig.model_validate(raw), clock=clock, wall_clock=clock.wall)
    error("input_too_large", lambda: bounded.create(client, ContextCreateRequest(common_system="", shared_context="é" * 33)))
    (source / "a.py").write_text("a" * 40)
    (source / "b.py").write_text("b" * 40)
    error("input_too_large", lambda: bounded.create(client, ContextCreateRequest(common_system="", shared_context="",
        source_root_id="worktree", source_paths=["a.py", "b.py"])))
    owner = bounded.create(client, ContextCreateRequest(common_system="", shared_context="data", max_total_output_tokens=100))
    too_large = GenerateRequest(context_id=owner["context_id"], access_token=owner["access_token"], request_id=str(uuid4()),
                                task="ééé", max_output_tokens=16)
    error("input_too_large", lambda: bounded.admit_job(client, too_large))


def test_safe_capture_detects_file_mutation_during_read(setup, monkeypatch):
    registry, client, source, _ = setup
    original_read = os.read
    mutated = False

    def racing_read(fd, count):
        nonlocal mutated
        data = original_read(fd, count)
        if not mutated:
            mutated = True
            (source / "a.py").write_text("changed during read")
        return data

    monkeypatch.setattr(os, "read", racing_read)
    error("source_changed", lambda: create(registry, client, source_root_id="worktree", source_paths=["a.py"]))


@pytest.mark.parametrize("change", ["edit", "remove", "symlink", "root"])
def test_freshness_repeats_safe_open_and_hash_without_mutating_registry(setup, change):
    registry, client, source, _ = setup
    owner = create(registry, client, source_root_id="worktree", source_paths=["a.py"])
    record, grant = registry.authenticate_access(client, owner["context_id"], owner["access_token"])
    registry.check_freshness(record)
    if change == "edit":
        (source / "a.py").write_text("different bytes")
    elif change == "remove":
        (source / "a.py").unlink()
    elif change == "symlink":
        (source / "other").write_text("print('captured')\n")
        (source / "a.py").unlink()
        (source / "a.py").symlink_to(source / "other")
    else:
        source.rename(source.with_name("old-root"))
        source.mkdir()
        (source / "a.py").write_text("print('captured')\n")
    error("source_changed", lambda: registry.check_freshness(record))
    assert not grant.revoked
    registry.invalidate_sources(record)
    assert grant.revoked and record.stale


def test_capture_can_be_split_from_loop_insertion_and_limits_before_read(setup):
    registry, client, _, _ = setup
    req = ContextCreateRequest(common_system="", shared_context="data", source_root_id="worktree", source_paths=["a.py"])
    capture = capture_sources(registry.config, client, req)
    owner = registry.create(client, req, capture=capture)
    assert owner["sources"][0]["path"] == "a.py"


def test_grants_do_not_reserve_shared_budget_and_labels_are_not_authority(setup):
    registry, client, _, _ = setup
    owner = create(registry, client)
    grants = [registry.grant(client, ContextGrantRequest(context_id=owner["context_id"], owner_token=owner["owner_token"],
        label="same-label", max_requests=10, max_total_output_tokens=100)) for _ in range(2)]
    record = registry.authenticate_owner(client, owner["context_id"], owner["owner_token"])
    assert record.remaining_requests == 10 and record.remaining_output_tokens == 100
    assert grants[0]["access_token"] != grants[1]["access_token"]
    error("permission_denied", lambda: registry.authenticate_access(client, owner["context_id"], "same-label"))
    error("permission_denied", lambda: registry.grant(client, ContextGrantRequest(context_id=owner["context_id"],
        owner_token=grants[0]["access_token"], label="child", max_requests=1, max_total_output_tokens=1)))


def test_idempotency_scope_conflict_and_busy_do_not_create_records(setup):
    registry, client, _, _ = setup
    owner = create(registry, client)
    req = request(owner)
    record, grant, job, duplicate = registry.admit_job(client, req)
    assert not duplicate
    assert "redacted" == job.request.access_token
    assert owner["access_token"] not in repr(job)
    assert registry.admit_job(client, req, admission_available=False)[2:] == (job, True)
    error("request_conflict", lambda: registry.admit_job(client, req.model_copy(update={"task": "changed"})))
    child = registry.grant(client, ContextGrantRequest(context_id=owner["context_id"], owner_token=owner["owner_token"],
                              label="child", max_requests=2, max_total_output_tokens=32))
    foreign = req.model_copy(update={"access_token": child["access_token"]})
    error("permission_denied", lambda: registry.admit_job(client, foreign))
    error("busy", lambda: registry.admit_job(client, request(owner), admission_available=False))
    assert len(record.jobs) == 1 and record.remaining_requests == 10


def test_dispatch_accounting_is_atomic_reconciled_and_never_repeated(setup):
    registry, client, _, _ = setup
    owner = registry.create(client, ContextCreateRequest(common_system="", shared_context="data",
        max_requests=2, max_total_output_tokens=24))
    record, grant, first, _ = registry.admit_job(client, request(owner))
    _, _, second, _ = registry.admit_job(client, request(owner))
    registry.reserve_dispatch(record, grant, first)
    assert (record.remaining_requests, record.remaining_output_tokens) == (1, 8)
    error("budget_exhausted", lambda: registry.reserve_dispatch(record, grant, second))
    assert (record.remaining_requests, record.remaining_output_tokens) == (1, 8)
    error("backend_protocol_error", lambda: registry.reserve_dispatch(record, grant, first))
    registry.reconcile_dispatch(record, grant, first, 4)
    registry.reconcile_dispatch(record, grant, first, 0)
    assert (record.remaining_requests, grant.remaining_requests) == (1, 1)
    assert (record.remaining_output_tokens, grant.remaining_output_tokens) == (20, 20)
    registry.reserve_dispatch(record, grant, second)
    registry.reconcile_dispatch(record, grant, second)
    assert (record.remaining_requests, record.remaining_output_tokens) == (0, 4)


def test_queued_terminal_records_are_retained_and_count_admission(setup):
    registry, client, _, _ = setup
    raw = registry.config.model_dump(mode="json", by_alias=True)
    raw["max_context_jobs"] = 1
    registry = ContextRegistry(OwnerConfig.model_validate(raw))
    owner = create(registry, client)
    req = request(owner)
    record, grant, job, _ = registry.admit_job(client, req)
    job.cancel_event.set()
    registry.finish_job(job, LocalInferenceError("cancelled", "cancelled").envelope(), "cancelled")
    assert job.request is None and job.done_event.is_set()
    assert registry.get_job(client, owner["context_id"], owner["access_token"], job.job_id)[2] is job
    assert registry.admit_job(client, req)[3]
    error("job_limit_reached", lambda: registry.admit_job(client, request(owner)))
    assert record.remaining_requests == 10 and record.remaining_output_tokens == 100


def test_owner_capability_only_inspects_child_jobs_and_revocation_suppresses_access(setup):
    registry, client, _, _ = setup
    owner = create(registry, client)
    child = registry.grant(client, ContextGrantRequest(context_id=owner["context_id"], owner_token=owner["owner_token"],
                              label="child", max_requests=2, max_total_output_tokens=32))
    req = GenerateRequest(context_id=owner["context_id"], access_token=child["access_token"], request_id=str(uuid4()),
                          task="task", max_output_tokens=16)
    record, grant, job, _ = registry.admit_job(client, req)
    assert registry.get_job(client, owner["context_id"], owner["owner_token"], job.job_id)[1] is None
    error("permission_denied", lambda: registry.get_job(client, owner["context_id"], owner["access_token"], job.job_id))
    revoke = ContextRevokeRequest(context_id=owner["context_id"], owner_token=owner["owner_token"], grant_id=grant.grant_id)
    assert registry.revoke(client, revoke)["cleanup_state"] == "done"
    assert registry.revoke(client, revoke)["cleanup_state"] == "done"
    assert job.state == "cancelled"
    error("permission_denied", lambda: registry.get_job(client, owner["context_id"], child["access_token"], job.job_id))
    error("permission_denied", lambda: registry.revoke(client, revoke.model_copy(update={"grant_id": owner["grant_id"]})))


def test_deadline_is_capped_by_grant_and_rechecked_at_dispatch(setup):
    registry, client, _, clock = setup
    owner = create(registry, client, ttl_seconds=30)
    child = registry.grant(client, ContextGrantRequest(context_id=owner["context_id"], owner_token=owner["owner_token"],
                              label="short", max_requests=2, max_total_output_tokens=32, ttl_seconds=5))
    req = GenerateRequest(context_id=owner["context_id"], access_token=child["access_token"], request_id=str(uuid4()),
                          task="task", max_output_tokens=16)
    record, grant, job, _ = registry.admit_job(client, req)
    assert job.accepted_at == 100 and job.deadline == 105
    clock.now = 105
    error("permission_denied", lambda: registry.reserve_dispatch(record, grant, job))
    assert record.remaining_requests == 10
    clock.now = 131
    error("context_unavailable", lambda: registry.authenticate_owner(client, owner["context_id"], owner["owner_token"]))
    assert not registry.contexts


def test_profile_binding_is_one_time_and_terminal_result_is_bounded(setup):
    registry, client, _, _ = setup
    owner = create(registry, client)
    record, _, job, _ = registry.admit_job(client, request(owner))
    registry.bind_profile(record, "a" * 64)
    registry.bind_profile(record, "a" * 64)
    error("context_unavailable", lambda: registry.bind_profile(record, "b" * 64))
    registry.finish_job(job, success({"text": "x" * 140000}), "succeeded")
    assert job.state == "failed" and job.terminal_result["error"]["code"] == "backend_protocol_error"
    assert job.terminal_result["consumed_budget"] == {"model_requests": 0, "output_tokens": 0}
    assert job.request is None


def test_release_tombstones_are_scoped_bounded_and_expire(setup):
    registry, client, _, clock = setup
    raw = registry.config.model_dump(mode="json", by_alias=True)
    raw["max_release_tombstones"] = 1
    registry = ContextRegistry(OwnerConfig.model_validate(raw), clock=clock, wall_clock=clock.wall)
    first = create(registry, client)
    release_first = ContextReleaseRequest(context_id=first["context_id"], owner_token=first["owner_token"])
    assert registry.release(client, release_first)["cleanup_state"] == "done"
    assert registry.release(client, release_first)["cleanup_state"] == "done"
    error("context_unavailable", lambda: registry.release(replace(client, workspace_id="foreign"), release_first))
    second = create(registry, client)
    release_second = ContextReleaseRequest(context_id=second["context_id"], owner_token=second["owner_token"])
    registry.release(client, release_second)
    assert len(registry.release_tombstones) == 1
    error("context_unavailable", lambda: registry.release(client, release_first))
    clock.now += registry.config.max_context_ttl_seconds + 1
    error("context_unavailable", lambda: registry.release(client, release_second))


def test_default_grant_expiry_never_extends_context_with_advancing_clock(setup):
    registry, client, _, clock = setup
    owner = create(registry, client, ttl_seconds=30)
    record = registry.authenticate_owner(client, owner["context_id"], owner["owner_token"])

    def advancing_clock():
        clock.now += 0.001
        return clock.now

    registry.clock = advancing_clock
    child = registry.grant(client, ContextGrantRequest(context_id=owner["context_id"], owner_token=owner["owner_token"],
        label="child", max_requests=1, max_total_output_tokens=16))
    assert record.grants[child["grant_id"]].expires_monotonic <= record.captured.expires_monotonic
