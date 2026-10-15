"""Immutable, bounded artifact references; these schemas never read stored data."""
from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Annotated, Literal
import unicodedata

from pydantic import (BaseModel, ConfigDict, Field, StringConstraints,
                      field_validator, model_serializer, model_validator)

ARTIFACT_MANIFEST_SCHEMA = "agents-core/artifact-manifest/1"
MAX_ARTIFACT_BYTES = 4194304
SHA256 = Annotated[str, StringConstraints(strict=True, pattern=r"^[a-f0-9]{64}$")]
ArtifactID = Annotated[str, StringConstraints(strict=True, pattern=r"^art_[a-f0-9]{64}$")]
TaskID = Annotated[str, StringConstraints(strict=True, min_length=1, max_length=128,
                                        pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")]
UTCTimestamp = Annotated[str, StringConstraints(
    strict=True, pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|\+00:00)$")]


class ImmutableArtifactModel(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class ArtifactDescriptor(ImmutableArtifactModel):
    artifact_id: ArtifactID
    sha256: SHA256
    size_bytes: int = Field(ge=0, le=MAX_ARTIFACT_BYTES)
    media_type: Literal["text/plain", "text/markdown", "application/json"]
    name: str = Field(min_length=1, max_length=120)

    @field_validator("name")
    @classmethod
    def safe_name(cls, value: str) -> str:
        if (not value.strip() or any(character in "/\\" or
                unicodedata.category(character) in {"Cc", "Cs", "Zl", "Zp"}
                for character in value)):
            raise ValueError("artifact name must be nonblank without controls or separators")
        return value

    @model_validator(mode="after")
    def content_address_matches(self) -> ArtifactDescriptor:
        if self.artifact_id != "art_" + self.sha256:
            raise ValueError("artifact ID must contain its content SHA256")
        return self


class ArtifactManifestEntry(ImmutableArtifactModel):
    artifact: ArtifactDescriptor
    required: bool
    purpose: str = Field(min_length=1, max_length=1000)

    @field_validator("purpose")
    @classmethod
    def nonblank_purpose(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("artifact purpose must not be blank")
        return value


class ArtifactManifest(ImmutableArtifactModel):
    manifest_schema: Literal["agents-core/artifact-manifest/1"] = Field(alias="schema")
    manifest_id: Annotated[str, StringConstraints(strict=True,
                                                 pattern=r"^manifest_[a-f0-9]{32}$")]
    workspace_sha256: SHA256
    task_id: TaskID
    created_at: UTCTimestamp
    expires_at: UTCTimestamp
    entries: Annotated[tuple[ArtifactManifestEntry, ...], Field(min_length=1, max_length=32)]
    sha256: SHA256

    @field_validator("entries", mode="before")
    @classmethod
    def immutable_entries(cls, value):
        # JSON arrays become immutable tuples; no other input coercion is allowed.
        return tuple(value) if type(value) is list else value

    @model_serializer(mode="wrap")
    def exact_manifest_shape(self, handler):
        data = handler(self)
        if "manifest_schema" in data:
            data["schema"] = data.pop("manifest_schema")
        if "entries" in data:
            data["entries"] = list(data["entries"])
        return data

    @model_validator(mode="after")
    def verify_manifest(self) -> ArtifactManifest:
        created = datetime.fromisoformat(self.created_at.replace("Z", "+00:00"))
        expires = datetime.fromisoformat(self.expires_at.replace("Z", "+00:00"))
        if expires <= created:
            raise ValueError("manifest expiry must follow creation")
        artifact_ids = [entry.artifact.artifact_id for entry in self.entries]
        if len(set(artifact_ids)) != len(artifact_ids):
            raise ValueError("manifest artifact IDs must be unique")
        value = self.model_dump(mode="json", by_alias=True, exclude={"sha256"})
        encoded = json.dumps(value, sort_keys=True, separators=(",", ":"),
                             ensure_ascii=False, allow_nan=False).encode("utf-8")
        if hashlib.sha256(encoded).hexdigest() != self.sha256:
            raise ValueError("manifest SHA256 does not match its canonical contents")
        return self
