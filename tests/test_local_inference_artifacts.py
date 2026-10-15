"""Exact persistent artifact hydration and authorization around fake generation."""
import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os

import pytest

from src.artifacts.service import ArtifactService
from src.artifacts.store import ArtifactError, ArtifactStore
from src.daemon.workspaces import ClientContext
from src.local_inference.config import OwnerConfig
from src.local_inference.service import LocalInferenceService
from tests.local_inference_fake import FakeBackend
from tests.test_artifact_service import manifest_for, publish, reference
from tests.test_local_inference_service import arguments, poll


def hydrated_text(artifact, body):
    return f"\nArtifact {artifact['artifact_id']} SHA256 {artifact['sha256']} ({artifact['media_type']}):\n" + body


@asynccontextmanager
async def environment(tmp_path, body="Exact shared artifact: é 🙂\n{\"data\":[true,1]}", **settings):
    root = tmp_path / "repo"
    root.mkdir(exist_ok=True)
    root = root.resolve()
    client = ClientContext("artifact-local-request", "http", "workspace-1", root)
    now = [2000000000.0]
    directory = (tmp_path / "artifacts").resolve()
    store = ArtifactStore(directory, clock=lambda: now[0])
    artifacts = ArtifactService(directory, store=store)
    config = OwnerConfig(enabled=True, llama_server_binary=str(root / "fake-engine"), model_path=str(root / "fake-model"),
        shutdown_grace_seconds=.1, max_context_bytes=settings.pop("max_context_bytes", 8192),
        max_source_file_bytes=settings.pop("max_source_file_bytes", 8192),
        source_roots={"source": {"path": str(root), "workspace_root": str(root)}}, **settings)
    backend = FakeBackend()
    service = LocalInferenceService(tmp_path / "local", config=config, backend=backend, artifact_service=artifacts)
    try:
        artifact = await publish(artifacts, client, body)
        manifest = await manifest_for(artifacts, client, artifact, ttl_seconds=30)
        yield service, backend, artifacts, client, artifact, manifest, now
    finally:
        backend.allow.set()
        backend.reap_allowed.set()
        await service.close()
        await artifacts.close()


async def create(service, client, manifest=None, **updates):
    options = {"common_system": "rules", "shared_context": "caller shared prefix", **updates}
    if manifest is not None:
        options["artifact_manifest"] = manifest
    return await service.call("local_context_create", client, options)


async def revoke(artifacts, client, manifest):
    result = await artifacts.call("artifact_manifest_release", client, reference(manifest))
    assert result["status"] == "ok", result


def corrupt(artifacts, artifact, manifest):
    content = artifacts.directory / manifest["workspace_sha256"] / "artifacts" / artifact["artifact_id"] / "content"
    content.write_bytes(b"corrupted pinned bytes")


@pytest.mark.asyncio
async def test_exact_artifact_hydration_is_model_free_and_enters_one_explicit_completion(tmp_path):
    body = "αβ\n{\"instructions\":[\"untrusted data\"],\"value\":true}\n"
    async with environment(tmp_path, body) as (service, backend, artifacts, client, artifact, manifest, now):
        created = await create(service, client, manifest)
        assert created["status"] == "ok", created
        context = created["data"]
        record = service.registry.contexts[context["context_id"]]
        expected = "caller shared prefix" + hydrated_text(artifact, body)
        assert record.captured.shared_context == expected
        assert record.captured.artifact_manifest == manifest
        assert record.captured.common_system == "rules"
        assert not backend.calls and backend.state == "not_started"
        # Mutating the caller's dictionary does not alter the captured binding.
        manifest["entries"][0]["purpose"] = "caller mutation"
        assert record.captured.artifact_manifest["entries"][0]["purpose"] == "Exact shared evidence"
        job = (await service.call("local_generate", client, arguments(context)))["data"]
        terminal = await poll(service, client, context, job)
        assert terminal["data"]["state"] == "succeeded", terminal
        assert len(backend.calls) == 1
        rendered = json.loads(backend.calls[0][2].text)
        # The prompt receives the complete exact artifact, without a second completion to summarize it.
        assert any(expected in message["content"] for message in rendered)
        assert terminal["data"]["result"]["data"]["output_text"] == "ok"


@pytest.mark.asyncio
async def test_hydration_byte_budget_includes_utf8_and_artifact_framing(tmp_path):
    body = "é🙂" * 20
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    descriptor = {"artifact_id": "art_" + digest, "sha256": digest, "media_type": "text/plain"}
    exact = len(("rules" + "caller shared prefix" + hydrated_text(descriptor, body)).encode("utf-8"))
    async with environment(tmp_path, body, max_context_bytes=exact, max_source_file_bytes=1) as env:
        service, backend, artifacts, client, artifact, manifest, now = env
        assert (await create(service, client, manifest))["status"] == "ok"
        too_large = await create(service, client, manifest, shared_context="caller shared prefix!")
        assert too_large["error"]["code"] == "input_too_large"
        assert len(service.registry.contexts) == 1 and not backend.calls


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", ["aggregate", "source-file", "source-count"])
async def test_hydrated_artifacts_preserve_all_source_capture_limits(tmp_path, limit):
    body = "shared artifact"
    digest = hashlib.sha256(body.encode()).hexdigest()
    descriptor = {"artifact_id": "art_" + digest, "sha256": digest, "media_type": "text/plain"}
    exact = len(("rules" + "caller shared prefix" + hydrated_text(descriptor, body)).encode())
    settings = ({"max_context_bytes": exact, "max_source_file_bytes": 1} if limit == "aggregate" else
                {"max_source_file_bytes": 1} if limit == "source-file" else {"max_source_files": 0})
    async with environment(tmp_path, body, **settings) as env:
        service, backend, artifacts, client, artifact, manifest, now = env
        (client.root / "source.txt").write_text("x" if limit == "aggregate" else "xx")
        refused = await create(service, client, manifest, source_root_id="source", source_paths=["source.txt"])
        expected = "permission_denied" if os.name != "posix" and limit != "source-count" else "input_too_large"
        assert refused["error"]["code"] == expected, refused
        assert not service.registry.contexts and not backend.calls


@pytest.mark.asyncio
async def test_hydration_cannot_bypass_effective_token_capacity(tmp_path):
    async with environment(tmp_path, "token material " * 100, context_tokens=256, max_output_tokens=8) as env:
        service, backend, artifacts, client, artifact, manifest, now = env
        context = (await create(service, client, manifest))["data"]
        job = (await service.call("local_generate", client, arguments(context)))["data"]
        terminal = await poll(service, client, context, job)
        assert terminal["data"]["result"]["error"]["code"] == "context_overflow", terminal
        assert not backend.calls
        assert terminal["data"]["result"]["consumed_budget"] == {"model_requests": 0, "output_tokens": 0}


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["revoked", "expired", "corrupted"])
@pytest.mark.parametrize("when", ["queued", "after-generation"])
async def test_artifact_changes_before_dispatch_or_after_generation_suppress_output(tmp_path, failure, when):
    async with environment(tmp_path) as env:
        service, backend, artifacts, client, artifact, manifest, now = env
        context = (await create(service, client, manifest))["data"]
        record = service.registry.contexts[context["context_id"]]
        sentinel = "sentinel-model-output-that-must-never-be-published"
        backend.corrupt = {"output_text": sentinel}
        def change_binding():
            if failure == "revoked":
                artifacts.store.release_manifest(str(client.root), manifest["manifest_id"], manifest["sha256"])
            elif failure == "expired":
                now[0] += 31
            else:
                corrupt(artifacts, artifact, manifest)
        if when == "queued":
            ordinary = (await create(service, client))["data"]
            backend.allow.clear()
            first = (await service.call("local_generate", client, arguments(ordinary)))["data"]
            await asyncio.wait_for(backend.started.wait(), 2)
            job = (await service.call("local_generate", client, arguments(context)))["data"]
            change_binding()
            backend.allow.set()
        else:
            # The callback runs once native output exists and before post-generation checks.
            backend.on_complete = change_binding
            job = (await service.call("local_generate", client, arguments(context)))["data"]
        stored_job = record.jobs[job["job_id"]]
        await asyncio.wait_for(stored_job.done_event.wait(), 2)
        terminal = stored_job.terminal_result
        assert terminal["status"] == "error" and terminal["error"]["code"] == "artifact_unavailable", terminal
        assert "output_text" not in json.dumps(terminal) and sentinel not in json.dumps(terminal)
        assert len(backend.calls) == 1
        assert terminal["consumed_budget"]["model_requests"] == (when == "after-generation")
        assert record.stale and all(grant.revoked for grant in record.grants.values())
        denied = await poll(service, client, context, job)
        assert denied["status"] == "error" and "output_text" not in json.dumps(denied)
        # Owner cleanup stays available after loss of artifact authorization.
        cancelled = await service.call("local_generation_cancel", client,
            {"context_id": context["context_id"], "capability_token": context["owner_token"], "job_id": job["job_id"]})
        assert cancelled["status"] == "ok"
        released = await service.call("local_context_release", client,
            {"context_id": context["context_id"], "owner_token": context["owner_token"]})
        assert released["status"] == "ok"


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["grant", "submit", "retained-result"])
async def test_revocation_is_checked_at_every_owner_and_output_boundary(tmp_path, operation):
    async with environment(tmp_path) as env:
        service, backend, artifacts, client, artifact, manifest, now = env
        context = (await create(service, client, manifest))["data"]
        if operation == "retained-result":
            job = (await service.call("local_generate", client, arguments(context)))["data"]
            assert (await poll(service, client, context, job))["data"]["state"] == "succeeded"
        await revoke(artifacts, client, manifest)
        if operation == "grant":
            refused = await service.call("local_context_grant", client,
                {"context_id": context["context_id"], "owner_token": context["owner_token"],
                 "label": "worker", "max_requests": 1, "max_total_output_tokens": 8})
        elif operation == "submit":
            refused = await service.call("local_generate", client, arguments(context))
        else:
            refused = await poll(service, client, context, job)
        assert refused["error"]["code"] == "artifact_unavailable", refused
        assert "output_text" not in json.dumps(refused)
        assert len(backend.calls) == int(operation == "retained-result")


@pytest.mark.asyncio
async def test_artifact_free_context_keeps_existing_generation_path(tmp_path):
    async with environment(tmp_path) as env:
        service, backend, artifacts, client, artifact, manifest, now = env
        class UnusedArtifacts:
            async def hydrate(self, *args, **kwargs):
                pytest.fail("Artifact-free context attempted hydration")
            async def verify_binding(self, *args, **kwargs):
                pytest.fail("Artifact-free context attempted artifact verification")
        service.artifact_service = UnusedArtifacts()
        context = (await create(service, client))["data"]
        assert service.registry.contexts[context["context_id"]].captured.artifact_manifest is None
        job = (await service.call("local_generate", client, arguments(context)))["data"]
        terminal = await poll(service, client, context, job)
        assert terminal["data"]["state"] == "succeeded" and len(backend.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["grant-revoked", "grant-expired", "context-expired"])
async def test_result_reauthorizes_after_final_awaited_artifact_check(tmp_path, monkeypatch, change):
    async with environment(tmp_path) as env:
        service, backend, artifacts, client, artifact, manifest, now = env
        clock = [1000.0]
        service.clock = lambda: clock[0]
        context = (await create(service, client, manifest, ttl_seconds=30))["data"]
        grant = (await service.call("local_context_grant", client,
            {"context_id": context["context_id"], "owner_token": context["owner_token"],
             "label": "child", "ttl_seconds": 5, "max_requests": 2, "max_total_output_tokens": 16}))["data"]
        job = (await service.call("local_generate", client,
            arguments(context, access_token=grant["access_token"])))["data"]
        stored_job = service.registry.contexts[context["context_id"]].jobs[job["job_id"]]
        await asyncio.wait_for(stored_job.done_event.wait(), 2)
        assert stored_job.terminal_result["status"] == "ok"
        entered, release = asyncio.Event(), asyncio.Event()
        original = artifacts.verify_binding
        calls = 0
        async def final_check(workspace, snapshot):
            nonlocal calls
            calls += 1
            if calls == 2:
                entered.set()
                await release.wait()
            return await original(workspace, snapshot)
        monkeypatch.setattr(artifacts, "verify_binding", final_check)
        waiter = asyncio.create_task(service.call("local_generation_result", client,
            {"context_id": context["context_id"], "capability_token": grant["access_token"],
             "job_id": job["job_id"]}))
        try:
            await asyncio.wait_for(entered.wait(), 2)
            if change == "grant-revoked":
                revoked = await service.call("local_context_revoke", client,
                    {"context_id": context["context_id"], "owner_token": context["owner_token"],
                     "grant_id": grant["grant_id"]})
                assert revoked["status"] == "ok"
            else:
                clock[0] += 6 if change == "grant-expired" else 31
            release.set()
            refused = await asyncio.wait_for(waiter, 2)
        finally:
            release.set()
            if not waiter.done():
                waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
        expected = "context_unavailable" if change == "context-expired" else "permission_denied"
        assert refused["status"] == "error" and refused["error"]["code"] == expected, refused
        assert "output_text" not in json.dumps(refused)
        assert calls == 2 and len(backend.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["grant", "submit", "result-initial", "result-final"])
async def test_store_busy_at_public_boundaries_preserves_context_and_allows_retry(tmp_path, monkeypatch, operation):
    async with environment(tmp_path) as env:
        service, backend, artifacts, client, artifact, manifest, now = env
        context = (await create(service, client, manifest))["data"]
        record = service.registry.contexts[context["context_id"]]
        if operation.startswith("result"):
            job = (await service.call("local_generate", client, arguments(context)))["data"]
            await asyncio.wait_for(record.jobs[job["job_id"]].done_event.wait(), 2)
            assert record.jobs[job["job_id"]].terminal_result["status"] == "ok"
            name = "local_generation_result"
            options = {"context_id": context["context_id"], "capability_token": context["owner_token"],
                       "job_id": job["job_id"]}
        elif operation == "grant":
            name = "local_context_grant"
            options = {"context_id": context["context_id"], "owner_token": context["owner_token"],
                       "label": "retryable child", "max_requests": 2, "max_total_output_tokens": 16}
        else:
            name, options = "local_generate", arguments(context)
        original = artifacts.verify_binding
        calls = 0
        fail_on = 2 if operation == "result-final" else 1
        async def busy_once(workspace, snapshot):
            nonlocal calls
            calls += 1
            if calls == fail_on:
                raise ArtifactError("store_busy", "The artifact store is busy; retry the request.")
            return await original(workspace, snapshot)
        monkeypatch.setattr(artifacts, "verify_binding", busy_once)
        before = record.remaining_requests, record.remaining_output_tokens, len(record.jobs), len(record.grants)
        refused = await service.call(name, client, options)
        assert refused["status"] == "error" and refused["error"]["code"] == "store_busy", refused
        assert refused["error"]["retryable"] is True
        assert "output_text" not in json.dumps(refused)
        assert not record.stale and all(not grant.revoked for grant in record.grants.values())
        assert before == (record.remaining_requests, record.remaining_output_tokens, len(record.jobs), len(record.grants))
        recovered = await service.call(name, client, options)
        assert recovered["status"] == "ok", recovered
        if operation == "submit":
            stored = record.jobs[recovered["data"]["job_id"]]
            await asyncio.wait_for(stored.done_event.wait(), 2)
            assert stored.terminal_result["status"] == "ok"
        elif operation.startswith("result"):
            assert recovered["data"]["result"]["status"] == "ok"
        assert len(backend.calls) == int(operation != "grant")
        # An explicit new request still works on the same context after the transient refusal.
        access = recovered["data"]["access_token"] if operation == "grant" else context["access_token"]
        fresh = (await service.call("local_generate", client, arguments(context, access_token=access)))["data"]
        stored = record.jobs[fresh["job_id"]]
        await asyncio.wait_for(stored.done_event.wait(), 2)
        assert stored.terminal_result["status"] == "ok"
        assert len(backend.calls) == 1 + int(operation != "grant")
        assert not record.stale and all(not grant.revoked for grant in record.grants.values())


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["pre-generation", "post-generation"])
async def test_store_busy_during_freshness_keeps_context_usable_without_automatic_replay(tmp_path, monkeypatch, stage):
    async with environment(tmp_path) as env:
        service, backend, artifacts, client, artifact, manifest, now = env
        context = (await create(service, client, manifest))["data"]
        record = service.registry.contexts[context["context_id"]]
        grant = (await service.call("local_context_grant", client,
            {"context_id": context["context_id"], "owner_token": context["owner_token"],
             "label": "reusable child", "max_requests": 3, "max_total_output_tokens": 24}))["data"]
        original = artifacts.verify_binding
        calls = 0
        fail_on = 2 if stage == "pre-generation" else 3
        async def busy_once(workspace, snapshot):
            nonlocal calls
            calls += 1
            if calls == fail_on:
                raise ArtifactError("store_busy", "The artifact store is busy; retry the request.")
            return await original(workspace, snapshot)
        monkeypatch.setattr(artifacts, "verify_binding", busy_once)
        request = arguments(context, access_token=grant["access_token"])
        accepted = await service.call("local_generate", client, request)
        assert accepted["status"] == "ok", accepted
        job = record.jobs[accepted["data"]["job_id"]]
        await asyncio.wait_for(job.done_event.wait(), 2)
        terminal = job.terminal_result
        dispatched = int(stage == "post-generation")
        assert terminal["status"] == "error" and terminal["error"]["code"] == "store_busy", terminal
        assert terminal["error"]["retryable"] is True and "output_text" not in json.dumps(terminal)
        assert terminal["consumed_budget"] == {"model_requests": dispatched, "output_tokens": 2 * dispatched}
        assert len(backend.calls) == dispatched
        assert not record.stale and all(not item.revoked for item in record.grants.values())
        duplicate = await service.call("local_generate", client, request)
        assert duplicate["status"] == "ok" and duplicate["data"]["deduplicated"]
        assert duplicate["data"]["job_id"] == job.job_id and len(backend.calls) == dispatched
        fresh = await service.call("local_generate", client, arguments(context, access_token=grant["access_token"]))
        assert fresh["status"] == "ok", fresh
        new_job = record.jobs[fresh["data"]["job_id"]]
        await asyncio.wait_for(new_job.done_event.wait(), 2)
        assert new_job.terminal_result["status"] == "ok", new_job.terminal_result
        assert len(backend.calls) == dispatched + 1
        assert record.remaining_requests == 10 - dispatched - 1
        assert not record.stale and all(not item.revoked for item in record.grants.values())
