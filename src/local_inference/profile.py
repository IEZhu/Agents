"""Canonical byte-level profile/content fingerprints; no file hashing on import."""
from __future__ import annotations

import hashlib
import json
import secrets
from collections.abc import Mapping
from types import MappingProxyType
from dataclasses import dataclass, field
from typing import Any


def _json_value(value: Any):
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings")
        return {key: _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    return value


def _freeze(value):
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    return value


def canonical_json(value: Any) -> bytes:
    """Finite UTF-8 JSON, expanded by the caller, with deterministic object order."""
    return json.dumps(_json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False).encode("utf-8")


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value)).hexdigest()


@dataclass(frozen=True)
class ModelProfile:
    model_sha256: str
    engine_sha256: str
    tokenizer_identity: str
    template_identity: str
    renderer_version: str
    context_tokens: int
    kv_type_k: str
    kv_type_v: str
    backend_identity: str
    model_settings: dict = field(default_factory=dict)
    rope_settings: dict = field(default_factory=dict)
    attention_settings: dict = field(default_factory=dict)
    adapters: tuple = ()

    def __post_init__(self):
        for name in ("model_settings", "rope_settings", "attention_settings", "adapters"):
            object.__setattr__(self, name, _freeze(getattr(self, name)))

    def canonical(self) -> dict:
        return {"schema": "agents-core/local-inference-profile/1", **self.__dict__}

    @property
    def stable_hash(self) -> str:
        return canonical_hash(self.canonical())


def new_engine_epoch() -> str:
    return secrets.token_hex(16)


def content_fingerprint(context, stable_profile_hash: str) -> str:
    captured = getattr(context, "captured", context)
    return canonical_hash({
        "schema": "agents-core/local-inference-content/1",
        "common_system": captured.common_system, "shared_context": captured.shared_context,
        "sources": [{"path": source.path, "sha256": source.sha256} for source in captured.sources],
        "source_root_id": captured.source_root_id, "source_root": captured.source_root,
        "source_root_identity": captured.source_root_identity,
        "workspace_id": captured.workspace_id, "workspace_root": captured.workspace_root,
        "owner_config_revision": captured.owner_config_revision, "profile": stable_profile_hash})


def snapshot_fingerprint(context, stable_profile_hash: str) -> str:
    captured = getattr(context, "captured", context)
    return canonical_hash({"schema": "agents-core/local-inference-snapshot/1",
                           "context_id": captured.context_id,
                           "content": content_fingerprint(captured, stable_profile_hash)})
