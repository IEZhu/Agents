#!/usr/bin/env python3
"""Stopped status for Claude desktop Code tabs after the user presses stop.

Claude Code runs no hook for a user interrupt (its Stop event skips it), and only the
model can rename a desktop chat. The UserPromptSubmit handler ``--watch`` therefore
runs as an ``asyncRewake`` background hook. It follows the current turn's transcript
and exits 2 when Claude Code records the interrupt of this prompt before Stop. That
exit wakes Claude for one short turn whose only job is setting the stopped title. The
Stop handler ``--done`` marks a normal end, and the watcher then exits quietly.

Every other path exits 0, because exit 2 is the wake-up signal. The watcher acts only
in the desktop app, from the Claude Code version whose asyncRewake handling is
verified, and never for subagents. It reads only transcript records of its own prompt.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import sys
import time

DESKTOP_ENTRYPOINT = "claude-desktop"
# asyncRewake exists before this release; 2.1.287 stops a missing hook script from
# waking Claude repeatedly. Older or unrecognized clients are skipped.
MIN_CLAUDE_CODE = (2, 1, 287)
_VERSION = re.compile(r"claude-code[\\/](\d+)\.(\d+)\.(\d+)(?:[\\/]|$)")
_IDENTITY = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\Z")
INTERRUPT_MARKER = "[Request interrupted by user"
STATE_NAME = "agents-core-thread-status-interrupt-state"
MAX_EVENT_BYTES = 65536
# The installed handler's timeout is six hours; finish before Claude Code cancels it.
WATCH_SECONDS = 6 * 3600 - 60
POLL_SECONDS = 0.5
# A prompt typed right after stop supersedes the watcher and keeps the turn working.
SETTLE_SECONDS = 1.0
# An immediate stop can precede the watcher's start, so begin slightly before the end.
LOOKBACK_BYTES = 65536
MAX_READ_BYTES = 1 << 20
# Longer unterminated text cannot be the short interrupt record (a large tool result).
MAX_PARTIAL_BYTES = 4 << 20
WAKE = 2
REMINDER = (
    "Agents-Core thread status: the user stopped the previous turn (a confirmed interrupt). "
    "This is a silent turn started by the interrupt hook, not a user request. Make exactly one "
    "call: mcp__ccd_session_mgmt__set_session_title with session_id \"self\" and the current title "
    "body prefixed by 🛑 (load the tool with ToolSearch first only if it is deferred). Then end the "
    "turn with no text at all: no reply, summary, footer or log_interaction. Ignore guidance to mark "
    "this turn working, and do not resume or continue the interrupted work."
)


def supported(environ) -> bool:
    """The desktop Code tab's bundled CLI path carries its version."""
    if environ.get("CLAUDE_CODE_ENTRYPOINT") != DESKTOP_ENTRYPOINT:
        return False
    match = _VERSION.search(environ.get("CLAUDE_CODE_EXECPATH", ""))
    return match is not None and tuple(map(int, match.groups())) >= MIN_CLAUDE_CODE


def _identities(event: object) -> tuple[str, str] | None:
    if not isinstance(event, dict) or event.get("agent_id") or event.get("is_sidechain"):
        return None
    session, prompt = event.get("session_id"), event.get("prompt_id")
    if not all(isinstance(value, str) and _IDENTITY.fullmatch(value) for value in (session, prompt)):
        return None
    return session, prompt


def interrupt_record(line: str, prompt: str) -> bool:
    """Claude Code's own interrupt record: one text item, bound to the prompt's ID."""
    try:
        record = json.loads(line)
    except ValueError:
        return False
    if (not isinstance(record, dict) or record.get("type") != "user" or record.get("isSidechain")
            or record.get("promptId") != prompt):
        return False
    content = (record.get("message") or {}).get("content")
    # A typed prompt is a string; the interrupt record is a single text block.
    return (isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
            and content[0].get("type") == "text"
            and str(content[0].get("text", "")).startswith(INTERRUPT_MARKER))


def _state(directory: Path) -> Path | None:
    if any(path.is_symlink() for path in (directory, *directory.parents)):
        return None
    directory.mkdir(mode=0o700, exist_ok=True)
    return directory


def _current(state: Path, session: str) -> str | None:
    try:
        value = json.loads((state / f"{session}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value.get("prompt") if isinstance(value, dict) else None


def _set_current(state: Path, session: str, prompt: str) -> None:
    target = state / f"{session}.json"
    temporary = state / f".{session}.{os.getpid()}.tmp"
    temporary.write_text(json.dumps({"prompt": prompt}), encoding="utf-8")
    os.replace(temporary, target)


def done(event: object, state_directory: Path) -> int:
    identities = _identities(event)
    state = _state(state_directory) if identities else None
    if state is None:
        return 0
    session, prompt = identities
    # A turn the hook woke has no watcher of its own; leave no marker behind for it.
    if _current(state, session) == prompt:
        (state / f"{session}.{prompt}.done").touch()
    return 0


def watch(event: object, state_directory: Path, *, environ=os.environ, poll: float = POLL_SECONDS,
          settle: float = SETTLE_SECONDS, seconds: float = WATCH_SECONDS,
          sleep=time.sleep, clock=time.monotonic) -> int:
    identities = _identities(event)
    if identities is None or not supported(environ):
        return 0
    transcript = event.get("transcript_path")
    if not isinstance(transcript, str) or not Path(transcript).is_absolute() or not Path(transcript).is_file():
        return 0
    state = _state(state_directory)
    if state is None:
        return 0
    session, prompt = identities
    for stale in state.glob(f"{session}.*.done"):
        stale.unlink(missing_ok=True)
    _set_current(state, session, prompt)
    marker = state / f"{session}.{prompt}.done"
    path = Path(transcript)
    offset = max(0, path.stat().st_size - LOOKBACK_BYTES)
    partial, skip_first = "", offset > 0
    deadline = clock() + seconds
    try:
        while clock() < deadline:
            if _current(state, session) != prompt or marker.exists():
                return 0  # a newer prompt started, or this turn ended normally
            size = path.stat().st_size
            if size < offset:
                # A rewritten, shorter transcript: rescan it; matches are bound to this prompt.
                offset, partial, skip_first = 0, "", False
            if size > offset:
                with path.open("rb") as stream:
                    stream.seek(offset)
                    chunk = stream.read(MAX_READ_BYTES)
                if not chunk:
                    sleep(poll)
                    continue
                offset += len(chunk)
                lines = (partial + chunk.decode("utf-8", errors="replace")).split("\n")
                partial = lines.pop()
                if skip_first and lines:
                    lines, skip_first = lines[1:], False
                if len(partial) > MAX_PARTIAL_BYTES:
                    # Drop a huge record up to its newline instead of buffering it whole.
                    partial, skip_first = "", True
                if any(interrupt_record(line, prompt) for line in lines):
                    sleep(settle)
                    if _current(state, session) == prompt and not marker.exists():
                        # UTF-8 bytes, whatever the platform's pipe encoding (the emoji matters).
                        sys.stderr.buffer.write(REMINDER.encode("utf-8") + b"\n")
                        sys.stderr.flush()
                        return WAKE
                    return 0
                continue
            sleep(poll)
        return 0
    finally:
        marker.unlink(missing_ok=True)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mode = argv[0] if len(argv) == 1 else None
    if mode not in ("--watch", "--done"):
        return 0  # never answer a usage error with the wake-up code
    raw = sys.stdin.buffer.read(MAX_EVENT_BYTES + 1)
    if len(raw) > MAX_EVENT_BYTES:
        return 0
    event = json.loads(raw.decode("utf-8"))
    state = Path(__file__).resolve().parent / STATE_NAME
    return watch(event, state) if mode == "--watch" else done(event, state)


if __name__ == "__main__":
    try:
        code = main()
    except Exception:  # noqa: BLE001 - any failure must stay silent; exit 2 would wake Claude
        code = 0
    raise SystemExit(code)
