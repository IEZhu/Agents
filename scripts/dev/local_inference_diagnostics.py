"""Explicit owner diagnostics; importing this module never starts an engine."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
import hashlib
import json
import os
from pathlib import Path
import platform
import secrets
import stat
import sys
import time
from uuid import uuid4

from src.local_inference.child_exec import LIMIT_ACK
from src.local_inference.llama import LlamaBackend, token_ids
from src.local_inference.backend import PreparedPrompt
from src.local_inference.models import GenerateRequest, LocalInferenceError
from src.local_inference.process import child_environment, private_directory
from src.local_inference.profile import canonical_hash, canonical_json


def absolute(value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("Diagnostic paths must be absolute.")
    return path


class PrivateArtifacts:
    """New private files only; bounded raw evidence never enters public reports."""
    def __init__(self, directory: Path, *, maximum=32 * 1024 * 1024):
        private_directory(directory)
        self.directory, self.maximum, self.used = directory, maximum, 0
        self.artifacts = []

    def write(self, name, value):
        data = value if isinstance(value, bytes) else canonical_json(value)
        if Path(name).name != name or self.used + len(data) > self.maximum:
            raise ValueError("Private evidence limit exceeded.")
        path = self.directory / name
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
        self.used += len(data)
        item = {"path": str(path), "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
        self.artifacts.append(item)
        return item


def write_report(path: Path, report):
    """Refuse replacement of an existing owner file, including a symlink."""
    private_directory(path.parent)
    encoded = canonical_json(report)
    if len(encoded) > 16 * 1024 * 1024:
        raise ValueError("Diagnostic report limit exceeded.")
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "wb") as stream:
        stream.write(encoded)


class _Response:
    def __init__(self, response, owner, row):
        self._response, self._owner, self._row = response, owner, row

    def __getattr__(self, name):
        return getattr(self._response, name)

    async def aiter_raw(self, *args, **kwargs):
        # Native streams already have an adapter bound. Keep an independent
        # diagnostic bound and preserve partial bytes even if parsing fails.
        parts, size = [], 0
        try:
            async for chunk in self._response.aiter_raw(*args, **kwargs):
                size += len(chunk)
                if size > 1024 * 1024:
                    raise ValueError("Diagnostic response limit exceeded.")
                parts.append(chunk)
                yield chunk
        finally:
            self._row["raw_response"] = self._owner.artifacts.write(
                f"call-{self._row['index']:03d}.sse", b"".join(parts))


class _AuditedClient:
    def __init__(self, client, owner):
        self.client, self.owner = client, owner

    async def aclose(self):
        await self.client.aclose()

    @asynccontextmanager
    async def stream(self, method, path, **kwargs):
        if path != "/completion":
            async with self.client.stream(method, path, **kwargs) as response:
                yield response
            return
        # Count dispatch attempts at the actual native HTTP boundary, including
        # failures before headers. Helpers and slot calls never increment this.
        row = {"index": len(self.owner.calls) + 1, **self.owner.call_context,
               "native_reuse": None, "native_stop_type": None, "error": None}
        row["raw_request"] = self.owner.artifacts.write(
            f"call-{row['index']:03d}.request.json", kwargs.get("content", b""))
        self.owner.calls.append(row)
        self.owner.current_call = row
        async with self.client.stream(method, path, **kwargs) as response:
            yield _Response(response, self.owner, row)


class DiagnosticBackend(LlamaBackend):
    """Same production adapter, with owner-only sampling cases and raw audit."""
    def __init__(self, config, directory, *, artifacts, **kwargs):
        super().__init__(config, directory, **kwargs)
        self.artifacts = artifacts
        self.calls, self.call_context, self.current_call = [], {}, None
        self.overrides = {}

    async def _new_client(self):
        await super()._new_client()
        self._client = _AuditedClient(self._client, self)

    def _body(self, value):
        if isinstance(value, dict) and value.get("stream") is True and "n_predict" in value:
            value = {**value, **self.overrides}
        return super()._body(value)

    async def _events(self, response):
        async for event in super()._events(response):
            if event and event.get("stop") is True and self.current_call is not None:
                self.current_call["native_stop_type"] = event.get("stop_type")
                timing = event.get("timings")
                self.current_call["native_reuse"] = timing.get("cache_n") if type(timing) is dict else None
                self.current_call["native_timings"] = timing if type(timing) is dict else None
                self.current_call["native_tokens_cached"] = event.get("tokens_cached")
            yield event

    async def generate(self, prompt, request, *, cache_enabled):
        if request.context_id != "owner-probe":
            self.call_context = {"case": "benchmark", "rendered_token_hash": canonical_hash(prompt.tokens),
                                 "rendered_input_hash": hashlib.sha256(prompt.text.encode()).hexdigest()}
        self.call_context["raw_prepared"] = self.artifacts.write(
            f"prepared-{secrets.token_hex(8)}.json", {"rendered_text": prompt.text, "token_ids": prompt.tokens})
        before, started = len(self.calls), time.monotonic()
        try:
            return await super().generate(prompt, request, cache_enabled=cache_enabled)
        except Exception as error:
            if len(self.calls) > before:
                self.calls[-1]["error"] = error.code if isinstance(error, LocalInferenceError) else "diagnostic_failure"
            raise
        finally:
            if len(self.calls) > before:
                self.calls[-1]["elapsed_ms"] = (time.monotonic() - started) * 1000


async def generation(backend, case, prompt, *, cap, timeout, cache, overrides=None):
    backend.call_context = {"case": case, "rendered_token_hash": canonical_hash(prompt.tokens),
                            "rendered_input_hash": hashlib.sha256(prompt.text.encode()).hexdigest()}
    backend.overrides = overrides or {}
    before = len(backend.calls)
    request = GenerateRequest(context_id="owner-probe", access_token="private-unused-probe-token",
                              request_id=str(uuid4()), task="owner diagnostic",
                              max_output_tokens=cap, timeout_seconds=timeout)
    started = time.monotonic()
    try:
        value = await backend.generate(prompt, request, cache_enabled=cache)
        if len(backend.calls) != before + 1:
            raise ValueError("Native model dispatch was not audited.")
        row = backend.calls[-1]
        row.update({"input_tokens": value.input_tokens, "evaluated_input_tokens": value.evaluated_input_tokens,
                    "reused_input_tokens": value.reused_input_tokens, "output_tokens": value.output_tokens,
                    "output_hash": hashlib.sha256(value.output_text.encode()).hexdigest(),
                    "output_ids_hash": canonical_hash(value.output_ids),
                    "output_ids_complete": len(value.output_ids) == value.output_tokens,
                    "saved_state_hash": canonical_hash(value.saved_token_state) if value.saved_token_state is not None else None,
                    "saved_state_tokens": len(value.saved_token_state) if value.saved_token_state is not None else None})
        return value
    except Exception as error:
        if len(backend.calls) > before:
            backend.calls[-1]["error"] = error.code if isinstance(error, LocalInferenceError) else "diagnostic_failure"
        raise
    finally:
        if len(backend.calls) > before:
            backend.calls[-1]["elapsed_ms"] = (time.monotonic() - started) * 1000
        backend.overrides = {}


async def prove_write_limit(directory: Path):
    """Execute the production exec wrapper with a bounded synthetic writer."""
    private_directory(directory)
    target = directory / f"limit-{secrets.token_hex(16)}.bin"
    limit = 4096
    wrapper = Path(__file__).resolve().parents[2] / "src/local_inference/child_exec.py"
    # The absolute interpreter is already selected by the owner invoking us.
    code = "import os,sys; f=os.open(sys.argv[1],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600); os.write(f,b'x'*8192); os.write(f,b'x'*8192)"
    process = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(wrapper), str(limit), sys.executable, "-I", "-c", code, str(target),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL, env=child_environment())
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 5)
        size = target.lstat().st_size if target.exists() else None
        return {"proven": output == f"{LIMIT_ACK}{limit}\n".encode() and size == limit and process.returncode != 0,
                "limit_bytes": limit, "actual_bytes": size, "returncode": process.returncode,
                "acknowledgement_sha256": hashlib.sha256(output).hexdigest()}
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
        target.unlink(missing_ok=True)


async def roundtrip(backend, value, prompt, *, cap, timeout, maximum, spool_budget):
    """Private diagnostic bypass; normal slot_action remains capability-gated."""
    from src.local_inference.backend import longest_common_prefix
    from src.local_inference.snapshots import SnapshotManager

    state = value.saved_token_state
    if state is None or state != prompt.tokens + value.output_ids[:-1] or len(value.output_ids) != value.output_tokens:
        return {"proven": False, "reason": "token_state_unavailable"}
    if backend.info is None or not backend.info.native_write_limit or backend.spool is None:
        return {"proven": False, "reason": "native_write_limit_unavailable"}
    spool = Path(backend.spool)
    private_directory(spool)
    entries = list(spool.iterdir())
    if len(entries) > 1024:
        return {"proven": False, "reason": "spool_capacity"}
    used = 0
    for entry in entries:
        info = entry.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid():
            return {"proven": False, "reason": "unsafe_spool"}
        used += info.st_size
    reservation = maximum + 4 * len(state)
    if used + reservation > spool_budget:
        return {"proven": False, "reason": "spool_capacity"}
    continuation_text = prompt.text + value.output_text + "\nContinue with OK."
    tokenized = await backend._json("/tokenize", {"content": continuation_text, "add_special": True,
                                                 "parse_special": True, "with_pieces": False}, timeout=timeout)
    continuation = PreparedPrompt(continuation_text, token_ids(tokenized["tokens"], backend.config.context_tokens))
    expected = longest_common_prefix(state, continuation.tokens)
    if expected < len(prompt.tokens) or len(state) > len(prompt.tokens) and expected <= len(prompt.tokens):
        return {"proven": False, "reason": "generated_state_prefix_unproven"}
    # Reserve the entire possible native file plus sidecar before the command.
    # The diagnostic is this backend's sole serialized caller.
    name = f"snapshot-{secrets.token_hex(16)}.bin"
    target = spool / name
    if target.exists() or target.is_symlink():
        raise ValueError("Snapshot filename collision.")
    try:
        saved = await backend._json("/slots/0?action=save", {"filename": name}, timeout=timeout)
        saved_evidence = backend.artifacts.write(f"{name}.save.json", saved)
        fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        digest = hashlib.sha256()
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
                raise ValueError("Snapshot file exceeds reservation.")
            SnapshotManager._native_counts(saved, "save", len(state), info.st_size)
            while chunk := os.read(fd, 1024 * 1024):
                digest.update(chunk)
        finally:
            os.close(fd)
        await backend.erase()
        restored = await backend._json("/slots/0?action=restore", {"filename": name}, timeout=timeout)
        restored_evidence = backend.artifacts.write(f"{name}.restore.json", restored)
        SnapshotManager._native_counts(restored, "restore", len(state), info.st_size)
        repeated = await generation(backend, "restored-continuation", continuation, cap=cap, timeout=timeout, cache=True)
        proven = (0 < repeated.reused_input_tokens <= expected and
                  (len(state) == len(prompt.tokens) or repeated.reused_input_tokens > len(prompt.tokens)))
        return {"proven": proven, "reserved_bytes": reservation, "snapshot_bytes": info.st_size,
                "snapshot_sha256": digest.hexdigest(), "state_tokens": len(state),
                "n_saved": saved["n_saved"], "n_restored": restored["n_restored"],
                "continuation_reuse": repeated.reused_input_tokens, "continuation_lcp": expected,
                "original_input_tokens": len(prompt.tokens),
                "save_evidence": saved_evidence, "restore_evidence": restored_evidence}
    finally:
        target.unlink(missing_ok=True)


async def probe(config, state_dir, artifacts, *, cap=32, timeout=120, backend_factory=DiagnosticBackend,
                write_limit_checker=prove_write_limit):
    from src.local_inference.backend import longest_common_prefix
    from src.local_inference.capabilities import MAPPING_VERSION, PROBE_PROTOCOL

    report = {"schema": "agents-core/local-inference-probe-report/1", "created_at": time.time(),
              "owner_config_revision": config.revision, "configuration": config.model_dump(mode="json", by_alias=True),
              "conditions": {"platform": platform.platform(), "machine": platform.machine(), "python": sys.version,
                             "temperature": 0.0, "seed": 42, "max_output_tokens": cap, "timeout_seconds": timeout,
                             "boundary": "owned startup through owned process close; every /completion attempt counted"},
              "resident": "unknown", "snapshot": "unknown", "finish_cases": {}, "capability_record": None,
              "errors": [], "calls": [], "model_calls": 0}
    if not config.enabled:
        raise ValueError("Owner diagnostic requires enabled configuration.")
    private_directory(state_dir)
    backend = backend_factory(config, state_dir, artifacts=artifacts)
    started = time.monotonic()
    record = None
    try:
        report["write_limit_test"] = await write_limit_checker(artifacts.directory)
        info = await backend.ensure_ready()
        report["profile_hash"] = info.profile_hash
        report["profile"] = backend.profile.canonical()
        report["engine_epoch"] = info.epoch
        prefix = "\n".join(f"Record {i:03d}: owner probe fixed shared context value {i * 3}." for i in range(32))
        def messages(task):
            return [{"role": "system", "content": "Follow the final instruction exactly. Return no explanation."},
                    {"role": "user", "content": prefix}, {"role": "user", "content": task}]
        cold_prompt = await backend.prepare(messages("Return just READY."))
        cold = await generation(backend, "resident-cold", cold_prompt, cap=cap, timeout=timeout, cache=False)
        warm_prompt = await backend.prepare(messages("Return just READY again."))
        warm = await generation(backend, "resident-repeat", warm_prompt, cap=cap, timeout=timeout, cache=True)
        lcp = longest_common_prefix(cold.saved_token_state or cold_prompt.tokens, warm_prompt.tokens)
        if cold.reused_input_tokens != 0 or warm.reused_input_tokens > lcp:
            raise LocalInferenceError("backend_protocol_error", "Native reuse exceeded the controlled prefix.")
        report["resident"] = "supported" if 0 < warm.reused_input_tokens <= lcp and cold.reused_input_tokens == 0 else "unsupported"
        report["resident_lcp"] = lcp
        cases = [("eos", "Return just READY.", cap, {}),
                 ("word", "Return exactly READY STOP trailing text.", cap,
                  {"grammar": 'root ::= "READY STOP trailing text"', "stop": ["STOP"]}),
                 ("limit", "Return the digits 123456789 repeatedly without stopping.", 1,
                  {"grammar": 'root ::= "123456789123456789"'}),
                 ("unicode", "Return exactly the character 𐀀.", 1, {"grammar": 'root ::= "𐀀"'})]
        for name, task, allowance, overrides in cases:
            prompt = await backend.prepare(messages(task))
            value = await generation(backend, name, prompt, cap=allowance, timeout=timeout, cache=False, overrides=overrides)
            native = backend.calls[-1]
            if name == "unicode":
                # A suppressed incomplete UTF-8 piece has no authoritative full
                # IDs. Correct mapping is to refuse any snapshot of that state.
                proven = (native["native_stop_type"] == "limit" and len(value.output_ids) < value.output_tokens
                          and value.saved_token_state is None and value.output_text == "")
                case = {"proven": proven, "snapshot_refused": value.saved_token_state is None,
                        "reason": None if proven else "incomplete_utf8_not_observed"}
            else:
                case = {"proven": False, "reason": "requested_finish_not_observed"}
                if native["native_stop_type"] == name:
                    case = await roundtrip(backend, value, prompt, cap=cap, timeout=timeout,
                                           maximum=config.redis.max_snapshot_bytes, spool_budget=config.redis.max_spool_bytes)
            report["finish_cases"][name] = case
        mapped = all(report["finish_cases"].get(name, {}).get("proven") for name in ("eos", "word", "limit", "unicode"))
        write_bound = info.native_write_limit and report["write_limit_test"]["proven"]
        if mapped and write_bound and report["resident"] == "supported":
            report["snapshot"] = "supported"
        counts = [row["native_reuse"] for row in backend.calls]
        if not all(type(count) is int and count >= 0 for count in counts):
            report["resident"] = report["snapshot"] = "unknown"
        if (2 <= len(backend.calls) <= 100 and all(type(count) is int and count >= 0 for count in counts)
                and not any(row["error"] for row in backend.calls)):
            record = {"schema": PROBE_PROTOCOL, "profile_hash": info.profile_hash, "mapping_version": MAPPING_VERSION,
                      "resident": report["resident"], "snapshot": report["snapshot"], "mapping_proven": mapped,
                      "native_write_limit": bool(write_bound), "finish_cases": [name for name, case in report["finish_cases"].items() if case["proven"]],
                      "model_calls": len(backend.calls), "created_at": time.time(),
                      "rendered_token_hashes": [row["rendered_token_hash"] for row in backend.calls], "native_reuse_counts": counts}
    except Exception as error:
        report["errors"].append(error.code if isinstance(error, LocalInferenceError) else "diagnostic_failure")
    finally:
        report["calls"] = backend.calls
        report["model_calls"] = len(backend.calls)
        try:
            await backend.close()
        except Exception:
            report["errors"].append("owned_backend_cleanup_failed")
        report["elapsed_ms"] = (time.monotonic() - started) * 1000
        report["owned_backend_closed"] = backend.state == "stopping"
        report["raw_artifacts"] = artifacts.artifacts
    if record is not None and report["owned_backend_closed"] and not report["errors"]:
        try:
            report["capability_record"] = str(backend.records.write(record))
        except Exception:
            report["errors"].append("capability_record_publication_failed")
    return report
