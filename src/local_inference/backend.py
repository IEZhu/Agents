"""Narrow asynchronous engine boundary shared by production and fake backends."""
from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from .models import GenerateRequest


@dataclass(frozen=True)
class BackendInfo:
    profile_hash: str
    epoch: str
    resident: str = "unknown"
    snapshot: str = "unknown"
    native_write_limit: bool = False
    context_headroom: int = 0


@dataclass(frozen=True)
class PreparedPrompt:
    text: str = field(repr=False)
    tokens: tuple[int, ...] = field(repr=False)


@dataclass(frozen=True)
class Completion:
    output_text: str = field(repr=False)
    finish_reason: str
    input_tokens: int
    evaluated_input_tokens: int
    reused_input_tokens: int
    output_tokens: int
    first_token_ms: float | None
    output_ids: tuple[int, ...] = field(default=(), repr=False)
    saved_token_state: tuple[int, ...] | None = field(default=None, repr=False)


class Backend(Protocol):
    state: str
    info: BackendInfo | None
    spool: Path | None

    async def ensure_ready(self) -> BackendInfo: ...
    async def prepare(self, messages: list[dict[str, str]]) -> PreparedPrompt: ...
    async def generate(self, prompt: PreparedPrompt, request: GenerateRequest,
                       *, cache_enabled: bool) -> Completion: ...
    async def erase(self) -> None: ...
    async def fence(self) -> bool: ...
    async def close(self) -> None: ...


def longest_common_prefix(left: tuple[int, ...], right: tuple[int, ...]) -> int:
    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return index
    return min(len(left), len(right))
