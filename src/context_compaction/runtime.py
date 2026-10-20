"""Cursor hooks: bounded input, owner switch, per-conversation cadence and deduplication.

Only completed stop events can request compaction. No hook returns a follow-up
prompt. A submitted UI action is never reported as a completed compaction.
"""
from __future__ import annotations

import argparse
from contextlib import contextmanager
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time

from .config import config_path, load, refuse_links


@contextmanager
def session_lock(path: Path):
    """Nonblocking OS lock, automatically released even when a hook dies."""
    refuse_links(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    with os.fdopen(fd, "r+b") as stream:
        locked = False
        try:
            if os.name == "nt":
                import msvcrt
                stream.seek(0)
                if not stream.read(1):
                    stream.write(b"0")
                    stream.flush()
                stream.seek(0)
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
        except OSError:
            pass
        try:
            yield locked
        finally:
            if locked:
                if os.name == "nt":
                    stream.seek(0)
                    msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(stream, fcntl.LOCK_UN)


def _save(path: Path, state: dict) -> None:
    # This state is private to one locked session; replace atomically.
    import tempfile
    fd, temporary = tempfile.mkstemp(prefix=path.name, dir=path.parent)
    try:
        with os.fdopen(fd, "w") as stream:
            json.dump(state, stream)
        refuse_links(path)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def handle(payload: dict, *, path=None, adapter=None, now=None) -> dict:
    settings, error = load(path)
    if error or settings["enabled"] is not True:
        return {"status": "skipped", "reason": error or "disabled"}
    if not isinstance(payload, dict):
        return {"status": "skipped", "reason": "invalid_event"}
    event = payload.get("hook_event_name")
    if event != "stop" or payload.get("status") != "completed":
        return {"status": "skipped", "reason": "not_completed_main_turn"}
    if payload.get("is_background_agent") or payload.get("agent_id") or payload.get("subagent_id"):
        return {"status": "skipped", "reason": "subagent"}
    if payload.get("loop_count", 0) != 0:
        return {"status": "skipped", "reason": "automatic_followup"}
    if os.environ.get("CURSOR_CODE_REMOTE", "").lower() == "true":
        return {"status": "skipped", "reason": "remote_workspace"}
    session = payload.get("conversation_id")
    generation = payload.get("generation_id")
    if not all(isinstance(value, str) and 0 < len(value) <= 512 for value in (session, generation)):
        return {"status": "skipped", "reason": "missing_identity"}
    if not any(binding["conversation_id"] == session for binding in settings["cursor_bindings"]):
        return {"status": "skipped", "reason": "unbound_cursor_chat"}
    target = config_path(path)
    digest = hashlib.sha256(session.encode()).hexdigest()
    state_dir = target.parent / "context-compaction-state"
    state_path = state_dir / (digest + ".json")
    current = time.time() if now is None else now
    with session_lock(state_dir / (digest + ".lock")) as locked:
        if not locked:
            return {"status": "skipped", "reason": "busy"}
        refuse_links(state_path)
        try:
            with state_path.open("rb") as stream:
                raw = stream.read(8193)
            if len(raw) > 8192:
                raise ValueError("oversized state")
            state = json.loads(raw)
            if not isinstance(state, dict):
                raise ValueError("invalid state")
        except FileNotFoundError:
            state = {}
        except (OSError, ValueError, RecursionError):
            return {"status": "skipped", "reason": "invalid_state"}
        recent = state.get("recent", [])
        if (not isinstance(recent, list) or len(recent) > 64
                or any(not isinstance(item, str) or len(item) != 64
                       or any(char not in "0123456789abcdef" for char in item) for item in recent)):
            return {"status": "skipped", "reason": "invalid_state"}
        generation_hash = hashlib.sha256(generation.encode()).hexdigest()
        if generation_hash in recent:
            return {"status": "skipped", "reason": "duplicate_turn"}
        turns = state.get("turns", 0)
        last = state.get("attempt_at")
        if (type(turns) is not int or not 0 <= turns <= 1000
                or (last is not None and (type(last) not in (int, float) or not 0 <= last <= 1e15 or not math.isfinite(last)))):
            return {"status": "skipped", "reason": "invalid_state"}
        state.update(recent=(recent + [generation_hash])[-64:], turns=min(turns + 1, 1000))
        _save(state_path, state)
        if state["turns"] < settings["min_turns"]:
            return {"status": "skipped", "reason": "turn_threshold"}
        if last is not None and current - last < settings["cooldown_seconds"]:
            return {"status": "skipped", "reason": "cooldown"}
        latest, changed = load(path)
        if changed or latest != settings:
            return {"status": "skipped", "reason": "config_changed"}
        state.update(attempt_at=current, turns=0, outcome="pending")
        _save(state_path, state)
        if adapter is None:
            from .cursor import compact_cursor
            adapter = lambda event, options: compact_cursor(
                event, options, guard=lambda: load(path) == (settings, None))
        try:
            result = adapter(payload, settings)
            if not isinstance(result, dict) or result.get("status") not in {"skipped", "unsupported", "submitted", "completed", "error"}:
                result = {"status": "error", "reason": "invalid_adapter_result"}
        except Exception:
            result = {"status": "error", "reason": "adapter_failed"}
        state["outcome"] = result["status"]
        _save(state_path, state)
        return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config")
    parser.add_argument("--capture-identity", metavar="ABSOLUTE_PATH",
                        help="Explicit setup diagnostic: write only the stop event's conversation ID; do not compact")
    args = parser.parse_args(argv)
    try:
        raw = sys.stdin.buffer.read(65537)
        if len(raw) > 65536:
            raise ValueError("oversized hook input")
        payload = json.loads(raw)
        if args.capture_identity:
            # This diagnostic is explicitly installed by the owner for binding setup;
            # ordinary disabled hooks never capture identity or write state.
            identity = payload.get("conversation_id") if isinstance(payload, dict) else None
            if (isinstance(identity, str) and 0 < len(identity) <= 512
                    and payload.get("hook_event_name") == "stop"):
                # A diagnostic output is never resolved through the config env
                # override, and must never replace the owner's settings file.
                target = Path(args.capture_identity)
                if not target.is_absolute() or target == config_path(args.config):
                    raise ValueError("identity output must be an absolute separate file")
                refuse_links(target)
                if target.exists():
                    with target.open("rb") as stream:
                        existing_raw = stream.read(1025)
                    if len(existing_raw) > 1024:
                        raise ValueError("oversized diagnostic output")
                    existing = json.loads(existing_raw)
                    if not isinstance(existing, dict) or set(existing) != {"conversation_id"}:
                        raise ValueError("refusing to overwrite an unrelated diagnostic output")
                target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                _save(target, {"conversation_id": identity})
        else:
            handle(payload, path=args.config)
    except (OSError, ValueError, TypeError, RecursionError):
        pass
    # Cursor expects hook JSON, not a prompt or model-visible implementation log.
    print("{}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
