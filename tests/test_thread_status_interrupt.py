"""Claude desktop stopped-status watcher: interrupt detection, turn lifecycle, ordering and safe exits."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("status_interrupt", ROOT / "scripts/thread_status_interrupt.py")
watcher = importlib.util.module_from_spec(spec)
spec.loader.exec_module(watcher)

SESSION = "550e8400-e29b-41d4-a716-446655440000"
PROMPT = "1f68b279-5fc9-4e77-a6c7-9a8bb3b71971"
NEWER = "7c9e6679-7425-40de-944b-e07fc1f90ae7"
DESKTOP = {"CLAUDE_CODE_ENTRYPOINT": "claude-desktop",
           "CLAUDE_CODE_EXECPATH": "/Users/me/Library/Application Support/Claude/claude-code/2.1.299/"
                                   "claude.app/Contents/MacOS/claude"}


def interrupt(prompt=PROMPT, text="[Request interrupted by user]", **extra):
    record = {"parentUuid": "p", "isSidechain": False, "promptId": prompt, "type": "user",
              "message": {"role": "user", "content": [{"type": "text", "text": text}]}, **extra}
    return json.dumps(record) + "\n"


def typed(prompt=PROMPT, text="Wait a minute"):
    return json.dumps({"type": "user", "isSidechain": False, "promptId": prompt,
                       "message": {"role": "user", "content": text}}) + "\n"


def blocked_stop(prompt=PROMPT):
    """The meta user record Claude Code writes when another Stop hook blocks the stop."""
    return json.dumps({"type": "user", "isMeta": True, "isSidechain": False, "promptId": prompt,
                       "message": {"role": "user", "content": "Stop hook feedback:\n[check]: run the tests"}}) + "\n"


def tool_result(prompt=PROMPT):
    return json.dumps({"type": "user", "isSidechain": False, "promptId": prompt, "message": {
        "role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "ok"}]}}) + "\n"


def reply(text="Done."):
    return json.dumps({"type": "assistant", "message": {"role": "assistant",
                                                        "content": [{"type": "text", "text": text}]}}) + "\n"


def event(transcript, prompt=PROMPT, **extra):
    return {"hook_event_name": "UserPromptSubmit", "session_id": SESSION, "prompt_id": prompt,
            "transcript_path": str(transcript), **extra}


def stop(transcript, state, prompt=PROMPT):
    return watcher.done(event(transcript, prompt, hook_event_name="Stop", stop_hook_active=False),
                        state, environ=DESKTOP)


@pytest.fixture
def transcript(tmp_path):
    path = tmp_path / "session.jsonl"
    path.write_text(typed())
    return path


def start(transcript, state, prompt=PROMPT, **options):
    """Run one watcher in a thread with fast timings; return its thread and result box."""
    result = {}
    options = {"environ": DESKTOP, "poll": 0.01, "settle": 0.05, "seconds": 5, "linger": 0.2, **options}
    thread = threading.Thread(target=lambda: result.setdefault(
        "code", watcher.watch(event(transcript, prompt), state, **options)))
    thread.start()
    return thread, result


def record(state):
    """The session record, read under its lock as every hook does."""
    try:
        with watcher._locked(state, SESSION, create=False) as value:
            return dict(value)
    except (OSError, watcher._Busy):
        return {}


def register(state, prompt, size=0):
    state.mkdir(exist_ok=True)
    with watcher._locked(state, SESSION) as value:
        value.update(prompt=prompt, size=size, woken=False)


def wait_started(state, prompt=PROMPT):
    for _ in range(500):
        if record(state).get("prompt") == prompt:
            return
        time.sleep(0.01)
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
    (interrupt().encode(), True),
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
    (json.dumps({"type": "user", "promptId": PROMPT, "message": ["not", "an", "object"]}), False),
    ("not json", False),
])
def test_only_claude_codes_own_interrupt_record_for_this_prompt_matches(line, expected):
    assert watcher.interrupt_record(line, PROMPT) is expected


def test_interrupt_wakes_claude_once_with_the_stopped_title_reminder(transcript, tmp_path, capsys):
    state = tmp_path / "state"
    thread, result = start(transcript, state)
    wait_started(state)
    # Written in two parts, as an appending process may flush a partial line first.
    line = interrupt()
    append(transcript, line[:20])
    time.sleep(0.05)
    append(transcript, line[20:])
    thread.join(5)
    assert result["code"] == watcher.WAKE == 2
    reminder = capsys.readouterr().err
    assert reminder.count("set_session_title") == 1 and "🛑" in reminder and "no text at all" in reminder
    assert record(state)["woken"] is True  # the claim is persisted before the wake


def test_reminder_lets_a_user_message_in_the_woken_turn_be_handled_normally():
    text = watcher.REMINDER
    assert "exactly one call" in text and "set_session_title" in text and "🛑" in text
    assert "handle that message normally after the title call" in text
    # Without a user message the turn still ends silently.
    assert "Otherwise end the turn after the title call with no text at all" in text
    lowered = text.lower()
    assert not any(phrase in lowered for phrase in ("do not resume", "not resume", "do not continue", "or continue"))


def test_a_normal_stop_retires_the_watcher_after_the_linger_window(transcript, tmp_path, capsys):
    state = tmp_path / "state"
    thread, result = start(transcript, state, linger=1.0)
    wait_started(state)
    append(transcript, reply())
    assert stop(transcript, state) == 0
    time.sleep(0.1)
    assert thread.is_alive()  # another Stop hook might still continue the turn
    thread.join(5)
    assert result["code"] == 0 and capsys.readouterr().err == ""
    assert record(state)["stops"] == {PROMPT: transcript.stat().st_size}


@pytest.mark.parametrize("order", ["feedback-after-stop-record", "feedback-before-stop-record"])
def test_a_stop_another_hook_blocks_keeps_the_watcher_for_a_later_interrupt(transcript, tmp_path, capsys, order):
    state = tmp_path / "state"
    thread, result = start(transcript, state, linger=0.5)
    wait_started(state)
    append(transcript, reply())
    if order == "feedback-after-stop-record":
        assert stop(transcript, state) == 0
        append(transcript, blocked_stop())
    else:
        # A fast foreign hook's feedback can land before this Stop handler reads the size;
        # the continued turn's next tool result then shows the turn going on.
        append(transcript, blocked_stop())
        assert stop(transcript, state) == 0
        append(transcript, tool_result())
    time.sleep(1.0)  # well past the linger window
    assert thread.is_alive()
    append(transcript, interrupt())
    thread.join(5)
    assert result["code"] == 2
    assert capsys.readouterr().err.count("set_session_title") == 1
    # The woken turn inherits the prompt; its own Stop changes nothing and wakes nothing.
    assert stop(transcript, state) == 0
    assert record(state)["woken"] is True and record(state)["prompt"] == PROMPT


def test_a_normal_end_after_the_interrupt_keeps_claude_asleep(transcript, tmp_path, capsys):
    state = tmp_path / "state"

    def sleep(seconds):
        if seconds == 0.05:  # the settle window: this prompt then ends normally
            stop(transcript, state)
        time.sleep(min(seconds, 0.01))

    thread, result = start(transcript, state, sleep=sleep)
    wait_started(state)
    append(transcript, interrupt())
    thread.join(5)
    assert result["code"] == 0 and capsys.readouterr().err == ""


def test_a_newer_prompt_supersedes_and_only_its_own_interrupt_counts(tmp_path):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(typed("old-prompt"))
    state = tmp_path / "state"
    old, old_result = start(transcript, state, "old-prompt")
    wait_started(state, "old-prompt")
    new, new_result = start(transcript, state)
    old.join(5)
    assert old_result["code"] == 0
    append(transcript, interrupt(prompt="old-prompt"))
    time.sleep(0.1)
    assert new.is_alive()
    append(transcript, typed() + interrupt())
    new.join(5)
    assert new_result["code"] == 2
    assert record(state)["history"] == ["old-prompt"]


def test_a_prompt_typed_right_after_stop_keeps_claude_asleep(transcript, tmp_path):
    state = tmp_path / "state"
    calls = []

    def sleep(seconds):
        calls.append(seconds)
        if seconds == 0.05:  # the settle window after the interrupt record
            register(state, "typed-next", transcript.stat().st_size)
        time.sleep(min(seconds, 0.01))

    thread, result = start(transcript, state, sleep=sleep)
    wait_started(state)
    append(transcript, interrupt())
    thread.join(5)
    assert result["code"] == 0 and 0.05 in calls


def test_a_newer_prompts_record_keeps_claude_asleep_before_its_watcher_starts(transcript, tmp_path, capsys):
    state = tmp_path / "state"

    def sleep(seconds):
        if seconds == 0.05:  # the newer prompt is recorded; its own watcher is still starting
            append(transcript, typed(NEWER, "Actually, do this instead"))
        time.sleep(min(seconds, 0.01))

    thread, result = start(transcript, state, sleep=sleep)
    wait_started(state)
    append(transcript, interrupt())
    thread.join(5)
    assert result["code"] == 0 and capsys.readouterr().err == ""
    assert record(state)["prompt"] == PROMPT and not record(state).get("woken")


@pytest.mark.parametrize("earlier", [False, True])
def test_an_immediate_stop_before_the_watcher_starts_is_still_seen(transcript, tmp_path, earlier):
    state = tmp_path / "state"
    if earlier:
        register(state, "earlier-prompt", 0)  # registered before this prompt was typed
    append(transcript, interrupt())
    thread, result = start(transcript, state)
    thread.join(5)
    assert result["code"] == 2
    assert record(state)["history"] == (["earlier-prompt"] if earlier else [])


@pytest.mark.parametrize("newer_known_from", ["registration", "transcript"])
def test_a_late_watcher_neither_replaces_a_newer_prompt_nor_wakes(tmp_path, capsys, newer_known_from):
    transcript = tmp_path / "session.jsonl"
    transcript.write_text(typed() + interrupt())  # this prompt was stopped before its watcher ran
    state = tmp_path / "state"
    newer = None
    if newer_known_from == "registration":
        # The newer prompt's watcher registered first; its own record is not written yet.
        newer, newer_result = start(transcript, state, NEWER)
        wait_started(state, NEWER)
    else:
        append(transcript, typed(NEWER))
    code = watcher.watch(event(transcript), state, environ=DESKTOP, poll=0.01, settle=0.01, seconds=2)
    assert code == 0 and capsys.readouterr().err == ""
    current = record(state)
    assert current.get("prompt") == (NEWER if newer else None)
    assert PROMPT in current["history"] and not current.get("woken")
    # Neither a retry of the late watcher nor its prompt's Stop changes the newer prompt's state.
    assert watcher.watch(event(transcript), state, environ=DESKTOP, poll=0.01, seconds=1) == 0
    assert stop(transcript, state) == 0
    assert PROMPT not in record(state).get("stops", {})
    if newer:
        assert newer.is_alive()
        append(transcript, typed(NEWER) + interrupt(NEWER))
        newer.join(5)
        assert newer_result["code"] == 2


def test_two_watchers_of_one_prompt_wake_once(transcript, tmp_path, capsys):
    state = tmp_path / "state"
    first, first_result = start(transcript, state)
    second, second_result = start(transcript, state)
    wait_started(state)
    time.sleep(0.1)
    assert first.is_alive() and second.is_alive()
    append(transcript, interrupt())
    first.join(5)
    second.join(5)
    assert sorted((first_result["code"], second_result["code"])) == [0, 2]
    assert capsys.readouterr().err.count("set_session_title") == 1


def test_stop_registration_and_wake_wait_for_the_session_lock(transcript, tmp_path, monkeypatch):
    state = tmp_path / "state"
    register(state, PROMPT, 0)
    monkeypatch.setattr(watcher, "LOCK_SECONDS", 0.05)
    with watcher._locked(state, SESSION):
        # Another handle of the same record: flock on POSIX, msvcrt.locking on Windows.
        assert stop(transcript, state) == 0
        assert watcher.watch(event(transcript, NEWER), state, environ=DESKTOP, poll=0.01, seconds=1) == 0
    assert record(state) == {"prompt": PROMPT, "size": 0, "woken": False}
    assert stop(transcript, state) == 0
    assert record(state)["stops"] == {PROMPT: transcript.stat().st_size}


def test_a_first_turn_transcript_created_after_the_watcher_starts_is_followed(tmp_path):
    transcript = tmp_path / "projects" / "session.jsonl"
    state = tmp_path / "state"
    thread, result = start(transcript, state, appear=5)
    wait_started(state)
    time.sleep(0.1)
    assert thread.is_alive()
    transcript.parent.mkdir()
    transcript.write_text(typed() + interrupt())
    thread.join(5)
    assert result["code"] == 2


def test_a_transcript_that_never_appears_ends_the_watcher_quietly(tmp_path):
    state = tmp_path / "state"
    began = time.monotonic()
    thread, result = start(tmp_path / "absent.jsonl", state, appear=0.1)
    thread.join(5)
    assert result["code"] == 0 and time.monotonic() - began < 3


def test_unrelated_turn_end_and_deadline_return_quietly(transcript, tmp_path):
    state = tmp_path / "state"
    thread, result = start(transcript, state, seconds=0.2)
    thread.join(5)
    assert result["code"] == 0


@pytest.mark.parametrize("change", ["terminal", "old-client", "subagent", "sidechain", "relative", "directory",
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
    elif change == "directory":
        hook["transcript_path"] = str(tmp_path)
    elif change == "bad-session":
        hook["session_id"] = "../other"
    else:
        del hook["prompt_id"]
    append(transcript, interrupt())
    assert watcher.watch(hook, state, environ=environ, poll=0.01, settle=0.01, seconds=1) == 0
    assert watcher.done({**hook, "hook_event_name": "Stop"}, state, environ=environ) == 0
    assert not state.exists()


def test_stop_without_a_watcher_record_creates_nothing(transcript, tmp_path):
    state = tmp_path / "state"
    assert stop(transcript, state) == 0
    assert not state.exists()
    state.mkdir()
    assert stop(transcript, state) == 0
    assert watcher.done(event(transcript, hook_event_name="Stop"), state, environ={}) == 0
    assert not list(state.iterdir())


def test_linked_state_directory_is_refused(transcript, tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real, target_is_directory=True)
    append(transcript, interrupt())
    assert watcher.watch(event(transcript), link / "state", environ=DESKTOP, poll=0.01, seconds=1) == 0
    assert stop(transcript, link / "state") == 0
    assert not list(real.iterdir())


@pytest.mark.parametrize("arguments,stdin", [
    (["--watch", "extra"], b"{}"), (["--unknown"], b"{}"), ([], b"{}"),
    (["--watch"], b"not json"), (["--done"], b"\xff"), (["--watch"], b"x" * 65537),
# Short IDs: Windows rejects PYTEST_CURRENT_TEST longer than 32767 characters.
], ids=["extra-argument", "unknown-mode", "no-mode", "invalid-json", "invalid-utf8", "oversized-event"])
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
    time.sleep(0.05)
    transcript.write_text(interrupt())  # shorter than the watcher's current offset
    thread.join(5)
    assert result["code"] == 2


def test_an_oversized_record_is_skipped_without_buffering_it(transcript, tmp_path, monkeypatch):
    monkeypatch.setattr(watcher, "MAX_PARTIAL_BYTES", 512)
    state = tmp_path / "state"
    thread, result = start(transcript, state)
    wait_started(state)
    append(transcript, "y" * 1000)  # one record far beyond the cap, still unterminated
    time.sleep(0.1)
    append(transcript, "\n" + interrupt())
    thread.join(5)
    assert result["code"] == 2
