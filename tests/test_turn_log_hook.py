"""The Stop hook that logs a headless turn in place of the model, and the protocol text it needs (#734)."""
import io
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from src.engine import config, protocol_text
from src.memory import turn_log_hook as hook
from src.memory.history import HistoryReader

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "claude_code_turn_transcript.jsonl"
PROMPT = "Now make it return the build version too."
ANSWER = "The health check now returns the build version from VERSION."
FOOTER = "**Agent**: software_engineer | **Skills**: skill-dev-testing"
REVISION = "ab" * 32
PERSONA = {"agent": "software_engineer", "activation_id": "act-1", "bundle_revision": REVISION,
           "scope": "Implement, debug and refactor software.", "skills_loaded": ["skill-dev-testing"],
           "implants_loaded": [], "rules_loaded": ["no-fabrication"]}
# Claude Code shows this many characters of the server instructions before v2.1.296 (4,096 since).
OLDER_CLAUDE_CODE_WINDOW = 2048
# Fake secrets, split so that the tracked-files scan (tests/test_schedules_scan.py) finds no token
# or high-entropy string in this file's text.
FAKE_ANTHROPIC_KEY = "sk-ant-" + "api03-abcdefghijklmnop"
FAKE_OPENAI_PROJECT_KEY = "sk-proj-" + "AbCdEf0123456789xyzXYZ"
FAKE_JWT = ".".join(["eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
                     "dozjgNryP4J3jVmNHl0w5" + "N_XgL0n3I9PlFUP0THsR8U"])
FAKE_CAPABILITY = "WPQbiALKXRfZMU80BVl1" + "LKvV6QkpPyif3h5H2UOEhb8"


@pytest.fixture
def switches(monkeypatch):
    monkeypatch.setattr(config, "TURN_LOG_HOOK", True)
    monkeypatch.setattr(config, "CLIENT_PROFILE", "headless")


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    (root / "app").mkdir(parents=True)
    (root / "CLAUDE.md").write_text("", encoding="utf-8")
    monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    return root


def stop_event(project, transcript=FIXTURE, **extra):
    return {"session_id": "5f0c7a2e-1b6d-4c2a-9e51-0d7b3c4a8f10", "transcript_path": str(transcript),
            "cwd": str(project / "app"), "hook_event_name": "Stop", "stop_hook_active": False, **extra}


def entries(project):
    return HistoryReader(str(project / "history.md"), str(project / "history")).read_all()


def run_main(monkeypatch, capsys, raw: bytes) -> tuple[int, str]:
    """``main`` on ``raw`` as stdin: its exit code and stderr. Its stdout must stay empty."""
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
    code = hook.main([])
    output = capsys.readouterr()
    assert output.out == ""  # a Stop hook's stdout would reach the client
    return code, output.err


# --- The hook ------------------------------------------------------------------------------------


def test_the_fixture_turn_is_one_entry_with_its_prompt_answer_and_persona(switches, project, monkeypatch, capsys):
    result = hook.record_turn(stop_event(project, prompt_id="prompt-2"))
    assert result["status"] == "recorded" and result["state"] == "durably_accepted"
    [entry] = entries(project)
    assert entry.intent == PROMPT  # the system reminder in front of the prompt is not part of it
    assert entry.outcome.startswith(ANSWER) and entry.outcome.endswith("**Rules**: no-fabrication")
    assert "Let me read the handler first." not in entry.outcome and "sidechain" not in entry.outcome
    assert entry.action == ("Agent: software_engineer Persona (client-reported): software_engineer; "
                            f"activation=act-1; revision={REVISION}; action=keep")
    assert entry.metadata["logged_by"] == "stop_hook"
    assert re.fullmatch(r"\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2}", entry.metadata["answer_timestamp"])
    # The same Stop event again, through the command: the journal keeps one entry.
    assert run_main(monkeypatch, capsys, json.dumps(stop_event(project, prompt_id="prompt-2")).encode()) == (0, "")
    assert hook.record_turn(stop_event(project))["status"] == "duplicate"
    assert len(entries(project)) == 1


@pytest.mark.parametrize("given", [
    b"", b"not json", b"[]", b'"Stop"', b"{}", b" " * (hook.MAX_INPUT_BYTES + 1),
    # Stop events whose fields cannot lead to a turn; the test adds the project as their cwd.
    {"hook_event_name": "SessionStart", "transcript_path": str(FIXTURE)},
    {"hook_event_name": "Stop"},
    {"hook_event_name": "Stop", "transcript_path": "relative/session.jsonl"},
    {"hook_event_name": "Stop", "transcript_path": "/nonexistent/session.jsonl"},
    {"hook_event_name": "Stop", "transcript_path": 7},
])
def test_malformed_input_exits_zero_without_writing(switches, project, monkeypatch, capsys, given):
    raw = given if isinstance(given, bytes) else json.dumps({**given, "cwd": str(project)}).encode()
    assert run_main(monkeypatch, capsys, raw)[0] == 0
    assert not (project / "history.md").exists()


def test_a_malformed_transcript_writes_nothing(switches, project, tmp_path):
    transcript = tmp_path / "broken.jsonl"
    transcript.write_text('{"type":"user"\nnot json\n[1, 2]\n{"type": "assistant", "message": 7}\n'
                          '{"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": ["t"]}]}}\n',
                          encoding="utf-8")
    assert hook.record_turn(stop_event(project, transcript))["status"] == "skipped"
    assert not (project / "history.md").exists()


@pytest.mark.parametrize("flag,profile", [(True, "interactive"), (False, "headless"), (False, "interactive")])
def test_either_switch_alone_writes_nothing(project, monkeypatch, flag, profile):
    monkeypatch.setattr(config, "TURN_LOG_HOOK", flag)
    monkeypatch.setattr(config, "CLIENT_PROFILE", profile)
    assert hook.record_turn(stop_event(project)) == {"status": "skipped", "reason": "disabled"}
    assert not (project / "history.md").exists()


def test_the_prompt_id_selects_the_hooks_own_turn(switches, project):
    hook.record_turn(stop_event(project, prompt_id="prompt-1"))
    [entry] = entries(project)
    assert entry.intent == "Add a health check endpoint to the service."
    assert entry.outcome.startswith('Added GET /health, which answers 200 with {"status": "ok"}.')
    assert entry.action.endswith("action=switch")  # activated in that turn


def write_transcript(path, persona_call, status="SUCCESS", activate_in_turn=True):
    """A two-turn transcript whose activation is ``persona_call`` (tool name, input)."""
    tool, arguments = persona_call
    rows = [{"type": "user", "promptId": "p1", "message": {"role": "user", "content": "first"}}]
    activation = [
        {"type": "assistant", "message": {"id": "m1", "content": [
            {"type": "tool_use", "id": "t1", "name": f"mcp__agents-core__{tool}", "input": arguments}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": [
                {"type": "text", "text": json.dumps({"status": status, "persona": PERSONA})}]}]}},
    ]
    second = [{"type": "user", "promptId": "p2", "message": {"role": "user", "content": "second"}}]
    rows += second + activation if activate_in_turn else activation + second
    rows.append({"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": "Done."}]}})
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


@pytest.mark.parametrize("call,status,in_turn,action", [
    (("route_and_load", {"query": "x"}), "SUCCESS", True, "switch"),
    (("get_agent_context", {"agent_name": "software_engineer", "force_reload": True}), "SUCCESS", True, "restore"),
    (("refresh_persona_context", {"query": "x"}), "SUCCESS", True, "refresh"),
    (("route_and_load", {"query": "x"}), "NO_CHANGE", True, "keep"),
    (("get_agent_context", {"agent_name": "software_engineer"}), "SUCCESS", False, "keep"),
])
def test_the_persona_action_follows_where_the_activation_happened(switches, project, tmp_path, call, status,
                                                                   in_turn, action):
    transcript = write_transcript(tmp_path / "t.jsonl", call, status, in_turn)
    hook.record_turn(stop_event(project, transcript))
    [entry] = entries(project)
    assert (entry.intent, entry.outcome) == ("second", "Done.")
    assert entry.action.endswith(f"action={action}")


@pytest.mark.parametrize("response", [
    {"status": "ROUTE_REQUIRED", "candidates": []},
    {"status": "ERROR", "persona": PERSONA},
])
def test_a_turn_without_an_applied_persona_writes_nothing(switches, project, tmp_path, response):
    transcript = tmp_path / "t.jsonl"
    rows = [{"type": "user", "message": {"role": "user", "content": "question"}},
            {"type": "assistant", "message": {"id": "m1", "content": [
                {"type": "tool_use", "id": "t1", "name": "mcp__Agents-Core__route_and_load", "input": {}}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": json.dumps({"result": json.dumps(response)})}]}},
            {"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": "Answer."}]}}]
    transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    assert hook.record_turn(stop_event(project, transcript)) == {"status": "skipped", "reason": "no_persona"}
    assert not (project / "history.md").exists()


def test_the_hook_inputs_last_message_wins_without_its_time_line_and_secrets(switches, project):
    given = f"2026.11.24 10:00:00\n\nDeployed with token {FAKE_ANTHROPIC_KEY}.\n\n" + FOOTER
    hook.record_turn(stop_event(project, last_assistant_message=given))
    [entry] = entries(project)
    assert entry.outcome == f"Deployed with token sk-ant-***. {FOOTER}"
    assert "abcdefghijklmnop" not in entry.outcome and "2026.11.24" not in entry.outcome


def test_the_project_resolves_as_a_stdio_server_resolves_it(switches, project, tmp_path, monkeypatch):
    other = tmp_path / "other"
    other.mkdir()
    (other / "AGENTS.md").write_text("", encoding="utf-8")
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(other))
    assert hook.journal_root(stop_event(project)) == other.resolve()
    monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT")
    monkeypatch.setenv("CLAUDE_PROJECT_DIR", str(other))
    assert hook.journal_root(stop_event(project)) == other.resolve()
    monkeypatch.delenv("CLAUDE_PROJECT_DIR")
    assert hook.journal_root(stop_event(project)) == project.resolve()  # cwd is project/app


def test_an_unsafe_project_is_refused_and_the_command_still_exits_zero(switches, project, monkeypatch, capsys):
    event = {**stop_event(project), "cwd": "/"}
    # Looked up at call time: tests/test_model_migration.py reloads the config module, which
    # replaces the class an import at the top of this file would have kept.
    with pytest.raises(config.ClientRootError):
        hook.record_turn(event)
    code, err = run_main(monkeypatch, capsys, json.dumps(event).encode())
    assert code == 0 and "turn not logged (ClientRootError)" in err
    assert not (project / "history.md").exists()


def test_the_module_runs_as_a_stop_hook_command(project):
    env = {**os.environ, "AGENTS_TURN_LOG_HOOK": "1", "AGENTS_CLIENT_PROFILE": "headless",
           "LANGFUSE_TRACING_ENABLED": "false"}
    command = [sys.executable, "-m", "src.memory.turn_log_hook"]
    for payload in (b"not json", json.dumps(stop_event(project)).encode()):
        result = subprocess.run(command, input=payload, cwd=ROOT, env=env, capture_output=True, timeout=120)
        assert (result.returncode, result.stdout) == (0, b""), result.stderr
    [entry] = entries(project)
    assert entry.intent == PROMPT and entry.outcome.startswith(ANSWER)


# --- The protocol text -----------------------------------------------------------------------------


def _renders():
    return {name: getattr(protocol_text, name)()
            for name in ("server_instructions", "server_pointer", "bundle_instruction", "instruction_revision",
                         "bundle_pointer", "bundle_section_pointer")}


def test_both_switches_leave_logging_to_the_hook_in_the_server_and_success_instructions(switches):
    server, bundle = protocol_text.server_instructions(), protocol_text.bundle_instruction()
    hook_rule = " ".join(protocol_text.part("finish", "hook").split())
    assert hook_rule.startswith("Do not call log_interaction:")
    for text in (server, bundle, protocol_text.server_pointer()):
        assert hook_rule in text
        assert "Before delivery call log_interaction" not in text
        assert "call log_interaction with the complete answer" not in text
        assert "Before logging, replace secrets" not in text
    assert " ".join(protocol_text.part("finish", "server-hook").split()) in server
    assert " ".join(protocol_text.part("finish", "bundle-hook").split()) in bundle
    # The hook rule takes the place of the logging line, inside the shorter window with the
    # delegation request after it.
    for phrase in ("Do not call log_interaction", "Require status: prepared."):
        assert server.index(phrase) + len(phrase) < OLDER_CLAUDE_CODE_WINDOW, phrase


@pytest.mark.parametrize("flag,profile", [(True, "interactive"), (False, "headless")])
def test_either_switch_alone_leaves_the_instructions_unchanged(monkeypatch, flag, profile):
    monkeypatch.setattr(config, "TURN_LOG_HOOK", False)
    monkeypatch.setattr(config, "CLIENT_PROFILE", "interactive")
    default = _renders()
    monkeypatch.setattr(config, "TURN_LOG_HOOK", flag)
    monkeypatch.setattr(config, "CLIENT_PROFILE", profile)
    assert _renders() == default
    monkeypatch.setattr(config, "TURN_LOG_HOOK", True)
    monkeypatch.setattr(config, "CLIENT_PROFILE", "headless")
    changed = _renders()
    assert all(changed[name] != default[name] for name in default)


@pytest.mark.parametrize("surface", ["server_instructions", "bundle_instruction"])
def test_the_hook_variant_repeats_no_sentence(switches, surface):
    text = getattr(protocol_text, surface)()
    parts = re.split(r"\n\n|(?<=\.) (?=[A-Z])", text)
    assert protocol_text.repeated_sentences({str(i): chunk for i, chunk in enumerate(parts)}) == {}


def test_the_committed_files_never_depend_on_the_switches(switches):
    assert protocol_text.stale_files() == []


@pytest.mark.parametrize("surface", ["bundle_pointer", "bundle_section_pointer"])
def test_the_pointers_leave_logging_to_the_hook(switches, surface):
    """A headless first activation gets the section pointer, not the full instruction (#734), so the
    pointers must not send the model back to log_interaction while the Stop hook logs the turn."""
    text = getattr(protocol_text, surface)()
    assert "Do not call log_interaction" in text
    assert "footer and log_interaction" not in text


# --- #734 review follow-ups -----------------------------------------------------------------------

def test_a_flow_persona_attributes_the_turn(switches, project, tmp_path):
    """run_flow nests the flow persona's SUCCESS under persona_activation; the hook must read it."""
    transcript = tmp_path / "flow.jsonl"
    rows = [{"type": "user", "promptId": "p1", "message": {"role": "user", "content": "review the PR"}},
            {"type": "assistant", "message": {"id": "m1", "content": [
                {"type": "tool_use", "id": "t1", "name": "mcp__agents-core__run_flow",
                 "input": {"flow": "pr-review"}}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": json.dumps(
                    {"status": "needs_execution",
                     "persona_activation": {"status": "SUCCESS", "persona": PERSONA}})}]}},
            {"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": "Reviewed."}]}}]
    transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    hook.record_turn(stop_event(project, transcript))
    [entry] = entries(project)
    assert entry.action.startswith("Agent: software_engineer") and entry.action.endswith("action=switch")


def test_reminders_are_stripped_only_before_the_prompt_and_in_linear_time():
    quoted = "Explain this tag: <system-reminder>quoted</system-reminder> in my notes"
    assert hook._without_leading_reminders(quoted) == quoted
    assert hook._without_leading_reminders(
        "<system-reminder>a</system-reminder>\n <system-reminder>b</system-reminder> prompt") == "prompt"
    assert hook._without_leading_reminders("<system-reminder>never closed prompt") == \
        "<system-reminder>never closed prompt"
    import time
    started = time.monotonic()
    hook._without_leading_reminders(" " * 200_000 + "<system-reminder>" * 20_000 + "x")
    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize("secret,kept", [
    (FAKE_OPENAI_PROJECT_KEY, "sk-proj-"),
    ("xoxb-1234567890-abcdefghij", "xoxb-"),
    ("AIzaSyA1234567890abcdefghijklmnopqrstu", "AIza"),
    (FAKE_JWT, "eyJ"),
    ("aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "aws_secret_access_key="),
    (f'"capability":"{FAKE_CAPABILITY}"', '"capability":"'),
    ('{"password":"hunter2"}', '{"password":"'),
    ("password=short1", "password="),
    ("ghp_abcdefghijklmnop1234", "ghp_"),
])
def test_the_hook_masks_common_secret_formats(secret, kept):
    from src.schedules.records import mask
    masked = hook._masked(f"before {secret} after", mask)
    assert masked.startswith(f"before {kept}***") and masked.endswith(" after")
    assert secret[len(kept):].rstrip('"') not in masked


def test_a_bare_capability_in_prose_is_masked_and_a_hex_digest_is_not():
    from src.schedules.records import mask
    capability = FAKE_CAPABILITY
    digest = "a3f1c2" * 7 + "a"
    masked = hook._masked(f"use {capability} and commit {digest}", mask)
    assert capability not in masked and digest in masked


@pytest.mark.parametrize("token", [
    "Ab" * 21 + "C",    # no digit
    "q7" * 21 + "z",    # no uppercase
    "Q7" * 21 + "Z",    # no lowercase
    "Ab_-" * 10 + "Cde",  # no digit, with the URL-safe punctuation
])
def test_a_capability_missing_a_character_class_is_still_masked(token):
    """#734 review: secrets.token_urlsafe(32) can lack a digit or a case."""
    from src.schedules.records import mask
    assert len(token) == 43
    assert hook._masked(f"use {token} now", mask) == "use *** now"


@pytest.mark.parametrize("text,leak", [
    ('{"password":"abc\'def456"}', "def456"),
    ('{"password":"ab\\"cd456 tail"}', "cd456"),
    ("password='x\"yz789 tail'", "yz789"),
    ("token: 'it\\'s-sec456'", "s-sec456"),
])
def test_a_quoted_secret_is_masked_up_to_its_own_closing_quote(text, leak):
    """#734 review: the other quote character or an escaped quote must not end the value."""
    from src.schedules.records import mask
    masked = hook._masked(text, mask)
    assert leak not in masked and "***" in masked


def test_a_prompt_without_text_ends_the_previous_turn(switches, project, tmp_path):
    """#734 review: an image-only prompt must not lend the previous prompt to its answer."""
    transcript = tmp_path / "image.jsonl"
    rows = [{"type": "user", "promptId": "p1", "message": {"role": "user", "content": "first question"}},
            {"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "First."}]}},
            {"type": "user", "promptId": "p2", "message": {"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}}]}},
            {"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": "Second."}]}}]
    transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    assert hook.read_turn(transcript.read_text(encoding="utf-8").splitlines()) is None
