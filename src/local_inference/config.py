"""Owner-controlled configuration. Validation is pure; loading is explicit/lazy."""
from __future__ import annotations

import json
import os
import re
import stat
from pathlib import Path
from types import MappingProxyType
from typing import Literal, Mapping

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_serializer, field_validator, model_validator

from .models import LocalInferenceError
from .profile import canonical_hash

CONFIG_ENV = "AGENTS_LOCAL_INFERENCE_CONFIG"
CONFIG_SCHEMA = "agents-core/local-inference-config/1"
MAX_CONFIG_BYTES = 1024 * 1024


class StrictConfig(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, allow_inf_nan=False,
                              populate_by_name=True)

    @field_validator("*", mode="after")
    @classmethod
    def valid_unicode(cls, value):
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError("configuration text must contain valid UTF-8 characters") from None
        return value


class SourceRootConfig(StrictConfig):
    path: str = Field(min_length=1, max_length=4096)
    workspace_root: str = Field(min_length=1, max_length=4096)

    @field_validator("path", "workspace_root")
    @classmethod
    def absolute_path(cls, value: str) -> str:
        if not Path(value).is_absolute() or "\x00" in value:
            raise ValueError("paths must be absolute")
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            raise ValueError("paths must contain valid UTF-8 characters") from None
        return value


class RedisConfig(StrictConfig):
    enabled: bool = False
    url_env: str = Field(default="AGENTS_LOCAL_KV_REDIS_URL", pattern=r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
    chunk_bytes: int = Field(default=4194304, ge=1, le=67108864)
    max_snapshot_bytes: int = Field(default=1073741824, ge=1)
    max_namespace_bytes: int = Field(default=2147483648, ge=1)
    max_spool_bytes: int = Field(default=2147483648, ge=1)
    max_pipeline_chunks: int = Field(default=8, ge=1, le=8)
    connect_timeout_seconds: float = Field(default=1.0, gt=0, le=600)
    io_timeout_seconds: float = Field(default=5.0, gt=0, le=600)
    operation_timeout_seconds: float = Field(default=30.0, gt=0, le=3600)
    ttl_seconds: int = Field(default=900, ge=1, le=3600)

    @model_validator(mode="after")
    def compatible_limits(self):
        if (self.max_snapshot_bytes + self.chunk_bytes - 1) // self.chunk_bytes > 256:
            raise ValueError("snapshot limit exceeds 256 chunks")
        if self.chunk_bytes > self.max_snapshot_bytes:
            raise ValueError("chunk_bytes exceeds max_snapshot_bytes")
        if self.max_snapshot_bytes > min(self.max_namespace_bytes, self.max_spool_bytes):
            raise ValueError("snapshot limit exceeds namespace or spool capacity")
        if max(self.connect_timeout_seconds, self.io_timeout_seconds) > self.operation_timeout_seconds:
            raise ValueError("component timeout exceeds operation timeout")
        return self


class OwnerConfig(StrictConfig):
    schema_version: Literal["agents-core/local-inference-config/1"] = Field(default=CONFIG_SCHEMA, alias="schema")
    enabled: bool = False
    llama_server_binary: str | None = Field(default=None, min_length=1, max_length=4096)
    model_path: str | None = Field(default=None, min_length=1, max_length=4096)
    model_label: str = Field(default="owner-selected-local-model", min_length=1, max_length=128)
    port: int = Field(default=18190, ge=1024, le=65535)
    context_tokens: int = Field(default=16384, ge=1)
    gpu_layers: int = Field(default=99, ge=0)
    threads: int = Field(default=8, ge=1)
    kv_type_k: Literal["f32", "f16", "bf16", "q8_0", "q4_0", "q4_1", "q5_0", "q5_1", "iq4_nl"] = "f16"
    kv_type_v: Literal["f32", "f16", "bf16", "q8_0", "q4_0", "q4_1", "q5_0", "q5_1", "iq4_nl"] = "f16"
    max_contexts: int = Field(default=8, ge=1)
    max_grants_per_context: int = Field(default=16, ge=1)
    max_queue: int = Field(default=16, ge=0)
    max_context_bytes: int = Field(default=262144, ge=1)
    max_source_files: int = Field(default=32, ge=0, le=32)
    max_source_file_bytes: int = Field(default=131072, ge=1)
    max_task_bytes: int = Field(default=65536, ge=1, le=65536)
    max_output_tokens: int = Field(default=2048, ge=1, le=2048)
    max_context_requests: int = Field(default=100, ge=1, le=100)
    max_context_output_tokens: int = Field(default=65536, ge=1, le=65536)
    max_context_ttl_seconds: int = Field(default=3600, ge=30, le=3600)
    max_job_seconds: int = Field(default=600, ge=1, le=600)
    shutdown_grace_seconds: float = Field(default=5.0, gt=0, le=600)
    source_roots: dict[str, SourceRootConfig] = Field(default_factory=dict, max_length=128)
    redis: RedisConfig = Field(default_factory=RedisConfig)
    max_context_jobs: int = Field(default=200, ge=1)
    max_release_tombstones: int = Field(default=1024, ge=1)

    @field_validator("llama_server_binary", "model_path")
    @classmethod
    def absolute_path(cls, value: str | None) -> str | None:
        if value is not None:
            SourceRootConfig.absolute_path(value)
        return value

    @field_validator("source_roots")
    @classmethod
    def root_ids(cls, value: dict[str, SourceRootConfig]) -> dict[str, SourceRootConfig]:
        if any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", key) for key in value):
            raise ValueError("invalid source root ID")
        return value

    @field_serializer("source_roots")
    def serialize_source_roots(self, value):
        return {key: root.model_dump(mode="json") for key, root in value.items()}

    @model_validator(mode="after")
    def compatible_limits(self):
        object.__setattr__(self, "source_roots", MappingProxyType(dict(self.source_roots)))
        if self.enabled and (not self.llama_server_binary or not self.model_path):
            raise ValueError("enabled inference requires binary and model paths")
        if self.max_source_file_bytes > self.max_context_bytes:
            raise ValueError("source-file limit exceeds context byte limit")
        if self.max_output_tokens > self.context_tokens:
            raise ValueError("output limit exceeds context token capacity")
        return self

    @property
    def revision(self) -> str:
        return canonical_hash(self.model_dump(mode="json", by_alias=True))


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate configuration key")
        result[key] = value
    return result


def load_owner_config(path: str | None = None, *, environ: Mapping[str, str] | None = None) -> OwnerConfig:
    """Unset environment means disabled. Never resolves Redis credentials."""
    selected = path if path is not None else (os.environ if environ is None else environ).get(CONFIG_ENV)
    if selected is None:
        return OwnerConfig()
    try:
        if not isinstance(selected, str) or not Path(selected).is_absolute():
            raise ValueError("configuration path must be absolute")
        fd = os.open(selected, os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_BINARY", 0))
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CONFIG_BYTES:
                raise ValueError("configuration must be a bounded regular file")
            data = os.read(fd, MAX_CONFIG_BYTES + 1)
        finally:
            os.close(fd)
        if len(data) > MAX_CONFIG_BYTES:
            raise ValueError("configuration file too large")
        parsed = json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                            parse_constant=lambda _: (_ for _ in ()).throw(ValueError("nonfinite number")))
        return OwnerConfig.model_validate(parsed)
    except (OSError, UnicodeError, ValueError, ValidationError):
        raise LocalInferenceError("configuration_invalid", "The owner inference configuration is invalid.") from None
