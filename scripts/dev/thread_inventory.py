"""Inventory of a Claude Code thread from its transcript, for flows/thread-close.md.

    python scripts/dev/thread_inventory.py --session <uuid>
    python scripts/dev/thread_inventory.py --transcript <part.jsonl> [--transcript <part.jsonl> ...]
    python scripts/dev/thread_inventory.py --project-dir <working directory> --latest

Prints one JSON object and only reads. The inventory is evidence of what the thread
did, taken from its transcript instead of the model's memory, which may have been
compacted. It is not evidence that the work is correct or still current: the flow
verifies every item against the live state.

`--session` looks for `<uuid>.jsonl` under `projects/*/` of `$CLAUDE_CONFIG_DIR` and
every `~/.claude*` directory. In Claude Code the session id is the directory name
above the session's scratchpad. `--latest` takes the newest transcript of a project and
lists the other transcripts written in the last hour, because several sessions can
run in one project at the same time.

Token usage is counted once per model response: the transcript repeats a response's
usage on every entry (thinking, text, tool call) that the response produced.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path

GIT_MUTATION = re.compile(
    r"\bgit\b(?:\s+-C\s+\S+|\s+-c\s+\S+|\s+--no-optional-locks)*\s+"
    r"(commit|push|pull|rebase|merge|reset|cherry-pick|revert|stash|tag|worktree\s+(?:add|remove|prune)"
    r"|branch\s+-[dDmM]|switch\s+-[cC]|checkout\s+-[bB])\b")
GH_MUTATION = re.compile(
    r"\bgh\s+(?:pr\s+(?:create|merge|close|comment|edit|review|ready)|issue\s+(?:create|close|comment|edit|reopen)"
    r"|api\s+(?:-X|--method)\s+(?:POST|PATCH|PUT|DELETE))\b")
GITHUB_REF = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/(pull|issues)/(\d+)")
SCRATCHPAD = re.compile(r"(/[^\s'\"`|;&()<>]*?/scratchpad(?:/[^\s'\"`|;&()<>]*)?)")
CD_PATH = re.compile(r"(?:\bcd|\bgit\s+-C|\bworktree\s+add(?:\s+-[bB]\s+\S+|\s+-\S+)*)\s+(['\"]?)([^\s'\";&|)<>]+)\1")
# Phrases with which a model takes back its own earlier statement. They are candidates
# for the audit, not conclusions: the flow reads each one in context.
CORRECTION = re.compile(
    r"(?i)\b(?:my mistake|i was wrong|my error|i misstated|i retract|that was wrong|incorrectly (?:stated|claimed|said))\b"
    r"|мо(?:я|ей|ю) ошибк|я ошиб(?:ся|лась)|по моей вине|(?:неверн|ошибочн)\w* (?:подсч|утвержд|сказал|написал|назвал)"
    r"|был[аои]? неверн")
BACKGROUND_STARTED = (re.compile(r"running in background with ID: (\w+)"), re.compile(r"Task ID: (\w+)"),
                      re.compile(r"agentId: (\w+)"))
BACKGROUND_ENDED = re.compile(r"<task-id>(\w+)</task-id>.*?<status>(\w+)</status>", re.S)
STOPPED = re.compile(r"stopped task: (\w+)")
WRITE_VERB = re.compile(
    r"(?:^|_)(create|update|delete|add|save|write|set|post|send|comment|merge|close|upload|move|remove|edit|publish|batch)"
    r"(?:_|$)", re.I)
REMOTE_WRITE_ACTIONS = {"create", "update", "run", "create_webhook_trigger", "delete"}
WRITE_TOOLS = {"Edit", "Write", "NotebookEdit"}
MAX_TEXT = 500
# Common credential shapes, masked in every text the inventory prints.
SECRET = re.compile(
    r"(?i)(authorization:\s*(?:bearer\s+|basic\s+|token\s+)?|bearer\s+"
    r"|(?:api[_-]?key|token|password|secret)[\"']?\s*[=:]\s*[\"']?)[^\s\"',;]+"
    r"|\bsk-[A-Za-z0-9_-]{12,}|\bgh[pousr]_[A-Za-z0-9]{20,}|\bgithub_pat_[A-Za-z0-9_]{20,}|\bxox[abprs]-[A-Za-z0-9-]{10,}"
    r"|\bAKIA[0-9A-Z]{16}\b|\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")


def _content(entry: dict) -> list:
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    return content if isinstance(content, list) else []


def _text(value) -> str:
    """Text of a tool result, which is a string or a list of content blocks."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(_text(item.get("text", "")) if isinstance(item, dict) else str(item) for item in value)
    return ""


def _mask(match: re.Match) -> str:
    return (match.group(1) or "") + "[masked]"


def _short(text: str) -> str:
    text = SECRET.sub(_mask, " ".join(text.split()))
    return text if len(text) <= MAX_TEXT else text[:MAX_TEXT - 1] + "…"


def load_entries(paths: list[Path]) -> tuple[list[dict], int]:
    """Entries of all transcript parts in file order, and the count of unreadable lines."""
    entries, bad = [], 0
    for path in paths:
        with path.open(encoding="utf-8") as stream:
            for line in stream:
                try:
                    entry = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                if isinstance(entry, dict):
                    entries.append(entry)
    return entries, bad


def inventory(entries: list[dict]) -> dict:
    """The thread's artifacts and signals, without touching the file system."""
    sessions, bridges, titles, prompts = [], [], [], []
    models, tokens, responses = Counter(), Counter(), {}
    files, memory, git_mutations, external, scratch, corrections = {}, {}, [], [], {}, []
    directories, refs = {}, {}
    started, ended, results = {}, {}, {}
    first = last = None

    for entry in entries:
        kind, ts = entry.get("type"), entry.get("timestamp")
        if ts:
            first = first or ts
            last = ts
        for key in ("sessionId", "session_id"):
            if entry.get(key) and entry[key] not in sessions:
                sessions.append(entry[key])
        if entry.get("cwd"):
            directories.setdefault(entry["cwd"], ts)
        if kind == "bridge-session" and entry.get("bridgeSessionId") not in bridges:
            bridges.append(entry.get("bridgeSessionId"))
        elif kind == "ai-title" and entry.get("aiTitle"):
            titles.append(entry["aiTitle"])
        elif kind == "pr-link" and entry.get("prUrl"):
            refs[entry["prUrl"]] = {"url": entry["prUrl"], "repository": entry.get("prRepository"),
                                    "number": entry.get("prNumber"), "kind": "pull", "first_seen": ts}
        elif kind == "file-history-delta" and entry.get("trackingPath"):
            parent = (entry.get("backup") or {}).get("realParentDir")
            path = entry["trackingPath"]
            full = path if path.startswith("/") or not parent else str(Path(parent) / Path(path).name)
            files.setdefault(full, ts)

        message = entry.get("message")
        if kind == "user" and isinstance(message, dict) and isinstance(message.get("content"), str):
            text = message["content"].strip()
            if text and not entry.get("isMeta") and not text.startswith("<"):
                prompts.append({"ts": ts, "text": _short(text)})
        # Completion notices arrive in user messages or as queued operations.
        notices = [entry.get("content"), message.get("content") if isinstance(message, dict) else None]
        notices += [block.get("text") for block in _content(entry) if isinstance(block, dict)]
        for notice in notices:
            if isinstance(notice, str) and "<task-id>" in notice:
                for task_id, status in BACKGROUND_ENDED.findall(notice):
                    ended[task_id] = {"status": status, "ts": ts}
        if kind == "assistant" and isinstance(message, dict):
            key = message.get("id") or entry.get("requestId") or entry.get("uuid")
            if key not in responses:
                responses[key] = True
                models[message.get("model") or "unknown"] += 1
                usage = message.get("usage") or {}
                for field in ("input_tokens", "output_tokens", "cache_read_input_tokens",
                              "cache_creation_input_tokens"):
                    tokens[field] += usage.get(field) or 0

        for block in _content(entry):
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and kind == "assistant":
                text = block.get("text") or ""
                for match in CORRECTION.finditer(text):
                    start = max(0, match.start() - 160)
                    corrections.append({"ts": ts, "snippet": _short(text[start:match.end() + 200])})
            elif block.get("type") == "tool_use":
                name, data = block.get("name") or "", block.get("input") or {}
                _record_tool(name, data, ts, block.get("id"), entry.get("cwd"), files, memory, git_mutations,
                             external, scratch, directories, started)
            elif block.get("type") == "tool_result":
                text = _text(block.get("content"))
                results[block.get("tool_use_id")] = text
                for url in GITHUB_REF.finditer(text):
                    refs.setdefault(url.group(0), {"url": url.group(0), "repository": url.group(1),
                                                   "number": int(url.group(3)), "kind": url.group(2),
                                                   "first_seen": ts})
                for task_id in STOPPED.findall(text):
                    ended[task_id] = {"status": "stopped", "ts": ts}

    background = []
    for tool_use_id, task in started.items():
        text = results.get(tool_use_id, "")
        task_ids = [match.group(1) for pattern in BACKGROUND_STARTED for match in pattern.finditer(text)]
        for task_id in task_ids or [None]:
            end = ended.get(task_id) if task_id else None
            background.append({**task, "id": task_id, "ended": end})
    return {
        "sessions": sessions,
        "bridge_sessions": [b for b in bridges if b],
        "title": titles[-1] if titles else None,
        "first": first,
        "last": last,
        "models": dict(models),
        "responses": len(responses),
        "tokens": dict(tokens),
        "prompts": prompts,
        "directories": sorted(directories),
        "files_written": sorted(files),
        "memory_writes": sorted(memory),
        "git_mutations": git_mutations,
        "github_refs": sorted(refs.values(), key=lambda ref: (ref["repository"] or "", ref["number"] or 0)),
        "external_writes": external,
        "background_tasks": background,
        "scratchpad_paths": sorted(scratch),
        "corrections": corrections,
    }


def _record_tool(name, data, ts, tool_use_id, cwd, files, memory, git_mutations, external, scratch,
                 directories, started) -> None:
    if name in WRITE_TOOLS:
        path = data.get("file_path") or data.get("notebook_path")
        if path:
            files.setdefault(path, ts)
            if "/memory/" in path or path.endswith("MEMORY.md"):
                memory.setdefault(path, ts)
            for match in SCRATCHPAD.finditer(path):
                scratch.setdefault(match.group(1), ts)
    elif name == "Bash":
        command = data.get("command") or ""
        if GIT_MUTATION.search(command) or GH_MUTATION.search(command):
            git_mutations.append({"ts": ts, "command": _short(command)})
        for match in SCRATCHPAD.finditer(command):
            scratch.setdefault(match.group(1).rstrip(".,"), ts)
        for match in CD_PATH.finditer(command):
            path = os.path.expanduser(match.group(2))
            if not path.startswith("/") and cwd:
                path = os.path.normpath(os.path.join(cwd, path))
            if path.startswith("/"):
                directories.setdefault(path, ts)
        if data.get("run_in_background"):
            started[tool_use_id] = {"tool": name, "ts": ts, "description": _short(data.get("description") or command)}
    elif name in ("Monitor", "Workflow") or (name == "Agent" and data.get("run_in_background")):
        started[tool_use_id] = {"tool": name, "ts": ts,
                                "description": _short(data.get("description") or data.get("name") or "")}
    elif name in ("ScheduleWakeup", "CronCreate"):
        external.append({"ts": ts, "tool": name, "summary": _short(json.dumps(data, ensure_ascii=False))})
    elif name == "RemoteTrigger" and data.get("action") in REMOTE_WRITE_ACTIONS:
        external.append({"ts": ts, "tool": name,
                         "summary": f"{data.get('action')} {data.get('trigger_id') or ''}".strip()})
    elif name in ("Artifact", "ArtifactData") and (data.get("action") or "publish") not in ("read", "list", "get",
                                                                                              "query", "open",
                                                                                              "quickstart"):
        external.append({"ts": ts, "tool": name, "summary": _short(
            f"{data.get('action') or 'publish'} {data.get('url') or data.get('file_path') or ''}".strip())})
    elif name.startswith("mcp__") and WRITE_VERB.search(name.rsplit("__", 1)[-1]):
        external.append({"ts": ts, "tool": name, "summary": _short(json.dumps(data, ensure_ascii=False))})


def git_roots(directories: list[str]) -> list[str]:
    """Distinct git work trees among the directories that still exist."""
    roots = []
    for directory in directories:
        if not os.path.isdir(directory):
            continue
        result = subprocess.run(["git", "-C", directory, "rev-parse", "--show-toplevel"],
                                capture_output=True, text=True, check=False)
        root = result.stdout.strip()
        if result.returncode == 0 and root and root not in roots:
            roots.append(root)
    return roots


def config_dirs() -> list[Path]:
    dirs = []
    if os.environ.get("CLAUDE_CONFIG_DIR"):
        dirs.append(Path(os.environ["CLAUDE_CONFIG_DIR"]).expanduser())
    dirs += sorted(Path.home().glob(".claude*"))
    return [d for d in dict.fromkeys(dirs) if (d / "projects").is_dir()]


def find_session(session: str) -> list[Path]:
    if not re.fullmatch(r"[0-9a-fA-F-]{8,64}", session):
        raise SystemExit("session id must be a UUID")
    return [path for base in config_dirs() for path in (base / "projects").glob(f"*/{session}.jsonl")]


def project_dir_name(directory: str) -> str:
    return re.sub(r"[^A-Za-z0-9]", "-", str(Path(directory).resolve()))


def latest_in_project(directory: str) -> tuple[Path | None, list[str]]:
    candidates = [path for base in config_dirs()
                  for path in (base / "projects" / project_dir_name(directory)).glob("*.jsonl")]
    if not candidates:
        return None, []
    candidates.sort(key=lambda path: path.stat().st_mtime, reverse=True)
    recent = [str(path) for path in candidates[1:] if time.time() - path.stat().st_mtime < 3600]
    return candidates[0], recent


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--session", help="session UUID")
    source.add_argument("--transcript", action="append", type=Path, help="transcript part, repeatable")
    source.add_argument("--project-dir", help="working directory of the project, with --latest")
    parser.add_argument("--latest", action="store_true", help="with --project-dir: the newest transcript")
    args = parser.parse_args(argv)

    notes = []
    if args.session:
        paths = find_session(args.session)
    elif args.transcript:
        paths = args.transcript
    else:
        if not args.latest:
            parser.error("--project-dir needs --latest")
        newest, recent = latest_in_project(args.project_dir)
        paths = [newest] if newest else []
        if recent:
            notes.append("other transcripts of this project changed in the last hour; confirm the session: "
                         + ", ".join(recent))
    if not paths:
        print(json.dumps({"error": "transcript not found"}))
        return 1
    entries, bad = load_entries(paths)
    result = inventory(entries)
    result["transcripts"] = [str(path) for path in paths]
    result["unreadable_lines"] = bad
    result["git_roots"] = git_roots(result["directories"])
    result["notes"] = notes
    json.dump(result, sys.stdout, ensure_ascii=False, indent=1)
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
