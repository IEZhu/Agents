"""Change single keys of a ``.env`` file binding by binding, as python-dotenv reads it (#381).

The writers of the update channel (``src.update_channel.set_branch`` and the installers'
``scripts/_helpers/env_branch.py``) replace or remove whole bindings: a multi-line quoted
value of another key, a quoted key (``'KEY'=``), ``export`` and inline comments stay as
they are, and so do the file's line endings. ``set_branch`` passes python-dotenv's own
parser (``dotenv.parser.parse_stream``); the installers run before the virtual
environment exists and use :func:`parse`, a copy of that grammar (python-dotenv 1.x)
that tests/test_env_file.py checks against the library.

Standard library only, for those installers (Python 3.10 on Windows).
"""
from __future__ import annotations

import codecs
import os
import re
import tempfile
from typing import Callable, Iterable, NamedTuple, Optional


class Binding(NamedTuple):
    """One binding: its key (None for a comment, blank or unreadable text), its value and its exact text."""

    key: Optional[str]
    value: Optional[str]
    original: str
    error: bool


# python-dotenv 1.x, dotenv/parser.py.
_MULTILINE_WHITESPACE = re.compile(r"\s*", re.MULTILINE)
_WHITESPACE = re.compile(r"[^\S\r\n]*")
_EXPORT = re.compile(r"(?:export[^\S\r\n]+)?")
_SINGLE_QUOTED_KEY = re.compile(r"'([^']+)'")
_UNQUOTED_KEY = re.compile(r"([^=\#\s]+)")
_EQUAL_SIGN = re.compile(r"(=[^\S\r\n]*)")
_SINGLE_QUOTED_VALUE = re.compile(r"'((?:\\'|[^'])*)'")
_DOUBLE_QUOTED_VALUE = re.compile(r'"((?:\\"|[^"])*)"')
_UNQUOTED_VALUE = re.compile(r"([^\r\n]*)")
_COMMENT = re.compile(r"(?:[^\S\r\n]*#[^\r\n]*)?")
_END_OF_LINE = re.compile(r"[^\S\r\n]*(?:\r\n|\n|\r|$)")
_REST_OF_LINE = re.compile(r"[^\r\n]*(?:\r|\n|\r\n)?")
_DOUBLE_QUOTE_ESCAPES = re.compile(r"\\[\\'\"abfnrtv]")
_SINGLE_QUOTE_ESCAPES = re.compile(r"\\[\\']")


class _Unreadable(Exception):
    pass


def _decode(escapes, value):
    return escapes.sub(lambda match: codecs.decode(match.group(0), "unicode-escape"), value)


def parse(text: str) -> list:
    """The bindings of *text* as python-dotenv's ``parse_stream`` yields them; their texts add up to *text*."""
    bindings, position = [], 0

    def read(regex):
        nonlocal position
        match = regex.match(text, position)
        if match is None:
            raise _Unreadable
        position = match.end()
        return match.groups()

    while position < len(text):
        mark = position
        try:
            read(_MULTILINE_WHITESPACE)
            if position >= len(text):
                bindings.append(Binding(None, None, text[mark:], False))
                break
            read(_EXPORT)
            char = text[position:position + 1]
            if char == "#":
                key = None
            elif char == "'":
                (key,) = read(_SINGLE_QUOTED_KEY)
            else:
                (key,) = read(_UNQUOTED_KEY)
            read(_WHITESPACE)
            value = None
            if text[position:position + 1] == "=":
                read(_EQUAL_SIGN)
                char = text[position:position + 1]
                if char == "'":
                    (raw,) = read(_SINGLE_QUOTED_VALUE)
                    value = _decode(_SINGLE_QUOTE_ESCAPES, raw)
                elif char == '"':
                    (raw,) = read(_DOUBLE_QUOTED_VALUE)
                    value = _decode(_DOUBLE_QUOTE_ESCAPES, raw)
                elif char in ("", "\n", "\r"):
                    value = ""
                else:
                    (raw,) = read(_UNQUOTED_VALUE)
                    value = re.sub(r"\s+#.*", "", raw).rstrip()
            read(_COMMENT)
            read(_END_OF_LINE)
            bindings.append(Binding(key, value, text[mark:position], False))
        except _Unreadable:
            read(_REST_OF_LINE)
            bindings.append(Binding(None, None, text[mark:position], True))
    return bindings


def dotenv_parse(text: str) -> list:
    """:func:`parse` done by python-dotenv itself (``dotenv.parser.parse_stream``)."""
    import io
    from dotenv.parser import parse_stream
    return [Binding(found.key, found.value, found.original.string, found.error)
            for found in parse_stream(io.StringIO(text, newline=""))]


# The text of an assignment up to its value: leading blank lines, `export`, the key in either form, `=`.
_LEAD = re.compile(r"\s*(?:export[^\S\r\n]+)?(?:'[^']+'|[^=#\s]+)[^\S\r\n]*(?:=[^\S\r\n]*)?")
_ENDING = re.compile(r"(?:\r\n|\n|\r)?\Z")


def _with_value(original: str, value: str) -> str:
    """The binding *original* with *value* (written as is) instead of its value; comment and line ending kept."""
    lead = _LEAD.match(original).group()
    ending = _ENDING.search(original).group()
    body = original[len(lead):len(original) - len(ending)]
    if not lead.rstrip().endswith("="):  # `KEY` without a value
        return lead.rstrip() + "=" + value + ending
    quoted = {"'": _SINGLE_QUOTED_VALUE, '"': _DOUBLE_QUOTED_VALUE}.get(body[:1])
    if quoted is not None:
        tail = body[quoted.match(body).end():]
    else:
        comment = re.search(r"\s+#.*", body, re.DOTALL)
        tail = comment.group() if comment else ""
    return lead + value + tail + ending


def _leading_lines(original: str) -> str:
    """The blank lines before a binding, which stay when the binding goes."""
    space = re.match(r"\s*", original).group()
    cut = max(space.rfind("\n"), space.rfind("\r"))
    return space[:cut + 1]


def rewrite_text(text: str, updates: dict, parse: Callable[[str], Iterable[Binding]] = parse) -> str:
    """*text* with each key of *updates* set to its value, or removed for None.

    The first binding of a key keeps its place, its form and its inline comment; later
    ones go. A key not in *text* is appended in the file's line ending. Every other
    binding stays byte for byte. Values are written unquoted: the caller validates them.
    """
    bindings = list(parse(text))
    if "".join(binding.original for binding in bindings) != text:
        raise ValueError("the .env file could not be split into bindings")
    newline = "\r\n" if "\r\n" in text else "\n"
    seen, kept = set(), []
    for binding in bindings:
        if binding.error or binding.key not in updates:
            kept.append(binding.original)
        elif binding.key in seen or updates[binding.key] is None:
            kept.append(_leading_lines(binding.original))
        else:
            seen.add(binding.key)
            kept.append(_with_value(binding.original, updates[binding.key]))
    result = "".join(kept)
    added = [f"{key}={value}{newline}" for key, value in updates.items() if value is not None and key not in seen]
    if added and result and not result.endswith(("\n", "\r")):
        result += newline
    return result + "".join(added)


def read_text(path) -> str:
    """The file's text with its line endings as they are."""
    with open(path, encoding="utf-8", newline="") as stream:
        return stream.read()


def write_atomic(path, text: str) -> None:
    """Replace the file at *path* with *text*, durably.

    A missing file is created private; an existing one keeps its permission bits, and a
    symlinked one its link (the target is replaced).
    """
    path = os.path.realpath(path)
    directory = os.path.dirname(path)
    mode = os.stat(path).st_mode & 0o7777 if os.path.exists(path) else 0o600
    descriptor, staging = tempfile.mkstemp(prefix=".env.", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(staging, mode)
        os.replace(staging, path)
    except BaseException:
        try:
            os.unlink(staging)
        except FileNotFoundError:
            pass
        raise
    if os.name == "posix":
        handle = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(handle)
        finally:
            os.close(handle)


def rewrite(path, updates: dict, parse: Callable[[str], Iterable[Binding]] = parse) -> None:
    """:func:`rewrite_text` on the file at *path* (missing: empty), written with :func:`write_atomic`."""
    text = read_text(path) if os.path.exists(path) else ""
    write_atomic(path, rewrite_text(text, updates, parse))
