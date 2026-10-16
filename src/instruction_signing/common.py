"""Bounded, filesystem-independent signing inputs and portable resource names."""
from __future__ import annotations

import json
import math
import re
import unicodedata
from dataclasses import dataclass
from enum import Enum


class Status(str, Enum):
    VALID = "valid"
    UNSIGNED = "unsigned"
    TAMPERED = "tampered"
    UNTRUSTED = "untrusted"
    INVALID = "invalid"


@dataclass(frozen=True)
class Limits:
    seal_bytes: int = 8 * 1024 * 1024
    payload_bytes: int = 4 * 1024 * 1024
    file_bytes: int = 64 * 1024 * 1024
    total_bytes: int = 256 * 1024 * 1024
    resources: int = 20_000
    path_bytes: int = 1024

    def __post_init__(self):
        if any(type(value) is not int or value <= 0 for value in vars(self).values()):
            raise ValueError("Limits must be positive integers")


DEFAULT_LIMITS = Limits()
HEX_DIGEST = re.compile(r"[0-9a-f]{64}")
_RESERVED = {"con", "prn", "aux", "nul", "conin$", "conout$"}
_RESERVED.update(f"{prefix}{n}" for prefix in ("com", "lpt") for n in "123456789¹²³")


def text(value: object, *, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not value or len(value.encode("utf-8")) > maximum:
        raise ValueError("Expected a bounded nonempty UTF-8 string")
    if any(ord(c) < 32 or ord(c) == 127 for c in value):
        raise ValueError("Control characters are unsupported")
    return value


def validate_path(value: object, limits: Limits = DEFAULT_LIMITS) -> str:
    """Require one portable spelling on POSIX, Windows and case-insensitive disks."""
    name = text(value, maximum=limits.path_bytes)
    if unicodedata.normalize("NFC", name) != name:
        raise ValueError("Resource paths must use NFC Unicode")
    if any(c in name for c in '\\:<>"|?*'):
        raise ValueError("Nonportable resource path")
    for part in name.split("/"):
        if not part or part in {".", ".."} or part.endswith((" ", ".")):
            raise ValueError("Ambiguous or escaping resource path")
        if part.split(".")[0].casefold() in _RESERVED:
            raise ValueError("Reserved Windows resource path")
    return name


def validate_paths(names, limits: Limits = DEFAULT_LIMITS) -> tuple[str, ...]:
    if len(names) > limits.resources:
        raise ValueError("Resource count exceeds limit")
    result = tuple(validate_path(name, limits) for name in names)
    aliases = {name.casefold() for name in result}
    if len(aliases) != len(result):
        raise ValueError("Duplicate or platform-aliasing paths")
    component_spellings = {}
    for name in result:
        parts = name.split("/")
        for count in range(1, len(parts) + 1):
            prefix = "/".join(parts[:count])
            alias = prefix.casefold()
            if alias in component_spellings and component_spellings[alias] != prefix:
                raise ValueError("Platform-aliasing directory components")
            component_spellings[alias] = prefix
    for name in aliases:
        parts = name.split("/")
        if any("/".join(parts[:i]) in aliases for i in range(1, len(parts))):
            raise ValueError("A file path cannot also be a directory")
    return result


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def _nonfinite(value):
    raise ValueError("Nonfinite JSON number")


def _finite_float(value):
    result = float(value)
    if not math.isfinite(result):
        raise ValueError("Nonfinite JSON number")
    return result


def load_json(data: bytes, limit: int):
    if type(data) is not bytes or len(data) > limit:
        raise ValueError("JSON input exceeds byte bound or is not immutable bytes")
    return json.loads(data.decode("utf-8"), object_pairs_hook=_unique_object,
                      parse_constant=_nonfinite, parse_float=_finite_float)


@dataclass(frozen=True)
class Resource:
    name: str
    digest: str


@dataclass(frozen=True)
class SealResult:
    status: Status
    reason: str
    fingerprint: str | None = None
    seal_kind: str | None = None
    resources: tuple[Resource, ...] = ()
    signature_verified: bool = False
    profile: str | None = None
    data: bytes | None = None
    # Key/root authorization belongs to #236, never a display label or signature.
    authorization_checked: bool = False
