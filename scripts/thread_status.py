#!/usr/bin/env python3
"""Lifecycle context and supported Claude SDK writes for emoji thread titles."""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import importlib.util
import json
import os
from pathlib import Path
import re
import sys
import time
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


def _main_event(event: object) -> bool:
    return (isinstance(event, dict) and not event.get("agent_id")
            and not event.get("is_sidechain") and not event.get("is_background_agent"))


def hook_output(event: dict, client: str, *, script: str | None = None) -> dict:
    """Return only fields that the selected client's lifecycle event supports."""
    if not _main_event(event):
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
                    "sessionTitle output, and cannot itself invoke a model-only tool. "
                    "The Interrupt adapter can persist a stopped title through the public "
                    "App Server metadata API; it does not restart the interrupted model.")
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


def rename_codex_session(session_id: str, status: str, *, timeout: float = 2.5) -> dict:
    """Persist metadata through Codex's API without resuming or starting a turn."""
    try:
        from agents_core_codex_titles import rename_codex_thread
    except ModuleNotFoundError as error:
        if error.name != "agents_core_codex_titles":
            raise
        from src.codex_thread_titles import rename_codex_thread
    return rename_codex_thread(session_id, status, status_title, timeout=timeout)


@contextmanager
def _turn_record(directory: Path, session_id: str, timeout: float):
    """Serialize our interrupt write with the next prompt; never lock client files."""
    if str(uuid.UUID(session_id)) != session_id.lower():
        raise ValueError("A canonical current-session UUID is required")
    if any(path.is_symlink() for path in (directory, *directory.parents)):
        raise ValueError("Refusing a symbolic link in thread-status state")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (session_id + ".json")
    if path.is_symlink():
        raise ValueError("Refusing a symbolic link for a thread-status record")
    descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(descriptor, "r+b") as stream:
        if stream.seek(0, 2) == 0:
            stream.write(b" ")
            stream.flush()
        deadline = time.monotonic() + timeout
        while True:
            try:
                if sys.platform == "win32":
                    import msvcrt
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except (BlockingIOError, OSError):
                if time.monotonic() >= deadline:
                    raise RuntimeError("Thread-status turn record is busy") from None
                time.sleep(0.01)
        try:
            yield stream
        finally:
            if sys.platform == "win32":
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def handle_hook_event(event: object, client: str, *, state_directory: Path | None = None) -> dict:
    if (_main_event(event) and client == "codex"
            and event.get("hook_event_name") in ("UserPromptSubmit", "Interrupt")
            and _known_identity(event.get("session_id")) is not None
            and _known_identity(event.get("turn_id")) is not None):
        interrupted = event["hook_event_name"] == "Interrupt"
        directory = state_directory or Path(__file__).resolve().parent / "agents-core-thread-status-state"
        # Interrupt hooks have a three-second maximum; a prompt can wait longer
        # for the previous interrupt write, before its model is allowed to start.
        with _turn_record(directory, event["session_id"], 0.15 if interrupted else 2.5) as record:
            if not interrupted:
                record.seek(0)
                record.truncate()
                record.write(json.dumps({"turn_id": event["turn_id"]}).encode("utf-8"))
                record.flush()
            else:
                record.seek(0)
                try:
                    current = json.loads(record.read(1024))
                except (ValueError, UnicodeError):
                    return {}
                if not isinstance(current, dict) or current.get("turn_id") != event["turn_id"]:
                    return {}  # A newer prompt already owns this chat.
                rename_codex_session(event["session_id"], "stopped", timeout=2.0)
                return {"systemMessage": "Stopped: title saved as 🛑; live UI refresh is unverified."}
    return hook_output(event, client)


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
            if args.client == "codex":
                if not args.session_id:
                    raise ValueError("Codex metadata writes require --session-id")
                result = rename_codex_session(args.session_id, args.set_status)
            elif args.client == "claude":
                if not args.session_id or not args.directory:
                    raise ValueError("Claude writes require --session-id and --directory")
                result = rename_claude_session(args.session_id, args.directory, args.set_status)
            else:
                raise ValueError("Use this client's native rename capability; this helper has no title writer")
        else:
            raw = sys.stdin.buffer.read(MAX_EVENT_BYTES + 1)
            if len(raw) > MAX_EVENT_BYTES:
                raise ValueError("Hook input exceeds its byte limit")
            event = json.loads(raw.decode("utf-8"))
            result = handle_hook_event(event, args.client)
    except (OSError, ValueError, RuntimeError, UnicodeError, TypeError) as error:
        # A hook must never block the user's work. An explicit write must fail visibly.
        print("Thread status: " + str(error), file=sys.stderr)
        print("{}")
        return 1 if args.set_status else 0
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
