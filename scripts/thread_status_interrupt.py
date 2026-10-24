#!/usr/bin/env python3
"""Stopped status for Claude desktop Code tabs after the user presses stop.

Claude Code runs no hook for a user interrupt (its Stop event skips it), and only the
model can rename a desktop chat. The UserPromptSubmit handler ``--watch`` therefore
runs as an ``asyncRewake`` background hook. It follows the turn's transcript and exits 2
when Claude Code records the interrupt of this prompt. That exit wakes Claude for a
turn whose first job is setting the stopped title.

The Stop handler ``--done`` only records where the turn stopped. Another Stop hook can
block that stop and continue the same prompt, so the watcher keeps following the turn:
it retires when no record of the prompt follows the stop within ``STOP_LINGER_SECONDS``.

One locked record per session (``flock`` on POSIX, ``msvcrt.locking`` on Windows)
serializes registration, Stop records and the wake claim. A watcher that starts after
a newer prompt registered, or after the transcript moved on to a newer prompt, neither
replaces it nor wakes, and each prompt wakes at most once, however many watchers run.

Every other path exits 0, because exit 2 is the wake-up signal. The watcher acts only
in the desktop app, from the Claude Code version whose asyncRewake handling is
verified, and never for subagents. It reads only the transcript's user records.
"""
from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import re
import sys
import time

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None
try:
    import msvcrt
except ImportError:  # POSIX
    msvcrt = None

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
# After a Stop, wait this long for the turn to continue (another Stop hook blocked the
# stop and may itself run for minutes) before treating the stop as the turn's end.
STOP_LINGER_SECONDS = 600.0
# In a chat's first turn the hook can start before Claude Code creates the transcript.
TRANSCRIPT_WAIT_SECONDS = 120.0
# The Stop handler's own timeout is five seconds; registration and claims are short.
LOCK_SECONDS = 2.0
# Superseded prompts kept so that a late watcher of one can never become current again.
HISTORY = 32
STOPS = 8
MAX_RECORD_BYTES = 65536
# An immediate stop can precede the watcher's start, so begin slightly before the end.
LOOKBACK_BYTES = 65536
MAX_READ_BYTES = 1 << 20
# Longer unterminated text cannot be the short interrupt record (a large tool result).
MAX_PARTIAL_BYTES = 4 << 20
WAKE = 2
REMINDER = (
    "Agents-Core thread status: the user stopped the previous turn (a confirmed interrupt). "
    "The interrupt hook started this turn; it is not a user request. First make exactly one "
    "call: mcp__ccd_session_mgmt__set_session_title with session_id \"self\" and the current title "
    "body prefixed by 🛑 (load the tool with ToolSearch first only if it is deferred). If this turn "
    "also carries a user message, for example one sent right after stop, handle that message "
    "normally after the title call, as you would any user request, including its thread status. "
    "Otherwise end the turn after the title call with no text at all: no reply, summary, footer or "
    "log_interaction, and ignore guidance to mark this turn working."
)


class _Busy(Exception):
    """Another hook held the session record for longer than this one waits."""


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


def _transcript(event: dict) -> Path | None:
    value = event.get("transcript_path")
    if not isinstance(value, str) or not Path(value).is_absolute():
        return None
    return Path(value)


def _interrupted(record: dict) -> bool:
    message = record.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    # A typed prompt is a string; the interrupt record is a single text block.
    return (isinstance(content, list) and len(content) == 1 and isinstance(content[0], dict)
            and content[0].get("type") == "text"
            and str(content[0].get("text", "")).startswith(INTERRUPT_MARKER))


def interrupt_record(line: str | bytes, prompt: str) -> bool:
    """Claude Code's own interrupt record: one text item, bound to the prompt's ID."""
    try:
        record = json.loads(line)
    except ValueError:
        return False
    return (isinstance(record, dict) and record.get("type") == "user" and not record.get("isSidechain")
            and record.get("promptId") == prompt and _interrupted(record))


class _Follower:
    """Complete transcript records from a byte offset, and what they say about one prompt.

    Claude Code stamps every main-thread user record with the current prompt's ID, so the
    transcript also orders prompts: another prompt's record after ours means a newer one.
    """

    def __init__(self, path: Path, prompt: str, offset: int = 0):
        self.path, self.prompt = path, prompt
        self._reset(offset)

    def _reset(self, offset: int) -> None:
        self.offset, self.partial, self.skip = offset, b"", offset > 0
        self.own_end = None   # end of this prompt's latest user record
        self.activity = -1    # start of its latest user record other than an interrupt
        self.interrupt = None  # end of its latest interrupt record
        self.newer = False    # another prompt's user record followed one of ours

    def read(self) -> bool:
        """Consume up to ``MAX_READ_BYTES`` of new records; return whether any bytes arrived."""
        size = self.path.stat().st_size
        if size < self.offset:
            # A rewritten, shorter transcript: rescan it; matches are bound to this prompt.
            self._reset(0)
        if size == self.offset:
            return False
        with self.path.open("rb") as stream:
            stream.seek(self.offset)
            chunk = stream.read(MAX_READ_BYTES)
        if not chunk:
            return False
        position = self.offset - len(self.partial)
        self.offset += len(chunk)
        lines = (self.partial + chunk).split(b"\n")
        self.partial = lines.pop()
        for line in lines:
            start, position = position, position + len(line) + 1
            if self.skip:
                self.skip = False  # the lookback, or a dropped oversized record, began mid-record
            else:
                self._consume(line, start, position)
        if len(self.partial) > MAX_PARTIAL_BYTES:
            # Drop a huge record up to its newline instead of buffering it whole.
            self.partial, self.skip = b"", True
        return True

    def _consume(self, line: bytes, start: int, end: int) -> None:
        if b"promptId" not in line:
            return  # assistant and system records carry no prompt ID
        try:
            record = json.loads(line)
        except ValueError:
            return
        if not isinstance(record, dict) or record.get("type") != "user" or record.get("isSidechain"):
            return
        prompt = record.get("promptId")
        if prompt == self.prompt:
            self.own_end = end
            if _interrupted(record):
                self.interrupt = end
            else:
                self.activity = start
        elif isinstance(prompt, str) and self.own_end is not None:
            self.newer = True


def _state(directory: Path, *, create: bool = True) -> Path | None:
    if any(path.is_symlink() for path in (directory, *directory.parents)):
        return None
    if create:
        directory.mkdir(mode=0o700, exist_ok=True)
    return directory if directory.is_dir() else None


def _lock(stream, *, release: bool = False) -> None:
    stream.seek(0)  # msvcrt locks bytes from the current position
    if fcntl is not None:
        fcntl.flock(stream.fileno(), fcntl.LOCK_UN if release else fcntl.LOCK_EX | fcntl.LOCK_NB)
    elif msvcrt is not None:
        msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK if release else msvcrt.LK_NBLCK, 1)
    else:
        raise OSError("No file locking on this platform")


@contextmanager
def _locked(state: Path, session: str, *, create: bool = True, timeout: float | None = None):
    """Hold the session record's OS lock; yield its JSON object and write it back if changed.

    Every reader takes the lock too: Windows byte-range locks block other handles' reads.
    """
    path = state / f"{session}.json"
    if path.is_symlink():
        raise OSError(f"Refusing a linked session record: {path}")
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    with os.fdopen(os.open(path, flags | (os.O_CREAT if create else 0), 0o600), "r+b") as stream:
        deadline = time.monotonic() + (LOCK_SECONDS if timeout is None else timeout)
        while True:
            try:
                _lock(stream)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise _Busy from None
                time.sleep(0.01)
        try:
            stream.seek(0)
            try:
                record = json.loads(stream.read(MAX_RECORD_BYTES) or b"{}")
            except ValueError:
                record = {}
            record = record if isinstance(record, dict) else {}
            before = json.dumps(record, sort_keys=True)
            yield record
            if json.dumps(record, sort_keys=True) != before:
                stream.seek(0)
                stream.truncate()
                stream.write(json.dumps(record).encode("utf-8"))
                stream.flush()
        finally:
            try:
                _lock(stream, release=True)
            except OSError:
                pass  # closing the file releases the lock as well


def _current(record: dict) -> tuple[str | None, int]:
    """The registered prompt and the transcript size when it registered."""
    prompt, size = record.get("prompt"), record.get("size")
    return (prompt if isinstance(prompt, str) else None), (size if type(size) is int else 0)


def _history(record: dict) -> list[str]:
    history = record.get("history")
    return [item for item in history if isinstance(item, str)] if isinstance(history, list) else []


def _stops(record: dict) -> dict[str, int]:
    stops = record.get("stops")
    if not isinstance(stops, dict):
        return {}
    return {key: value for key, value in stops.items() if isinstance(key, str) and type(value) is int}


def _register(record: dict, prompt: str, size: int, follower: _Follower) -> bool:
    """Make the prompt current unless the record or the transcript shows a newer one."""
    current, registered = _current(record)
    if current == prompt:
        return True  # another watcher of this prompt: they share its single wake
    history = _history(record)
    if prompt in history:
        return False
    # Our records followed by another prompt's, or written before the current prompt
    # registered: that prompt is newer and this watcher started late.
    if follower.newer or (current is not None and follower.own_end is not None
                          and follower.own_end <= registered):
        record["history"] = (history + [prompt])[-HISTORY:]
        return False
    if current is not None:
        history.append(current)
    # Keep a Stop that arrived before this watcher did; drop the older prompts' stops.
    pending = {key: value for key, value in _stops(record).items() if key == prompt}
    record.update(prompt=prompt, size=size, woken=False, history=history[-HISTORY:], stops=pending)
    return True


def _wake() -> int:
    # UTF-8 bytes, whatever the platform's pipe encoding (the emoji matters).
    sys.stderr.buffer.write(REMINDER.encode("utf-8") + b"\n")
    sys.stderr.flush()
    return WAKE


def done(event: object, state_directory: Path, *, environ=os.environ) -> int:
    """Record where the prompt's turn stopped; another Stop hook may still continue it."""
    identities = _identities(event)
    if identities is None or not supported(environ):
        return 0
    path = _transcript(event)
    session, prompt = identities
    try:
        if path is None or not path.is_file():
            return 0
        size = path.stat().st_size
        state = _state(state_directory, create=False)
        if state is None:
            return 0  # no watcher has run here
        with _locked(state, session, create=False) as record:
            if prompt not in _history(record):
                stops = _stops(record)
                stops.pop(prompt, None)
                stops[prompt] = size
                record["stops"] = dict(list(stops.items())[-STOPS:])
    except (OSError, _Busy):
        pass
    return 0


def watch(event: object, state_directory: Path, *, environ=os.environ, poll: float = POLL_SECONDS,
          settle: float = SETTLE_SECONDS, seconds: float = WATCH_SECONDS,
          linger: float = STOP_LINGER_SECONDS, appear: float = TRANSCRIPT_WAIT_SECONDS,
          sleep=time.sleep, clock=time.monotonic) -> int:
    identities = _identities(event)
    if identities is None or not supported(environ):
        return 0
    path = _transcript(event)
    if path is None or (path.exists() and not path.is_file()):
        return 0
    session, prompt = identities
    started = clock()
    try:
        state = _state(state_directory)
        if state is None:
            return 0
        # Register against a snapshot of the transcript's end, under the session lock.
        with _locked(state, session) as record:
            size = path.stat().st_size if path.is_file() else 0
            follower = _Follower(path, prompt, max(0, size - LOOKBACK_BYTES))
            if size:
                follower.read()
            if not _register(record, prompt, size, follower):
                return 0
        settled = noticed = None
        while clock() < started + seconds:
            if not path.is_file():
                if clock() >= started + appear:
                    return 0  # the first turn's transcript never appeared
                try:
                    with _locked(state, session) as record:
                        if _current(record)[0] != prompt:
                            return 0
                except _Busy:
                    pass
                sleep(poll)
                continue
            progressed = follower.read()
            if follower.newer:
                return 0  # a newer prompt's records follow ours
            claimed = wake = False
            stop = None
            try:
                with _locked(state, session) as record:
                    if _current(record)[0] != prompt or record.get("woken"):
                        return 0  # a newer prompt started, or this prompt already woke
                    stop = _stops(record).get(prompt)
                    # An interrupt counts unless a Stop recorded after it ended the turn normally.
                    wake = follower.interrupt is not None and (stop is None or stop < follower.interrupt)
                    if wake and settled == follower.interrupt:
                        record["woken"] = True  # the one-shot claim, written before the wake
                        claimed = True
            except _Busy:
                sleep(poll)
                continue
            if claimed:
                return _wake()
            if wake:
                # Let a prompt typed right after stop supersede this watcher first.
                sleep(settle)
                settled = follower.interrupt
                continue
            if stop is not None and follower.activity < stop:
                # Stopped, and nothing of this prompt followed yet: wait whether it continues.
                if noticed is None or noticed[0] != stop:
                    noticed = (stop, clock())
                elif clock() - noticed[1] >= linger:
                    return 0  # the turn ended normally
            else:
                noticed = None
            if not progressed:
                sleep(poll)
        return 0
    except (OSError, _Busy):
        return 0


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
