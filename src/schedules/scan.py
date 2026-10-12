"""Outgoing-text scan of self-development (autodev): a tripwire for secrets (#341).

The autodev broker (#342) scans every text a run sends out (diffs, PR text, comments and worker
output) before it writes it (docs/autodev-plan.md, section 5.6; reference:
docs/autodev-guards.md). Any hit refuses the operation and blocks the run. The scan is a
tripwire, not a protection: sessions get no secrets and no network, and the scan only notices a
leak in a shape it knows. It looks for:

* token-shaped secrets (``TOKEN_PATTERNS``): GitHub tokens, Anthropic, OpenRouter and OpenAI
  project keys, AWS access key IDs, private key blocks and JWTs; a bare prefix such as ``ghp_``
  in prose is not a hit. They are also searched with Unicode format characters (category Cf,
  such as U+200B) removed and lines joined (the line break, the indentation around it and, in a
  unified diff, the line marker), and a prefixed token whose body one blank breaks inside a line
  still counts when both parts have at least ``_SPLIT_PART`` characters and together the body's
  minimum length;
* high-entropy strings: runs of at least ``ENTROPY_MIN_LENGTH`` characters of ``[A-Za-z0-9+_-]``
  with digits and both letter cases and at least ``ENTROPY_THRESHOLD`` bits of Shannon entropy
  per character. ``/`` ends a run, so each component of a URL or path counts alone, and hex
  runs of 7 or more characters inside the run (commit SHAs, digests) are left out of the
  measure. Reaching the threshold takes at least 23 distinct characters, so the check is
  statistical: a random alphanumeric string is caught about once in twenty at 24 characters,
  two times in three at 32 and nearly always from 40 (docs/autodev-guards.md). The session id
  of a ``https://claude.ai/code/session_01...`` link, as commit trailers carry it, is exempt.
  The values keep every tracked file under ``docs/``, ``src/``, ``flows/``, ``agents/``,
  ``tests/`` and ``evals/`` clean except known fixtures (tests/test_schedules_scan.py);
* the caller's known secrets and decoy contents (``KnownSecret``: #342 its token and key, #343
  the decoys): raw; with whitespace, Cf characters and unified-diff line markers removed from both
  the value and the text, so line endings, indentation, a missing final newline or a split do not
  hide it, and each line of a multi-line value with at least ``KNOWN_LINE_MIN_LENGTH`` such
  characters also on its own; as base64 (standard or URL-safe, padded or not, at any byte offset
  inside a larger blob) and as hex in either case, also when whitespace or diff markers split
  the encoded form;
* all of the above inside base64, base64url and hex runs, decoded up to ``DECODE_DEPTH`` levels,
  and after escapes are decoded: percent-encoding, ``\\xNN``, ``\\uNNNN`` and octal ``\\NNN``,
  and hex pairs with ``0x`` prefixes or ``:``, ``,``, ``-`` or blank separators, which become one
  hex run.

A ``Hit`` carries its kind, the pattern name or the caller's label, and character offsets in the
scanned text, never the matched text; a hit inside an encoded run points at the encoded
characters. Hits and ``SecretFound`` can therefore be logged and shown. A text longer than
``MAX_TEXT_LENGTH`` characters is not scanned and is one ``limit`` hit; after ``MAX_HITS`` hits
the scan stops and adds a ``limit`` hit. Either way the text is not clean.
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
import sys
import unicodedata

KNOWN_MIN_LENGTH = 8
KNOWN_LINE_MIN_LENGTH = 16
ENTROPY_MIN_LENGTH = 24
ENTROPY_THRESHOLD = 4.5
ENTROPY_NAME = "high-entropy-string"
DECODE_DEPTH = 2
MAX_TEXT_LENGTH = 4_000_000  # characters; adversarial text scans in about 2 s per million
MAX_HITS = 1000
TOO_LARGE_NAME = "text-too-large"
TOO_MANY_HITS_NAME = "too-many-hits"
_MIN_ENCODED_RUN = 16  # 12 bytes as base64, 8 as hex: shorter runs hide no token
_SPLIT_PART = 8
_HITS_IN_MESSAGE = 10

# Tokens that are a fixed prefix and a body: the prefix, the body's characters, its minimum length.
_PREFIXED_TOKENS = {
    "github-token": ("gh[opsur]_", "A-Za-z0-9", 36),
    "github-fine-grained-token": ("github_pat_", "A-Za-z0-9_", 22),
    "anthropic-key": ("sk-ant-", "A-Za-z0-9_-", 20),
    "openrouter-key": ("sk-or-", "A-Za-z0-9_-", 20),
    "openai-project-key": ("sk-proj-", "A-Za-z0-9_-", 20),
}
TOKEN_PATTERNS: dict[str, re.Pattern[str]] = {
    **{name: re.compile(rf"{prefix}[{body}]{{{minimum},}}")
       for name, (prefix, body, minimum) in _PREFIXED_TOKENS.items()},
    "aws-access-key-id": re.compile(r"(?<![A-Z0-9])AKIA[A-Z0-9]{16}(?![A-Z0-9])"),
    "private-key": re.compile(r"-----BEGIN (?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?-----"),
    # Only at the start of a run: retrying inside a long dotless run would take quadratic time.
    "jwt": re.compile(r"(?<![A-Za-z0-9_-])eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
}
# A prefixed token whose body one blank breaks inside a line: (pattern, minimum body length). Only
# at the start of a run, like the JWT: this pattern can fail after the run's end, and retrying at
# each prefix inside a run such as `sk-ant-sk-ant-...` would take quadratic time.
_SPLIT_TOKENS = {
    name: (re.compile(rf"(?<![{body}]){prefix}([{body}]{{{_SPLIT_PART},}})[^\S\r\n]([{body}]{{{_SPLIT_PART},}})"),
           minimum)
    for name, (prefix, body, minimum) in _PREFIXED_TOKENS.items()
}


def _format_characters() -> str:
    """The Unicode format characters (category Cf: U+200B, U+FEFF, ...) as a character class body."""
    ranges: list[list[int]] = []
    for code in range(sys.maxunicode + 1):
        if unicodedata.category(chr(code)) == "Cf":
            if ranges and ranges[-1][1] == code - 1:
                ranges[-1][1] = code
            else:
                ranges.append([code, code])
    return "".join(rf"\U{first:08x}-\U{last:08x}" for first, last in ranges)


_CF = _format_characters()
_ENTROPY_CANDIDATE = re.compile(rf"[A-Za-z0-9+_-]{{{ENTROPY_MIN_LENGTH},}}")
_HEX_IN_RUN = re.compile(r"[0-9A-Fa-f]{7,}")
# The id of a Claude Code session link, as the `Claude-Session:` trailer of a commit carries it.
_ENTROPY_EXEMPT = re.compile(r"(?<=https://claude\.ai/code/)session_01[A-Za-z0-9]{22}(?![A-Za-z0-9+_-])")
_BASE64_RUN = re.compile(rf"[A-Za-z0-9+/_-]{{{_MIN_ENCODED_RUN},}}")
_HEX_RUN = re.compile(rf"[0-9A-Fa-f]{{{_MIN_ENCODED_RUN},}}")
# Around a known value: Markdown quote markers at a line start (also around a diff line marker,
# as in a quoted diff), the string escapes of JSON and
# `repr` (a quote, backslash or slash stays as itself, a line break or tab goes), whitespace and Cf.
_QUOTE_MARKERS = r"(?:[^\S\r\n]*>)+[^\S\r\n]?"
_STRING_ESCAPE = r"\\[nrt\"'\\/]"
_IGNORABLE = re.compile(rf"(?m)^{_QUOTE_MARKERS}|{_STRING_ESCAPE}|[\s{_CF}]+")
_DIFF_MARKERS = re.compile(rf"(?m)^(?:{_QUOTE_MARKERS})?[+ -](?:{_QUOTE_MARKERS})?|^{_QUOTE_MARKERS}|{_STRING_ESCAPE}|[\s{_CF}]+")
_DIFF_HUNK = re.compile(rf"(?m)^(?:{_QUOTE_MARKERS})?@@ -[0-9]+(?:,[0-9]+)? \+[0-9]+(?:,[0-9]+)? @@")
# Line joins start only where a run of blanks starts: retrying inside a long run would be quadratic.
_LINE_JOINS = re.compile(rf"[{_CF}]+|(?<![^\S\r\n])[^\S\r\n]*(?:(?:\r\n?|\n)[^\S\r\n]*)+")
_DIFF_LINE_JOINS = re.compile(rf"[{_CF}]+|(?<![^\S\r\n])[^\S\r\n]*(?:(?:\r\n?|\n)[+ -]?[^\S\r\n]*)+")
_HEX_PAIR = r"(?:0[xX])?[0-9A-Fa-f]{2}(?![0-9A-Za-z])"
_ESCAPE = re.compile(
    r"%([0-9A-Fa-f]{2})"
    # Only from the first backslash of a run, possessively: a run with no escape after it is tried
    # once, not once per backslash (quadratic).
    r"|(?<!\\)\\++(?:x([0-9A-Fa-f]{2})|u([0-9A-Fa-f]{4})|([0-3][0-7]{2}))"
    rf"|(?<![0-9A-Za-z]){_HEX_PAIR}(?:(?:[:,-]\s*|\s+){_HEX_PAIR}){{{_MIN_ENCODED_RUN // 2 - 1},}}"
)
_HEX_PAIR_NOISE = re.compile(r"0[xX]|[^0-9A-Fa-f]")
_ESCAPED_TOKEN_CHARACTERS = re.compile(r"[A-Za-z0-9+/=_.~-]*")
_TO_STANDARD = str.maketrans("-_", "+/")
_TO_URLSAFE = str.maketrans("+/", "-_")


class HitKind(StrEnum):
    PATTERN = "pattern"
    ENTROPY = "entropy"
    KNOWN = "known"
    LIMIT = "limit"  # the text is too large to scan, or has too many hits to list


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
    joined: tuple[str, ...] = field(repr=False)  # ``_joined_needles`` of ``text``
    joined_latin: tuple[str, ...] = field(repr=False)  # ``_joined_needles`` of ``latin``
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
    latin = data.decode("latin-1")
    return _Fingerprint(secret.label, text, latin, _joined_needles(text) if text is not None else (),
                        _joined_needles(latin), tuple(sorted(cores)),
                        re.compile(re.escape(data.hex()), re.IGNORECASE))


def _joined_needles(value: str) -> tuple[str, ...]:
    """What to look for in a view without whitespace and Cf characters: ``value`` so stripped,
    then each of its lines that keeps at least ``KNOWN_LINE_MIN_LENGTH`` characters so."""
    needles = [_IGNORABLE.sub(_unquote, value)]
    if len(needles[0]) < KNOWN_MIN_LENGTH:
        needles.pop()
    for line in value.splitlines():
        joined = _IGNORABLE.sub(_unquote, line)
        if len(joined) >= KNOWN_LINE_MIN_LENGTH:
            needles.append(joined)
    return tuple(dict.fromkeys(needles))


@dataclass(frozen=True)
class _Layer:
    """Text to scan: the caller's (depth 0) or bytes decoded from an encoded run, as Latin-1."""

    text: str
    origin: Callable[[int, int], tuple[int, int]]  # a span of ``text`` -> a span of the caller's text
    depth: int


class _View:
    """A layer with each span ``pattern`` matches replaced by ``replace(match)``, removed by
    default: whitespace and diff markers go, so an encoded form split across lines is contiguous,
    and escapes are decoded. Offsets map back to the layer; a replaced character maps to the
    whole span it replaced."""

    def __init__(self, layer: _Layer, pattern: re.Pattern[str],
                 replace: Callable[[re.Match[str]], str] | None = None):
        self.layer = layer
        pieces: list[str] = []
        self._ends: list[int] = []  # view offset after each replacement
        self._layer_ends: list[int] = []  # layer offset after each replaced span
        self._replaced: dict[int, tuple[int, int]] = {}  # index -> view and layer offset of a non-empty one
        kept = length = 0
        for match in pattern.finditer(layer.text):
            pieces.append(layer.text[kept:match.start()])
            length += match.start() - kept
            replacement = replace(match) if replace else ""
            if replacement:
                self._replaced[len(self._ends)] = (length, match.start())
                pieces.append(replacement)
                length += len(replacement)
            self._ends.append(length)
            self._layer_ends.append(match.end())
            kept = match.end()
        pieces.append(layer.text[kept:])
        self.text = "".join(pieces)

    def _span(self, offset: int) -> tuple[int, int]:
        """The layer span of the view character at ``offset``."""
        index = bisect_right(self._ends, offset)  # replacements that end at or before ``offset``
        replaced = self._replaced.get(index)
        if replaced is not None and replaced[0] <= offset:
            return replaced[1], self._layer_ends[index]
        position = offset - self._ends[index - 1] + self._layer_ends[index - 1] if index else offset
        return position, position + 1

    def origin(self, start: int, end: int) -> tuple[int, int]:
        return self.layer.origin(self._span(start)[0], self._span(end - 1)[1])


class _TooManyHits(Exception):
    pass


class _Hits:
    """The hits found so far; ``_TooManyHits`` once there are ``MAX_HITS``."""

    def __init__(self) -> None:
        self.found: set[Hit] = set()

    def add(self, kind: HitKind, name: str, span: tuple[int, int]) -> None:
        self.found.add(Hit(*span, kind, name))
        if len(self.found) >= MAX_HITS:
            raise _TooManyHits


class Scanner:
    """Scans texts against the token patterns, entropy and fingerprints of ``known``."""

    def __init__(self, known: Iterable[KnownSecret] = ()):
        known = tuple(known)
        if not all(isinstance(secret, KnownSecret) for secret in known):
            raise TypeError("known secrets must be KnownSecret instances")
        self._fingerprints = tuple(_fingerprint(secret) for secret in known)

    def scan(self, text: str) -> tuple[Hit, ...]:
        """Every hit in ``text``, ordered by offset; empty when the text is clean.

        Text longer than ``MAX_TEXT_LENGTH`` is one ``limit`` hit, unscanned; after ``MAX_HITS``
        hits the scan stops and adds a ``limit`` hit."""
        if not isinstance(text, str):
            raise TypeError("the scan takes text (str)")
        if len(text) > MAX_TEXT_LENGTH:
            return (Hit(0, len(text), HitKind.LIMIT, TOO_LARGE_NAME),)
        hits = _Hits()
        try:
            self._scan_layer(_Layer(text, lambda start, end: (start, end), 0), hits)
        except _TooManyHits:
            hits.found.add(Hit(0, len(text), HitKind.LIMIT, TOO_MANY_HITS_NAME))
        return tuple(sorted(hits.found))

    def require_clean(self, text: str) -> None:
        """``SecretFound`` unless ``text`` is clean."""
        hits = self.scan(text)
        if hits:
            raise SecretFound(hits)

    def _scan_layer(self, layer: _Layer, hits: _Hits) -> None:
        for name, pattern in TOKEN_PATTERNS.items():
            for match in pattern.finditer(layer.text):
                hits.add(HitKind.PATTERN, name, layer.origin(*match.span()))
        diff = layer.depth == 0 and _DIFF_HUNK.search(layer.text) is not None
        if layer.depth == 0:
            self._scan_joined_lines(layer, diff, hits)
            # Decoded bytes look random; entropy there would only add false hits.
            exempt = {match.span() for match in _ENTROPY_EXEMPT.finditer(layer.text)}
            for match in _ENTROPY_CANDIDATE.finditer(layer.text):
                if match.span() not in exempt and _high_entropy(match.group()):
                    hits.add(HitKind.ENTROPY, ENTROPY_NAME, match.span())
        for fingerprint in self._fingerprints:
            needle = fingerprint.text if layer.depth == 0 else fingerprint.latin
            for start in _find_all(layer.text, needle):
                hits.add(HitKind.KNOWN, fingerprint.label, layer.origin(start, start + len(needle)))
        views = [_View(layer, _IGNORABLE, _unquote)]
        if diff:
            views.append(_View(layer, _DIFF_MARKERS, _unquote))
        for view in views:
            for fingerprint in self._fingerprints:
                for needle in (fingerprint.joined if layer.depth == 0 else fingerprint.joined_latin):
                    for start in _find_all(view.text, needle):
                        hits.add(HitKind.KNOWN, fingerprint.label, view.origin(start, start + len(needle)))
                for core in fingerprint.base64:
                    for start in _find_all(view.text, core):
                        hits.add(HitKind.KNOWN, fingerprint.label, view.origin(start, start + len(core)))
                for match in fingerprint.hex.finditer(view.text):
                    hits.add(HitKind.KNOWN, fingerprint.label, view.origin(*match.span()))
            if layer.depth < DECODE_DEPTH:
                for decoded in _decoded_layers(view, layer.depth + 1):
                    self._scan_layer(decoded, hits)
        if layer.depth == 0:
            for unescaped in _unescaped_layers(layer):
                self._scan_layer(unescaped, hits)

    @staticmethod
    def _scan_joined_lines(layer: _Layer, diff: bool, hits: _Hits) -> None:
        """Token patterns across line breaks and Cf characters, and prefixed tokens one blank splits."""
        view = _View(layer, _DIFF_LINE_JOINS if diff else _LINE_JOINS)
        for name, pattern in TOKEN_PATTERNS.items():
            for match in pattern.finditer(view.text):
                hits.add(HitKind.PATTERN, name, view.origin(*match.span()))
        for name, (pattern, minimum) in _SPLIT_TOKENS.items():
            for match in pattern.finditer(view.text):
                if len(match.group(1)) + len(match.group(2)) >= minimum:
                    hits.add(HitKind.PATTERN, name, view.origin(*match.span()))


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
    # Hex runs (commit SHAs, digests) do not count, so pure hex never does.
    rest = _HEX_IN_RUN.sub("", run)
    if not (any(c.isdigit() for c in rest) and any(c.isupper() for c in rest) and any(c.islower() for c in rest)):
        return False
    return _entropy(rest) >= ENTROPY_THRESHOLD


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


def _unescaped_layers(layer: _Layer) -> Iterator[_Layer]:
    """Each run of token characters and escapes in ``layer``, with its escapes decoded."""
    text = layer.text
    end = -1
    for match in _ESCAPE.finditer(text):
        if match.start() < end:
            continue  # already inside the last run
        start = match.start()
        while start and _ESCAPED_TOKEN_CHARACTERS.fullmatch(text[start - 1]):
            start -= 1
        end = match.end()
        while True:
            end = _ESCAPED_TOKEN_CHARACTERS.match(text, end).end()
            following = _ESCAPE.match(text, end)
            if following is None:
                break
            end = following.end()
        part = _Layer(text[start:end], _shifted_origin(layer, start), layer.depth)
        view = _View(part, _ESCAPE, _unescape)
        yield _Layer(view.text, view.origin, layer.depth + 1)


def _shifted_origin(layer: _Layer, start: int) -> Callable[[int, int], tuple[int, int]]:
    return lambda first, end: layer.origin(start + first, start + end)


def _unquote(match: re.Match[str]) -> str:
    """A quoted quote, backslash or slash as itself; any other noise around a known value goes."""
    text = match.group()
    return text[1] if len(text) == 2 and text[0] == "\\" and text[1] in "\"'\\/" else ""


def _unescape(match: re.Match[str]) -> str:
    """An escape as the byte (a Latin-1 character) it stands for; hex pairs as one hex run."""
    percent, hex_escape, code, octal = match.groups()
    if percent or hex_escape:
        return chr(int(percent or hex_escape, 16))
    if code:
        return chr(int(code, 16)).encode("utf-8", "surrogatepass").decode("latin-1")
    if octal:
        return chr(int(octal, 8))
    return _HEX_PAIR_NOISE.sub("", match.group())
