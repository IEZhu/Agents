"""Pinned native llama.cpp HTTP protocol; generation is dispatched exactly once."""
from __future__ import annotations

import asyncio
import importlib
import json
import math
from pathlib import Path
import platform
import re
import sys
import time

from .backend import BackendInfo, Completion, PreparedPrompt
from .capabilities import CapabilityRecords, MAPPING_VERSION
from .config import OwnerConfig
from .models import GenerateRequest, LocalInferenceError
from .process import OwnedProcess, PINNED_COMMIT, bounded_wait
from .profile import ModelProfile, canonical_hash, canonical_json
from .render import RENDERER_VERSION

MAX_BODY_BYTES = 4 * 1024 * 1024
MAX_FRAME_BYTES = 128 * 1024
MAX_STREAM_BYTES = 16 * 1024 * 1024
MAX_TEXT_BYTES_PER_TOKEN = 32


def _protocol(dispatched=False):
    return LocalInferenceError("backend_protocol_error", "The local engine returned an invalid response.",
                               generation_dispatched=dispatched)


def _object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON field")
        result[key] = value
    return result


def decode_object(value: bytes) -> dict:
    parsed = json.loads(value.decode("utf-8"), object_pairs_hook=_object,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError()))
    if type(parsed) is not dict:
        raise ValueError("expected object")
    return parsed


def token_ids(value, maximum: int, *, empty=False) -> tuple[int, ...]:
    if (type(value) is not list or len(value) > maximum or (not value and not empty)
            or any(type(item) is not int or not 0 <= item <= 0xffffffff for item in value)):
        raise ValueError("invalid token IDs")
    return tuple(value)


def _text(value, maximum: int) -> str:
    if type(value) is not str or len(value.encode("utf-8")) > maximum:
        raise ValueError("invalid text")
    return value


def _count(value) -> int:
    if type(value) is not int or value < 0:
        raise ValueError("invalid native counter")
    return value


class LlamaBackend:
    def __init__(self, config: OwnerConfig, directory: Path | str, *, supervisor=None,
                 transport=None, client_factory=None, clock=time.monotonic,
                 helper_timeout=30.0, max_body_bytes=MAX_BODY_BYTES,
                 max_frame_bytes=MAX_FRAME_BYTES, max_stream_bytes=MAX_STREAM_BYTES):
        self.config = config
        self.process = supervisor if supervisor is not None else OwnedProcess(config, directory)
        self.records = CapabilityRecords(directory)
        self._transport = transport
        self._client_factory = client_factory
        self._clock = clock
        self._helper_timeout = helper_timeout
        self._max_body = max_body_bytes
        self._max_frame = max_frame_bytes
        self._max_stream = max_stream_bytes
        self._client = None
        self._client_epoch = None
        self._startup = asyncio.Lock()
        self.info: BackendInfo | None = None
        self.profile: ModelProfile | None = None
        self.spool = None
        self.state = "not_started"
        self._saved_state = None
        self._closed = False

    def _alive(self):
        if not self.process.alive:
            self.state = "unavailable"
            raise LocalInferenceError("backend_unavailable", "The owned engine is not running.")

    async def _new_client(self):
        # Import only after an explicitly requested startup, never at module import.
        try:
            httpx = importlib.import_module("httpx")
        except ImportError:
            raise LocalInferenceError("unsupported", "Install the optional local-inference HTTP dependency.") from None
        if self._client is not None:
            await self._close_client()
        factory = self._client_factory or httpx.AsyncClient
        limits = httpx.Limits(max_connections=1, max_keepalive_connections=1)
        transport = self._transport
        if transport is None:
            # No TCP connection is attempted, including during startup retries.
            transport = httpx.AsyncHTTPTransport(uds=str(self.process.socket_path),
                                                 trust_env=False, limits=limits)
        self._client = factory(
            base_url=self.process.base_url, trust_env=False, follow_redirects=False,
            timeout=httpx.Timeout(5.0, connect=1.0, pool=1.0),
            limits=limits, transport=transport,
            headers={"Authorization": "Bearer " + self.process.api_key, "Accept-Encoding": "identity"})
        self._client_epoch = self.process.epoch

    async def _close_client(self):
        client, self._client = self._client, None
        if client is not None:
            await bounded_wait(client.aclose(), 0.1)

    def _body(self, value) -> bytes | None:
        if value is None:
            return None
        encoded = canonical_json(value)
        if len(encoded) > self._max_body:
            raise LocalInferenceError("input_too_large", "The native request exceeds its byte limit.")
        return encoded

    def _response_headers(self, response, expected: str):
        if response.status_code != 200:
            raise _protocol()
        encoding = response.headers.get("content-encoding", "identity").lower()
        if encoding not in ("", "identity"):
            raise _protocol()
        content_type = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != expected:
            raise _protocol()
        length = response.headers.get("content-length")
        limit = self._max_stream if expected == "text/event-stream" else self._max_body
        if length is not None and (not length.isascii() or not length.isdigit() or int(length) > limit):
            raise _protocol()

    async def _json(self, path: str, value=None, *, timeout=None):
        self._alive()
        try:
            body = self._body(value)
            async with asyncio.timeout(self._helper_timeout if timeout is None else timeout):
                async with self._client.stream("GET" if value is None else "POST", path, content=body,
                                               headers={"Content-Type": "application/json"}) as response:
                    self._response_headers(response, "application/json")
                    parts, total = [], 0
                    async for chunk in response.aiter_raw(chunk_size=65536):
                        self._alive()
                        total += len(chunk)
                        if total > self._max_body:
                            raise _protocol()
                        parts.append(chunk)
                    result = decode_object(b"".join(parts))
            self._alive()
            if "error" in result:
                raise _protocol()
            return result
        except LocalInferenceError:
            raise
        except asyncio.TimeoutError:
            raise LocalInferenceError("deadline_exceeded", "The local engine operation exceeded its deadline.") from None
        except (ValueError, UnicodeError, TypeError):
            raise _protocol() from None
        except Exception:
            raise LocalInferenceError("backend_unavailable", "The local engine connection failed.") from None

    async def ensure_ready(self) -> BackendInfo:
        async with self._startup:
            if self._closed:
                raise LocalInferenceError("backend_unavailable", "The backend is stopping.")
            self.state = "starting"
            try:
                await self.process.ensure_started()
                self._alive()
                if self.info is not None and self._client_epoch == self.process.epoch:
                    self.state = "ready"
                    return self.info
                self._saved_state = None
                await self._new_client()
                httpx = importlib.import_module("httpx")
                async with asyncio.timeout(self.config.max_job_seconds):
                    # Exec acknowledgement precedes the native listener. Only
                    # this read-only owned-startup health check retries refused
                    # connections/loading; no completion is ever replayed.
                    while True:
                        self._alive()
                        try:
                            async with self._client.stream("GET", "/health") as response:
                                if response.status_code == 200:
                                    break
                                if response.status_code != 503:
                                    raise _protocol()
                        except (httpx.ConnectError, httpx.ConnectTimeout):
                            self._alive()
                        await asyncio.sleep(0.05)
                    props = await self._json("/props")
                self.profile = self._profile(props)
                resident, snapshot = await asyncio.to_thread(
                    self.records.load, self.profile.stable_hash, native_write_limit=self.process.native_write_limit)
                self.info = BackendInfo(self.profile.stable_hash, self.process.epoch, resident, snapshot,
                                        self.process.native_write_limit, context_headroom=1)
                self.spool = self.process.spool
                self.state = self.process.state = "ready"
                return self.info
            except BaseException:
                self.state = "unavailable"
                raise

    def _profile(self, props: dict) -> ModelProfile:
        try:
            settings = props["default_generation_settings"]
            params = settings["params"]
            if (type(settings["n_ctx"]) is not int or settings["n_ctx"] != self.config.context_tokens
                    or type(props["total_slots"]) is not int or props["total_slots"] != 1
                    or props["build_info"] != "b11429-d81235049"
                    or params["speculative.types"] != "none" or params["lora"] != []
                    or props.get("modalities", {}) != {"vision": False, "video": False, "audio": False}):
                raise ValueError()
            template = _text(props["chat_template"], self._max_body)
            if not template:
                raise ValueError()
            binary, model = self.process.files
            return ModelProfile(
                model_sha256=model.sha256, engine_sha256=binary.sha256,
                tokenizer_identity=canonical_hash({"model": model.sha256, "add_special": True, "parse_special": True}),
                template_identity=canonical_hash({"template": template, "caps": props.get("chat_template_caps", {})}),
                renderer_version=RENDERER_VERSION, context_tokens=settings["n_ctx"],
                kv_type_k=self.config.kv_type_k, kv_type_v=self.config.kv_type_v,
                backend_identity=canonical_hash({"platform": platform.platform(), "machine": platform.machine(),
                                                  "gpu_layers": self.config.gpu_layers, "build": self.process.build}),
                model_settings={"engine_commit": PINNED_COMMIT, "threads": self.config.threads,
                                "parallel": 1, "cache_reuse": 0, "cache_ram": 0,
                                "context_shift": False, "reasoning": "off", "speculation": "none",
                                "context_headroom": 1,
                                "token_state_mapping": MAPPING_VERSION},
                attention_settings={"flash_attention": "on"})
        except (KeyError, ValueError, TypeError, AttributeError):
            raise _protocol() from None

    async def prepare(self, messages: list[dict[str, str]]) -> PreparedPrompt:
        self._alive()
        rendered = await self._json("/apply-template", {"messages": messages})
        try:
            text = _text(rendered["prompt"], self._max_body)
            if not text:
                raise ValueError()
            tokens = await self._json("/tokenize", {"content": text, "add_special": True,
                                                   "parse_special": True, "with_pieces": False})
            ids = token_ids(tokens["tokens"], self.config.context_tokens)
            return PreparedPrompt(text, ids)
        except (ValueError, UnicodeError, KeyError, TypeError):
            raise _protocol() from None

    async def _events(self, response):
        buffer = bytearray()
        lines, frame_size, total = [], 0, 0
        async for chunk in response.aiter_raw():
            self._alive()
            total += len(chunk)
            if total > self._max_stream:
                raise _protocol(True)
            buffer.extend(chunk)
            while (end := buffer.find(b"\n")) >= 0:
                line = bytes(buffer[:end]).removesuffix(b"\r")
                del buffer[:end + 1]
                frame_size += end + 1
                if frame_size > self._max_frame:
                    raise _protocol(True)
                if line:
                    lines.append(line)
                    continue
                data = [entry[5:].removeprefix(b" ") for entry in lines if entry.startswith(b"data:")]
                if data:
                    raw = b"\n".join(data)
                    yield None if raw == b"[DONE]" else decode_object(raw)
                elif any(not entry.startswith(b":") for entry in lines):
                    raise _protocol(True)
                lines, frame_size = [], 0
            if len(buffer) + frame_size > self._max_frame:
                raise _protocol(True)
        if buffer or lines:
            raise _protocol(True)

    async def generate(self, prompt: PreparedPrompt, request: GenerateRequest, *, cache_enabled: bool) -> Completion:
        self._alive()
        if self.info is None or self._client_epoch != self.process.epoch:
            raise LocalInferenceError("backend_unavailable", "The native profile is not ready.")
        if len(prompt.tokens) + request.max_output_tokens >= self.config.context_tokens:
            raise LocalInferenceError("context_overflow", "The complete prompt and output allowance exceed the model context.")
        body = self._body({"prompt": prompt.text, "id_slot": 0, "stream": True, "return_tokens": True,
                           "cache_prompt": cache_enabled, "n_cache_reuse": 0,
                           "n_predict": request.max_output_tokens, "temperature": request.temperature,
                           "seed": request.seed})
        self._saved_state = None
        generation_epoch = self.process.epoch
        started, first = self._clock(), None
        content, ids, final, text_bytes, done_marker, last_predicted = [], [], None, 0, False, 0
        try:
            async with asyncio.timeout(request.timeout_seconds):
                async with self._client.stream("POST", "/completion", content=body,
                                               headers={"Content-Type": "application/json"},
                                               timeout=(1.0, float(request.timeout_seconds), 5.0, 1.0)) as response:
                    self._response_headers(response, "text/event-stream")
                    async for event in self._events(response):
                        if self.process.epoch != generation_epoch:
                            raise _protocol(True)
                        if event is None:
                            if final is None or done_marker:
                                raise ValueError()
                            done_marker = True
                            continue
                        if final is not None or done_marker or "error" in event:
                            raise ValueError()
                        if type(event.get("id_slot")) is not int or type(event.get("stop")) is not bool:
                            raise ValueError()
                        # b11429 leaves the base result's slot sentinel on partial
                        # messages; only send_final_response assigns slot.id.
                        if event["id_slot"] != (0 if event["stop"] else -1):
                            raise ValueError()
                        text = _text(event["content"], request.max_output_tokens * MAX_TEXT_BYTES_PER_TOKEN + 4096)
                        emitted = token_ids(event["tokens"], request.max_output_tokens, empty=True)
                        text_bytes += len(text.encode("utf-8"))
                        if text_bytes > request.max_output_tokens * MAX_TEXT_BYTES_PER_TOKEN + 4096:
                            raise ValueError()
                        if len(ids) + len(emitted) > request.max_output_tokens:
                            raise ValueError()
                        if event["stop"]:
                            if text or emitted:
                                raise ValueError()
                            final = event
                        else:
                            predicted = _count(event["tokens_predicted"])
                            evaluated = _count(event["tokens_evaluated"])
                            if (predicted > request.max_output_tokens or predicted < last_predicted
                                    or predicted < len(ids) + len(emitted) or len(emitted) != 1
                                    or evaluated != len(prompt.tokens)):
                                raise ValueError()
                            last_predicted = predicted
                            if first is None and (emitted or text):
                                first = max(0.0, self._clock() - started) * 1000
                            content.append(text)
                            ids.extend(emitted)
                self._alive()
                if self.process.epoch != generation_epoch:
                    raise _protocol(True)
                if final is None:
                    raise ValueError()
                timing = final["timings"]
                if type(timing) is not dict:
                    raise ValueError()
                for metric in timing.values():
                    if type(metric) not in (int, float) or not math.isfinite(metric) or metric < 0:
                        raise ValueError()
                reused, evaluated, generated = (_count(timing[name]) for name in ("cache_n", "prompt_n", "predicted_n"))
                if (reused + evaluated != len(prompt.tokens) or (not cache_enabled and reused)
                        or _count(final["tokens_evaluated"]) != len(prompt.tokens)
                        or _count(final["tokens_predicted"]) != generated
                        or not 1 <= generated <= request.max_output_tokens or generated < last_predicted or len(ids) > generated
                        or final["truncated"] is not False or final["stop_type"] not in ("eos", "word", "limit")):
                    raise ValueError()
                # A processed prompt can add BOS/EOS specials. This native helper
                # verifies its exact sequence without sampling or retokenizing output.
                processed = _text(final["prompt"], self._max_body)
                agreement = await self._json("/tokenize", {"content": processed, "add_special": False,
                                                          "parse_special": True, "with_pieces": False})
                if token_ids(agreement["tokens"], self.config.context_tokens) != prompt.tokens:
                    raise ValueError()
                if self.process.epoch != generation_epoch:
                    raise _protocol(True)
                cached = _count(final["tokens_cached"])
                candidate = prompt.tokens + tuple(ids[:-1])
                saved = candidate if len(ids) == generated and cached == len(candidate) else None
                self._saved_state = saved
                return Completion("".join(content), "length" if final["stop_type"] == "limit" else "stop",
                                  len(prompt.tokens), evaluated, reused, generated, first, tuple(ids), saved)
        except LocalInferenceError as error:
            error.generation_dispatched = True
            raise
        except asyncio.TimeoutError:
            raise LocalInferenceError("deadline_exceeded", "The native generation exceeded its deadline.",
                                      generation_dispatched=True) from None
        except (ValueError, UnicodeError, KeyError, TypeError, OverflowError):
            raise _protocol(True) from None
        except Exception:
            raise LocalInferenceError("backend_unavailable", "The native generation connection failed.",
                                      generation_dispatched=True) from None

    async def erase(self) -> None:
        self._saved_state = None
        if not self.process.alive:
            return
        result = await self._json("/slots/0?action=erase", {})
        try:
            if type(result["id_slot"]) is not int or result["id_slot"] != 0:
                raise ValueError()
            _count(result["n_erased"])
        except (ValueError, TypeError, KeyError):
            raise _protocol() from None

    async def slot_action(self, action: str, filename: str) -> dict:
        """Only authenticated snapshot plumbing may call this gated boundary."""
        if (self.info is None or self.info.snapshot != "supported" or not self.info.native_write_limit
                or action not in ("save", "restore") or not re.fullmatch(r"snapshot-[a-f0-9]{32}\.bin", filename)):
            raise LocalInferenceError("unsupported", "Native snapshots have not been proven for this profile.")
        if action == "save" and self._saved_state is None:
            raise LocalInferenceError("unsupported", "The completed native token state is unknown.")
        return await self._json(f"/slots/0?action={action}", {"filename": filename})

    async def fence(self) -> bool:
        self._saved_state = None
        self.info = None
        self.spool = None
        self.state = "restarting"
        confirmed = await self.process.fence()
        try:
            await self._close_client()
        except Exception:
            confirmed = False
        self.state = "not_started" if confirmed else "unavailable"
        return confirmed

    async def close(self) -> None:
        self._closed = True
        confirmed = await self.fence()
        await self.process.close()
        self.state = "stopping" if confirmed else "unavailable"
