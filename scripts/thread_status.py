#!/usr/bin/env python3
"""Lifecycle context and supported Claude SDK writes for emoji thread titles."""
from __future__ import annotations

import time

# Start before other imports and stdin parsing so they consume the hook budget.
_HOOK_STARTED_AT = time.monotonic()
_INTERRUPT_LIMIT = 3.0
_INTERRUPT_EXIT_MARGIN = 0.3
_INTERRUPT_WRITER_CAP = 2.4

import argparse
from contextlib import contextmanager
import importlib.util
import json
import os
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
# The desktop app launches its Code-tab sessions with this entrypoint. Its sidebar
# title lives in the app's session store. Prefer its native title tools rather
# than depending on CLI-side metadata writes being reflected in that store.
DESKTOP_ENTRYPOINT = "claude-desktop"
DESKTOP_TOOLS = ("mcp__ccd_session_mgmt__get_session", "mcp__ccd_session_mgmt__set_session_title")


def _known_identity(value: object) -> str | None:
    return value if isinstance(value, str) and _IDENTITY.fullmatch(value) else None


def _claude_desktop() -> bool:
    return os.environ.get("CLAUDE_CODE_ENTRYPOINT") == DESKTOP_ENTRYPOINT


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
    desktop = client == "claude" and _claude_desktop()
    if desktop:
        context += ("\nClaude desktop app (Code tab): the app owns this chat's sidebar title. Read it with "
                    f"{DESKTOP_TOOLS[0]} and rename it with {DESKTOP_TOOLS[1]}, both with "
                    "session_id \"self\". If they are deferred, load both once with ToolSearch "
                    "\"select:" + ",".join(DESKTOP_TOOLS) + "\". Hook sessionTitle and the SDK helper "
                    "write CLI metadata; use the native app tools for the sidebar. If the app declines or asks "
                    "the user to approve a rename, report that once instead of retrying.")
    elif client == "claude":
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
        if client == "claude":
            # Instruction files load once per session; a per-turn line keeps phase chapters in view.
            context += ("\nIf the client exposes a chapter tool (the Claude desktop app's mark_chapter) "
                        "and this request moves the work to a meaningfully different phase of the "
                        "session, such as from implementation to an incident or a cleanup, mark the "
                        "chapter first. Never mark one for the session's first message.")
    if client == "cursor":
        return {"additional_context": context}
    output = {"hookEventName": name, "additionalContext": context}
    # session_title is an optional custom title; never substitute the raw prompt.
    if client == "claude" and not desktop and name == "UserPromptSubmit" and event.get("session_title"):
        try:
            # A prompt may only ask for status. Let the model decide whether it
            # actually reopens a paused/finished/waiting task.
            if managed_status(event["session_title"]) in (None, "unknown", "working"):
                output["sessionTitle"] = status_title(event["session_title"], "working")
        except ValueError:
            pass  # No native title write when its title is missing or malformed.
    return {"hookSpecificOutput": output}


def rename_codex_session(session_id: str, status: str, *, timeout: float = 2.5,
                         expected_turn_id: str | None = None, recorded_turn_id: str | None = None) -> dict:
    """Persist metadata through Codex's API without resuming or starting a turn."""
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not 0 < timeout < float("inf")):
        raise ValueError("The Codex title timeout must be finite and positive")
    deadline = time.monotonic() + timeout
    try:
        from agents_core_codex_titles import rename_codex_thread
    except ModuleNotFoundError as error:
        if error.name != "agents_core_codex_titles":
            raise
        from src.codex_thread_titles import rename_codex_thread
    timeout = deadline - time.monotonic()
    if timeout <= 0:
        raise RuntimeError("Codex title deadline exhausted while loading adapter")
    return rename_codex_thread(session_id, status, status_title, timeout=timeout,
                               expected_turn_id=expected_turn_id, recorded_turn_id=recorded_turn_id)


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


def _read_record(record) -> dict:
    record.seek(0)
    try:
        current = json.loads(record.read(4096))
    except (ValueError, UnicodeError):
        return {}
    return current if isinstance(current, dict) else {}


def _write_record(record, current: dict) -> None:
    record.seek(0)
    record.truncate()
    record.write(json.dumps(current).encode("utf-8"))
    record.flush()


def handle_hook_event(event: object, client: str, *, state_directory: Path | None = None,
                      started_at: float | None = None) -> dict:
    started_at = time.monotonic() if started_at is None else started_at
    if (_main_event(event) and client == "codex"
            and event.get("hook_event_name") in ("UserPromptSubmit", "Interrupt")
            and _known_identity(event.get("session_id")) is not None
            and _known_identity(event.get("turn_id")) is not None):
        interrupted = event["hook_event_name"] == "Interrupt"
        directory = state_directory or Path(__file__).resolve().parent / "agents-core-thread-status-state"
        # Interrupt hooks have a three-second maximum; a prompt can wait longer
        # for the previous interrupt write, before its model is allowed to start.
        with _turn_record(directory, event["session_id"], 0.15 if interrupted else _INTERRUPT_LIMIT) as record:
            current = _read_record(record)
            if not interrupted:
                # Keep the last receipt available after the user resumes work.
                current["turn_id"] = event["turn_id"]
                _write_record(record, current)
            else:
                # Stop can precede UserPromptSubmit during context setup. The
                # public turn metadata, rather than this optional prompt record,
                # must establish whether the interrupted turn still owns the title.
                receipt = {"turn_id": event["turn_id"], "received_at": time.time(),
                           "outcome": "running", "persisted": False}
                current["last_interrupt"] = receipt
                _write_record(record, current)
                try:
                    timeout = min(_INTERRUPT_WRITER_CAP,
                                  _INTERRUPT_LIMIT - _INTERRUPT_EXIT_MARGIN - (time.monotonic() - started_at))
                    if timeout <= 0:
                        raise RuntimeError("Codex Interrupt deadline exhausted before title update")
                    result = rename_codex_session(event["session_id"], "stopped", timeout=timeout,
                                                  expected_turn_id=event["turn_id"],
                                                  recorded_turn_id=current.get("turn_id"))
                    if result.get("persisted") is not True:
                        receipt.update(outcome="skipped", reason=result.get("reason", "unconfirmed"))
                        _write_record(record, current)
                        return {}
                except (OSError, ValueError, RuntimeError, UnicodeError, TypeError) as error:
                    receipt.update(outcome="failed", error_type=type(error).__name__, error=str(error)[:240])
                    _write_record(record, current)
                    raise
                receipt.update(outcome="persisted", persisted=True, live_ui_refresh_verified=False)
                _write_record(record, current)
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
            result = handle_hook_event(event, args.client, started_at=_HOOK_STARTED_AT)
    except (OSError, ValueError, RuntimeError, UnicodeError, TypeError) as error:
        # A hook must never block the user's work. An explicit write must fail visibly.
        print("Thread status: " + str(error), file=sys.stderr)
        print("{}")
        return 1 if args.set_status else 0
    print(json.dumps(result, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
