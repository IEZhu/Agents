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
import glob
import json
import os
from pathlib import Path
import re
import shlex

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
    versions: tuple[str, ...] = ()  # the Claude Code versions that wrote the records

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
    models, efforts, strings, calls, results, versions = [], [], [], [], {}, []
    with open(path, encoding="utf-8", errors="replace") as handle:
        for line in handle:
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if not isinstance(record, dict):
                continue
            _strings(record, strings)
            version = record.get("version")
            if isinstance(version, str) and version and version not in versions:
                versions.append(version)
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
    return Facts(models=tuple(models), efforts=tuple(efforts), strings=tuple(strings), versions=tuple(versions),
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


_SHELL_OPERATORS = frozenset({"|", "||", "&&", ";", "&", ">", ">>", "<", "2>", "2>&1"})


# A file-like path inside quoted code, as in python3 -c "print(open('src/x.py').read())".
_FILE_FRAGMENT = re.compile(r"[A-Za-z0-9_./-]*[A-Za-z0-9_-]\.[A-Za-z0-9]+")


def names_snapshot_path(command: str, cwd: str | None, snapshot: Path) -> bool:
    """Whether a shell command names an existing file or directory of the snapshot.

    The command is only read, never run, so this is a lexical check: an operand, or a
    file-like path inside quoted code, resolved against the working directory and every
    directory a ``cd`` in the command enters. A working directory alone, ``pwd`` or the
    directory a ``cd`` enters names nothing by itself.
    """
    try:
        tokens = shlex.split(command, comments=True)
    except ValueError:
        tokens = command.split()
    root = str(snapshot)
    directories = [cwd or root]
    candidates, entered = [], set()
    previous = None
    for token in tokens:
        operand = token.split("=", 1)[1] if token.startswith("--") and "=" in token else token
        if previous == "cd":
            entered.add(token)
            directories.append(os.path.normpath(token if os.path.isabs(token) else os.path.join(directories[-1], token)))
        elif operand not in _SHELL_OPERATORS and not operand.startswith("-"):
            candidates.append(operand)
        previous = token
    # Fragments come from the parsed tokens, which keep quoted code and drop shell comments.
    candidates += [fragment for fragment in _FILE_FRAGMENT.findall(" ".join(tokens)) if fragment not in entered]
    for candidate in candidates:
        for base in directories:
            path = candidate if os.path.isabs(candidate) else os.path.join(base, candidate)
            matches = glob.glob(path) if any(mark in candidate for mark in "*?[") else [path]
            if any(_inside(match, root, None) and os.path.exists(match) for match in matches):
                return True
    return False


def snapshot_reads(facts: Facts, snapshot: Path) -> int:
    """Recorded reads in the snapshot: a file tool aimed inside it, or a shell command naming its files.

    A read counts once the host recorded its result, whatever the result was: ``grep`` that
    finds nothing exits 1, which the host marks as an error, and a ``Read`` of a missing path
    is a look in the snapshot too. The pilot counted only successful calls, and three
    verdicts that rested on a ``grep`` without a match were filed as unsupported. A shell
    command counts only when it names a snapshot file or directory (`names_snapshot_path`).
    """
    root, count = str(snapshot), 0
    for call in facts.calls:
        if call.ok is None:
            continue
        if call.name == "Read" and _inside(call.input.get("file_path"), root, call.cwd):
            count += 1
        elif call.name in ("Grep", "Glob") and _inside(call.input.get("path"), root, call.cwd):
            count += 1
        elif call.name in EXECUTION_TOOLS and names_snapshot_path(str(call.input.get("command", "")),
                                                                   call.cwd, snapshot):
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
