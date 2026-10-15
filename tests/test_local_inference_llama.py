"""Native protocol checks use synthetic HTTP only; no model is loaded."""
import asyncio
from dataclasses import replace
import importlib
import json
from pathlib import Path
import time
import tempfile
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from src.local_inference.backend import PreparedPrompt
from src.local_inference.capabilities import CapabilityRecords, MAPPING_VERSION, PROBE_PROTOCOL
from src.local_inference.config import OwnerConfig
from src.local_inference.llama import LlamaBackend
from src.local_inference.models import GenerateRequest, LocalInferenceError
from src.local_inference.process import FileFingerprint


class FakeProcess:
    def __init__(self, config, directory, *, port=None):
        self.config = config
        self.alive = False
        self.state = "not_started"
        self.epoch = "epoch-1"
        self.api_key = "private-sentinel-api-key"
        self.spool = Path(directory)
        self.base_url = f"http://127.0.0.1:{port or config.port}"
        self.native_write_limit = True
        self.files = (FileFingerprint((1, 2, 3, 4, 5), "a" * 64),
                      FileFingerprint((1, 2, 3, 4, 5), "b" * 64))
        self.build = "b11429-d81235049384534c167caea52b85a694f6103d14"
        self.starts = 0

    async def ensure_started(self):
        if not self.alive:
            self.starts += 1
            self.alive = True

    async def fence(self):
        self.alive = False
        self.epoch = "epoch-" + str(self.starts + 1)
        return True

    async def close(self):
        await self.fence()


class Chunks(httpx.AsyncByteStream):
    def __init__(self, chunks, delay=0):
        self.chunks, self.delay = chunks, delay

    async def __aiter__(self):
        for chunk in self.chunks:
            if self.delay:
                await asyncio.sleep(self.delay)
            yield chunk


def final_event(**changes):
    value = {"id_slot": 0, "content": "", "tokens": [], "stop": True,
             "tokens_evaluated": 3, "tokens_predicted": 2, "tokens_cached": 4,
             "prompt": "<bos>rendered", "truncated": False, "stop_type": "eos",
             "timings": {"cache_n": 0, "prompt_n": 3, "predicted_n": 2,
                         "prompt_ms": 1.0, "predicted_ms": 2.0}}
    value.update(changes)
    return value


def partial(token=10, text="a", count=1):
    return {"id_slot": -1, "content": text, "tokens": [token], "stop": False,
            "tokens_evaluated": 3, "tokens_predicted": count}


def frame(event):
    return b"data: " + json.dumps(event).encode() + b"\n\n"


def json_response(value):
    return httpx.Response(200, headers={"content-type": "application/json"},
                          stream=Chunks([json.dumps(value).encode()]))


class NativeHTTP:
    def __init__(self, *, events=None, chunks=None, status=200, headers=None, delay=0):
        self.events = [partial(), partial(11, "b", 2), final_event()] if events is None else events
        self.chunks = chunks
        self.status = status
        self.headers = {"content-type": "text/event-stream", **(headers or {})}
        self.delay = delay
        self.calls = []
        self.request_timeouts = []
        self.tokenizer_ids = [1, 2, 3]
        self.props_changes = {}

    def response(self, request):
        body = json.loads(request.content) if request.content else None
        self.calls.append((request.url.path, body, dict(request.headers)))
        if request.url.path == "/completion":
            self.request_timeouts.append(request.extensions.get("timeout"))
        if request.url.path == "/health":
            return json_response({"status": "ok"})
        if request.url.path == "/props":
            data = {"default_generation_settings": {"n_ctx": 128, "params": {"speculative.types": "none", "lora": []}},
                    "total_slots": 1, "build_info": "b11429-d81235049", "chat_template": "template-v1",
                    "chat_template_caps": {}, "modalities": {"vision": False, "video": False, "audio": False}}
            data.update(self.props_changes)
            return json_response(data)
        if request.url.path == "/apply-template":
            return json_response({"prompt": "rendered"})
        if request.url.path == "/tokenize":
            # The processed echo contains BOS text, but prepared original text
            # receives BOS through add_special. Both authoritative sequences match.
            correct = body["content"] == "rendered" and body["add_special"] is True
            correct |= body["content"] == "<bos>rendered" and body["add_special"] is False
            return json_response({"tokens": self.tokenizer_ids if correct else [2, 3]})
        if request.url.path == "/completion":
            chunks = self.chunks if self.chunks is not None else [frame(event) for event in self.events]
            return httpx.Response(self.status, headers=self.headers, stream=Chunks(chunks, self.delay))
        if request.url.path == "/slots":
            return json_response({"id_slot": 0, "n_erased": 4})
        raise AssertionError(request.url)


def request(**changes):
    return GenerateRequest(context_id="ctx", access_token="secret-capability", request_id=str(uuid4()),
                           task="task", max_output_tokens=8, **changes)


async def backend(tmp_path, native=None, **kwargs):
    config = OwnerConfig(enabled=True, llama_server_binary="/fake/server", model_path="/fake/model",
                         context_tokens=128, max_output_tokens=8)
    native = NativeHTTP() if native is None else native
    process = FakeProcess(config, tmp_path)
    value = LlamaBackend(config, tmp_path, supervisor=process, transport=httpx.MockTransport(native.response), **kwargs)
    await value.ensure_ready()
    return value, native


@pytest.mark.asyncio
async def test_owned_startup_health_retries_listener_race_without_model_dispatch(tmp_path):
    native = NativeHTTP()
    health_calls = 0
    def response(request):
        nonlocal health_calls
        if request.url.path == "/health":
            health_calls += 1
            if health_calls == 1:
                raise httpx.ConnectError("not listening yet", request=request)
            if health_calls == 2:
                raise httpx.ConnectTimeout("not connected yet", request=request)
            if health_calls == 3:
                return httpx.Response(503)
        return native.response(request)
    config = OwnerConfig(enabled=True, llama_server_binary="/fake/server", model_path="/fake/model",
                         context_tokens=128, max_output_tokens=8)
    process = FakeProcess(config, tmp_path)
    value = LlamaBackend(config, tmp_path, supervisor=process, transport=httpx.MockTransport(response))
    try:
        await value.ensure_ready()
        assert health_calls == 4 and value.state == "ready" and process.starts == 1
        assert all(path != "/completion" for path, _, _ in native.calls)
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_full_native_request_bos_and_conventional_state(tmp_path):
    value, native = await backend(tmp_path)
    try:
        prompt = await value.prepare([{"role": "user", "content": "hello"}])
        result = await value.generate(prompt, request(), cache_enabled=False)
        assert result.output_text == "ab"
        assert result.output_tokens == 2 and result.saved_token_state == (1, 2, 3, 10)
        assert result.input_tokens == result.evaluated_input_tokens == 3
        assert result.reused_input_tokens == 0 and result.first_token_ms is not None
        assert value.info.resident == value.info.snapshot == "unknown"
        assert value.info.context_headroom == 1
        sent = [body for path, body, _ in native.calls if path == "/completion"]
        assert len(sent) == 1 and sent[0]["prompt"] == "rendered"
        assert sent[0]["stream"] is sent[0]["return_tokens"] is True
        assert sent[0]["id_slot"] == 0 and sent[0]["n_predict"] == 8
        assert native.request_timeouts == [{"connect": 1.0, "pool": 1.0, "write": 5.0, "read": 120.0}]
        assert all(headers["authorization"] == "Bearer private-sentinel-api-key" for _, _, headers in native.calls)
        assert all(headers["accept-encoding"] == "identity" for _, _, headers in native.calls)
    finally:
        await value.close()


@pytest.mark.parametrize("finish,expected", [("eos", "stop"), ("word", "stop"), ("limit", "length")])
@pytest.mark.asyncio
async def test_finish_mapping_does_not_retokenize_output(tmp_path, finish, expected):
    native = NativeHTTP(events=[partial(10, "visible"), partial(11, "", 2), final_event(stop_type=finish)])
    value, native = await backend(tmp_path, native)
    try:
        result = await value.generate(PreparedPrompt("rendered", (1, 2, 3)), request(), cache_enabled=False)
        assert result.finish_reason == expected and result.output_text == "visible"
        assert result.saved_token_state == (1, 2, 3, 10)
        assert [body["content"] for path, body, _ in native.calls if path == "/tokenize"] == ["<bos>rendered"]
    finally:
        await value.close()


@pytest.mark.parametrize("slot", [0, 1, -2, False, None])
@pytest.mark.asyncio
async def test_pinned_partial_slot_requires_unassigned_sentinel(tmp_path, slot):
    # Live b11429 partials inherit id_slot=-1; final frames alone identify slot0.
    event = {**partial(), "id_slot": slot}
    value, native = await backend(tmp_path, NativeHTTP(events=[event, final_event()]))
    try:
        with pytest.raises(LocalInferenceError) as caught:
            await value.generate(PreparedPrompt("rendered", (1, 2, 3)), request(), cache_enabled=False)
        assert caught.value.code == "backend_protocol_error" and caught.value.generation_dispatched
        assert sum(path == "/completion" for path, _, _ in native.calls) == 1
    finally:
        await value.close()


@pytest.mark.parametrize("events", [
    [partial(11, "unicode", 2), final_event()],
    [partial(), partial(11, "b", 2), final_event(tokens_cached=5)],
])
@pytest.mark.asyncio
async def test_incomplete_id_or_unproven_state_succeeds_without_residency(tmp_path, events):
    value, _ = await backend(tmp_path, NativeHTTP(events=events))
    try:
        result = await value.generate(PreparedPrompt("rendered", (1, 2, 3)), request(), cache_enabled=False)
        assert result.output_tokens == 2 and result.saved_token_state is None
    finally:
        await value.close()


def bad_final_cases():
    missing = final_event(); missing["timings"].pop("cache_n")
    return [missing, final_event(timings={"cache_n": True, "prompt_n": 3, "predicted_n": 2}),
            final_event(timings={"cache_n": 0, "prompt_n": 2, "predicted_n": 2}),
            final_event(timings={"cache_n": 0, "prompt_n": 3, "predicted_n": 2, "prompt_ms": float("nan")}),
            final_event(timings={"cache_n": 0, "prompt_n": 3, "predicted_n": 2, "prompt_ms": -1}),
            final_event(tokens_predicted=3), final_event(tokens_evaluated=2), final_event(truncated=True),
            final_event(stop_type="none"), final_event(id_slot=False), final_event(id_slot=-1), final_event(prompt="wrong echo"),
            final_event(tokens=[11]), final_event(content="repeated content"), final_event(tokens_cached=-1)]


@pytest.mark.parametrize("final", bad_final_cases())
@pytest.mark.asyncio
async def test_invalid_native_final_refused_without_replay(tmp_path, final):
    value, native = await backend(tmp_path, NativeHTTP(events=[partial(), partial(11, "b", 2), final]))
    try:
        with pytest.raises(LocalInferenceError) as caught:
            await value.generate(PreparedPrompt("rendered", (1, 2, 3)), request(), cache_enabled=False)
        assert caught.value.code == "backend_protocol_error" and caught.value.generation_dispatched
        assert sum(path == "/completion" for path, _, _ in native.calls) == 1
    finally:
        await value.close()


@pytest.mark.parametrize("chunks", [
    [frame(partial())], [b"data: {bad}\n\n"], [b'data: {"stop":true,"stop":false}\n\n'],
    [frame(partial())[:-1]], [b"data: [DONE]\n\n"], [frame({"error": {"message": "secret"}})],
    [frame(partial()), frame(final_event()), frame(final_event())], [b"event: bogus\n\n"],
])
@pytest.mark.asyncio
async def test_malformed_incomplete_or_error_sse(tmp_path, chunks):
    value, native = await backend(tmp_path, NativeHTTP(chunks=chunks))
    try:
        with pytest.raises(LocalInferenceError) as caught:
            await value.generate(PreparedPrompt("rendered", (1, 2, 3)), request(), cache_enabled=False)
        assert caught.value.code == "backend_protocol_error"
        assert "secret" not in str(caught.value)
        assert sum(path == "/completion" for path, _, _ in native.calls) == 1
    finally:
        await value.close()


@pytest.mark.parametrize("header,status", [({"location": "http://example.invalid"}, 307),
                                           ({"content-encoding": "gzip"}, 200),
                                           ({"content-length": "999999999"}, 200),
                                           ({"content-type": "application/json"}, 200)])
@pytest.mark.asyncio
async def test_redirect_encoded_or_wrong_content_refused(tmp_path, header, status):
    value, native = await backend(tmp_path, NativeHTTP(headers=header, status=status))
    try:
        with pytest.raises(LocalInferenceError):
            await value.generate(PreparedPrompt("rendered", (1, 2, 3)), request(), cache_enabled=False)
        assert sum(path == "/completion" for path, _, _ in native.calls) == 1
    finally:
        await value.close()


@pytest.mark.parametrize("limits,chunks", [({"max_frame_bytes": 50}, [b":" + b"x" * 60]),
                                          ({"max_stream_bytes": 100}, [b": ping\n\n" * 20])])
@pytest.mark.asyncio
async def test_stream_and_frame_bounds(tmp_path, limits, chunks):
    value, _ = await backend(tmp_path, NativeHTTP(chunks=chunks), **limits)
    try:
        with pytest.raises(LocalInferenceError) as caught:
            await value.generate(PreparedPrompt("rendered", (1, 2, 3)), request(), cache_enabled=False)
        assert caught.value.code == "backend_protocol_error"
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_fragmented_crlf_and_first_token_before_final(tmp_path):
    raw = b": ping\r\n\r\n" + b"".join(frame(event).replace(b"\n", b"\r\n") for event in
                                           [partial(), partial(11, "b", 2), final_event()])
    value, _ = await backend(tmp_path, NativeHTTP(chunks=[raw[index:index + 7] for index in range(0, len(raw), 7)]))
    try:
        result = await value.generate(PreparedPrompt("rendered", (1, 2, 3)), request(), cache_enabled=False)
        assert result.output_text == "ab" and result.saved_token_state == (1, 2, 3, 10)
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_trickled_sse_has_total_deadline_and_no_replay(tmp_path):
    value, native = await backend(tmp_path, NativeHTTP(chunks=[b": ping\n\n"] * 100, delay=0.03))
    try:
        started = time.monotonic()
        with pytest.raises(LocalInferenceError) as caught:
            await value.generate(PreparedPrompt("rendered", (1, 2, 3)), request(timeout_seconds=1), cache_enabled=False)
        assert caught.value.code == "deadline_exceeded" and time.monotonic() - started < 1.5
        assert sum(path == "/completion" for path, _, _ in native.calls) == 1
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_stopped_child_refused_before_dispatch(tmp_path):
    value, native = await backend(tmp_path)
    value.process.alive = False
    with pytest.raises(LocalInferenceError) as caught:
        await value.generate(PreparedPrompt("rendered", (1, 2, 3)), request(), cache_enabled=False)
    assert caught.value.code == "backend_unavailable" and not caught.value.generation_dispatched
    assert all(path != "/completion" for path, _, _ in native.calls)
    await value.close()


@pytest.mark.asyncio
async def test_exact_context_boundary_refused_before_dispatch(tmp_path):
    value, native = await backend(tmp_path)
    try:
        with pytest.raises(LocalInferenceError) as caught:
            await value.generate(PreparedPrompt("rendered", tuple(range(120))), request(), cache_enabled=False)
        assert caught.value.code == "context_overflow" and not caught.value.generation_dispatched
        assert all(path != "/completion" for path, _, _ in native.calls)
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_client_disables_environment_proxies_and_redirects(tmp_path, monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    options = []
    def factory(**kwargs):
        options.append(kwargs)
        return httpx.AsyncClient(**kwargs)
    value, _ = await backend(tmp_path, client_factory=factory)
    try:
        assert options[0]["trust_env"] is options[0]["follow_redirects"] is False
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_no_optional_import_until_explicit_start(tmp_path, monkeypatch):
    real = importlib.import_module
    def missing(name, *args, **kwargs):
        if name == "httpx":
            raise ImportError()
        return real(name, *args, **kwargs)
    monkeypatch.setattr(importlib, "import_module", missing)
    config = OwnerConfig(enabled=True, llama_server_binary="/fake/server", model_path="/fake/model")
    value = LlamaBackend(config, tmp_path, supervisor=FakeProcess(config, tmp_path))
    assert value.state == "not_started" and not tmp_path.joinpath("capabilities").exists()
    with pytest.raises(LocalInferenceError) as caught:
        await value.ensure_ready()
    assert caught.value.code == "unsupported"
    await value.close()


def probe(profile_hash="c" * 64, **changes):
    value = {"schema": PROBE_PROTOCOL, "profile_hash": profile_hash, "mapping_version": MAPPING_VERSION,
             "resident": "supported", "snapshot": "supported", "mapping_proven": True,
             "native_write_limit": True, "finish_cases": ["eos", "word", "limit", "unicode"],
             "model_calls": 6, "created_at": 1.0, "rendered_token_hashes": ["d" * 64] * 6,
             "native_reuse_counts": [0, 2, 0, 2, 0, 2]}
    value.update(changes)
    if "model_calls" in changes:
        value["rendered_token_hashes"] = ["d" * 64] * changes["model_calls"]
        value["native_reuse_counts"] = [0, 2] * (changes["model_calls"] // 2)
    return value


def test_signed_probe_exact_profile_and_native_write_gate(tmp_path):
    records = CapabilityRecords(tmp_path, clock=lambda: 10)
    assert records.load("c" * 64, native_write_limit=True) == ("unknown", "unknown")
    path = records.write(probe())
    assert records.load("c" * 64, native_write_limit=True) == ("supported", "supported")
    assert records.load("c" * 64, native_write_limit=False) == ("supported", "unknown")
    assert records.load("f" * 64, native_write_limit=True) == ("unknown", "unknown")
    changed = json.loads(path.read_text()); changed["model_calls"] = 7
    path.write_text(json.dumps(changed))
    assert records.load("c" * 64, native_write_limit=True) == ("unknown", "unknown")


@pytest.mark.parametrize("changes", [{"mapping_proven": False}, {"finish_cases": ["eos", "limit"]},
                                     {"model_calls": 2}, {"native_write_limit": False}])
def test_snapshot_probe_needs_all_proofs(tmp_path, changes):
    records = CapabilityRecords(tmp_path, clock=lambda: 10)
    records.write(probe(**changes))
    assert records.load("c" * 64, native_write_limit=True) == ("supported", "unknown")


@pytest.mark.asyncio
async def test_snapshot_unknown_never_dispatches_slot_operation(tmp_path):
    value, native = await backend(tmp_path)
    try:
        with pytest.raises(LocalInferenceError) as caught:
            await value.slot_action("save", "snapshot-" + "a" * 32 + ".bin")
        assert caught.value.code == "unsupported" and all(path != "/slots" for path, _, _ in native.calls)
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_private_unix_http_ignores_tcp_interceptor_proxy_and_redirect(tmp_path, monkeypatch):
    native = NativeHTTP()
    handlers = set()
    intercepted = []
    async def serve(reader, writer, *, foreign=False):
        task = asyncio.current_task(); handlers.add(task)
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            lines = header.decode().split("\r\n")
            method, route, _ = lines[0].split()
            if foreign:
                intercepted.append(route)
            headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
            content = await reader.readexactly(int(headers.get("Content-Length", "0")))
            response = native.response(httpx.Request(method, "http://127.0.0.1" + route, content=content, headers=headers))
            raw = await response.aread()
            wire = (f"HTTP/1.1 {response.status_code} synthetic\r\n" +
                    "".join(f"{key}: {value}\r\n" for key, value in response.headers.items()
                            if key.lower() != "content-length") +
                    f"Content-Length: {len(raw)}\r\nConnection: close\r\n\r\n").encode() + raw
            writer.write(wire); await writer.drain()
        finally:
            writer.close(); await writer.wait_closed(); handlers.discard(task)
    server = await asyncio.start_server(lambda r, w: serve(r, w, foreign=True), "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    directory = tempfile.TemporaryDirectory(prefix="agents-native-test-", dir="/tmp")
    endpoint = Path(directory.name) / "engine.sock"
    unix = await asyncio.start_unix_server(serve, path=str(endpoint))
    config = OwnerConfig(enabled=True, llama_server_binary="/fake/server", model_path="/fake/model",
                         port=port, context_tokens=128, max_output_tokens=8)
    process = FakeProcess(config, tmp_path)
    process.socket_path = endpoint
    value = LlamaBackend(config, tmp_path, supervisor=process)
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("ALL_PROXY", "http://127.0.0.1:1")
    monkeypatch.setenv("NO_PROXY", "")
    try:
        await value.ensure_ready()
        prompt = await value.prepare([{"role": "user", "content": "synthetic"}])
        result = await value.generate(prompt, request(), cache_enabled=False)
        assert result.output_text == "ab"
        assert intercepted == []
        native.status = 302; native.headers["location"] = "http://127.0.0.1:1/foreign"
        with pytest.raises(LocalInferenceError) as caught:
            await value.generate(prompt, request(), cache_enabled=False)
        assert caught.value.code == "backend_protocol_error"
        assert sum(path == "/completion" for path, _, _ in native.calls) == 2
        assert intercepted == []
    finally:
        await value.close()
        server.close(); await server.wait_closed()
        unix.close(); await unix.wait_closed()
        if handlers:
            await asyncio.gather(*handlers)
        directory.cleanup()


@pytest.mark.asyncio
async def test_missing_private_socket_never_falls_back_to_tcp(tmp_path):
    intercepted = []
    async def foreign(reader, writer):
        intercepted.append(await reader.read(4096))
        writer.close()
        await writer.wait_closed()
    server = await asyncio.start_server(foreign, "127.0.0.1", 0)
    config = OwnerConfig(enabled=True, llama_server_binary="/fake/server", model_path="/fake/model",
                         port=server.sockets[0].getsockname()[1])
    process = FakeProcess(config, tmp_path)
    process.alive = True
    with tempfile.TemporaryDirectory(prefix="agents-native-test-", dir="/tmp") as directory:
        process.socket_path = Path(directory) / "missing.sock"
        value = LlamaBackend(config, tmp_path, supervisor=process)
        try:
            await value._new_client()
            with pytest.raises(LocalInferenceError) as caught:
                await value._json("/props")
            assert caught.value.code == "backend_unavailable"
            assert intercepted == []
        finally:
            await value.close()
            server.close(); await server.wait_closed()


@pytest.mark.parametrize("payload", [b'{"prompt":"x","prompt":"y"}', b'{"prompt":NaN}',
                                     b'[]', b'{"prompt":' + b'"x' * 1000])
@pytest.mark.asyncio
async def test_helper_malformed_and_oversized_body(tmp_path, payload):
    value, native = await backend(tmp_path)
    original = native.response
    def changed(req):
        if req.url.path == "/apply-template":
            return httpx.Response(200, headers={"content-type": "application/json"}, stream=Chunks([payload]))
        return original(req)
    await value._close_client()
    value._transport = httpx.MockTransport(changed)
    await value._new_client()
    value._max_body = 512
    try:
        with pytest.raises(LocalInferenceError) as caught:
            await value.prepare([{"role": "user", "content": "x"}])
        assert caught.value.code == "backend_protocol_error"
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_helper_trickling_has_whole_operation_deadline(tmp_path):
    value, native = await backend(tmp_path, helper_timeout=0.03)
    original = native.response
    def changed(req):
        if req.url.path == "/apply-template":
            return httpx.Response(200, headers={"content-type": "application/json"},
                                  stream=Chunks([b" "] * 100, delay=0.01))
        return original(req)
    await value._close_client(); value._transport = httpx.MockTransport(changed); await value._new_client()
    try:
        with pytest.raises(LocalInferenceError) as caught:
            await value.prepare([{"role": "user", "content": "x"}])
        assert caught.value.code == "deadline_exceeded"
    finally:
        await value.close()


@pytest.mark.parametrize("tokens", [[True], [-1], [2**32], [], [1, "2"]])
@pytest.mark.asyncio
async def test_tokenizer_type_and_id_boundaries(tmp_path, tokens):
    value, native = await backend(tmp_path)
    native.tokenizer_ids = tokens
    try:
        with pytest.raises(LocalInferenceError) as caught:
            await value.prepare([{"role": "user", "content": "x"}])
        assert caught.value.code == "backend_protocol_error"
    finally:
        await value.close()


@pytest.mark.asyncio
async def test_first_token_is_observed_before_delayed_final(tmp_path):
    value, _ = await backend(tmp_path, NativeHTTP(delay=0.02))
    try:
        started = time.monotonic()
        result = await value.generate(PreparedPrompt("rendered", (1, 2, 3)), request(), cache_enabled=False)
        total_ms = (time.monotonic() - started) * 1000
        assert result.first_token_ms + 20 < total_ms
    finally:
        await value.close()


def test_mismatched_probe_lengths_and_unsafe_record_are_unknown(tmp_path):
    records = CapabilityRecords(tmp_path, clock=lambda: 10)
    with pytest.raises(ValueError):
        records.write(probe(rendered_token_hashes=["a" * 64]))
    path = records.write(probe())
    path.chmod(0o644)
    assert records.load("c" * 64, native_write_limit=True) == ("unknown", "unknown")
    path.chmod(0o600)
    records.directory.chmod(0o755)
    assert records.load("c" * 64, native_write_limit=True) == ("unknown", "unknown")


@pytest.mark.asyncio
async def test_stopped_or_replaced_child_during_response_is_refused(tmp_path):
    value, native = await backend(tmp_path)
    class ReplacedStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield frame(partial())
            value.process.epoch = "different-child-epoch"
            yield frame(partial(11, "b", 2))
            yield frame(final_event())
    original = native.response
    def changed(req):
        if req.url.path == "/completion":
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=ReplacedStream())
        return original(req)
    await value._close_client(); value._transport = httpx.MockTransport(changed); await value._new_client()
    try:
        with pytest.raises(LocalInferenceError) as caught:
            await value.generate(PreparedPrompt("rendered", (1, 2, 3)), request(), cache_enabled=False)
        assert caught.value.code == "backend_protocol_error" and caught.value.generation_dispatched
        assert value._saved_state is None
    finally:
        await value.close()
