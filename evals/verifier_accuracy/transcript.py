"""Facts of one worker run, read from the transcript the host recorded, never from the worker's text.

docs/verifier-accuracy-measurement.md, "The runs": a verdict's level comes from what the
host recorded. A V3-arm verdict is V3 when the transcript records at least one read of a
file in the snapshot worktree, otherwise it is counted as unsupported; a V2-arm verdict is
V2 when the transcript records at least one shell or tool execution by the worker,
otherwise it is counted as V1. Nothing the worker wrote is re-run. The resolved model is
the provider's model ID on the worker's assistant messages; a run without one is excluded.
"""
from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path

READ_TOOLS = frozenset({"Read", "Grep", "Glob"})
EXECUTION_TOOLS = frozenset({"Bash"})
UNSUPPORTED = "unsupported"
SYNTHETIC_MODEL = "<synthetic>"  # Claude Code's own placeholder messages, not a provider model


@dataclass(frozen=True)
class ToolCall:
    name: str
    input: dict
    cwd: str | None
    ok: bool | None  # None when no result was recorded


@dataclass(frozen=True)
class Facts:
    models: tuple[str, ...]
    efforts: tuple[str, ...]
    calls: tuple[ToolCall, ...]
    strings: tuple[str, ...]  # every string the transcript holds, for the path searches

    def mentions(self, needle: str) -> bool:
        return any(needle in text for text in self.strings)


def _strings(value, out: list[str]) -> None:
    if isinstance(value, str):
        out.append(value)
    elif isinstance(value, dict):
        for item in value.values():
            _strings(item, out)
    elif isinstance(value, list):
        for item in value:
            _strings(item, out)


def read(path: Path) -> Facts:
    """The assistant tool calls with their results, the models and every string of a transcript."""
    models, efforts, strings, calls, results = [], [], [], [], {}
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            _strings(record, strings)
            message = record.get("message")
            if not isinstance(message, dict):
                continue
            content = message.get("content") if isinstance(message.get("content"), list) else []
            if record.get("type") == "assistant":
                model = message.get("model")
                if isinstance(model, str) and model and model != SYNTHETIC_MODEL and model not in models:
                    models.append(model)
                effort = record.get("effort")
                if isinstance(effort, str) and effort and effort not in efforts:
                    efforts.append(effort)
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_use" and isinstance(block.get("id"), str):
                        calls.append((block["id"], str(block.get("name")),
                                      block.get("input") if isinstance(block.get("input"), dict) else {},
                                      record.get("cwd") if isinstance(record.get("cwd"), str) else None))
            elif record.get("type") == "user":
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        results[block.get("tool_use_id")] = block.get("is_error") is not True
    return Facts(models=tuple(models), efforts=tuple(efforts), strings=tuple(strings),
                 calls=tuple(ToolCall(name, arguments, cwd, results.get(identifier))
                             for identifier, name, arguments, cwd in calls))


def _inside(path: str | None, root: str, cwd: str | None) -> bool:
    if not path:
        path = cwd
    if not path:
        return False
    if not os.path.isabs(path):
        if not cwd:
            return False
        path = os.path.join(cwd, path)
    real, base = os.path.realpath(path), os.path.realpath(root)
    return real == base or real.startswith(base.rstrip(os.sep) + os.sep)


def snapshot_reads(facts: Facts, snapshot: Path) -> int:
    """Successful reads in the snapshot: a file tool aimed inside it, or a shell command run there."""
    root, count = str(snapshot), 0
    for call in facts.calls:
        if call.ok is not True:
            continue
        if call.name == "Read" and _inside(call.input.get("file_path"), root, call.cwd):
            count += 1
        elif call.name in ("Grep", "Glob") and _inside(call.input.get("path"), root, call.cwd):
            count += 1
        elif call.name in EXECUTION_TOOLS and (_inside(None, root, call.cwd)
                                               or root in str(call.input.get("command", ""))):
            count += 1
    return count


def executions(facts: Facts) -> int:
    """Shell or tool executions with a recorded result, whatever their exit status."""
    return sum(1 for call in facts.calls if call.name in EXECUTION_TOOLS and call.ok is not None)


def level(arm: str, facts: Facts, snapshot: Path) -> str:
    if arm == "V3":
        return "V3" if snapshot_reads(facts, snapshot) else UNSUPPORTED
    if arm == "V2":
        return "V2" if executions(facts) else "V1"
    raise ValueError(f"unknown arm {arm!r}")


def resolved_model(facts: Facts) -> str | None:
    """The one provider model the worker ran on; ``None`` when none, or more than one, is named."""
    return facts.models[0] if len(facts.models) == 1 else None


def resolved_effort(facts: Facts) -> str | None:
    return facts.efforts[0] if len(facts.efforts) == 1 else None


def find(session_id: str, config_dir: Path | None = None) -> Path | None:
    """The host transcript of a headless session: ``projects/<project>/<session>.jsonl``.

    Workers run without ``CLAUDE_CONFIG_DIR`` (`worker.environment`), so their transcripts are
    under ``~/.claude`` whatever the runner's own environment says.
    """
    base = config_dir or Path.home() / ".claude"
    matches = sorted((base / "projects").glob(f"*/{session_id}.jsonl"))
    return matches[0] if len(matches) == 1 else None
