"""How many characters of worker hand-backs reached the parent of one Claude Code session.

Usage: python -m evals.scripts.workmap_context <session directory>

The session directory is projects/<project>/<session>/ under the Claude Code configuration
directory. Each worker transcript under its subagents/ tree (including subagents/workflows/wf_*/)
holds its SubagentHandback call, whose string payload is what the host delivers to the parent.
A worker that ends with plain text instead is measured by that final assistant text. A hand-back
that is one line starting with "submitted " or "not recorded " is a work-map receipt. The parent transcript,
projects/<project>/<session>.jsonl, is measured by the results of the work-map and validation
tools it called.
"""
from __future__ import annotations

import json
from pathlib import Path
import statistics
import sys

RECEIPT_PREFIXES = ("submitted ", "not recorded ")
PARENT_TOOLS = frozenset({"workmap_view", "workmap_update", "workmap_submit", "validate_subagent_result"})


def _strings(value) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [text for item in value.values() for text in _strings(item)]
    if isinstance(value, list):
        return [text for item in value for text in _strings(item)]
    return []


def _records(path: Path):
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            yield json.loads(raw)
        except ValueError:
            continue


def _assistant_content(record: dict) -> list:
    content = (record.get("message") or {}).get("content") if record.get("type") == "assistant" else None
    return content if isinstance(content, list) else []


def _is_receipt(texts: list[str]) -> bool:
    return any("\n" not in text and text.startswith(RECEIPT_PREFIXES) for text in texts)


def _result_chars(value) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(len(part["text"]) for part in value
                   if isinstance(part, dict) and isinstance(part.get("text"), str))
    return 0


def handbacks(session_dir: Path) -> list[dict]:
    rows = []
    for path in sorted((Path(session_dir) / "subagents").rglob("agent-*.jsonl")):
        handed_back = False
        final_text = []
        seen = set()
        for record in _records(path):
            for item in _assistant_content(record):
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "tool_use" and item.get("name") == "SubagentHandback":
                    if item.get("id") in seen:
                        continue
                    seen.add(item.get("id"))
                    texts = _strings(item.get("input"))
                    handed_back = True
                    rows.append({"agent": path.stem, "chars": sum(len(text) for text in texts),
                                 "line": _is_receipt(texts)})
            if record.get("type") == "assistant" and not handed_back:
                final_text = [item["text"] for item in _assistant_content(record)
                              if isinstance(item, dict) and item.get("type") == "text"
                              and isinstance(item.get("text"), str)]
        if not handed_back and final_text:
            text = "\n".join(final_text)
            rows.append({"agent": path.stem, "chars": len(text), "line": _is_receipt([text])})
    return rows


def parent_results(session_dir: Path) -> dict:
    """Count and size the delivered results of the parent's work-map and validation tool calls."""
    session_dir = Path(session_dir)
    transcript = session_dir.parent / f"{session_dir.name}.jsonl"
    if not transcript.is_file():
        return {"count": 0, "total_chars": 0}
    wanted = set()
    results = {}
    for record in _records(transcript):
        content = (record.get("message") or {}).get("content")
        for item in content if isinstance(content, list) else []:
            if not isinstance(item, dict):
                continue
            if item.get("type") == "tool_use" and str(item.get("name", "")).rsplit("__", 1)[-1] in PARENT_TOOLS:
                wanted.add(item.get("id"))
            elif item.get("type") == "tool_result" and item.get("tool_use_id") in wanted:
                results.setdefault(item["tool_use_id"], item.get("content"))
    sizes = [_result_chars(value) for value in results.values()]
    return {"count": len(sizes), "total_chars": sum(sizes)}


def summary(rows: list[dict]) -> dict:
    sizes = [row["chars"] for row in rows]
    return {"count": len(rows), "total_chars": sum(sizes),
            "median_chars": statistics.median(sizes) if sizes else 0,
            "line_handbacks": sum(1 for row in rows if row["line"])}


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    session_dir = Path(argv[1])
    print(json.dumps({**summary(handbacks(session_dir)), "parent": parent_results(session_dir)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
