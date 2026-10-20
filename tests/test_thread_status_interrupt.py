"""Claude desktop stopped-status watcher: interrupt detection, turn lifecycle and safe exits."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("status_interrupt", ROOT / "scripts/thread_status_interrupt.py")
watcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watcher)

SESSION = "550e8400-e29b-41d4-a716-446655440000"
PROMPT = "1f68b279-5fc9-4e77-a6c7-9a8bb3b71971"
DESKTOP = {"CLAUDE_CODE_ENTRYPOINT": "claude-desktop",
           "CLAUDE_CODE_EXECPATH": "/Users/me/Library/Application Support/Claude/claude-code/2.1.299/"
                                   "claude.app/Contents/MacOS/claude"}


def interrupt(prompt=PROMPT, text="[Request interrupted by user]", **extra):
    record = {"parentUuid": "p", "isSidechain": False, "promptId": prompt, "type": "user",
              "message": {"role": "user", "content": [{"type": "text", "text": text}]}, **extra}
    return json.dumps(record) + "\n"


def event(transcript, prompt=PROMPT, **extra):
    return {"hook_event_name": "UserPromptSubmit", "session_id": SESSION, "prompt_id": prompt,
            "transcript_path": str(transcript), **extra}


@pytest.fixture
def transcript(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(json.dumps({"type": "user", "promptId": PROMPT, "message": {"content": "Wait a minute"}}) + "\n")
    return path


def start(transcript, state, prompt=PROMPT, **options):
    """Run one watcher in a thread with fast timings; return its thread and result box."""
    result = {}
    options = {"environ": DESKTOP, "poll": 0.01, "settle": 0.05, "seconds": 5, **options}
    thread = threading.Thread(target=lambda: result.setdefault(
        "code", watcher.watch(event(transcript, prompt), state, **options)))
    thread.start()
    return thread, result


def wait_started(state, prompt=PROMPT):
    for _ in range(500):
        if watcher._current(state, SESSION) == prompt:
            return
        threading.Event().wait(0.01)
    raise AssertionError("watcher did not start")


def append(path, text):
    with path.open("a", encoding="utf-8") as stream:
        stream.write(text)


@pytest.mark.parametrize("environ,expected", [
    (DESKTOP, True),
    ({**DESKTOP, "CLAUDE_CODE_EXECPATH": r"C:\Users\me\AppData\Roaming\Claude\claude-code\2.1.300\claude.exe"}, True),
    ({**DESKTOP, "CLAUDE_CODE_EXECPATH": "/x/claude-code/2.1.287/claude"}, True),
    ({**DESKTOP, "CLAUDE_CODE_EXECPATH": "/x/claude-code/2.1.286/claude"}, False),
    ({**DESKTOP, "CLAUDE_CODE_EXECPATH": "/usr/local/bin/claude"}, False),
    ({"CLAUDE_CODE_ENTRYPOINT": "claude-desktop"}, False),
    ({**DESKTOP, "CLAUDE_CODE_ENTRYPOINT": "cli"}, False),
])
def test_only_supported_desktop_clients_are_watched(environ, expected):
    assert watcher.supported(environ) is expected


@pytest.mark.parametrize("line,expected", [
    (interrupt(), True),
    (interrupt(text="[Request interrupted by user for tool use]"), True),
    (interrupt(prompt="another-prompt"), False),
    (interrupt(isSidechain=True), False),
    (json.dumps({"type": "user", "promptId": PROMPT, "message": {"content": "[Request interrupted by user]"}}), False),
    (json.dumps({"type": "user", "promptId": PROMPT, "message": {"content": [
        {"type": "tool_result", "content": "[Request interrupted by user]"}]}}), False),
    (json.dumps({"type": "user", "promptId": PROMPT, "message": {"content": [
        {"type": "text", "text": "[Request interrupted by user]"}, {"type": "text", "text": "more"}]}}), False),
    (json.dumps({"type": "assistant", "promptId": PROMPT, "message": {"content": [
        {"type": "text", "text": "[Request interrupted by user]"}]}}), False),
    ("not json", False),
])
def test_only_claude_codes_own_interrupt_record_for_this_prompt_matches(line, expected):
    assert watcher.interrupt_record(line, PROMPT) is expected


def test_interrupt_wakes_claude_with_the_stopped_title_reminder(transcript, tmp_path, capsys):
    state = tmp_path / "state"
    thread, result = start(transcript, state)
    wait_started(state)
    # Written in two parts, as an appending process may flush a partial line first.
    record = interrupt()
    append(transcript, record[:20])
    threading.Event().wait(0.05)
    append(transcript, record[20:])
    thread.join(5)
    assert result["code"] == watcher.WAKE == 2
    reminder = capsys.readouterr().err
    assert "set_session_title" in reminder and "🛑" in reminder and "no text at all" in reminder
    assert not list(state.glob("*.done"))


def test_normal_stop_ends_the_watcher_quietly(transcript, tmp_path, capsys):
    state = tmp_path / "state"
    thread, result = start(transcript, state)
    wait_started(state)
    assert watcher.done(event(transcript, hook_event_name="Stop"), state) == 0
    thread.join(5)
    assert result["code"] == 0 and capsys.readouterr().err == ""
    assert not list(state.glob("*.done"))


def test_a_newer_prompt_supersedes_and_only_its_own_interrupt_counts(transcript, tmp_path):
    state = tmp_path / "state"
    old, old_result = start(transcript, state, "old-prompt")
    wait_started(state, "old-prompt")
    new, new_result = start(transcript, state)
    old.join(5)
    assert old_result["code"] == 0
    append(transcript, interrupt(prompt="old-prompt"))
    threading.Event().wait(0.1)
    assert new.is_alive()
    append(transcript, interrupt())
    new.join(5)
    assert new_result["code"] == 2


def test_a_prompt_typed_right_after_stop_keeps_claude_asleep(transcript, tmp_path):
    state = tmp_path / "state"
    calls = []

    def sleep(seconds):
        calls.append(seconds)
        if seconds == 0.05:  # the settle window after the interrupt record
            watcher._set_current(state, SESSION, "typed-next")
        threading.Event().wait(min(seconds, 0.01))

    thread, result = start(transcript, state, sleep=sleep)
    wait_started(state)
    append(transcript, interrupt())
    thread.join(5)
    assert result["code"] == 0 and 0.05 in calls


def test_an_immediate_stop_before_the_watcher_starts_is_still_seen(transcript, tmp_path):
    append(transcript, interrupt())
    state = tmp_path / "state"
    thread, result = start(transcript, state)
    thread.join(5)
    assert result["code"] == 2


def test_unrelated_turn_end_and_deadline_return_quietly(transcript, tmp_path):
    state = tmp_path / "state"
    thread, result = start(transcript, state, seconds=0.2)
    thread.join(5)
    assert result["code"] == 0


@pytest.mark.parametrize("change", ["terminal", "old-client", "subagent", "sidechain", "relative", "missing",
                                    "bad-session", "no-prompt"])
def test_unsupported_or_untrusted_events_never_wake(transcript, tmp_path, change):
    state = tmp_path / "state"
    environ = DESKTOP
    hook = event(transcript)
    if change == "terminal":
        environ = {**DESKTOP, "CLAUDE_CODE_ENTRYPOINT": "cli"}
    elif change == "old-client":
        environ = {**DESKTOP, "CLAUDE_CODE_EXECPATH": "/x/claude-code/2.1.200/claude"}
    elif change == "subagent":
        hook["agent_id"] = "child"
    elif change == "sidechain":
        hook["is_sidechain"] = True
    elif change == "relative":
        hook["transcript_path"] = "session.jsonl"
    elif change == "missing":
        hook["transcript_path"] = str(tmp_path / "absent.jsonl")
    elif change == "bad-session":
        hook["session_id"] = "../other"
    else:
        del hook["prompt_id"]
    append(transcript, interrupt())
    assert watcher.watch(hook, state, environ=environ, poll=0.01, settle=0.01, seconds=1) == 0
    assert not state.exists()


def test_stop_for_a_turn_without_its_own_watcher_leaves_no_marker(transcript, tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    watcher._set_current(state, SESSION, "current-prompt")
    assert watcher.done(event(transcript, "woken-turn", hook_event_name="Stop"), state) == 0
    assert not list(state.glob("*.done"))


def test_linked_state_directory_is_refused(transcript, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    append(transcript, interrupt())
    assert watcher.watch(event(transcript), link / "state", environ=DESKTOP, poll=0.01, seconds=1) == 0
    assert watcher.done(event(transcript), link / "state") == 0
    assert not list(real.iterdir())


@pytest.mark.parametrize("arguments,stdin", [
    (["--watch", "extra"], b"{}"), (["--unknown"], b"{}"), ([], b"{}"),
    (["--watch"], b"not json"), (["--done"], b"\xff"), (["--watch"], b"x" * 65537),
])
def test_cli_failures_exit_zero_never_the_wake_code(tmp_path, arguments, stdin):
    script = tmp_path / "hooks" / "agents-core-thread-status-interrupt.py"
    script.parent.mkdir()
    script.write_bytes((ROOT / "scripts/thread_status_interrupt.py").read_bytes())
    completed = subprocess.run([sys.executable, str(script), *arguments], input=stdin, capture_output=True,
                               env={**os.environ, **DESKTOP})
    assert completed.returncode == 0 and completed.stderr == b""


def test_a_rewritten_shorter_transcript_is_rescanned(transcript, tmp_path):
    append(transcript, "x" * 4096 + "\n")
    state = tmp_path / "state"
    thread, result = start(transcript, state)
    wait_started(state)
    threading.Event().wait(0.05)
    transcript.write_text(interrupt())  # shorter than the watcher's current offset
    thread.join(5)
    assert result["code"] == 2


def test_an_oversized_record_is_skipped_without_buffering_it(transcript, tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "MAX_PARTIAL_BYTES", 512)
    state = tmp_path / "state"
    thread, result = start(transcript, state)
    wait_started(state)
    append(transcript, "y" * 1000)  # one record far beyond the cap, still unterminated
    threading.Event().wait(0.1)
    append(transcript, "\n" + interrupt())
    thread.join(5)
    assert result["code"] == 2
