"""How Claude Code sizes an MCP tool's result (#196, #212, #520).

Claude Code saves a tool's text result longer than ``INLINE_LIMIT`` characters to a file and
gives the model a pointer instead, unless the tool declares a larger
``anthropic/maxResultSizeChars`` in its tools/list entry; it accepts at most
``RESULT_SIZE_CEILING``. It counts what it shows the model: for a tool with structured output,
``JSON.stringify(structuredContent)``, where FastMCP's structured content is
``{"result": <the tool's JSON text>}``, in UTF-16 code units. The tool's JSON is thus escaped a
second time: a quote it wrote as ``\\"`` shows as four characters, a control character it
wrote as ``\\u00XX`` as seven.

Over stdio the persona tools, ``log_interaction`` and ``describe_repo`` answer with the JSON object
itself as structured content (``SINGLE_ENCODED_TOOLS`` in ``src/server.py``, #734), so Claude Code
shows it escaped once. ``shown_size`` still measures the ``{"result": ...}`` form, which the shared
service and every other string-valued tool return; for those five tools over stdio it is the
conservative side: it counts more than Claude Code shows.
"""
from __future__ import annotations

import json

INLINE_LIMIT = 50_000
RESULT_SIZE_CEILING = 500_000


def shown_size(text: str) -> int:
    """The size Claude Code counts for a tool that returns ``text``."""
    shown = json.dumps({"result": text}, ensure_ascii=False, separators=(",", ":"))
    return len(shown.encode("utf-16-le", "surrogatepass")) // 2
