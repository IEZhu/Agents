"""Title preservation, real hook contracts and explicit SDK-write failure paths."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import threading
from types import SimpleNamespace

import pytest

from src.thread_status import STATUSES, STATUS_GUIDANCE, managed_status, status_title

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("status_runtime", ROOT / "scripts/thread_status.py")
runtime = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runtime)
SESSION = "550e8400-e29b-41d4-a716-446655440000"


@pytest.fixture(autouse=True)
def terminal_claude(monkeypatch):
    # Tests may run inside a desktop Code-tab session; subprocesses inherit this too.
    monkeypatch.delenv("CLAUDE_CODE_ENTRYPOINT", raising=False)


@pytest.mark.parametrize("status", STATUSES)
def test_status_roundtrip_is_idempotent_and_keeps_body(status):
    title = status_title("👀 🔄 🧪 Проверить API — #42", status)
    assert title == STATUSES[status] + " 🧪 Проверить API — #42"
    assert status_title(title, status) == title
    assert managed_status(title) == status


def test_prefix_requires_boundary_and_preserves_unrelated_emoji():
    assert status_title("✅Build status", "working") == "🔄 ✅Build status"
    assert status_title("⏸ Проверить", "ready_to_close") == "🏁 Проверить"
    assert managed_status("🤖 Build") is None


def test_finishing_is_recognized_and_replaced_without_changing_title_body():
    assert STATUSES["finishing"] == "🧹"
    assert managed_status("🧹 Проверить API — #42") == "finishing"
    assert status_title("🏁 🧹 🧪 Проверить API — #42", "finishing") == "🧹 🧪 Проверить API — #42"
    assert status_title("🧹 🧪 Проверить API — #42", "working") == "🔄 🧪 Проверить API — #42"
    assert status_title("🧹Decorative title", "working") == "🔄 🧹Decorative title"


@pytest.mark.parametrize("length", [997, 998, 999, 1000])
@pytest.mark.parametrize("first", STATUSES)
def test_longest_accepted_bodies_survive_every_status_transition(length, first):
    body = "x" * length
    title = status_title(body, first)
    for status in STATUSES:
        title = status_title(title, status)
        assert title == STATUSES[status] + " " + body
        assert status_title(title, status) == title


@pytest.mark.parametrize("title", ["", " ", "🔄", "🔄 👀", "🧹", "🧹 🏁", "line\nbreak", "\x1b[31mTitle", "x" * 1001, None, 12])
def test_invalid_or_missing_title_never_becomes_invented_title(title):
    with pytest.raises(ValueError):
        status_title(title, "working")
    with pytest.raises(ValueError):
        status_title("Task", "done")


def event(name="UserPromptSubmit", **values):
    return {"hook_event_name": name, "session_id": SESSION, **values}


def test_claude_prompt_adds_native_title_without_reading_transcript(tmp_path):
    out = runtime.hook_output(event(session_title="Task", cwd=str(tmp_path),
                                    transcript_path="/must/not/be/read"), "claude")
    fields = out["hookSpecificOutput"]
    assert fields["sessionTitle"] == "🔄 Task"
    assert SESSION in fields["additionalContext"]
    assert "--set-status" in fields["additionalContext"]
    assert "must/not/be/read" not in json.dumps(out)


@pytest.mark.parametrize("event_name", ["SessionStart", "UserPromptSubmit"])
def test_claude_desktop_points_to_app_title_tools_instead_of_cli_title(monkeypatch, tmp_path, event_name):
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", runtime.DESKTOP_ENTRYPOINT)
    fields = runtime.hook_output(event(event_name, session_title="Task", cwd=str(tmp_path)),
                                 "claude")["hookSpecificOutput"]
    assert "sessionTitle" not in fields
    context = fields["additionalContext"]
    assert context.startswith(STATUS_GUIDANCE)
    assert 'session_id "self"' in context
    assert '"select:mcp__ccd_session_mgmt__get_session,mcp__ccd_session_mgmt__set_session_title"' in context
    # The SDK helper writes only the CLI title, so the desktop context must not offer it.
    assert "--set-status" not in context


def test_other_claude_entrypoints_keep_cli_title_and_sdk_helper(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", "cli")
    fields = runtime.hook_output(event(session_title="Task", cwd=str(tmp_path)), "claude")["hookSpecificOutput"]
    assert fields["sessionTitle"] == "🔄 Task"
    assert "--set-status" in fields["additionalContext"]
    assert "mcp__ccd_session_mgmt" not in fields["additionalContext"]


@pytest.mark.parametrize("title", ["⏸️ Paused", "🛑 Stopped", "🧹 Remaining obligations", "✅ Closed", "🏁 Finished", "🙋 Need answer", "👀 Review", "⏳ CI", "🚧 Blocked"])
def test_lifecycle_alone_does_not_reopen_task(title):
    output = runtime.hook_output(event(session_title=title), "claude")
    assert "sessionTitle" not in output["hookSpecificOutput"]


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_session_start_gives_policy_without_claiming_work_started(client):
    fields = runtime.hook_output(event("SessionStart", source="resume", session_title="✅ Task"), client)["hookSpecificOutput"]
    assert "sessionTitle" not in fields
    assert "ready_to_close=🏁" in fields["additionalContext"]


@pytest.mark.parametrize("client,event_name", [("claude", "SessionStart"), ("claude", "UserPromptSubmit"),
                                              ("codex", "SessionStart"), ("codex", "UserPromptSubmit"),
                                              ("cursor", "sessionStart")])
def test_all_clients_receive_finishing_and_final_handoff_policy(client, event_name):
    output = runtime.hook_output(event(event_name, conversation_id=SESSION), client)
    context = (output["additional_context"] if client == "cursor"
               else output["hookSpecificOutput"]["additionalContext"])
    assert context.startswith(STATUS_GUIDANCE)
    for phrase in ("finishing=🧹", "required work remains from a closure audit; task is unfinished",
                   "Use needs_user, review or ready_to_close only at the final handoff",
                   "a tool, CI or a subagent is working/waiting, never human review",
                   "At a closure-audit handoff, use finishing when required obligations remain",
                   "unless essential input or required human approval calls for needs_user/review",
                   "finish or explicitly resolve each before ready_to_close",
                   "Resume implementation as working", "a new work request after closed is also working"):
        assert phrase in context


def test_no_unsupported_title_fields_in_codex_or_cursor(monkeypatch):
    # A desktop entrypoint applies only to Claude events.
    monkeypatch.setenv("CLAUDE_CODE_ENTRYPOINT", runtime.DESKTOP_ENTRYPOINT)
    codex = runtime.hook_output(event(session_title="Task"), "codex")
    assert "sessionTitle" not in codex["hookSpecificOutput"]
    assert "mcp__ccd_session_mgmt" not in codex["hookSpecificOutput"]["additionalContext"]
    cursor = runtime.hook_output({"hook_event_name": "sessionStart", "conversation_id": SESSION}, "cursor")
    assert set(cursor) == {"additional_context"}
    assert "Classic IDE" in cursor["additional_context"]


@pytest.mark.parametrize("extra", [{"agent_id": "child"}, {"is_sidechain": True}, {"is_background_agent": True}])
def test_worker_events_cannot_rename_parent(extra):
    assert runtime.hook_output(event(session_title="Task", **extra), "claude") == {}


@pytest.mark.parametrize("bad", [{}, [], None, event("Stop"), event("SessionEnd"), event(session_id="../other")])
def test_unknown_or_missing_identity_and_stop_are_inert(bad):
    assert runtime.hook_output(bad, "claude") == {}


def test_confirmed_current_turn_interrupt_persists_stopped_only(monkeypatch, tmp_path):
    writes = []
    monkeypatch.setattr(runtime, "rename_codex_session", lambda *args, **kw: writes.append((args, kw)) or {"persisted": True})
    runtime.handle_hook_event(event(turn_id="turn-1"), "codex", state_directory=tmp_path)
    receipt = runtime.handle_hook_event(event("Interrupt", turn_id="turn-1"), "codex", state_directory=tmp_path)
    assert writes == [((SESSION, "stopped"), {"timeout": 2.4, "expected_turn_id": "turn-1", "recorded_turn_id": "turn-1"})]
    assert "Stopped" in receipt["systemMessage"]
    assert "unverified" in receipt["systemMessage"]
    saved = json.loads((tmp_path / (SESSION + ".json")).read_text())["last_interrupt"]
    assert saved["outcome"] == "persisted" and saved["persisted"] is True


@pytest.mark.parametrize("previous_turn", [None, "previous-turn"])
def test_early_interrupt_uses_public_turn_check_before_prompt_hook(monkeypatch, tmp_path, previous_turn):
    if previous_turn:
        runtime.handle_hook_event(event(turn_id=previous_turn), "codex", state_directory=tmp_path)
    calls = []
    monkeypatch.setattr(runtime, "rename_codex_session", lambda *a, **kw: calls.append(kw) or {"persisted": True})
    assert "Stopped" in runtime.handle_hook_event(event("Interrupt", turn_id="early-turn"),
                                                 "codex", state_directory=tmp_path)["systemMessage"]
    assert calls == [{"timeout": 2.4, "expected_turn_id": "early-turn", "recorded_turn_id": previous_turn}]
    runtime.handle_hook_event(event(turn_id="resumed-turn"), "codex", state_directory=tmp_path)
    saved = json.loads((tmp_path / (SESSION + ".json")).read_text())
    assert saved["turn_id"] == "resumed-turn"
    assert saved["last_interrupt"]["turn_id"] == "early-turn"
    assert saved["last_interrupt"]["outcome"] == "persisted"


def test_stale_interrupt_and_normal_completion_do_not_overwrite_new_work(monkeypatch, tmp_path):
    calls = []
    # The metadata adapter rejects an old turn before performing any title write.
    monkeypatch.setattr(runtime, "rename_codex_session", lambda *a, **kw: calls.append(kw) or {
        "persisted": False, "reason": "stale_interrupt"})
    runtime.handle_hook_event(event(turn_id="new-turn"), "codex", state_directory=tmp_path)
    for stale in [event("Interrupt", turn_id="old-turn"), event("Interrupt"), event("Stop", turn_id="new-turn")]:
        assert runtime.handle_hook_event(stale, "codex", state_directory=tmp_path) == {}
    assert runtime.handle_hook_event(event("Interrupt", turn_id="new-turn", agent_id="child"),
                                     "codex", state_directory=tmp_path) == {}
    assert calls == [{"timeout": 2.4, "expected_turn_id": "old-turn", "recorded_turn_id": "new-turn"}]
    saved = json.loads((tmp_path / (SESSION + ".json")).read_text())
    assert saved["turn_id"] == "new-turn" and saved["last_interrupt"]["outcome"] == "skipped"


@pytest.mark.parametrize("hold_seconds", [0.05, 2.6])
def test_new_prompt_waits_for_our_older_interrupt_write(monkeypatch, tmp_path, hold_seconds):
    started, release, prompt_finished = threading.Event(), threading.Event(), threading.Event()
    def writer(*a, **kw):
        started.set()
        assert release.wait(4)
        return {"persisted": True}
    monkeypatch.setattr(runtime, "rename_codex_session", writer)
    runtime.handle_hook_event(event(turn_id="old-turn"), "codex", state_directory=tmp_path)
    errors = []
    def run(payload, done=None):
        try:
            runtime.handle_hook_event(payload, "codex", state_directory=tmp_path)
        except Exception as error:
            errors.append(error)
        finally:
            if done:
                done.set()
    interrupt = threading.Thread(target=run, args=(event("Interrupt", turn_id="old-turn"),))
    interrupt.start()
    assert started.wait(1)
    prompt = threading.Thread(target=run, args=(event(turn_id="new-turn"), prompt_finished))
    prompt.start()
    try:
        assert not prompt_finished.wait(hold_seconds)
    finally:
        release.set()
        interrupt.join(2)
        prompt.join(2)
    assert not interrupt.is_alive() and not prompt.is_alive() and not errors
    assert json.loads((tmp_path / (SESSION + ".json")).read_text())["turn_id"] == "new-turn"


@pytest.mark.parametrize("import_elapsed", [0.45, 2.5])
def test_codex_adapter_import_consumes_writer_deadline(monkeypatch, import_elapsed):
    import builtins

    clock, calls = [100.0], []
    real_import = builtins.__import__
    adapter = SimpleNamespace(rename_codex_thread=lambda *a, **kw:
                              calls.append(kw) or {"persisted": True})

    def import_adapter(name, *args, **kwargs):
        if name == "agents_core_codex_titles":
            clock[0] += import_elapsed
            return adapter
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(runtime.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(builtins, "__import__", import_adapter)
    if import_elapsed < 2.4:
        assert runtime.rename_codex_session(SESSION, "stopped", timeout=2.4)["persisted"] is True
        assert calls[0]["timeout"] == pytest.approx(2.4 - import_elapsed)
    else:
        with pytest.raises(RuntimeError, match="deadline"):
            runtime.rename_codex_session(SESSION, "stopped", timeout=2.4)
        assert calls == []


@pytest.mark.parametrize("elapsed,expected_timeout", [(1.1, 1.6), (2.8, None)])
def test_interrupt_main_accounts_for_startup_before_writer(monkeypatch, tmp_path, capsys,
                                                          elapsed, expected_timeout):
    import io

    monkeypatch.setattr(runtime, "_HOOK_STARTED_AT", 100.0 - elapsed, raising=False)
    monkeypatch.setattr(runtime.time, "monotonic", lambda: 100.0)
    monkeypatch.setattr(runtime, "__file__", str(tmp_path / "hook.py"))
    payload = event("Interrupt", turn_id="01a14e91-da9f-75f0-b282-911dfe1f8bbe")
    monkeypatch.setattr(runtime.sys, "stdin", SimpleNamespace(buffer=io.BytesIO(json.dumps(payload).encode())))
    calls = []
    monkeypatch.setattr(runtime, "rename_codex_session", lambda *args, **kwargs:
                        calls.append(kwargs) or {"persisted": True})
    assert runtime.main(["--client", "codex"]) == 0
    output = json.loads(capsys.readouterr().out)
    receipt = json.loads((tmp_path / "agents-core-thread-status-state" / (SESSION + ".json")).read_text())["last_interrupt"]
    if expected_timeout is None:
        assert calls == [] and output == {}
        assert receipt["outcome"] == "failed" and receipt["persisted"] is False
        assert "deadline" in receipt["error"]
    else:
        assert calls[0]["timeout"] == pytest.approx(expected_timeout)
        assert "Stopped" in output["systemMessage"] and receipt["persisted"] is True


def test_interrupt_writer_failure_is_not_reported_as_saved(monkeypatch, tmp_path):
    def failure(*a, **kw):
        raise RuntimeError("write failed")
    monkeypatch.setattr(runtime, "rename_codex_session", failure)
    runtime.handle_hook_event(event(turn_id="turn-1"), "codex", state_directory=tmp_path)
    with pytest.raises(RuntimeError, match="write failed"):
        runtime.handle_hook_event(event("Interrupt", turn_id="turn-1"), "codex", state_directory=tmp_path)
    saved = json.loads((tmp_path / (SESSION + ".json")).read_text())["last_interrupt"]
    assert saved["outcome"] == "failed" and saved["persisted"] is False
    assert saved["error"] == "write failed"


def test_turn_state_never_follows_links_into_client_files(monkeypatch, tmp_path):
    victim = tmp_path / "client.json"
    victim.write_text("preserve")
    state = tmp_path / "state"
    state.mkdir()
    (state / (SESSION + ".json")).symlink_to(victim)
    with pytest.raises(ValueError, match="symbolic link"):
        runtime.handle_hook_event(event(turn_id="turn-1"), "codex", state_directory=state)
    assert victim.read_text() == "preserve"


@pytest.fixture
def sdk(monkeypatch):
    state = {"title": "👀 Test task", "writes": []}
    def info(session_id, directory):
        assert session_id == SESSION and Path(directory).is_absolute()
        return SimpleNamespace(custom_title=state["title"], summary="Original task")
    def rename(session_id, title, directory):
        state["writes"].append((session_id, title, directory))
        state["title"] = title
    module = SimpleNamespace(get_session_info=info, rename_session=rename)
    monkeypatch.setitem(sys.modules, "claude_agent_sdk", module)
    return state, module


@pytest.mark.parametrize("status,prefix", [("ready_to_close", "🏁"), ("finishing", "🧹")])
def test_sdk_uses_explicit_session_reads_back_and_avoids_duplicate_writes(sdk, tmp_path, status, prefix):
    state, _ = sdk
    result = runtime.rename_claude_session(SESSION, str(tmp_path), status)
    assert result["title"] == prefix + " Test task" and result["persisted"] is True
    assert result["live_ui_refresh_verified"] is False
    assert len(state["writes"]) == 1
    assert runtime.rename_claude_session(SESSION, str(tmp_path), status)["changed"] is False
    assert len(state["writes"]) == 1


def test_sdk_failure_or_missing_session_is_not_reported_as_applied(sdk, tmp_path):
    state, module = sdk
    module.rename_session = lambda *a, **kw: None
    with pytest.raises(RuntimeError, match="readback"):
        runtime.rename_claude_session(SESSION, str(tmp_path), "ready_to_close")
    module.get_session_info = lambda *a, **kw: None
    with pytest.raises(FileNotFoundError):
        runtime.rename_claude_session(SESSION, str(tmp_path), "working")
    assert state["writes"] == []


@pytest.mark.parametrize("session,directory", [("../other", "/tmp"), (SESSION, "relative")])
def test_sdk_rejects_unknown_session_scope_before_access(sdk, session, directory):
    with pytest.raises(ValueError):
        runtime.rename_claude_session(session, directory, "working")
    assert sdk[0]["writes"] == []


@pytest.mark.parametrize("client,event_name", [("claude", "UserPromptSubmit"), ("codex", "SessionStart"),
                                              ("cursor", "sessionStart")])
def test_installed_runtime_runs_outside_checkout_with_sibling_core(tmp_path, client, event_name):
    hook = tmp_path / "hooks/agents-core-thread-status.py"
    hook.parent.mkdir()
    shutil.copyfile(ROOT / "scripts/thread_status.py", hook)
    shutil.copyfile(ROOT / "src/thread_status.py", hook.with_name("agents_core_thread_titles.py"))
    result = subprocess.run([sys.executable, str(hook), "--client", client], cwd=tmp_path,
                            input=json.dumps(event(event_name, session_title="Task", conversation_id=SESSION)),
                            capture_output=True, text=True, check=True)
    output = json.loads(result.stdout)
    if client == "claude":
        assert output["hookSpecificOutput"]["sessionTitle"] == "🔄 Task"
    context = (output["additional_context"] if client == "cursor"
               else output["hookSpecificOutput"]["additionalContext"])
    assert context.startswith(STATUS_GUIDANCE)
    assert "finishing=🧹" in context
    # JSON remains ASCII safe even under legacy terminal encodings.
    assert result.stdout.isascii()


@pytest.mark.parametrize("client", ["claude", "codex", "cursor"])
@pytest.mark.parametrize("status,prefix", [("ready_to_close", "🏁"), ("finishing", "🧹")])
def test_cli_format_is_explicitly_not_an_applied_write(client, status, prefix):
    result = subprocess.run([sys.executable, str(ROOT / "scripts/thread_status.py"), "--client", client,
                             "--title", "Task", "--set-status", status],
                            capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"title": prefix + " Task", "applied": False}


@pytest.mark.parametrize("raw", [b"not json", b"\xff", b"x" * 65537],
                         ids=["invalid-json", "invalid-utf8", "oversized-event"])
def test_bad_hook_input_fails_open_without_leaking_input(raw):
    result = subprocess.run([sys.executable, str(ROOT / "scripts/thread_status.py"), "--client", "codex"],
                            input=raw, capture_output=True, check=True)
    assert json.loads(result.stdout) == {}
    assert b"Thread status:" in result.stderr
