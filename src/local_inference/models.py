"""Strict public requests and bounded internal records for local inference."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator
from src.schemas.artifacts import ArtifactManifest

SCHEMA_VERSION = "agents-core/local-inference/1"


def _bounded_paths(value: Any) -> Any:
    if not isinstance(value, list) or len(value) > 32:
        raise ValueError("source_paths must be an array of at most 32 paths")
    return value


class StrictRequest(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, allow_inf_nan=False)

    @field_validator("*", mode="after")
    @classmethod
    def valid_unicode(cls, value: Any) -> Any:
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError("text must contain valid UTF-8 characters") from None
        return value


Identifier = Annotated[str, Field(min_length=1, max_length=128)]
Token = Annotated[str, Field(min_length=1, max_length=256)]
SourcePath = Annotated[str, Field(min_length=1, max_length=4096)]


class ContextCreateRequest(StrictRequest):
    common_system: str = Field(max_length=262144)
    shared_context: str = Field(max_length=262144)
    source_paths: Annotated[list[SourcePath], BeforeValidator(_bounded_paths)] = Field(default_factory=list)
    source_root_id: Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")] | None = None
    ttl_seconds: int = Field(default=900, ge=30, le=3600)
    max_requests: int = Field(default=10, ge=1, le=100)
    max_total_output_tokens: int = Field(default=4096, ge=1, le=65536)
    artifact_manifest: ArtifactManifest | None = None

    @field_validator("source_paths")
    @classmethod
    def unique_utf8_paths(cls, value: list[str]) -> list[str]:
        if len(set(value)) != len(value):
            raise ValueError("source paths must be unique")
        for path in value:
            try:
                path.encode("utf-8")
            except UnicodeEncodeError:
                raise ValueError("source paths must be valid UTF-8") from None
        return value


class ContextGrantRequest(StrictRequest):
    context_id: Identifier
    owner_token: Token = Field(repr=False)
    label: str = Field(min_length=1, max_length=80)
    max_requests: int = Field(ge=1, le=100)
    max_total_output_tokens: int = Field(ge=1, le=65536)
    ttl_seconds: int | None = Field(default=None, ge=1, le=3600)


class ContextRevokeRequest(StrictRequest):
    context_id: Identifier
    owner_token: Token = Field(repr=False)
    grant_id: Identifier


class GenerateRequest(StrictRequest):
    context_id: Identifier
    access_token: Token = Field(repr=False)
    request_id: str = Field(min_length=36, max_length=36)
    task: str = Field(min_length=1, max_length=65536)
    max_output_tokens: int = Field(default=512, ge=1, le=2048)
    timeout_seconds: int = Field(default=120, ge=1, le=600)
    cache_mode: Literal["auto", "off"] = "auto"
    temperature: float = Field(default=0.0, ge=0, le=2)
    seed: int = Field(default=42, ge=0, le=2147483647)

    @field_validator("request_id")
    @classmethod
    def canonical_uuid(cls, value: str) -> str:
        try:
            if str(UUID(value)) != value:
                raise ValueError()
        except (ValueError, AttributeError):
            raise ValueError("request_id must be a canonical UUID") from None
        return value


class GenerationResultRequest(StrictRequest):
    context_id: Identifier
    capability_token: Token = Field(repr=False)
    job_id: Identifier
    wait_seconds: int = Field(default=0, ge=0, le=20)


class GenerationCancelRequest(StrictRequest):
    context_id: Identifier
    capability_token: Token = Field(repr=False)
    job_id: Identifier


class ContextReleaseRequest(StrictRequest):
    context_id: Identifier
    owner_token: Token = Field(repr=False)


class LocalInferenceError(ValueError):
    """Operational refusal with a public, secret-free error envelope."""

    def __init__(self, code: str, message: str, *, retryable: bool = False,
                 generation_dispatched: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.generation_dispatched = generation_dispatched

    def envelope(self) -> dict:
        return {"schema": SCHEMA_VERSION, "status": "error", "error": {
            "code": self.code, "message": self.message, "retryable": self.retryable,
            "generation_dispatched": self.generation_dispatched}}


def success(data: dict) -> dict:
    return {"schema": SCHEMA_VERSION, "status": "ok", "data": data}


@dataclass(frozen=True)
class SourceFile:
    path: str
    sha256: str
    content: str = field(repr=False)
    size: int
    device: int
    inode: int
    mtime_ns: int


@dataclass(frozen=True)
class CapturedContext:
    context_id: str
    workspace_id: str
    workspace_root: str
    source_root_id: str | None
    source_root: str | None
    source_root_identity: tuple[int, int] | None
    common_system: str = field(repr=False)
    shared_context: str = field(repr=False)
    sources: tuple[SourceFile, ...]
    owner_config_revision: str
    created_at: datetime
    expires_at: datetime
    expires_monotonic: float
    max_requests: int
    max_total_output_tokens: int
    artifact_manifest: dict | None = field(default=None, repr=False)


@dataclass
class GrantRecord:
    grant_id: str
    capability_digest: str = field(repr=False)
    label: str
    expires_monotonic: float
    remaining_requests: int
    remaining_output_tokens: int
    max_requests: int
    max_total_output_tokens: int
    owner_access: bool = False
    revoked: bool = False


@dataclass
class JobRecord:
    job_id: str
    request_id: str
    context_id: str
    grant_id: str
    argument_hash: str
    accepted_at: float
    deadline: float
    deadline_at: datetime
    request: GenerateRequest | None = field(repr=False)
    state: str = "queued"
    dispatched: bool = False
    reserved_output_tokens: int = 0
    reconciled: bool = False
    consumed_output_tokens: int | None = None
    terminal_result: dict | None = field(default=None, repr=False)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    done_event: asyncio.Event = field(default_factory=asyncio.Event, repr=False)


@dataclass
class ContextRecord:
    captured: CapturedContext
    owner_digest: str = field(repr=False)
    grants: dict[str, GrantRecord] = field(default_factory=dict)
    jobs: dict[str, JobRecord] = field(default_factory=dict)
    requests: dict[str, str] = field(default_factory=dict)
    remaining_requests: int = 0
    remaining_output_tokens: int = 0
    profile_hash: str | None = None
    stale: bool = False
    released: bool = False

    @property
    def context_id(self) -> str:
        return self.captured.context_id
