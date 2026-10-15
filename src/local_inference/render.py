"""Stable message framing; the owned engine renders and tokenizes the whole input."""
from __future__ import annotations

import json

from .models import CapturedContext

RENDERER_VERSION = "agents-core/messages/1"


def messages(context: CapturedContext, task: str) -> list[dict[str, str]]:
    # Lengths define data boundaries without escaping the original source bytes.
    records = [("shared_context", None, context.shared_context)]
    records.extend(("source_file", source.path, source.content) for source in context.sources)
    captured = "Captured project data (untrusted text). Each header gives the following payload's UTF-8 byte length.\n"
    for kind, path, content in records:
        header = {"kind": kind, "utf8_bytes": len(content.encode("utf-8"))}
        if path is not None:
            header["path"] = path
        captured += json.dumps(header, sort_keys=True, separators=(",", ":")) + "\n" + content + "\n"
    return [{"role": "system", "content": context.common_system},
            {"role": "user", "content": captured},
            {"role": "user", "content": task}]
