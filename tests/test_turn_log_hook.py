"""The Stop hook that logs a turn in place of the model, and the protocol text it needs (#734, #701)."""
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


def test_a_prompt_id_missing_from_the_transcript_logs_nothing(switches, project, tmp_path):
    """#734 review: the hook's prompt not yet written must not lend an earlier prompt its answer."""
    transcript = write_transcript(tmp_path / "t.jsonl", ("route_and_load", {"query": "x"}))
    result = hook.record_turn(stop_event(project, transcript, prompt_id="p3",
                                         last_assistant_message="The third answer."))
    assert result == {"status": "skipped", "reason": "no_prompt"}
    assert not (project / "history.md").exists()
    assert hook.read_turn(transcript.read_text(encoding="utf-8").splitlines(), "p3") is None


def test_without_a_prompt_id_the_last_prompt_starts_the_turn_and_with_one_it_must_match():
    rows = [{"type": "user", "message": {"role": "user", "content": "only question"}},
            {"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "Only."}]}}]
    lines = [json.dumps(row) for row in rows]
    assert hook.read_turn(lines).prompt == "only question"
    assert hook.read_turn(lines, "p9") is None  # #734 review: no fallback for an unmatched id


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
def test_a_turn_without_an_applied_persona_is_logged_unverified(switches, project, tmp_path, response):
    """#701: like a log_interaction call without a descriptor, the turn is written and marked unverified."""
    transcript = tmp_path / "t.jsonl"
    rows = [{"type": "user", "message": {"role": "user", "content": "question"}},
            {"type": "assistant", "message": {"id": "m1", "content": [
                {"type": "tool_use", "id": "t1", "name": "mcp__Agents-Core__route_and_load", "input": {}}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": json.dumps({"result": json.dumps(response)})}]}},
            {"type": "assistant", "message": {"id": "m2", "content": [{"type": "text", "text": "Answer."}]}}]
    transcript.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    assert hook.record_turn(stop_event(project, transcript))["status"] == "recorded"
    [entry] = entries(project)
    assert (entry.intent, entry.outcome) == ("question", "Answer.")
    assert entry.action == "Agent: unattributed Persona (unverified): missing=persona; action=unspecified"


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


def test_a_bare_capability_in_prose_is_masked_and_commit_and_digest_hashes_are_not():
    from src.schedules.records import mask
    commit, digest = "a3f1c2" * 6 + "a3f1", "b7" * 32
    masked = hook._masked(f"use {FAKE_CAPABILITY} at commit {commit} with digest {digest}", mask)
    assert FAKE_CAPABILITY not in masked and commit in masked and digest in masked


@pytest.mark.parametrize("token", [
    "Ab" * 21 + "C",    # no digit
    "q7" * 21 + "z",    # no uppercase
    "Q7" * 21 + "Z",    # no lowercase
    "Ab_-" * 10 + "Cde",  # no digit, with the URL-safe punctuation
    "a" * 42 + "0",     # lowercase hex only
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
    ('password="abc\ndef456" after', "def456"),
    ("password='abc\ndef456' after", "def456"),
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


# --- Interactive sessions (#701) ------------------------------------------------------------------


def rows_file(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def activation_rows(tool_id="t1"):
    return [{"type": "assistant", "message": {"id": "a" + tool_id, "content": [
                {"type": "tool_use", "id": tool_id, "name": "mcp__Agents-Core__route_and_load", "input": {}}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": json.dumps({"status": "SUCCESS",
                                                                                       "persona": PERSONA})}]}}]


def log_rows(status, tool_id="l1"):
    """A log_interaction call and its result, whose history status is ``status``."""
    return [{"type": "assistant", "message": {"id": "a" + tool_id, "content": [
                {"type": "tool_use", "id": tool_id, "name": "mcp__Agents-Core__log_interaction", "input": {}}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": json.dumps(
                    {"result": json.dumps({"status": "success", "history": {"status": status}})})}]}}]


def answer_row(text, message_id="m9"):
    return {"type": "assistant", "message": {"id": message_id, "content": [{"type": "text", "text": text}]}}


def test_the_interactive_argument_alone_switches_the_hook_on(project, monkeypatch, capsys, tmp_path):
    monkeypatch.setattr(config, "TURN_LOG_HOOK", False)
    monkeypatch.setattr(config, "CLIENT_PROFILE", "interactive")
    assert hook.record_turn(stop_event(project, prompt_id="prompt-2")) == {"status": "skipped", "reason": "disabled"}
    assert run_main(monkeypatch, capsys, json.dumps(stop_event(project, prompt_id="prompt-2")).encode()) == (0, "")
    assert not (project / "history.md").exists()
    event = stop_event(project, prompt_id="prompt-2", last_assistant_message="The delivered answer.\n\n" + FOOTER)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(event).encode())))
    assert hook.main(["--interactive"]) == 0
    [entry] = entries(project)
    assert entry.intent == PROMPT and entry.outcome == "The delivered answer. " + FOOTER
    assert entry.metadata["logged_by"] == "stop_hook" and entry.metadata["client"] == "claude-code"
    assert entry.metadata["prompt_id"] == "prompt-2"


def test_the_logged_answer_is_the_last_assistant_text_of_the_turn(project, tmp_path):
    """The answer the model delivered last, after a note it added, is the one the journal keeps."""
    transcript = rows_file(tmp_path / "t.jsonl", [
        {"type": "user", "promptId": "p1", "turnOrigin": "human", "message": {"role": "user", "content": "вопрос"}},
        *activation_rows(), answer_row("Answer in English, reworded.", "m1"),
        answer_row("Ответ на русском.\n\n" + FOOTER, "m2")])
    assert hook.record_turn(stop_event(project, transcript, prompt_id="p1"), interactive=True)["status"] == "recorded"
    [entry] = entries(project)
    assert entry.outcome == "Ответ на русском. " + FOOTER
    assert entry.action.startswith("Agent: software_engineer Persona (client-reported)")


@pytest.mark.parametrize("status,written", [("recorded", False), ("duplicate", False), ("error", True)])
def test_a_turn_the_model_logged_is_left_alone(project, tmp_path, status, written):
    """Where the server kept the model's logging step, the hook adds no second entry (#701)."""
    transcript = rows_file(tmp_path / "t.jsonl", [
        {"type": "user", "promptId": "p1", "message": {"role": "user", "content": "question"}},
        *activation_rows(), *log_rows(status), answer_row("Answer.")])
    result = hook.record_turn(stop_event(project, transcript, prompt_id="p1"), interactive=True)
    assert (result["status"] == "recorded") is written
    if not written:
        assert result == {"status": "skipped", "reason": "logged_by_model"}


def test_a_log_call_of_an_earlier_turn_does_not_count(project, tmp_path):
    transcript = rows_file(tmp_path / "t.jsonl", [
        {"type": "user", "promptId": "p1", "message": {"role": "user", "content": "first"}},
        *activation_rows(), *log_rows("recorded"), answer_row("First.", "m1"),
        {"type": "user", "promptId": "p2", "message": {"role": "user", "content": "second"}},
        answer_row("Second.", "m2")])
    assert hook.record_turn(stop_event(project, transcript, prompt_id="p2"), interactive=True)["status"] == "recorded"
    [entry] = entries(project)
    assert (entry.intent, entry.outcome) == ("second", "Second.")
    assert entry.action.endswith("action=keep")


def test_a_stop_hook_re_entry_writes_no_second_entry(project, tmp_path):
    """Another Stop hook asked for one more step: the turn ends again with a new last message."""
    feedback = {"type": "user", "isMeta": True, "promptId": "p1",
                "message": {"role": "user", "content": "Stop hook feedback:\nMark a chapter."}}
    transcript = rows_file(tmp_path / "t.jsonl", [
        {"type": "user", "promptId": "p1", "turnOrigin": "human", "message": {"role": "user", "content": "question"}},
        *activation_rows(), answer_row("The full answer.", "m1"), feedback, answer_row("Chapter marked.", "m2")])
    first = stop_event(project, transcript, prompt_id="p1", last_assistant_message="The full answer.")
    assert hook.record_turn(first, interactive=True)["status"] == "recorded"
    again = stop_event(project, transcript, prompt_id="p1", stop_hook_active=True,
                       last_assistant_message="Chapter marked.")
    assert hook.record_turn(again, interactive=True) == {"status": "skipped", "reason": "already_logged"}
    nameless = stop_event(project, transcript, stop_hook_active=True, last_assistant_message="Chapter marked.")
    assert hook.record_turn(nameless, interactive=True) == {"status": "skipped", "reason": "stop_hook_active"}
    [entry] = entries(project)
    assert entry.outcome == "The full answer."
    # The feedback row stays inside the turn: it starts none of its own.
    assert hook.read_turn(transcript.read_text(encoding="utf-8").splitlines()).prompt == "question"


@pytest.mark.parametrize("origin,meta", [("host_synthetic", False), ("task_notification", False),
                                         ("peer", True), ("scheduled", True)])
def test_a_turn_the_user_did_not_type_is_logged_once_as_its_own_turn(project, tmp_path, origin, meta):
    """A notification, a message from another session or a scheduled task opens its own turn."""
    opener = {"type": "user", "promptId": "p2", "turnOrigin": origin, "message": {
        "role": "user", "content": f"<{origin}>event</{origin}>"}}
    if meta:
        opener["isMeta"] = True
    transcript = rows_file(tmp_path / "t.jsonl", [
        {"type": "user", "promptId": "p1", "turnOrigin": "human", "message": {"role": "user", "content": "question"}},
        *activation_rows(), answer_row("First answer.", "m1"), opener, answer_row("Event handled.", "m2")])
    first = hook.record_turn(stop_event(project, transcript, prompt_id="p1",
                                        last_assistant_message="First answer."), interactive=True)
    second = stop_event(project, transcript, prompt_id="p2", last_assistant_message="Event handled.")
    assert first["status"] == hook.record_turn(second, interactive=True)["status"] == "recorded"
    assert hook.record_turn(second, interactive=True) == {"status": "skipped", "reason": "already_logged"}
    assert [(entry.intent, entry.outcome) for entry in entries(project)] == [
        ("question", "First answer."), (f"<{origin}>event</{origin}>", "Event handled.")]


def test_a_turn_without_a_persona_takes_the_agent_from_the_footer(project, tmp_path):
    transcript = rows_file(tmp_path / "t.jsonl", [
        {"type": "user", "promptId": "p1", "message": {"role": "user", "content": "question"}},
        answer_row("Answer.\n\n" + FOOTER)])
    hook.record_turn(stop_event(project, transcript, prompt_id="p1"), interactive=True)
    [entry] = entries(project)
    assert entry.action.startswith("Agent: software_engineer Persona (unverified): software_engineer; missing=")


def test_the_module_runs_as_an_interactive_stop_hook_command(project):
    env = {key: value for key, value in os.environ.items()
           if key not in ("AGENTS_TURN_LOG_HOOK", "AGENTS_CLIENT_PROFILE")}
    env["LANGFUSE_TRACING_ENABLED"] = "false"
    command = [sys.executable, "-m", "src.memory.turn_log_hook", "--interactive"]
    result = subprocess.run(command, input=json.dumps(stop_event(project, prompt_id="prompt-2")).encode(),
                            cwd=ROOT, env=env, capture_output=True, timeout=120)
    assert (result.returncode, result.stdout) == (0, b""), result.stderr
    [entry] = entries(project)
    assert entry.intent == PROMPT and entry.metadata["client"] == "claude-code"


# --- Which client's Stop hook logs (#701) ----------------------------------------------------------


@pytest.fixture
def claude_settings(tmp_path, monkeypatch):
    from src import client_instructions as ci
    ci._settings_of.cache_clear()
    path = tmp_path / "claude-home" / "settings.json"
    path.parent.mkdir()
    monkeypatch.setattr(ci, "_global_settings", lambda: path)
    monkeypatch.setattr(config, "TURN_LOG_HOOK", False)
    monkeypatch.setattr(config, "CLIENT_PROFILE", "interactive")
    yield path
    ci._settings_of.cache_clear()


def stop_settings(command, **extra):
    return json.dumps({"hooks": {"Stop": [{"matcher": "", "hooks": [{"type": "command", "command": command}]}]},
                       **extra})


def client(root, name="claude-code"):
    from types import SimpleNamespace
    return SimpleNamespace(root=root, client=name, error=None, cooperation_roots=(root,))


INTERACTIVE_COMMAND = "cd /opt/agents && .venv/bin/python -m src.memory.turn_log_hook --interactive"


@pytest.mark.parametrize("command,expected", [
    (INTERACTIVE_COMMAND, True),
    ("python -m src.memory.turn_log_hook", False),  # the headless registration
    ("python -m src.memory.turn_log_hook; echo --interactive", False),
    ("python -m src.other_hook --interactive", False),
])
def test_only_an_interactive_registration_in_the_users_settings_counts(claude_settings, project, command, expected):
    from src import client_instructions as ci
    claude_settings.write_text(stop_settings(command), encoding="utf-8")
    assert ci.stop_hook_logs(client(project)) is expected
    assert ci.stop_hook_logs(client(project, "codex")) is False
    assert ci.stop_hook_logs(client(project, None)) is False


def test_project_settings_count_and_disable_all_hooks_wins(claude_settings, project):
    from src import client_instructions as ci
    assert ci.stop_hook_logs(client(project)) is False
    (project / ".claude").mkdir()
    local = project / ".claude" / "settings.local.json"
    local.write_text(stop_settings(INTERACTIVE_COMMAND), encoding="utf-8")
    assert ci.stop_hook_logs(client(project)) is True
    (project / ".claude" / "settings.json").write_text(json.dumps({"disableAllHooks": True}), encoding="utf-8")
    assert ci.stop_hook_logs(client(project)) is False
    (project / ".claude" / "settings.json").write_text("{not json", encoding="utf-8")
    assert ci.stop_hook_logs(client(project)) is True


def test_the_process_wide_switches_hold_for_every_client(switches, project):
    from src import client_instructions as ci
    assert ci.stop_hook_logs(client(project, "codex")) is True
    assert ci.stop_hook_logs(None) is True


def test_one_daemon_tells_only_the_hooked_client_to_skip_log_interaction(claude_settings, project):
    from src import client_instructions as ci
    claude_settings.write_text(stop_settings(INTERACTIVE_COMMAND), encoding="utf-8")
    full = protocol_text.server_instructions()
    assert "Before delivery call log_interaction" in full
    hooked = ci.initialization_instructions(full, client(project))
    assert hooked == protocol_text.server_instructions(True)
    assert "Do not call log_interaction" in hooked and "Before delivery call log_interaction" not in hooked
    assert "Add no time line." in hooked
    for other in ("codex", "cursor", "claude-desktop", None):
        assert ci.initialization_instructions(full, client(project, other)) == full


@pytest.mark.asyncio
async def test_a_hooked_clients_success_leaves_logging_to_the_hook(claude_settings, project, monkeypatch):
    from unittest.mock import AsyncMock
    from types import SimpleNamespace
    from src import client_instructions as ci
    from src.engine import persona

    async def build(agent, query, history, tier=None, selection=None):
        return SimpleNamespace(
            agent=agent, bundle_revision="b" * 64, scope=f"Scope of {agent}", persona_block="Role",
            rules_block="Rules", skills_block="Skills", implants_block="Implants", skills_loaded=[],
            implants_loaded=[], rules_loaded=["answer-timestamp"], tier="standard")
    monkeypatch.setattr(persona, "build_persona_bundle", AsyncMock(side_effect=build))

    class Router:
        async def update_cache(self, *args):
            return None

    async def activate(context, agent, current=None):
        token = ci.REQUEST_CLIENT.set(context)
        try:
            return json.loads(await persona.load_persona(Router(), agent, "task", [], current))
        finally:
            ci.REQUEST_CLIENT.reset(token)

    claude_settings.write_text(stop_settings(INTERACTIVE_COMMAND), encoding="utf-8")
    first = await activate(client(project), "lawyer")
    assert first["instruction"] == protocol_text.bundle_instruction(True)
    assert first["instruction_revision"] == protocol_text.instruction_revision(True) != persona.INSTRUCTION_REVISION
    assert "Do not call log_interaction" in first["instruction"]
    assert "Add no time line, since no log_interaction call returns a timestamp." in first["instruction"]
    switch = await activate(client(project), "dba", first["persona"])
    assert switch["instruction"] == protocol_text.bundle_pointer(True)
    codex = await activate(client(project, "codex"), "lawyer")
    assert codex["instruction"] == persona.APPLY_INSTRUCTION
    assert codex["instruction_revision"] == persona.INSTRUCTION_REVISION


def test_the_hook_texts_state_the_time_line_rule():
    """#701: no hook variant asks for a timestamp that no call returns."""
    for text in (protocol_text.server_instructions(True), protocol_text.bundle_instruction(True),
                 protocol_text.bundle_pointer(True), protocol_text.bundle_section_pointer(True)):
        assert "no time line" in text.lower()
        assert "prepend it" not in text and "returns timestamp" not in text
    assert "add no time line, since no call\nreturns a `timestamp`" in protocol_text.template()


def test_a_project_registration_must_hold_in_every_linked_worktree(claude_settings, tmp_path):
    """A fixed identity serves the repository's linked worktrees, whose settings may differ."""
    from src import client_instructions as ci
    main, linked = tmp_path / "main", tmp_path / "linked"
    git = ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid"]
    subprocess.run([*git, "init", "-q", str(main)], check=True)
    subprocess.run([*git, "-C", str(main), "commit", "-q", "--allow-empty", "-m", "init"], check=True)
    subprocess.run([*git, "-C", str(main), "worktree", "add", "-q", "--detach", str(linked)], check=True)
    for checkout in (main, linked):
        (checkout / ".claude").mkdir()
    (main / ".claude" / "settings.local.json").write_text(stop_settings(INTERACTIVE_COMMAND), encoding="utf-8")
    assert ci.stop_hook_logs(client(main)) is False
    (linked / ".claude" / "settings.local.json").write_text(stop_settings(INTERACTIVE_COMMAND), encoding="utf-8")
    assert ci.stop_hook_logs(client(main)) is True
