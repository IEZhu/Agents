"""read_history listings that Claude Code keeps in the conversation (#212).

Claude Code saves an MCP tool's text result longer than 50,000 characters to a file and gives
the model a pointer instead (#196). It counts what it shows the model: for a tool with
structured output, ``JSON.stringify(structuredContent)``, where FastMCP's structured content
is ``{"result": <the JSON text the tool returns>}``, in UTF-16 code units. A history entry
carries the whole answer it recorded, so the twenty newest entries of a busy repository
already pass that limit.

A listing therefore shows long texts as previews: ``outcome`` (recency) and ``document``
(semantic) up to ``PREVIEW_CHARS``, ``intent`` and ``action`` up to ``BRIEF_CHARS``. A
shortened entry names each cut field with its full length in ``truncated``, and
``read_history(entry_id=...)`` returns the entry whole. When the previews still do not fit
``RESULT_BUDGET``, they shrink together down to ``MIN_PREVIEW_CHARS``; only then do the last
entries stay out, counted in ``omitted``.

Of the means #212 lists, a lower default ``limit`` alone still lets a few long answers pass
the limit, and ``anthropic/maxResultSizeChars`` (as for the flow tools, #196) would keep every
entry's full text in the conversation at its full token cost, which a listing rarely needs:
callers mostly look for times, intents and tags.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, NamedTuple, Optional, Sequence

# Claude Code's threshold for a tool that declares no result size (#196).
INLINE_LIMIT = 50_000
# What a listing may take: the rest is room for a client that counts a little differently.
RESULT_BUDGET = INLINE_LIMIT - 5_000
PREVIEW_CHARS = 600
BRIEF_CHARS = 300
MIN_PREVIEW_CHARS = 100

# The texts a listing shortens, with their preview lengths, per mode.
RECENCY_TEXTS = {"outcome": PREVIEW_CHARS, "intent": BRIEF_CHARS, "action": BRIEF_CHARS}
SEMANTIC_TEXTS = {"document": PREVIEW_CHARS, "intent": BRIEF_CHARS}

_ELLIPSIS = "…"


def shown_size(text: str) -> int:
    """The size Claude Code counts for a tool that returns ``text``: the UTF-16 length of
    ``JSON.stringify({"result": text})``, which escapes the tool's JSON a second time."""
    shown = json.dumps({"result": text}, ensure_ascii=False, separators=(",", ":"))
    return len(shown.encode("utf-16-le")) // 2


def preview(text: str, limit: int) -> str:
    """``text`` cut to ``limit`` characters plus an ellipsis, at a space near the end when there is one."""
    cut = text[:limit]
    space = cut.rfind(" ")
    if space > limit * 0.8:
        cut = cut[:space]
    return cut.rstrip() + _ELLIPSIS


def shorten(entry: Mapping[str, Any], limits: Mapping[str, int]) -> Dict[str, Any]:
    """A copy of ``entry`` whose texts longer than their limit are previews, listed in ``truncated``."""
    out = dict(entry)
    cut = {}
    for name, limit in limits.items():
        text = out.get(name)
        if isinstance(text, str) and len(text) > limit:
            out[name] = preview(text, limit)
            cut[name] = len(text)
    if cut:
        out["truncated"] = cut
    return out


def _instruction(omitted: int) -> str:
    text = ("Long texts are previews; truncated gives each cut field's full length. "
            "read_history(entry_id=<id>) returns an entry whole.")
    if omitted:
        text += (f" {omitted} more entries were left out to keep the result under "
                 f"{INLINE_LIMIT:,} characters: lower limit or narrow since or query.")
    return text


class Listing(NamedTuple):
    text: str  # the JSON the tool returns
    total: int  # entries shown
    omitted: int  # entries left out


def listing(mode: str, entries: Sequence[Mapping[str, Any]], texts: Mapping[str, int],
            extra: Mapping[str, Any], budget: int = RESULT_BUDGET) -> Listing:
    """A read_history listing of ``entries`` that Claude Code keeps inline.

    ``texts`` maps each field to shorten to its preview length; ``extra`` is added to the
    payload after the entries (the workspace report). The text is at most ``budget`` by
    ``shown_size``: previews first, then shorter previews, then the first entries that fit.
    """
    def render(size: int, count: int) -> Listing:
        limits = {name: min(length, size) for name, length in texts.items()}
        shown: List[Dict[str, Any]] = [shorten(entry, limits) for entry in entries[:count]]
        omitted = len(entries) - count
        payload: Dict[str, Any] = {"mode": mode, "total": count, "entries": shown}
        if omitted:
            payload["omitted"] = omitted
        if omitted or any("truncated" in entry for entry in shown):
            payload["instruction"] = _instruction(omitted)
        payload.update(extra)
        return Listing(json.dumps(payload, ensure_ascii=False), count, omitted)

    result = render(PREVIEW_CHARS, len(entries))
    if shown_size(result.text) <= budget:
        return result
    # The longest previews that fit, then the most entries that fit at the shortest previews.
    # A size need not grow strictly with either (a preview can outgrow a text a little over
    # its limit), so a search can settle below the best fit, never above the budget.
    fitted = _largest(MIN_PREVIEW_CHARS, PREVIEW_CHARS - 1, lambda size: render(size, len(entries)), budget)
    if fitted is None:
        fitted = _largest(1, len(entries) - 1, lambda count: render(MIN_PREVIEW_CHARS, count), budget)
    return fitted if fitted is not None else render(MIN_PREVIEW_CHARS, 0)


def _largest(low: int, high: int, render, budget: int) -> Optional[Listing]:
    """The rendering for the largest value in ``low..high`` that fits ``budget``, or None."""
    best = None
    while low <= high:
        middle = (low + high) // 2
        result = render(middle)
        if shown_size(result.text) <= budget:
            best, low = result, middle + 1
        else:
            high = middle - 1
    return best


__all__ = [
    "BRIEF_CHARS",
    "INLINE_LIMIT",
    "Listing",
    "MIN_PREVIEW_CHARS",
    "PREVIEW_CHARS",
    "RECENCY_TEXTS",
    "RESULT_BUDGET",
    "SEMANTIC_TEXTS",
    "listing",
    "preview",
    "shorten",
    "shown_size",
]
