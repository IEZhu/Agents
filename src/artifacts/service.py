"""Bounded asynchronous artifact operations for an explicitly resolved workspace."""
from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from src.daemon.workspaces import WorkspaceError
from src.schemas.artifacts import ArtifactManifest
from .store import ArtifactError, ArtifactStore

SCHEMA = "agents-core/artifacts/1"
SHA = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
ARTIFACT_ID = Annotated[str, Field(pattern=r"^art_[a-f0-9]{64}$")]
MANIFEST_ID = Annotated[str, Field(pattern=r"^manifest_[a-f0-9]{32}$")]


class Request(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, allow_inf_nan=False)

    @field_validator("*", mode="after")
    @classmethod
    def utf8_text(cls, value):
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeError:
                raise ValueError("Artifact arguments require valid UTF-8.") from None
        return value


class Publish(Request):
    name: str = Field(min_length=1, max_length=120)
    media_type: Literal["text/plain", "text/markdown", "application/json"] = "text/plain"
    content: str | None = Field(default=None, max_length=4194304, repr=False)
    source_path: str | None = Field(default=None, min_length=1, max_length=4096)

    @model_validator(mode="after")
    def one_source(self):
        if (self.content is None) == (self.source_path is None):
            raise ValueError("Select exactly one artifact source.")
        return self


class Entry(Request):
    artifact_id: ARTIFACT_ID
    sha256: SHA
    required: bool = True
    purpose: str = Field(min_length=1, max_length=1000)


class ManifestCreate(Request):
    task_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9_.:-]*$")
    entries: list[Entry] = Field(min_length=1, max_length=32)
    ttl_seconds: int = Field(default=86400, ge=30, le=604800)


class ManifestReference(Request):
    manifest_id: MANIFEST_ID
    manifest_sha256: SHA


class Read(ManifestReference):
    artifact_id: ARTIFACT_ID
    offset: int = Field(default=0, ge=0, le=4194304)
    max_bytes: int = Field(default=16384, ge=4, le=65536)


REQUEST_MODELS = {
    "artifact_publish": Publish, "artifact_manifest_create": ManifestCreate,
    "artifact_manifest_get": ManifestReference, "artifact_manifest_verify": ManifestReference,
    "artifact_manifest_release": ManifestReference, "artifact_read": Read,
}


def validate_arguments(name, arguments):
    if type(arguments) is not dict:
        raise ValueError("Invalid artifact arguments.")
    try:
        return REQUEST_MODELS[name].model_validate(arguments)
    except (ValidationError, KeyError):
        # Validation diagnostics can contain private content; do not echo them.
        raise ValueError("Invalid artifact arguments.") from None


def success(data):
    return {"schema": SCHEMA, "status": "ok", "data": data}


def failure(error):
    return {"schema": SCHEMA, "status": "error", "error": {
        "code": error.code, "message": error.message,
        "retryable": error.code in ("busy", "store_busy")}}


class ArtifactService:
    def __init__(self, directory, *, store=None):
        self.directory = Path(directory)
        self._store = store
        self._slots = asyncio.Semaphore(4)
        self._pending = set()
        self._closed = False

    @property
    def store(self):
        if self._store is None:
            self._store = ArtifactStore(self.directory)
        return self._store

    @property
    def pending(self):
        return len(self._pending)

    async def run(self, function, *args, **kwargs):
        await self._slots.acquire()
        if self._closed:
            self._slots.release()
            raise ArtifactError("artifact_unavailable", "Artifact storage is stopping.")
        task = asyncio.create_task(asyncio.to_thread(function, *args, **kwargs))
        self._pending.add(task)

        def finished(done):
            self._pending.discard(done)
            self._slots.release()
            if not done.cancelled():
                done.exception()

        task.add_done_callback(finished)
        # A disconnected MCP waiter cannot release admission while its file work
        # still executes. CAS publication remains idempotent if it reconnects.
        return await asyncio.shield(task)

    @staticmethod
    def workspace(client):
        try:
            return str(client.workspace_root())
        except WorkspaceError:
            code = (client.error or "workspace_required").split(":", 1)[0]
            if code not in ("workspace_required", "workspace_invalid", "workspace_unsafe"):
                code = "workspace_invalid"
            raise ArtifactError(code, "A valid workspace is required.") from None

    async def call(self, name, client, arguments):
        request = validate_arguments(name, arguments)
        try:
            workspace = self.workspace(client)
            if name == "artifact_publish":
                options = {"name": request.name, "media_type": request.media_type}
                if request.source_path is not None:
                    result = await self.run(self.store.capture, workspace, Path(workspace), request.source_path, **options)
                else:
                    try:
                        content = request.content.encode("utf-8")
                    except UnicodeError:
                        raise ArtifactError("invalid_artifact", "Artifact content must be valid UTF-8.") from None
                    result = await self.run(self.store.publish, workspace, content, **options)
            elif name == "artifact_manifest_create":
                result = await self.run(self.store.create_manifest, workspace, request.task_id,
                                        [entry.model_dump() for entry in request.entries], ttl_seconds=request.ttl_seconds)
                ArtifactManifest.model_validate(result)
            elif name == "artifact_read":
                result = await self.run(self.store.read, workspace, request.manifest_id, request.manifest_sha256,
                                        request.artifact_id, offset=request.offset, max_bytes=request.max_bytes)
            else:
                method = {"artifact_manifest_get": self.store.get_manifest,
                          "artifact_manifest_verify": self.store.verify,
                          "artifact_manifest_release": self.store.release_manifest}[name]
                result = await self.run(method, workspace, request.manifest_id, request.manifest_sha256)
            return success(result)
        except ArtifactError as error:
            return failure(error)
        except (OSError, ValidationError):
            return failure(ArtifactError("artifact_unavailable", "Artifact storage is unavailable."))

    async def verify_snapshot(self, client, manifest):
        """Validate an authoritative snapshot before preparation or local hydration."""
        return await self.verify_binding(self.workspace(client), manifest)

    async def verify_binding(self, workspace, manifest):
        expected = ArtifactManifest.model_validate(manifest).model_dump(mode="json", by_alias=True)
        current = await self.run(self.store.get_manifest, workspace, expected["manifest_id"], expected["sha256"])
        if current != expected:
            raise ArtifactError("artifact_unavailable", "The artifact manifest is unavailable.")
        await self.run(self.store.verify, workspace, expected["manifest_id"], expected["sha256"])
        return current

    async def hydrate(self, client, manifest, max_bytes):
        """Load exact immutable data before a bounded local completion; never summarize."""
        current = await self.verify_snapshot(client, manifest)
        chunks = []
        used = 0
        for entry in current["entries"]:
            artifact = entry["artifact"]
            # Account framing in the same byte allowance as captured content.
            header = f"\nArtifact {artifact['artifact_id']} SHA256 {artifact['sha256']} ({artifact['media_type']}):\n"
            used += len(header.encode("utf-8")) + artifact["size_bytes"]
            if used > max_bytes:
                raise ArtifactError("input_too_large", "Selected artifacts exceed the context byte budget.")
            parts, offset = [], 0
            while True:
                piece = await self.run(self.store.read, self.workspace(client), current["manifest_id"], current["sha256"],
                                       artifact["artifact_id"], offset=offset, max_bytes=65536)
                parts.append(piece["text"])
                if piece["eof"]:
                    break
                if piece["next_offset"] <= offset:
                    raise ArtifactError("artifact_unavailable", "Artifact read did not make progress.")
                offset = piece["next_offset"]
            chunks.append(header + "".join(parts))
        await self.verify_snapshot(client, current)
        return "".join(chunks)

    async def close(self):
        self._closed = True
        if self._pending:
            await asyncio.wait(tuple(self._pending), timeout=5)
