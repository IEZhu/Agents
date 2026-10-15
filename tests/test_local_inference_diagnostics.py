"""Explicit diagnostics remain bounded/model-free until an owner runs them."""
import asyncio
from dataclasses import replace
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import httpx

from scripts.dev import bench_local_inference as bench
from scripts.dev import local_inference_diagnostics as diag
from scripts.dev import local_inference_probe as command
from src.local_inference.backend import BackendInfo, Completion, PreparedPrompt, longest_common_prefix
from src.local_inference.capabilities import CapabilityRecords
from src.local_inference.config import OwnerConfig
from src.local_inference.models import LocalInferenceError
from src.local_inference.profile import ModelProfile, canonical_hash
from src.local_inference.service import LocalInferenceService

pytestmark = pytest.mark.skipif(os.name != "posix", reason="Owner diagnostics use the POSIX private-file boundary.")


def config(**updates):
    return OwnerConfig(enabled=True, llama_server_binary="/fake/engine", model_path="/fake/model",
                       shutdown_grace_seconds=.2, **updates)


class FakeDiagnostic:
    unicode_observed = True
    positive_reuse = True
    missing_counter = False
    fail_case = None
    instances = []

    def __init__(self, config, directory, *, artifacts):
        self.config, self.artifacts = config, artifacts
        self.profile = ModelProfile("a" * 64, "b" * 64, "tokens", "template", "renderer", 16384, "f16", "f16", "fake")
        self.info = BackendInfo(self.profile.stable_hash, "fake-epoch", "unknown", "unknown", True)
        self.spool = Path(directory) / "fake-spool"
        self.spool.mkdir(parents=True, mode=0o700)
        self.records = CapabilityRecords(directory)
        self.calls, self.call_context, self.overrides = [], {}, {}
        self.state, self.saved = "not_started", ()
        self.process = None
        self.__class__.instances.append(self)

    async def ensure_ready(self):
        self.state = "ready"
        return self.info

    async def prepare(self, messages):
        text = json.dumps(messages, ensure_ascii=False, separators=(",", ":"))
        return PreparedPrompt(text, tuple(text.encode()))

    async def generate(self, prompt, request, *, cache_enabled):
        case = self.call_context.get("case", "benchmark")
        reuse = longest_common_prefix(self.saved, prompt.tokens) if cache_enabled and self.positive_reuse else 0
        ids, count, text = (ord("R"), 1001), 2, "R"
        stop = "eos" if case in ("resident-cold", "resident-repeat", "restored-continuation", "benchmark") else case
        if case == "limit":
            ids, count, text = (1000,), 1, "1"
        if case == "unicode":
            ids, count, text, stop = ((), 1, "", "limit") if self.unicode_observed else ((1000,), 1, "𐀀", "limit")
        if case == "benchmark":
            lookup = {task: expected for tasks in bench.FAMILIES.values() for _, task, expected in tasks}
            text = json.dumps(lookup.get(request.task, {}))
        self.saved = prompt.tokens + ids[:-1]
        row = {"index": len(self.calls) + 1, "case": case, "native_reuse": None if self.missing_counter else reuse,
               "native_stop_type": stop, "rendered_token_hash": canonical_hash(prompt.tokens), "error": None}
        row["raw_response"] = self.artifacts.write(f"call-{row['index']:03d}.sse", {"stop_type": stop, "cache_n": row["native_reuse"]})
        self.calls.append(row)
        if case == self.fail_case:
            raise LocalInferenceError("backend_protocol_error", "Native missing counters.", generation_dispatched=True)
        return Completion(text, "length" if stop == "limit" else "stop", len(prompt.tokens), len(prompt.tokens) - reuse,
                          reuse, count, None, ids, None if len(ids) != count else self.saved)

    async def _json(self, path, payload, **kwargs):
        if path == "/tokenize":
            return {"tokens": list(payload["content"].encode())}
        target = self.spool / payload["filename"]
        if "save" in path:
            target.write_bytes(b"private-native-snapshot")
            return {"id_slot": 0, "n_saved": len(self.saved), "n_written": target.stat().st_size, "timings": {"save_ms": 1}}
        # Native state sidecar is deliberately held by the fake engine, not
        # inferred by retokenizing a generated text string.
        self.saved = self.restorable
        return {"id_slot": 0, "n_restored": len(self.saved), "n_read": target.stat().st_size, "timings": {"restore_ms": 1}}

    async def erase(self):
        self.restorable, self.saved = self.saved, ()

    async def fence(self):
        self.saved = ()
        self.info = replace(self.info, epoch=self.info.epoch + "r")
        return True

    async def close(self):
        self.state = "stopping"


async def fake_limit(directory):
    return {"proven": True, "limit_bytes": 4096, "actual_bytes": 4096}


@pytest.mark.asyncio
async def test_complete_fake_probe_requires_native_finish_mapping_roundtrips(tmp_path):
    raw = diag.PrivateArtifacts(tmp_path / "raw")
    result = await diag.probe(config(), tmp_path / "owner", raw, backend_factory=FakeDiagnostic, write_limit_checker=fake_limit)
    assert not result["errors"]
    assert result["resident"] == result["snapshot"] == "supported"
    assert result["model_calls"] == 9  # cold/warm + 4 cases + 3 actual continuations
    assert set(result["finish_cases"]) == {"eos", "word", "limit", "unicode"}
    assert all(case["proven"] for case in result["finish_cases"].values())
    assert result["finish_cases"]["unicode"]["snapshot_refused"]
    assert result["owned_backend_closed"]
    assert CapabilityRecords(tmp_path / "owner").load(result["profile_hash"], native_write_limit=True) == ("supported", "supported")
    assert not list((tmp_path / "owner/fake-spool").iterdir())


@pytest.mark.asyncio
async def test_unobserved_utf8_stays_unknown_without_losing_resident(tmp_path):
    class NotTriggered(FakeDiagnostic):
        unicode_observed = False
    result = await diag.probe(config(), tmp_path / "owner", diag.PrivateArtifacts(tmp_path / "raw"),
                              backend_factory=NotTriggered, write_limit_checker=fake_limit)
    assert result["resident"] == "supported" and result["snapshot"] == "unknown"
    assert not result["finish_cases"]["unicode"]["proven"]
    assert CapabilityRecords(tmp_path / "owner").load(result["profile_hash"], native_write_limit=True) == ("supported", "unknown")


@pytest.mark.asyncio
async def test_missing_native_counts_never_become_zero_or_signed(tmp_path):
    class Missing(FakeDiagnostic):
        missing_counter = True
    result = await diag.probe(config(), tmp_path / "owner", diag.PrivateArtifacts(tmp_path / "raw"),
                              backend_factory=Missing, write_limit_checker=fake_limit)
    assert result["capability_record"] is None
    assert result["resident"] == result["snapshot"] == "unknown"
    assert result["model_calls"] == 9
    assert all(row["native_reuse"] is None for row in result["calls"])


@pytest.mark.asyncio
async def test_observed_zero_hit_remains_a_negative_result(tmp_path):
    class ZeroHit(FakeDiagnostic):
        positive_reuse = False
    result = await diag.probe(config(), tmp_path / "owner", diag.PrivateArtifacts(tmp_path / "raw"),
                              backend_factory=ZeroHit, write_limit_checker=fake_limit)
    assert not result["errors"]
    assert result["resident"] == "unsupported" and result["snapshot"] == "unknown"
    assert result["model_calls"] == 9 and all(row["native_reuse"] == 0 for row in result["calls"])
    assert CapabilityRecords(tmp_path / "owner").load(result["profile_hash"], native_write_limit=True) == ("unsupported", "unknown")


@pytest.mark.asyncio
async def test_dispatched_failure_is_counted_closed_and_raw_evidence_retained(tmp_path):
    class Broken(FakeDiagnostic):
        fail_case = "resident-repeat"
    result = await diag.probe(config(), tmp_path / "owner", diag.PrivateArtifacts(tmp_path / "raw"),
                              backend_factory=Broken, write_limit_checker=fake_limit)
    assert result["model_calls"] == 2 and result["errors"] == ["backend_protocol_error"]
    assert result["calls"][-1]["error"] == "backend_protocol_error"
    assert result["capability_record"] is None and result["owned_backend_closed"]
    assert result["raw_artifacts"]


@pytest.mark.asyncio
async def test_capacity_refuses_native_save_before_call(tmp_path):
    backend = FakeDiagnostic(config(), tmp_path / "owner", artifacts=diag.PrivateArtifacts(tmp_path / "raw"))
    await backend.ensure_ready()
    prompt = PreparedPrompt("one", (1,))
    value = Completion("x", "stop", 1, 1, 0, 1, None, (2,), (1,))
    async def unexpected(*args, **kwargs):
        pytest.fail("native snapshot bypassed spool reservation")
    backend._json = unexpected
    assert (await diag.roundtrip(backend, value, prompt, cap=1, timeout=1, maximum=32, spool_budget=32))["reason"] == "spool_capacity"


@pytest.mark.asyncio
@pytest.mark.skipif(os.name != "posix", reason="Native snapshot write limits require POSIX.")
async def test_production_exec_wrapper_caps_actual_synthetic_bytes(tmp_path):
    evidence = await diag.prove_write_limit(tmp_path / "raw")
    assert evidence["proven"] and evidence["actual_bytes"] == evidence["limit_bytes"] == 4096
    assert not list((tmp_path / "raw").iterdir())


def test_explicit_cli_paths_and_reports_do_not_replace_existing_files(tmp_path):
    for parser in (command.parser(), bench.parser()):
        with pytest.raises(SystemExit):
            parser.parse_args([])
    with pytest.raises(ValueError):
        diag.absolute("relative.json")
    raw = diag.PrivateArtifacts(tmp_path / "private", maximum=8)
    item = raw.write("evidence", b"abc")
    assert item["bytes"] == 3
    with pytest.raises(FileExistsError):
        raw.write("evidence", b"abc")
    with pytest.raises(ValueError):
        raw.write("oversized", b"abcdef")
    with pytest.raises(ValueError):
        raw.write("../escape", b"a")
    diag.write_report(tmp_path / "private/report.json", {"ok": True})
    with pytest.raises(FileExistsError):
        diag.write_report(tmp_path / "private/report.json", {"ok": False})


@pytest.mark.asyncio
@pytest.mark.parametrize("transport", ["sdk", "mcp"])
async def test_benchmark_uses_same_service_budget_and_checker_trajectories(tmp_path, transport):
    raw = diag.PrivateArtifacts(tmp_path / "raw")
    backend = FakeDiagnostic(config(), tmp_path / "owner", artifacts=raw)
    service = LocalInferenceService(tmp_path / "owner", config=config(), backend=backend)
    async with bench.framework(tmp_path / "framework", tmp_path, service, transport) as call:
        result = await bench.workflow(call, service, raw, "questions", arm="resident", cap=32, timeout=5)
    await service.close()
    assert not result["errors"]
    assert result["completed_requests"] == result["first_attempt_successes"] == result["final_successes"] == 2
    first, second = result["outcomes"]
    assert first["reused_input_tokens"] == 0 < second["reused_input_tokens"]
    assert first["remaining_budgets"]["context"]["requests"] == 1
    assert second["remaining_budgets"]["context"]["requests"] == 0
    assert all(item["consumed_budget"]["model_requests"] == 1 for item in result["outcomes"])
    assert result["elapsed_ms"] >= sum(item["client_observed_ms"] for item in result["outcomes"])
    assert result["cleanup"][-1]["cleanup_completed"]
    assert service.metrics.dispatched == 2 and len(backend.calls) == 2


@pytest.mark.asyncio
async def test_benchmark_failed_quality_and_dispatch_budgets_preserved(tmp_path):
    raw = diag.PrivateArtifacts(tmp_path / "raw")
    backend = FakeDiagnostic(config(), tmp_path / "owner", artifacts=raw)
    service = LocalInferenceService(tmp_path / "owner", config=config(), backend=backend)
    async def wrong_answer(prompt, request, *, cache_enabled):
        return Completion("invalid JSON", "length", len(prompt.tokens), len(prompt.tokens), 0, 2, None, (1, 2), prompt.tokens + (1,))
    backend.generate = wrong_answer
    async def call(name, args):
        return await service.call(name, SimpleNamespace(transport="http", workspace_id="workspace", root=tmp_path, error=None), args)
    result = await bench.workflow(call, service, raw, "questions", arm="off", cap=32, timeout=5)
    await service.close()
    assert result["completed_requests"] == 2 and result["final_successes"] == 0
    assert all(row["checker"]["reason"] == "invalid_json" for row in result["outcomes"])
    assert all(row["consumed_budget"]["output_tokens"] == 2 for row in result["outcomes"])


@pytest.mark.asyncio
async def test_disabled_redis_arm_is_unperformed_without_any_model_call(tmp_path):
    result = await bench.arm_run(config(), tmp_path / "owner", diag.PrivateArtifacts(tmp_path / "raw"), tmp_path,
                                arm="redis", transport="sdk", families=["questions"], cap=32, timeout=5,
                                backend_factory=FakeDiagnostic)
    assert result["performed"] is False and result["reason"] == "redis_disabled"
    assert result["model_calls"] == 0 and result["service_dispatched"] == 0
    assert result["owned_backend_closed"]


def test_counterbalanced_order_frozen_families_and_small_sample_denominators():
    assert list(bench.orders(2, ["off", "resident", "redis"])) == [(0, ["off", "resident", "redis"]), (1, ["redis", "resident", "off"])]
    assert {"questions", "extraction", "implementation_review_parent", "no_shared_prefix", "long_output"} == set(bench.FAMILIES)
    assert not bench.checker("not JSON", {})["passed"]
    assert not bench.checker('{"x":2}', {"x": 1})["passed"]
    assert bench.checker('{"x":1}', {"x": 1})["passed"]
    assert bench.summary([{"performed": False}]) == []


@pytest.mark.parametrize("output,expected", [('{"count":21.0}', {"count": 21}),
    ("[false,true]", [0, 1]), ('{"nested":[{"count":true}]}', {"nested": [{"count": 1}]}),
    ('{"count":NaN}', {"count": 21}), ('{"count":0,"count":21}', {"count": 21})])
def test_quality_checker_rejects_numeric_bool_and_ambiguous_substitutions(output, expected):
    assert not bench.checker(output, expected)["passed"]


@pytest.mark.asyncio
async def test_real_audit_boundary_counts_only_native_model_dispatch_and_hashes_raw_bytes(tmp_path):
    raw = diag.PrivateArtifacts(tmp_path / "raw")
    owner = SimpleNamespace(artifacts=raw, calls=[], call_context={"case": "test", "rendered_token_hash": "a" * 64}, current_call=None)
    body = b'data: {"stop":true}\n\n'
    class Body(httpx.AsyncByteStream):
        def __init__(self, value):
            self.value = value
        async def __aiter__(self):
            yield self.value
    async def handler(request):
        return httpx.Response(200, stream=Body(body if request.url.path == "/completion" else b'{}'))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://owned.invalid") as client:
        audited = diag._AuditedClient(client, owner)
        async with audited.stream("GET", "/props") as response:
            await response.aread()
        async with audited.stream("POST", "/completion", content=b'{"prompt":"private"}') as response:
            assert b"".join([chunk async for chunk in response.aiter_raw()]) == body
    assert len(owner.calls) == 1
    assert Path(owner.calls[0]["raw_response"]["path"]).read_bytes() == body
    assert owner.calls[0]["native_reuse"] is None
    assert len(raw.artifacts) == 2


@pytest.mark.asyncio
async def test_actual_audit_counts_ambiguous_failed_connection_without_synthetic_counter(tmp_path):
    owner = SimpleNamespace(artifacts=diag.PrivateArtifacts(tmp_path / "raw"), calls=[], call_context={}, current_call=None)
    async def handler(request):
        raise httpx.ConnectError("No native response", request=request)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://owned.invalid") as client:
        audited = diag._AuditedClient(client, owner)
        with pytest.raises(httpx.ConnectError):
            async with audited.stream("POST", "/completion", content=b"{}"):
                pytest.fail("Failed dispatch produced a response")
    assert len(owner.calls) == 1 and owner.calls[0]["native_reuse"] is None
    assert "raw_response" not in owner.calls[0]


@pytest.mark.asyncio
async def test_exhausted_private_evidence_budget_never_dispatches_or_counts_model_call(tmp_path):
    owner = SimpleNamespace(artifacts=diag.PrivateArtifacts(tmp_path / "raw", maximum=1), calls=[], call_context={}, current_call=None)
    class Uncalled:
        def stream(self, *args, **kwargs):
            pytest.fail("Unrecordable model request was dispatched")
    with pytest.raises(ValueError):
        async with diag._AuditedClient(Uncalled(), owner).stream("POST", "/completion", content=b"{}"):
            pytest.fail("Unrecordable request produced a response")
    assert not owner.calls
