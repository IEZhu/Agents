"""read_history listings that Claude Code keeps in the conversation (#212).

Claude Code saves a tool result over ``INLINE_LIMIT`` characters, as it counts them
(``src/result_size.py``), to a file. A history entry carries the whole answer it recorded,
so the twenty newest entries of a busy repository already pass that limit.

A listing therefore shows long texts as previews of at most ``TEXTS`` characters (outcome,
intent, action) and lists of at most ``LIST_ITEMS`` items (files, tags). A shortened entry
gives each cut field's full length in ``truncated``: characters for a text, items for a list.
``read_history(entry_id=...)`` returns the entry whole. When the previews still do not fit
``RESULT_BUDGET``, they shrink together down to ``MIN_PREVIEW_CHARS``; only then do the last
entries stay out, counted in ``omitted``, and ``offset`` reads them. A first entry that does
not fit even then comes as a ``stub`` of its id and time.

Of the means #212 lists, a lower default ``limit`` alone still lets a few long answers pass
the limit, and ``anthropic/maxResultSizeChars`` (as for the flow tools, #196) would keep every
entry's full text in the conversation at its full token cost, which a listing rarely needs:
callers mostly look for times, intents and tags.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.result_size import INLINE_LIMIT, shown_size

# What a listing may take: the rest is room for a client that counts a little differently.
RESULT_BUDGET = INLINE_LIMIT - 5_000
TEXTS = {"outcome": 600, "intent": 300, "action": 300}
PREVIEW_CHARS = max(TEXTS.values())
MIN_PREVIEW_CHARS = 100
LISTS = ("files", "tags")
LIST_ITEMS = 20

_ELLIPSIS = "…"


def to_json(payload: Mapping[str, Any]) -> str:
    """``payload`` as the JSON text a tool returns; a lone surrogate, which no transport can
    encode, comes out as its ``\\uXXXX`` escape instead of failing the call."""
    text = json.dumps(payload, ensure_ascii=False)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        # Only inside a JSON string, where the escape reads back as the same character.
        text = text.encode("utf-8", "backslashreplace").decode("utf-8")
    return text


def preview(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters with the ellipsis, at a space near the end when there is one."""
    cut = text[:limit - 1]
    space = cut.rfind(" ")
    if space > limit * 0.8:
        cut = cut[:space]
    return cut.rstrip() + _ELLIPSIS


def shorten(entry: Mapping[str, Any], size: int = PREVIEW_CHARS) -> Dict[str, Any]:
    """A copy of ``entry`` with texts cut to their ``TEXTS`` limit, at most ``size``, and lists to
    ``LIST_ITEMS``; ``truncated`` gives each cut field's full length."""
    out = dict(entry)
    cut = {}
    for name, limit in TEXTS.items():
        text = out.get(name)
        limit = min(limit, size)
        if isinstance(text, str) and len(text) > limit:
            out[name] = preview(text, limit)
            cut[name] = len(text)
    for name in LISTS:
        items = out.get(name)
        if isinstance(items, list) and len(items) > LIST_ITEMS:
            out[name] = items[:LIST_ITEMS]
            cut[name] = len(items)
    if cut:
        out["truncated"] = cut
    return out


def stub(entry: Mapping[str, Any]) -> Dict[str, Any]:
    """``entry`` reduced to its id, time, distance and machine, each text at most
    ``MIN_PREVIEW_CHARS``; ``truncated`` gives the length of each field cut or left out
    (characters, items for a list, characters of JSON otherwise)."""
    out: Dict[str, Any] = {}
    cut = {}
    for name, value in entry.items():
        if name not in _STUB_FIELDS:
            if value not in (None, "", [], {}):
                cut[name] = _length(value)
        elif isinstance(value, str) and len(value) > MIN_PREVIEW_CHARS:
            out[name], cut[name] = preview(value, MIN_PREVIEW_CHARS), len(value)
        else:
            out[name] = value
    out["truncated"] = cut
    return out


_STUB_FIELDS = ("id", "timestamp", "distance", "machine")


def _length(value: Any) -> int:
    if isinstance(value, (str, list)):
        return len(value)
    return len(json.dumps(value, ensure_ascii=False))


def _instruction(omitted: int, next_offset: int) -> str:
    text = ("Long texts and lists are shortened; truncated gives each cut field's full length. "
            "read_history(entry_id=<id>) returns an entry whole.")
    if omitted:
        text += (f" {omitted} more entries were left out to keep the result under "
                 f"{INLINE_LIMIT:,} characters: repeat this call with offset={next_offset} and "
                 f"limit={omitted} to read them.")
    return text


def listing(mode: str, entries: Sequence[Mapping[str, Any]], extra: Mapping[str, Any],
            offset: int = 0) -> Dict[str, Any]:
    """The read_history payload for ``entries`` (those after ``offset``) that Claude Code keeps inline.

    ``extra`` is added after the entries (the workspace report). By ``shown_size`` of its
    ``to_json``, the payload is at most ``RESULT_BUDGET``: previews first, then shorter
    previews, then the first entries that fit at the shortest previews.
    """
    def render(size: int, count: int, stubbed: bool = False) -> Dict[str, Any]:
        shown: List[Dict[str, Any]] = [shorten(entry, size) for entry in entries[:count]]
        if stubbed:
            shown, count = [stub(entries[0])], 1
        omitted = len(entries) - count
        payload: Dict[str, Any] = {"mode": mode, "total": count, "entries": shown}
        if omitted:
            payload["omitted"] = omitted
        if omitted or any("truncated" in entry for entry in shown):
            payload["instruction"] = _instruction(omitted, offset + count)
        payload.update(extra)
        return payload

    def fits(payload: Dict[str, Any]) -> bool:
        return shown_size(to_json(payload)) <= RESULT_BUDGET

    everything = render(PREVIEW_CHARS, len(entries))
    if fits(everything):
        return everything
    floor = render(MIN_PREVIEW_CHARS, len(entries))
    if fits(floor):
        # The longest previews that fit. A size need not grow strictly with the preview (one
        # can outgrow a text a little over its limit), so the search can settle a little low.
        return _largest(MIN_PREVIEW_CHARS + 1, PREVIEW_CHARS - 1,
                        lambda size: render(size, len(entries)), fits) or floor
    # The most entries that fit at the shortest previews. When not even the first does (a huge
    # metadata line or list item), it comes as a stub: entry_id reads it and offset moves past it.
    fitted = _largest(1, len(entries) - 1, lambda count: render(MIN_PREVIEW_CHARS, count), fits)
    if fitted is None and entries:
        fitted = render(MIN_PREVIEW_CHARS, 0, stubbed=True)
    return fitted if fitted is not None and fits(fitted) else render(MIN_PREVIEW_CHARS, 0)


def _largest(low: int, high: int, render, fits) -> Optional[Dict[str, Any]]:
    """The rendering for the largest value in ``low..high`` that fits, or None."""
    best = None
    while low <= high:
        middle = (low + high) // 2
        payload = render(middle)
        if fits(payload):
            best, low = payload, middle + 1
        else:
            high = middle - 1
    return best


__all__ = [
    "LISTS",
    "LIST_ITEMS",
    "MIN_PREVIEW_CHARS",
    "PREVIEW_CHARS",
    "RESULT_BUDGET",
    "TEXTS",
    "listing",
    "preview",
    "shorten",
    "stub",
    "to_json",
]
