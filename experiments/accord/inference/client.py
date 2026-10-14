"""Loopback-only native llama.cpp client and prepared-input integrity checks."""

import hashlib
import json
import time
import urllib.parse
import urllib.request


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def sha(value):
    return hashlib.sha256(value if isinstance(value, bytes) else value.encode()).hexdigest()


def fingerprint(value):
    return sha(encode(value))


def make_artifact(profile, text, ids):
    return {"schema": "accord.prepared-input/experiment-1", "profile": profile,
            "text_sha256": sha(text), "ids_sha256": fingerprint(ids), "ids": ids}


def verify_artifact(artifact, profile, text):
    if set(artifact) != {"schema", "profile", "text_sha256", "ids_sha256", "ids"}:
        raise ValueError("invalid input artifact fields")
    if artifact["schema"] != "accord.prepared-input/experiment-1" or artifact["profile"] != profile:
        raise ValueError("incompatible input profile")
    ids = artifact["ids"]
    if type(ids) is not list or not ids or len(ids) > 8192 or any(type(i) is not int or i < 0 for i in ids):
        raise ValueError("invalid token IDs")
    if artifact["text_sha256"] != sha(text) or artifact["ids_sha256"] != fingerprint(ids):
        raise ValueError("input artifact integrity mismatch")
    return ids


def verify_cache(cache, profile, prefix):
    if cache["profile"] != profile or cache["prefix_sha256"] != fingerprint(prefix):
        raise ValueError("incompatible cache profile or prefix")


class StreamFailure(RuntimeError):
    def __init__(self, message, partial):
        super().__init__(message)
        self.partial = partial


class Client:
    def __init__(self, base="http://127.0.0.1:18187", timeout=120):
        parsed = urllib.parse.urlparse(base)
        if parsed.scheme != "http" or parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("this experiment only permits loopback inference")
        self.base, self.timeout = base.rstrip("/"), timeout

    def json(self, path, value=None):
        start = time.perf_counter()
        data = None if value is None else encode(value)
        request = urllib.request.Request(self.base + path, data=data, headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            result = json.load(response)
        return result, (time.perf_counter() - start) * 1000

    def completion(self, prompt, settings, *, cache):
        start = time.perf_counter()
        data = encode({**settings, "prompt": prompt, "cache_prompt": cache, "stream": True, "return_tokens": True})
        request = urllib.request.Request(self.base + "/completion", data=data,
                                         headers={"Content-Type": "application/json"})
        content, tokens, first, final = [], [], None, None
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                for line in response:
                    if not line.startswith(b"data:"):
                        continue
                    raw = line[5:].strip()
                    if raw == b"[DONE]":
                        continue
                    event = json.loads(raw)
                    if "error" in event:
                        raise StreamFailure(str(event["error"]), {"content": "".join(content), "tokens": tokens,
                                                                   "error_event": event, "client_ttft_ms": first})
                    if event.get("tokens") or event.get("content"):
                        if first is None:
                            first = (time.perf_counter() - start) * 1000
                    content.append(event.get("content", ""))
                    tokens.extend(event.get("tokens", []))
                    if event.get("stop") is True:
                        final = event
        except StreamFailure:
            raise
        except Exception as error:
            raise StreamFailure(str(error), {"content": "".join(content), "tokens": tokens,
                                             "client_ttft_ms": first}) from error
        elapsed = (time.perf_counter() - start) * 1000
        if final is None or first is None:
            raise StreamFailure("incomplete generation stream", {"content": "".join(content), "tokens": tokens,
                                                               "final": final, "client_ttft_ms": first})
        return {"content": "".join(content), "tokens": tokens, "client_ttft_ms": first,
                "client_total_ms": elapsed, "request_body_bytes": len(data), "final": final}
