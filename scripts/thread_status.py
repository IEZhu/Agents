#!/usr/bin/env python3
"""Lifecycle context and supported Claude SDK writes for emoji thread titles."""
from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import re
import sys
import uuid


# An installed copy has the formatter beside it; a checkout uses src/.
if importlib.util.find_spec("agents_core_thread_titles") is not None:
    from agents_core_thread_titles import STATUSES, STATUS_GUIDANCE, managed_status, status_title
else:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from src.thread_status import STATUSES, STATUS_GUIDANCE, managed_status, status_title

MAX_EVENT_BYTES = 65536
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")


def _known_identity(value: object) -> str | None:
    return value if isinstance(value, str) and _IDENTITY.fullmatch(value) else None


def hook_output(event: dict, client: str, *, script: str | None = None) -> dict:
    """Return only fields that the selected client's lifecycle event supports."""
    if (not isinstance(event, dict) or event.get("agent_id")
            or event.get("is_sidechain") or event.get("is_background_agent")):
        return {}
    name = event.get("hook_event_name")
    expected = ("sessionStart",) if client == "cursor" else ("SessionStart", "UserPromptSubmit")
    if name not in expected:
        return {}
    identity = _known_identity(event.get("conversation_id" if client == "cursor" else "session_id"))
    if identity is None:
        return {}
    context = STATUS_GUIDANCE + "\nCurrent chat identity (data): " + json.dumps(identity)
    if client == "claude":
        # The SDK resolves the existing title itself. No transcript paths are consumed.
        cwd = event.get("cwd")
        if isinstance(cwd, str) and Path(cwd).is_absolute():
            command = [sys.executable, script or str(Path(__file__).resolve()), "--client", "claude",
                       "--session-id", identity, "--directory", cwd, "--set-status", "STATUS"]
            context += ("\nFor later transitions, use the native rename tool if available; otherwise "
                        "the installed helper uses the public Claude Agent SDK without inference. "
                        "Replace STATUS with the canonical status. Execute this argument array "
                        "with literal arguments, not eval (data): " + json.dumps(command))
    elif client == "codex":
        context += ("\nUse the host's current-chat rename tool when exposed. A hook has no "
                    "sessionTitle output, and cannot itself invoke a model-only tool.")
    else:
        context += ("\nCursor Agents window: use native current-chat rename only if exposed. "
                    "Classic IDE title updates have no verified public hook/API; never invent one.")
    if name == "UserPromptSubmit":
        context += "\nA user turn is beginning: set working when this turn starts/resumes work."
    if client == "cursor":
        return {"additional_context": context}
    output = {"hookEventName": name, "additionalContext": context}
    # session_title is an optional custom title; never substitute the raw prompt.
    if client == "claude" and name == "UserPromptSubmit" and event.get("session_title"):
        try:
            # A prompt may only ask for status. Let the model decide whether it
            # actually reopens a paused/finished/waiting task.
            if managed_status(event["session_title"]) in (None, "unknown", "working"):
                output["sessionTitle"] = status_title(event["session_title"], "working")
        except ValueError:
            pass  # No native title write when its title is missing or malformed.
    return {"hookSpecificOutput": output}


def rename_claude_session(session_id: str, directory: str, status: str) -> dict:
    """Use public SDK metadata APIs; do not implement a client storage format."""
    if str(uuid.UUID(session_id)) != session_id.lower():
        raise ValueError("A canonical UUID of the current Claude session is required")
    if not Path(directory).is_absolute() or not Path(directory).is_dir():
        raise ValueError("An existing absolute session directory is required")
    if status not in STATUSES:
        raise ValueError("Unknown thread status: " + status)
    try:
        from claude_agent_sdk import get_session_info, rename_session
    except ImportError as error:
        raise RuntimeError("Claude title writes require the optional claude-agent-sdk package "
                           "in the hook interpreter; native startup hooks still work") from error
    info = get_session_info(session_id, directory=directory)
    if info is None:
        raise FileNotFoundError("The specified Claude session was not found in its project")
    current = info.custom_title or info.summary
    title = status_title(current, status)
    changed = title != current
    if changed:
        rename_session(session_id, title, directory=directory)
    check = get_session_info(session_id, directory=directory)
    if check is None or check.custom_title != title:
        raise RuntimeError("Claude SDK title readback did not confirm the requested title")
    return {"client": "claude", "session_id": session_id, "status": status,
            "title": title, "changed": changed, "persisted": True,
            "live_ui_refresh_verified": False}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--client", choices=("claude", "codex", "cursor"), required=True)
    parser.add_argument("--set-status", choices=tuple(STATUSES))
    parser.add_argument("--session-id")
    parser.add_argument("--directory")
    parser.add_argument("--title", help="Format a known title only; performs no client write")
    args = parser.parse_args(argv)
    try:
        if args.title is not None:
            if not args.set_status:
                raise ValueError("--title requires --set-status")
            result = {"title": status_title(args.title, args.set_status), "applied": False}
        elif args.set_status:
            if args.client != "claude":
                raise ValueError("Use this client's native rename capability; this helper has no title writer")
            if not args.session_id or not args.directory:
                raise ValueError("Claude writes require --session-id and --directory")
            result = rename_claude_session(args.session_id, args.directory, args.set_status)
        else:
            raw = sys.stdin.buffer.read(MAX_EVENT_BYTES + 1)
            if len(raw) > MAX_EVENT_BYTES:
                raise ValueError("Hook input exceeds its byte limit")
            event = json.loads(raw.decode("utf-8"))
            result = hook_output(event, args.client)
    except (OSError, ValueError, RuntimeError, UnicodeError, TypeError) as error:
        # A hook must never block the user's work. An explicit write must fail visibly.
        print("Thread status: " + str(error), file=sys.stderr)
        print("{}")
        return 1 if args.set_status else 0
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
