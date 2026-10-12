"""Outgoing-text scan of self-development (autodev): a tripwire for secrets (#341).

The autodev broker (#342) scans every text a run sends out (diffs, PR text, comments and worker
output) before it writes it (docs/autodev-plan.md, section 5.6; reference:
docs/autodev-guards.md). Any hit refuses the operation and blocks the run. The scan is a
tripwire, not a protection: sessions get no secrets and no network, and the scan only notices a
leak in a shape it knows. It looks for:

* token-shaped secrets (``TOKEN_PATTERNS``): GitHub tokens, Anthropic, OpenRouter and OpenAI
  project keys, AWS access key IDs, private key blocks and JWTs; a bare prefix such as ``ghp_``
  in prose is not a hit;
* high-entropy strings: base64-like runs of at least ``ENTROPY_MIN_LENGTH`` characters with
  digits and both letter cases and at least ``ENTROPY_THRESHOLD`` bits of Shannon entropy per
  character. Pure hex (commit SHAs, digests) never counts. The values keep every tracked file
  under ``docs/``, ``src/``, ``flows/`` and ``agents/`` clean (tests/test_schedules_scan.py);
* the caller's known secrets and decoy contents (``KnownSecret``: #342 its token and key, #343
  the decoys): raw, as base64 (standard or URL-safe, padded or not, at any byte offset inside a
  larger blob) and as hex in either case, also when whitespace or unified-diff line markers
  split the encoded form;
* all of the above inside base64, base64url and hex runs, decoded up to ``DECODE_DEPTH`` levels.

A ``Hit`` carries its kind, the pattern name or the caller's label, and character offsets in the
scanned text, never the matched text; a hit inside an encoded run points at the encoded
characters. Hits and ``SecretFound`` can therefore be logged and shown.
"""
from __future__ import annotations

import base64
import binascii
from bisect import bisect_right
from collections import Counter
from collections.abc import Callable, Iterable, Iterator
from dataclasses import dataclass, field
from enum import StrEnum
import math
import re

KNOWN_MIN_LENGTH = 8
ENTROPY_MIN_LENGTH = 24
ENTROPY_THRESHOLD = 4.5
ENTROPY_NAME = "high-entropy-string"
DECODE_DEPTH = 2
_MIN_ENCODED_RUN = 16  # 12 bytes as base64, 8 as hex: shorter runs hide no token
_HITS_IN_MESSAGE = 10

TOKEN_PATTERNS: dict[str, re.Pattern[str]] = {
    "github-token": re.compile(r"gh[opsur]_[A-Za-z0-9]{36,}"),
    "github-fine-grained-token": re.compile(r"github_pat_[A-Za-z0-9_]{22,}"),
    "anthropic-key": re.compile(r"sk-ant-[A-Za-z0-9_-]{20,}"),
    "openrouter-key": re.compile(r"sk-or-[A-Za-z0-9_-]{20,}"),
    "openai-project-key": re.compile(r"sk-proj-[A-Za-z0-9_-]{20,}"),
    "aws-access-key-id": re.compile(r"(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])"),
    "private-key": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    # Only at the start of a run: retrying inside a long dotless run would take quadratic time.
    "jwt": re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
}

_ENTROPY_CANDIDATE = re.compile(rf"[A-Za-z0-9+/_-]{{{ENTROPY_MIN_LENGTH},}}")
_HEX_ONLY = re.compile(r"[0-9A-Fa-f]+")
_BASE64_RUN = re.compile(rf"[A-Za-z0-9+/_-]{{{_MIN_ENCODED_RUN},}}")
_HEX_RUN = re.compile(rf"[0-9A-Fa-f]{{{_MIN_ENCODED_RUN},}}")
_WHITESPACE = re.compile(r"\s+")
_DIFF_MARKERS = re.compile(r"(?m)^[+ -]|\s+")
_DIFF_HUNK = re.compile(r"(?m)^@@ -[0-9]+(?:,[0-9]+)? \+[0-9]+(?:,[0-9]+)? @@")
_TO_STANDARD = str.maketrans("-_", "+/")
_TO_URLSAFE = str.maketrans("+/", "-_")


class HitKind(StrEnum):
    PATTERN = "pattern"
    ENTROPY = "entropy"
    KNOWN = "known"


@dataclass(frozen=True, order=True)
class Hit:
    """Where something secret-like is: ``text[start:end]``, or the encoded run holding it."""

    start: int
    end: int
    kind: HitKind
    name: str


class SecretFound(Exception):
    """The scanned text is not clean; the message lists the hits without their text."""

    def __init__(self, hits: Iterable[Hit]):
        self.hits = tuple(hits)
        listed = ", ".join(f"{hit.kind} {hit.name!r} at {hit.start}-{hit.end}"
                           for hit in self.hits[:_HITS_IN_MESSAGE])
        more = len(self.hits) - _HITS_IN_MESSAGE
        super().__init__(f"secret-like text found: {listed}" + (f" and {more} more" if more > 0 else ""))


@dataclass(frozen=True)
class KnownSecret:
    """A secret or decoy content the caller knows; ``label`` names it in hits, ``value`` never shows."""

    label: str
    value: str | bytes = field(repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.label, str) or not self.label.strip():
            raise ValueError("a known secret needs a non-empty label")
        if not isinstance(self.value, (str, bytes)):
            raise TypeError(f"known secret {self.label!r}: the value must be str or bytes")
        if len(self.value) < KNOWN_MIN_LENGTH:
            raise ValueError(f"known secret {self.label!r} is shorter than {KNOWN_MIN_LENGTH} characters")
        if isinstance(self.value, str) and self.value in self.label:
            raise ValueError("the label of a known secret must not contain its value")


@dataclass(frozen=True)
class _Fingerprint:
    """What a known secret looks like in each form the scan searches."""

    label: str
    text: str | None = field(repr=False)  # in the caller's text; None for bytes that are not UTF-8
    latin: str = field(repr=False)  # in decoded layers, whose characters are bytes (Latin-1)
    base64: tuple[str, ...] = field(repr=False)
    hex: re.Pattern[str] = field(repr=False)


def _fingerprint(secret: KnownSecret) -> _Fingerprint:
    if isinstance(secret.value, str):
        text, data = secret.value, secret.value.encode("utf-8")
    else:
        data = secret.value
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
    cores = set()
    # Inside a larger blob the value starts at byte offset 0, 1 or 2 of a 3-byte group. Keep the
    # characters that encode only the value's bits: they appear whatever surrounds it.
    for shift in range(3):
        encoded = base64.b64encode(bytes(shift) + data).decode("ascii").rstrip("=")
        core = encoded[-(-8 * shift // 6):8 * (shift + len(data)) // 6]
        cores.update((core, core.translate(_TO_URLSAFE)))
    return _Fingerprint(secret.label, text, data.decode("latin-1"), tuple(sorted(cores)),
                        re.compile(re.escape(data.hex()), re.IGNORECASE))


@dataclass(frozen=True)
class _Layer:
    """Text to scan: the caller's (depth 0) or bytes decoded from an encoded run, as Latin-1."""

    text: str
    origin: Callable[[int, int], tuple[int, int]]  # a span of ``text`` -> a span of the caller's text
    depth: int


class _View:
    """A layer without the spans ``removable`` matches (whitespace, diff markers), so an encoded
    form split across lines is contiguous; offsets map back to the layer."""

    def __init__(self, layer: _Layer, removable: re.Pattern[str]):
        self.layer = layer
        pieces: list[str] = []
        self._positions: list[int] = []  # view offset at which each removed span was
        self._shifts: list[int] = []  # characters removed up to and including that span
        kept = removed = 0
        for match in removable.finditer(layer.text):
            pieces.append(layer.text[kept:match.start()])
            self._positions.append(match.start() - removed)
            removed += match.end() - match.start()
            self._shifts.append(removed)
            kept = match.end()
        pieces.append(layer.text[kept:])
        self.text = "".join(pieces)

    def _in_layer(self, offset: int) -> int:
        index = bisect_right(self._positions, offset)
        return offset + (self._shifts[index - 1] if index else 0)

    def origin(self, start: int, end: int) -> tuple[int, int]:
        return self.layer.origin(self._in_layer(start), self._in_layer(end - 1) + 1)


class Scanner:
    """Scans texts against the token patterns, entropy and fingerprints of ``known``."""

    def __init__(self, known: Iterable[KnownSecret] = ()):
        known = tuple(known)
        if not all(isinstance(secret, KnownSecret) for secret in known):
            raise TypeError("known secrets must be KnownSecret instances")
        self._fingerprints = tuple(_fingerprint(secret) for secret in known)

    def scan(self, text: str) -> tuple[Hit, ...]:
        """Every hit in ``text``, ordered by offset; empty when the text is clean."""
        if not isinstance(text, str):
            raise TypeError("the scan takes text (str)")
        hits: set[Hit] = set()
        self._scan_layer(_Layer(text, lambda start, end: (start, end), 0), hits)
        return tuple(sorted(hits))

    def require_clean(self, text: str) -> None:
        """``SecretFound`` unless ``text`` is clean."""
        hits = self.scan(text)
        if hits:
            raise SecretFound(hits)

    def _scan_layer(self, layer: _Layer, hits: set[Hit]) -> None:
        def add(kind: HitKind, name: str, span: tuple[int, int]) -> None:
            hits.add(Hit(*span, kind, name))

        for name, pattern in TOKEN_PATTERNS.items():
            for match in pattern.finditer(layer.text):
                add(HitKind.PATTERN, name, layer.origin(*match.span()))
        if layer.depth == 0:
            # Decoded bytes look random; entropy there would only add false hits.
            for match in _ENTROPY_CANDIDATE.finditer(layer.text):
                if _high_entropy(match.group()):
                    add(HitKind.ENTROPY, ENTROPY_NAME, match.span())
        for fingerprint in self._fingerprints:
            needle = fingerprint.text if layer.depth == 0 else fingerprint.latin
            for start in _find_all(layer.text, needle):
                add(HitKind.KNOWN, fingerprint.label, layer.origin(start, start + len(needle)))
        views = [_View(layer, _WHITESPACE)]
        if layer.depth == 0 and _DIFF_HUNK.search(layer.text):
            views.append(_View(layer, _DIFF_MARKERS))
        for view in views:
            for fingerprint in self._fingerprints:
                for core in fingerprint.base64:
                    for start in _find_all(view.text, core):
                        add(HitKind.KNOWN, fingerprint.label, view.origin(start, start + len(core)))
                for match in fingerprint.hex.finditer(view.text):
                    add(HitKind.KNOWN, fingerprint.label, view.origin(*match.span()))
            if layer.depth < DECODE_DEPTH:
                for decoded in _decoded_layers(view, layer.depth + 1):
                    self._scan_layer(decoded, hits)


def scan(text: str, known: Iterable[KnownSecret] = ()) -> tuple[Hit, ...]:
    """Every hit in ``text``; see ``Scanner``."""
    return Scanner(known).scan(text)


def require_clean(text: str, known: Iterable[KnownSecret] = ()) -> None:
    """``SecretFound`` unless ``text`` is clean; see ``Scanner``."""
    Scanner(known).require_clean(text)


def _find_all(text: str, needle: str | None) -> Iterator[int]:
    if not needle:
        return
    start = text.find(needle)
    while start >= 0:
        yield start
        start = text.find(needle, start + 1)


def _high_entropy(run: str) -> bool:
    if _HEX_ONLY.fullmatch(run):
        return False
    if not (any(c.isdigit() for c in run) and any(c.isupper() for c in run) and any(c.islower() for c in run)):
        return False
    return _entropy(run) >= ENTROPY_THRESHOLD


def _entropy(text: str) -> float:
    """Shannon entropy in bits per character."""
    length = len(text)
    return -sum(count / length * math.log2(count / length) for count in Counter(text).values())


def _decoded_layers(view: _View, depth: int) -> Iterator[_Layer]:
    """Each base64 (either alphabet) and hex run of ``view``, decoded at every alignment."""
    for match in _BASE64_RUN.finditer(view.text):
        run = match.group().translate(_TO_STANDARD)
        for offset in range(4):  # the run may start anywhere in a 4-character group
            chunk = run[offset:]
            # Pad a final partial group instead of dropping it: the last bytes may end a token.
            chunk = chunk[:-1] if len(chunk) % 4 == 1 else chunk + "=" * (-len(chunk) % 4)
            if len(chunk) >= _MIN_ENCODED_RUN:
                yield _Layer(binascii.a2b_base64(chunk).decode("latin-1"),
                             _base64_origin(view, match.start() + offset), depth)
    for match in _HEX_RUN.finditer(view.text):
        run = match.group()
        for offset in range(2):
            usable = len(run) - offset
            chunk = run[offset:offset + usable - usable % 2]
            if len(chunk) >= _MIN_ENCODED_RUN:
                yield _Layer(bytes.fromhex(chunk).decode("latin-1"), _hex_origin(view, match.start() + offset), depth)


def _base64_origin(view: _View, start: int) -> Callable[[int, int], tuple[int, int]]:
    """Decoded bytes ``[first, end)`` -> the base64 characters that carry their bits."""
    return lambda first, end: view.origin(start + 4 * first // 3, start + (8 * end - 1) // 6 + 1)


def _hex_origin(view: _View, start: int) -> Callable[[int, int], tuple[int, int]]:
    return lambda first, end: view.origin(start + 2 * first, start + 2 * end)
