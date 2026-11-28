"""The hook that logs a turn in place of the model, and the protocol text it needs (#734, #701)."""
import base64
import io
import json
import os
import random
import re
import string
import subprocess
import sys
import threading
import time
import tracemalloc
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.engine import config, protocol_text
from src.memory import fallback
from src.memory import history as history_module
from src.memory import turn_log_hook as hook
from src.memory import turn_redaction as redaction
from src.memory.history import HistoryReader, HistoryWriter, JournalBusy

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests" / "fixtures" / "claude_code_turn_transcript.jsonl"
PROMPT = "Now make it return the build version too."
ANSWER = "The health check now returns the build version from VERSION."
FOOTER = "**Agent**: software_engineer | **Skills**: skill-dev-testing"
REVISION = "ab" * 32
SESSION = "5f0c7a2e-1b6d-4c2a-9e51-0d7b3c4a8f10"
PERSONA = {"agent": "software_engineer", "activation_id": "act-1", "bundle_revision": REVISION,
           "scope": "Implement, debug and refactor software.", "skills_loaded": ["skill-dev-testing"],
           "implants_loaded": [], "rules_loaded": ["no-fabrication"]}
# Claude Code shows this many characters of the server instructions before v2.1.296 (4,096 since).
OLDER_CLAUDE_CODE_WINDOW = 2048
CLAUDE_CODE_WINDOW = 4096
# Fake secrets, split so that the tracked-files scan (tests/test_schedules_scan.py) finds no token
# or high-entropy string in this file's text; the ones below them are made at run time.
FAKE_ANTHROPIC_KEY = "sk-ant-" + "api03-abcdefghijklmnop"
FAKE_OPENAI_PROJECT_KEY = "sk-proj-" + "AbCdEf0123456789xyzXYZ"
FAKE_JWT = ".".join(["eyJhbGciOiJIUzI1NiJ9", "eyJzdWIiOiIxMjM0NTY3ODkwIn0",
                     "dozjgNryP4J3jVmNHl0w5" + "N_XgL0n3I9PlFUP0THsR8U"])
FAKE_CAPABILITY = "WPQbiALKXRfZMU80BVl1" + "LKvV6QkpPyif3h5H2UOEhb8"


def fake(length, alphabet=string.ascii_letters + string.digits, seed=0):
    """A random-looking run made at run time, so this file's own text holds no token."""
    generator = random.Random(seed)
    return "".join(generator.choice(alphabet) for _ in range(length))


@pytest.fixture(autouse=True)
def quick_and_isolated(monkeypatch, tmp_path):
    """No test waits seconds for a transcript row, and none writes the installation's fallback journal."""
    monkeypatch.setattr(hook, "TRANSCRIPT_WAIT_SECONDS", 0.0)
    monkeypatch.setattr(hook, "TRANSCRIPT_POLL_SECONDS", 0.01)
    monkeypatch.setattr(fallback, "ROOT_OVERRIDE", tmp_path / "fallback-journal")


@pytest.fixture
def switches(monkeypatch):
    monkeypatch.setattr(config, "TURN_LOG_HOOK", True)
    monkeypatch.setattr(config, "CLIENT_PROFILE", "headless")


@pytest.fixture
def model_logging(monkeypatch):
    monkeypatch.setattr(config, "TURN_LOG_HOOK", False)
    monkeypatch.setattr(config, "CLIENT_PROFILE", "interactive")


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "project"
    (root / "app").mkdir(parents=True)
    (root / "CLAUDE.md").write_text("", encoding="utf-8")
    monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT", raising=False)
    monkeypatch.delenv("CLAUDE_PROJECT_DIR", raising=False)
    return root


def stop_event(project, transcript=FIXTURE, **extra):
    return {"session_id": SESSION, "transcript_path": str(transcript), "cwd": str(project / "app"),
            "hook_event_name": "Stop", "stop_hook_active": False, **extra}


def prompt_event(project, **extra):
    return {"session_id": SESSION, "transcript_path": str(FIXTURE), "cwd": str(project / "app"),
            "hook_event_name": "UserPromptSubmit", "prompt": "Do it.", **extra}


def entries(root):
    return HistoryReader(str(root / "history.md"), str(root / "history")).journal_entries()


def run_main(monkeypatch, capsys, raw: bytes, argv=()) -> tuple[int, str, str]:
    """``main`` on ``raw`` as stdin: its exit code, stdout and stderr."""
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))
    code = hook.main(list(argv))
    output = capsys.readouterr()
    return code, output.out, output.err


def rows_file(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows), encoding="utf-8")
    return path


def opener(prompt_id, text, origin="human", **extra):
    return {"type": "user", "promptId": prompt_id, "turnOrigin": origin,
            "message": {"role": "user", "content": text}, **extra}


def activation_rows(tool_id="t1", status="SUCCESS", tool="route_and_load", arguments=None, persona=None):
    return [{"type": "assistant", "message": {"id": "a" + tool_id, "content": [
                {"type": "tool_use", "id": tool_id, "name": f"mcp__Agents-Core__{tool}", "input": arguments or {}}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": tool_id, "content": json.dumps(
                    {"status": status, "persona": persona or PERSONA})}]}}]


def log_rows(history, tool_id="l1", arguments=None, result=True):
    """A log_interaction call and, with ``result``, its result carrying ``history``."""
    call = {"type": "assistant", "message": {"id": "a" + tool_id, "content": [
        {"type": "tool_use", "id": tool_id, "name": "mcp__Agents-Core__log_interaction", "input": arguments or {}}]}}
    answer = {"type": "user", "message": {"role": "user", "content": [
        {"type": "tool_result", "tool_use_id": tool_id, "content": json.dumps(
            {"result": json.dumps({"timestamp": "2026.11.27 10:00:00", "history": history})})}]}}
    return [call, answer] if result else [call]


def answer_row(text, message_id="m9"):
    return {"type": "assistant", "message": {"id": message_id, "content": [{"type": "text", "text": text}]}}


FEEDBACK = {"type": "user", "isMeta": True, "promptId": "p1",
            "message": {"role": "user", "content": "Stop hook feedback:\nMark a chapter."}}
LONG_ANSWER = ("The release notes list the four changes, each with its pull request, and the upgrade "
               "steps in the order the installer runs them.\n\n" + FOOTER)
CORRECTED = ("The release notes list the five changes, each with its pull request, and the upgrade "
             "steps in the order the installer runs them.\n\n" + FOOTER)


# --- The Stop hook ------------------------------------------------------------------------------


def test_the_fixture_turn_is_one_entry_with_its_prompt_answer_and_persona(switches, project, monkeypatch, capsys):
    result = hook.record_turn(stop_event(project, prompt_id="prompt-2"))
    assert result["status"] == "recorded" and result["state"] == "durably_accepted"
    [entry] = entries(project)
    assert entry.intent == PROMPT  # the system reminder in front of the prompt is not part of it
    assert entry.outcome.startswith(ANSWER) and entry.outcome.endswith("**Rules**: no-fabrication")
    assert "Let me read the handler first." not in entry.outcome and "sidechain" not in entry.outcome
    assert entry.action == ("Agent: software_engineer Persona (client-reported): software_engineer; "
                            f"activation=act-1; revision={REVISION}; action=keep")
    assert entry.metadata["logged_by"] == "stop_hook" and entry.metadata["client"] == "claude-code"
    assert (entry.metadata["session_id"], entry.metadata["prompt_id"]) == (SESSION, "prompt-2")
    assert re.fullmatch(r"\d{4}\.\d{2}\.\d{2} \d{2}:\d{2}:\d{2}", entry.metadata["answer_timestamp"])
    # The same Stop event again, through the command, and once without its prompt id, which the
    # opener row supplies: the journal keeps one entry.
    assert run_main(monkeypatch, capsys, json.dumps(stop_event(project, prompt_id="prompt-2")).encode()) == (0, "", "")
    assert hook.record_turn(stop_event(project))["entry_id"] == result["entry_id"]
    assert len(entries(project)) == 1


@pytest.mark.parametrize("given", [b"", b"not json", b"[]", b'"Stop"', b"{}", b" " * (hook.MAX_INPUT_BYTES + 1),
                                   {"hook_event_name": "SessionStart", "transcript_path": str(FIXTURE)}])
def test_input_that_is_no_stop_event_exits_zero_without_writing(switches, project, monkeypatch, capsys, given):
    raw = given if isinstance(given, bytes) else json.dumps({**given, "cwd": str(project)}).encode()
    assert run_main(monkeypatch, capsys, raw) == (0, "", "")
    assert not (project / "history.md").exists()


@pytest.mark.parametrize("transcript", [None, "relative/session.jsonl", "/nonexistent/session.jsonl", 7])
def test_a_stop_event_that_cannot_be_logged_exits_one_with_the_reason(switches, project, monkeypatch, capsys,
                                                                       transcript):
    """#701 review: a failure is a non-blocking hook error the client shows, never a silent exit 0."""
    event = {"hook_event_name": "Stop", "cwd": str(project), "transcript_path": transcript}
    code, out, err = run_main(monkeypatch, capsys, json.dumps(event).encode())
    assert (code, out) == (1, "") and err.startswith("agents-core turn log: turn not logged (")
    assert not (project / "history.md").exists()


def test_a_malformed_transcript_writes_nothing(switches, project, tmp_path):
    transcript = tmp_path / "broken.jsonl"
    transcript.write_text('{"type":"user"\nnot json\n[1, 2]\n{"type": "assistant", "message": 7}\n'
                          '{"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": ["t"]}]}}\n',
                          encoding="utf-8")
    with pytest.raises(hook.TurnNotLogged):
        hook.record_turn(stop_event(project, transcript))
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
    activation = activation_rows("t1", status, tool, arguments)
    second = [{"type": "user", "promptId": "p2", "message": {"role": "user", "content": "second"}}]
    rows += second + activation if activate_in_turn else activation + second
    rows.append(answer_row("Done.", "m2"))
    return rows_file(path, rows)


def test_a_prompt_id_missing_from_the_transcript_fails_after_waiting_for_it(switches, project, tmp_path):
    """#734 review: the hook's prompt not yet written must not lend an earlier prompt its answer; #701
    review: the turn is then a visible failure, after the transcript had time to catch up."""
    transcript = write_transcript(tmp_path / "t.jsonl", ("route_and_load", {"query": "x"}))
    with pytest.raises(hook.TurnNotLogged, match="prompt is not in the transcript"):
        hook.record_turn(stop_event(project, transcript, prompt_id="p3", last_assistant_message="The third answer."))
    assert not (project / "history.md").exists()
    assert hook.read_turn(transcript.read_text(encoding="utf-8").splitlines(), "p3") is None


def test_without_a_prompt_id_the_last_prompt_starts_the_turn_and_with_one_it_must_match():
    rows = [{"type": "user", "message": {"role": "user", "content": "only question"}},
            {"type": "assistant", "message": {"id": "m1", "content": [{"type": "text", "text": "Only."}]}}]
    lines = [json.dumps(row) for row in rows]
    assert hook.read_turn(lines).prompt == "only question"
    assert hook.read_turn(lines, "p9") is None  # #734 review: no fallback for an unmatched id


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
    rows_file(transcript, rows)
    assert hook.record_turn(stop_event(project, transcript))["status"] == "recorded"
    [entry] = entries(project)
    assert (entry.intent, entry.outcome) == ("question", "Answer.")
    assert entry.action == "Agent: unattributed Persona (unverified): missing=persona; action=unspecified"


def test_the_hook_inputs_last_message_wins_without_its_time_line_and_secrets(switches, project):
    given = f"2026.11.24 10:00:00\n\nDeployed with token {FAKE_ANTHROPIC_KEY}.\n\n" + FOOTER
    hook.record_turn(stop_event(project, prompt_id="prompt-2", last_assistant_message=given))
    [entry] = entries(project)
    # "token" names a secret: the rest of its line goes (owner's decision 2026-11-28).
    assert entry.outcome == f"Deployed with token{redaction.MASK} {FOOTER}"
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


def test_the_module_runs_as_a_stop_hook_command(project):
    env = {**os.environ, "AGENTS_TURN_LOG_HOOK": "1", "AGENTS_CLIENT_PROFILE": "headless",
           "LANGFUSE_TRACING_ENABLED": "false"}
    command = [sys.executable, "-m", "src.memory.turn_log_hook"]
    for payload in (b"not json", json.dumps(stop_event(project)).encode()):
        result = subprocess.run(command, input=payload, cwd=ROOT, env=env, capture_output=True, timeout=120)
        assert (result.returncode, result.stdout) == (0, b""), result.stderr
    [entry] = entries(project)
    assert entry.intent == PROMPT and entry.outcome.startswith(ANSWER)


# --- An unresolved project (#701 review, #292) ---------------------------------------------------


def unresolved_cwd(case, project, tmp_path, monkeypatch):
    """A session directory whose project does not resolve: the home directory, or a project whose
    CLAUDE.md leads out of it."""
    if case == "home":
        home = tmp_path / "home"
        home.mkdir()
        for name in ("HOME", "USERPROFILE"):  # what os.path.expanduser reads on POSIX and on Windows
            monkeypatch.setenv(name, str(home))
        return str(home)
    outside = tmp_path / "outside.md"
    outside.write_text("", encoding="utf-8")
    (project / "CLAUDE.md").unlink()
    try:
        (project / "CLAUDE.md").symlink_to(outside)
    except OSError:
        pytest.skip("this system cannot create a symbolic link")
    return str(project / "app")


@pytest.mark.parametrize("case", ["home", "escaping CLAUDE.md"])
def test_an_unresolved_project_is_never_told_the_hook_logs(case, project, tmp_path, monkeypatch, capsys):
    cwd = unresolved_cwd(case, project, tmp_path, monkeypatch)
    assert hook.turn_notice({**prompt_event(project), "cwd": cwd}, interactive=True) is None
    code, out, _ = run_main(monkeypatch, capsys, json.dumps({**prompt_event(project), "cwd": cwd}).encode(),
                            ["--interactive"])
    assert (code, out) == (0, "")


@pytest.mark.parametrize("case", ["home", "escaping CLAUDE.md"])
def test_an_unresolved_projects_turn_reaches_the_fallback_journal(case, project, tmp_path, monkeypatch):
    """The journal log_interaction uses for such a turn (#292), with the directory as unverified metadata."""
    cwd = unresolved_cwd(case, project, tmp_path, monkeypatch)
    result = hook.record_turn({**stop_event(project, prompt_id="prompt-2"), "cwd": cwd}, interactive=True)
    assert result["status"] == "recorded"
    [entry] = entries(tmp_path / "fallback-journal")
    assert entry.intent == PROMPT and entry.metadata["claimed_workspace"] == cwd
    assert entry.metadata["unresolved"] == ("workspace_unsafe" if case == "home" else "workspace_invalid")
    assert not (project / "history.md").exists()


# --- The protocol text ---------------------------------------------------------------------------


def _renders():
    return {name: getattr(protocol_text, name)()
            for name in ("server_instructions", "server_pointer", "bundle_instruction", "instruction_revision",
                         "bundle_pointer", "bundle_section_pointer", "no_change_instruction")}


def flat(text):
    return " ".join(text.split())


def test_both_switches_leave_logging_to_the_hook_in_the_server_and_success_instructions(switches):
    server, bundle = protocol_text.server_instructions(), protocol_text.bundle_instruction()
    hook_rule, calls = (flat(protocol_text.part("finish", name)) for name in ("hook", "hook-calls"))
    assert "make no per-turn log_interaction call" in hook_rule
    for text in (server, bundle, protocol_text.server_pointer()):
        assert hook_rule in text and calls in text
        assert "Before delivery call log_interaction" not in text
        assert "call log_interaction with the complete answer" not in text
        assert "Before logging, replace secrets" not in text
        assert "turn-log notice" not in text  # every turn is the hook's here
    assert flat(protocol_text.part("finish", "server-hook")) in server
    assert flat(protocol_text.part("finish", "bundle-hook")) in bundle
    # The hook rule takes the place of the logging line, inside the shorter window with the
    # delegation request after it.
    for phrase in ("make no per-turn log_interaction call", "Require status: prepared."):
        assert server.index(phrase) + len(phrase) < OLDER_CLAUDE_CODE_WINDOW, phrase


def test_a_call_a_flow_or_the_user_asks_for_is_still_made_with_its_time_line():
    """#701 review: thread-close's record and tags and autodev-retro's tag need the model's own call;
    the notice says when a redacted call or journal=false is due, that journal=false takes
    placeholders, and that the time line keeps the answer-timestamp condition."""
    calls = flat(protocol_text.part("finish", "hook-calls"))
    for phrase in ("when a flow or the user asks for it", "record, tags or files",
                   "when the prompt or the answer holds personal details or a secret the hook's detectors "
                   "may not recognise: log the turn redacted yourself, or pass journal=false",
                   "with placeholders, not the turn's text, as query and response_content",
                   "The hook then leaves that turn to the call.",
                   "Add a time line only when the footer lists answer-timestamp and such a call returns a timestamp."):
        assert phrase in calls, phrase
    assert ("a call a flow or the user asks for is still made; its timestamp gives a time line only when the "
            "footer lists answer-timestamp") in protocol_text.bundle_pointer(True)


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
@pytest.mark.parametrize("hook_logs", [True, False])
def test_no_variant_repeats_a_sentence(surface, hook_logs):
    text = getattr(protocol_text, surface)(hook_logs)
    parts = re.split(r"\n\n|(?<=\.) (?=[A-Z])", text)
    assert protocol_text.repeated_sentences({str(i): chunk for i, chunk in enumerate(parts)}) == {}


def test_the_committed_files_never_depend_on_the_switches(switches):
    assert protocol_text.stale_files() == []


@pytest.mark.parametrize("surface", ["bundle_pointer", "bundle_section_pointer"])
def test_the_pointers_leave_logging_to_the_hook(switches, surface):
    """A headless first activation gets the section pointer, not the full instruction (#734), so the
    pointers must not send the model back to log_interaction while the Stop hook logs the turn."""
    text = getattr(protocol_text, surface)()
    assert "no per-turn log_interaction call" in text
    assert "footer and log_interaction" not in text


def test_the_model_logging_texts_yield_to_the_turn_log_notice(model_logging):
    """#701 review: the session's own hook decides its turns; every surface a hooked Claude Code
    session reads names the notice, and the latest SUCCESS outranks the server instructions."""
    rule = flat(protocol_text.part("finish", "notice-rule"))
    server = protocol_text.server_instructions()
    assert rule in server and server.index(rule) + len(rule) < CLAUDE_CODE_WINDOW
    assert "the latest SUCCESS or NO_CHANGE decides over the server instructions" in rule
    assert rule in protocol_text.bundle_instruction()
    for text in (protocol_text.bundle_pointer(), protocol_text.bundle_section_pointer(),
                 protocol_text.no_change_instruction()):
        assert "footer and log_interaction unless this turn's Agents-Core turn-log notice" in text
    template = flat(protocol_text.template())
    assert ("When this turn's prompt carries the Agents-Core turn-log notice of the session's own Stop hook, "
            "or the server instructions or the latest `SUCCESS` or `NO_CHANGE`, which decides over them, say "
            "that this client's Stop hook logs each turn, follow that notice or instruction in place of the "
            "`log_interaction` steps of this protocol") in template
    reminder = flat(protocol_text.memory_reminder())
    assert ("Unless this turn's Agents-Core turn-log notice, or the server instructions or the latest SUCCESS "
            "or NO_CHANGE, report") in reminder
    # The turn-end sentence no longer tells a hooked session to log (#701 review).
    assert "log it and include the footer" not in reminder and "is logged like any other" in reminder


def test_the_notice_states_what_the_hook_records_and_what_the_model_still_does():
    notice = protocol_text.turn_log_notice()
    assert notice.startswith("Agents-Core turn log: this session's Stop hook records this turn")
    for phrase in ("still ends with the footer", "journal=false", "when a flow or the user asks for it",
                   "personal details or a secret the hook's detectors may not recognise",
                   "placeholders, not the turn's text",
                   "Add a time line only when the footer lists answer-timestamp and such a call returns a timestamp",
                   "Before a closing call such as wrap_up, deliver the final answer as text"):
        assert phrase in notice, phrase
    assert len(notice) < 2000  # Claude Code adds up to 10,000 characters of a hook's context in full


@pytest.mark.parametrize("hook_logs", [False, True])
def test_no_change_carries_the_current_logging_decision(monkeypatch, hook_logs):
    """#701 review: a NO_CHANGE says how to finish too, so it never leaves a stale decision standing."""
    from src.engine import persona
    from src.schemas.protocol import PersonaDescriptor
    monkeypatch.setattr(config, "TURN_LOG_HOOK", hook_logs)
    monkeypatch.setattr(config, "CLIENT_PROFILE", "headless" if hook_logs else "interactive")
    response = json.loads(persona.unchanged(PersonaDescriptor.model_validate(PERSONA), "request-1"))
    assert response["instruction"] == protocol_text.no_change_instruction()
    assert response["instruction"].startswith("Keep the existing activation and its instructions, and finish")
    assert ("no per-turn log_interaction call" in response["instruction"]) is hook_logs


def test_the_served_flow_and_persona_texts_count_the_hooks_turns():
    thread_close = flat((ROOT / "flows" / "thread-close.md").read_text(encoding="utf-8"))
    assert "the `read_history` entries whose metadata holds `logged_by` `stop_hook`" in thread_close
    assert "| `log` | `log_interaction`, and the Stop hook in a session that registers it |" in thread_close
    assert "also in a session whose Stop hook logs its turns: the flow asks for this call" in thread_close
    retro = flat((ROOT / "flows" / "autodev-retro.md").read_text(encoding="utf-8"))
    assert "also in a session whose Stop hook logs its turns" in retro
    researcher = (ROOT / "agents" / "purchase_researcher" / "system_prompt.mdc").read_text(encoding="utf-8")
    assert "unless the protocol leaves the turn to the session's Stop hook" in researcher


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
    rows_file(transcript, rows)
    hook.record_turn(stop_event(project, transcript))
    [entry] = entries(project)
    # No footer: the descriptor is reported unverified (#701 review).
    assert entry.action.startswith("Agent: software_engineer") and entry.action.endswith("action=switch")


def test_reminders_are_stripped_only_before_the_prompt_and_in_linear_time():
    quoted = "Explain this tag: <system-reminder>quoted</system-reminder> in my notes"
    assert hook._without_leading_reminders(quoted) == quoted
    assert hook._without_leading_reminders(
        "<system-reminder>a</system-reminder>\n <system-reminder>b</system-reminder> prompt") == "prompt"
    assert hook._without_leading_reminders("<system-reminder>never closed prompt") == \
        "<system-reminder>never closed prompt"
    started = time.monotonic()
    hook._without_leading_reminders(" " * 200_000 + "<system-reminder>" * 20_000 + "x")
    assert time.monotonic() - started < 1.0


@pytest.mark.parametrize("secret,kept", [
    (FAKE_OPENAI_PROJECT_KEY, "sk-proj-<secret> after"),
    ("xoxb-1234567890-abcdefghij", "xoxb-<secret> after"),
    ("AIzaSyA1234567890abcdefghijklmnopqrstu", "AIza<secret> after"),
    (FAKE_JWT, "eyJ<secret> after"),
    ("ghp_abcdefghijklmnop1234", "ghp_*** after"),
    # A label naming a secret loses the rest of its line (owner's decision 2026-11-28).
    ("aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "aws_secret_access_key<secret>"),
    (f'"capability":"{FAKE_CAPABILITY}"', '"capability<secret>'),
    ('{"password":"hunter2"}', '{"password<secret>'),
    ("password=short1", "password<secret>"),
])
def test_the_hook_masks_common_secret_formats(secret, kept):
    masked, clean = hook.redact(f"before {secret} after")
    assert clean and masked == f"before {kept}"


def test_a_bare_capability_in_prose_is_masked_and_commit_and_digest_hashes_are_not():
    commit, digest = "a3f1c2" * 6 + "a3f1", "b7" * 32
    masked, clean = hook.redact(f"use {FAKE_CAPABILITY} at commit {commit} with digest {digest}")
    assert clean and FAKE_CAPABILITY not in masked and commit in masked and digest in masked


@pytest.mark.parametrize("token", [
    "Ab" * 21 + "C",    # no digit
    "q7" * 21 + "z",    # no uppercase
    "Q7" * 21 + "Z",    # no lowercase
    "Ab_-" * 10 + "Cde",  # no digit, with the URL-safe punctuation
    "a" * 42 + "0",     # lowercase hex only
])
def test_a_capability_missing_a_character_class_is_still_masked(token):
    """#734 review: secrets.token_urlsafe(32) can lack a digit or a case."""
    assert len(token) == 43
    assert hook.redact(f"use {token} now") == (f"use {redaction.MASK} now", True)


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
    masked, clean = hook.redact(text)
    assert clean and leak not in masked and redaction.MASK in masked


# --- Secrets cleaned with the repository's own detectors (#701, owner's decision 2026-11-27) ----


def real_format_secrets():
    """One secret of each format the repository's detectors know, made at run time: its name, the
    text that carries it, and the secret itself."""
    alnum, upper = string.ascii_letters + string.digits, string.ascii_uppercase + string.digits
    key_body = "\n".join(fake(64, alnum + "+/", seed) for seed in range(3))
    bearer, password, labelled = fake(40, alnum, 17), fake(12, alnum, 18), fake(10, string.ascii_lowercase, 19) + "!42"
    plain = {
        "GitHub classic token": "ghp_" + fake(36, alnum, 1),
        "GitHub fine-grained token": "github_pat_" + fake(30, alnum + "_", 2),
        "GitLab token": "glpat-" + fake(20, alnum, 3),
        "Anthropic key": "sk-ant-api03-" + fake(40, alnum, 4),
        "OpenAI project key": "sk-proj-" + fake(40, alnum, 5),
        "OpenRouter key": "sk-or-v1-" + fake(40, alnum, 6),
        "AWS access key id": "AKIA" + fake(16, upper, 7),
        "Slack token": "xoxb-" + fake(12, string.digits, 8) + "-" + fake(24, alnum, 9),
        "Google API key": "AIza" + fake(35, alnum, 10),
        "Stripe key": "sk_live_" + fake(24, alnum, 11),
        "Hugging Face token": "hf_" + fake(34, alnum, 12),
        "npm token": "npm_" + fake(36, alnum, 13),
        "JWT": "eyJ" + fake(20, alnum, 14) + "." + fake(30, alnum, 15) + "." + fake(30, alnum, 16),
        "private key": "-----BEGIN RSA " + "PRIVATE KEY-----\n" + key_body + "\n-----END RSA " + "PRIVATE KEY-----",
    }
    carried = {name: (secret, secret) for name, secret in plain.items()}
    carried["Bearer token"] = ("Authorization: Bearer " + bearer, bearer)
    carried["URL credential"] = ("https://deploy:" + password + "@registry.example.com/simple", password)
    carried["labelled password"] = ("the database password is " + labelled, labelled)
    return carried


@pytest.mark.parametrize("name", sorted(real_format_secrets()))
def test_a_real_format_secret_is_cleaned(name):
    text, secret = real_format_secrets()[name]
    masked, clean = hook.redact(f"Deployed it.\n{text}\nAll green.")
    assert clean, name
    assert masked.startswith("Deployed it.") and masked.endswith("All green.")
    # No run of the secret's own characters longer than a format prefix is left.
    for run in re.findall(r"[A-Za-z0-9]{8,}", secret):
        assert run not in masked, (name, run)


def test_an_environment_secret_is_masked_wherever_and_however_it_appears(monkeypatch):
    """A value no format and no entropy check would flag, known only from the hook's environment:
    raw, split over two lines, base64- and hex-encoded."""
    value = "correct-horse-battery-staple-42"
    monkeypatch.setenv("DEPLOY_SERVICE_TOKEN", value)
    monkeypatch.setenv("BUILD_FLAVOUR", "plain-setting-value")  # neither its name nor its value is a secret
    known = hook.environment_secrets()
    text = (f"raw {value}; split {value[:15]}\n    {value[15:]}; "
            f"base64 {base64.b64encode(value.encode()).decode()}; hex {value.encode().hex()}; "
            "and plain-setting-value stays")
    masked, clean = hook.redact(text, known)
    assert clean
    assert "horse" not in masked and "staple" not in masked
    assert base64.b64encode(value.encode()).decode() not in masked and value.encode().hex() not in masked
    assert masked.endswith("and plain-setting-value stays")
    # Without the environment the value goes through: it is the known secret that catches it.
    assert value in hook.redact(text)[0]


def test_an_environment_path_or_short_value_is_no_secret(monkeypatch, tmp_path):
    monkeypatch.setenv("SSH_KEY_PATH", str(tmp_path))
    monkeypatch.setenv("API_KEY_SET", "true")
    names = {secret.label for secret in hook.environment_secrets()}
    assert "environment:SSH_KEY_PATH" not in names and "environment:API_KEY_SET" not in names


def test_a_high_entropy_string_is_masked():
    run = fake(40, string.ascii_letters + string.digits, 21)
    masked, clean = hook.redact(f"the session key {run} expires tonight")
    assert clean and run not in masked and masked.endswith("expires tonight")


def test_the_turn_written_from_a_stop_holds_no_secret_of_the_hooks_environment(switches, project, monkeypatch):
    value = "correct-horse-battery-staple-42"
    monkeypatch.setenv("DEPLOY_SERVICE_TOKEN", value)
    hook.record_turn(stop_event(project, prompt_id="prompt-2", last_assistant_message=f"Used {value} to deploy.\n\n{FOOTER}"))
    [entry] = entries(project)
    assert value not in entry.outcome and entry.outcome.startswith(f"Used {redaction.MASK} to deploy.")


def test_text_the_scanner_cannot_clean_is_withheld_and_the_entry_marked(switches, project, monkeypatch):
    """The hook never writes a text its detector could not clean; the turn stays, marked."""
    from src.schedules import scan
    monkeypatch.setattr(scan, "MAX_TEXT_LENGTH", 60)  # the answer is longer: unscannable
    hook.record_turn(stop_event(project, prompt_id="prompt-2", last_assistant_message="x" * 80 + "\n\n" + FOOTER))
    [entry] = entries(project)
    assert entry.outcome == hook.WITHHELD and entry.intent == PROMPT
    assert entry.metadata["redaction"] == {"withheld": ["outcome"]}


# --- Interactive sessions (#701) ------------------------------------------------------------------


def test_the_notice_comes_from_a_hook_that_runs_and_can_log(model_logging, project, monkeypatch, capsys):
    assert hook.turn_notice(prompt_event(project)) is None  # registered without --interactive
    assert run_main(monkeypatch, capsys, json.dumps(prompt_event(project)).encode()) == (0, "", "")
    notice = hook.turn_notice(prompt_event(project), interactive=True)
    assert notice == protocol_text.turn_log_notice()
    code, out, err = run_main(monkeypatch, capsys, json.dumps(prompt_event(project)).encode(), ["--interactive"])
    assert (code, err) == (0, "")
    assert json.loads(out) == {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit", "additionalContext": notice}}
    assert hook.turn_notice({**prompt_event(project), "hook_event_name": "Stop"}, interactive=True) is None


def test_the_interactive_argument_alone_switches_the_stop_hook_on(model_logging, project, monkeypatch, capsys):
    assert hook.record_turn(stop_event(project, prompt_id="prompt-2")) == {"status": "skipped", "reason": "disabled"}
    assert run_main(monkeypatch, capsys, json.dumps(stop_event(project, prompt_id="prompt-2")).encode()) == (0, "", "")
    assert not (project / "history.md").exists()
    event = stop_event(project, prompt_id="prompt-2", last_assistant_message="The delivered answer.\n\n" + FOOTER)
    assert run_main(monkeypatch, capsys, json.dumps(event).encode(), ["--interactive"]) == (0, "", "")
    [entry] = entries(project)
    assert entry.intent == PROMPT and entry.outcome == "The delivered answer. " + FOOTER


def test_the_module_runs_as_an_interactive_hook_command_for_both_events(project):
    env = {key: value for key, value in os.environ.items()
           if key not in ("AGENTS_TURN_LOG_HOOK", "AGENTS_CLIENT_PROFILE")}
    env["LANGFUSE_TRACING_ENABLED"] = "false"
    command = [sys.executable, "-m", "src.memory.turn_log_hook", "--interactive"]
    notice = subprocess.run(command, input=json.dumps(prompt_event(project)).encode(), cwd=ROOT, env=env,
                            capture_output=True, timeout=120)
    assert notice.returncode == 0, notice.stderr
    assert json.loads(notice.stdout)["hookSpecificOutput"]["additionalContext"] == protocol_text.turn_log_notice()
    stop = subprocess.run(command, input=json.dumps(stop_event(project, prompt_id="prompt-2")).encode(), cwd=ROOT,
                          env=env, capture_output=True, timeout=120)
    assert (stop.returncode, stop.stdout) == (0, b""), stop.stderr
    [entry] = entries(project)
    assert entry.intent == PROMPT and entry.metadata["client"] == "claude-code"


def test_the_logged_answer_is_the_last_assistant_text_of_the_turn(project, tmp_path):
    """The answer the model delivered last, after a note it added, is the one the journal keeps."""
    transcript = rows_file(tmp_path / "t.jsonl", [
        opener("p1", "вопрос"), *activation_rows(), answer_row("Answer in English, reworded.", "m1"),
        answer_row("Ответ на русском.\n\n" + FOOTER, "m2")])
    assert hook.record_turn(stop_event(project, transcript, prompt_id="p1"), interactive=True)["status"] == "recorded"
    [entry] = entries(project)
    assert entry.outcome == "Ответ на русском. " + FOOTER
    assert entry.action.startswith("Agent: software_engineer Persona (client-reported)")


@pytest.mark.parametrize("history", [
    {"status": "recorded"}, {"status": "duplicate"},
    {"status": "queued"},  # a daemon older than this hook accepted the write
    {"status": "error", "state": "unknown", "error": "history_timeout"},  # it may still commit
])
def test_a_turn_the_model_logged_or_may_have_logged_is_left_to_it(project, tmp_path, history):
    transcript = rows_file(tmp_path / "t.jsonl", [
        opener("p1", "question"), *activation_rows(), *log_rows(history), answer_row("Answer.\n\n" + FOOTER)])
    result = hook.record_turn(stop_event(project, transcript, prompt_id="p1"), interactive=True)
    assert result == {"status": "skipped", "reason": "logged_by_model", "withdrawn": False}


def test_a_log_call_of_an_earlier_turn_does_not_count(project, tmp_path):
    transcript = rows_file(tmp_path / "t.jsonl", [
        opener("p1", "first"), *activation_rows(), *log_rows({"status": "recorded"}), answer_row("First.", "m1"),
        opener("p2", "second"), answer_row("Second.\n\n" + FOOTER, "m2")])
    assert hook.record_turn(stop_event(project, transcript, prompt_id="p2"), interactive=True)["status"] == "recorded"
    [entry] = entries(project)
    assert (entry.intent, entry.outcome) == ("second", "Second. " + FOOTER)
    assert entry.action.endswith("action=keep")


def corrected_turn(tmp_path, *later):
    return rows_file(tmp_path / "t.jsonl", [opener("p1", "question"), *activation_rows(),
                                            answer_row(LONG_ANSWER, "m1"), FEEDBACK, *later])


def test_a_later_stop_replaces_the_entry_with_the_corrected_answer(project, tmp_path):
    """#701 review: another Stop hook continued the turn and the model delivered a corrected answer;
    the journal keeps what the user saw last."""
    transcript = corrected_turn(tmp_path, answer_row(CORRECTED, "m2"))
    first = hook.record_turn(stop_event(project, transcript, prompt_id="p1", last_assistant_message=LONG_ANSWER),
                             interactive=True)
    again = hook.record_turn(stop_event(project, transcript, prompt_id="p1", stop_hook_active=True,
                                        last_assistant_message=CORRECTED), interactive=True)
    assert first["status"] == again["status"] == "recorded"
    assert again["replaced"]["entry_id"] == first["entry_id"] == again["entry_id"]
    [entry] = entries(project)
    assert entry.outcome == flat(CORRECTED)
    # The feedback row stays inside the turn: it starts none of its own.
    assert hook.read_turn(transcript.read_text(encoding="utf-8").splitlines()).prompt == "question"


def test_a_short_note_after_the_answer_leaves_the_answer(project, tmp_path):
    transcript = corrected_turn(tmp_path, answer_row("Chapter marked.\n\n" + FOOTER, "m2"))
    hook.record_turn(stop_event(project, transcript, prompt_id="p1", last_assistant_message=LONG_ANSWER),
                     interactive=True)
    note = hook.record_turn(stop_event(project, transcript, prompt_id="p1", stop_hook_active=True,
                                       last_assistant_message="Chapter marked.\n\n" + FOOTER), interactive=True)
    assert note["status"] == "kept"
    [entry] = entries(project)
    assert entry.outcome == flat(LONG_ANSWER)


class SmallJournal(HistoryWriter):
    """A writer that rotates after every append, so the entry sits in an archive by the next Stop."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **{**kwargs, "rotation_kb": 0})


def test_a_rotation_between_the_two_stops_still_leaves_one_entry(project, tmp_path, monkeypatch):
    """#701 review (S2): the re-entry found no entry in the live file and wrote a second one."""
    monkeypatch.setattr(history_module, "HistoryWriter", SmallJournal)
    transcript = corrected_turn(tmp_path, answer_row(CORRECTED, "m2"))
    first = hook.record_turn(stop_event(project, transcript, prompt_id="p1", last_assistant_message=LONG_ANSWER),
                             interactive=True)
    [archive] = (project / "history").glob("*.md")
    assert first["entry_id"] in archive.read_text(encoding="utf-8")
    assert first["entry_id"] not in (project / "history.md").read_text(encoding="utf-8")
    again = hook.record_turn(stop_event(project, transcript, prompt_id="p1", stop_hook_active=True,
                                        last_assistant_message=CORRECTED), interactive=True)
    assert again["status"] == "recorded" and again["replaced"]["entry_id"] == first["entry_id"]
    turns = [entry for entry in entries(project) if entry.metadata and entry.metadata.get("prompt_id") == "p1"]
    assert [entry.outcome for entry in turns] == [flat(CORRECTED)]


def test_the_key_holds_the_session_so_two_sessions_keep_their_own_entries(project, tmp_path):
    transcript = corrected_turn(tmp_path, answer_row(CORRECTED, "m2"))
    for session in ("session-a", "session-b"):
        event = {**stop_event(project, transcript, prompt_id="p1", last_assistant_message=LONG_ANSWER),
                 "session_id": session}
        assert hook.record_turn(event, interactive=True)["status"] == "recorded"
    assert sorted(entry.metadata["session_id"] for entry in entries(project)) == ["session-a", "session-b"]
    assert hook.turn_key({"session_id": "s"}, "p") != hook.turn_key({"session_id": "t"}, "p")


def test_the_model_logging_after_the_hook_wrote_removes_the_hooks_entry(project, tmp_path):
    """#701 review: another Stop hook demanded log_interaction after the hook had written the turn."""
    rows = [opener("p1", "question"), *activation_rows(), answer_row(LONG_ANSWER, "m1")]
    transcript = rows_file(tmp_path / "t.jsonl", rows)
    hook.record_turn(stop_event(project, transcript, prompt_id="p1", last_assistant_message=LONG_ANSWER),
                     interactive=True)
    assert len(entries(project)) == 1
    rows_file(transcript, [*rows, FEEDBACK, *log_rows({"status": "recorded"}), answer_row("Logged.", "m2")])
    again = hook.record_turn(stop_event(project, transcript, prompt_id="p1", stop_hook_active=True,
                                        last_assistant_message="Logged."), interactive=True)
    assert again == {"status": "skipped", "reason": "logged_by_model", "withdrawn": True}
    assert entries(project) == []  # the model's own entry is the server's to write
    # A third Stop of the turn neither writes nor finds anything of the hook's.
    assert hook.record_turn(stop_event(project, transcript, prompt_id="p1", stop_hook_active=True,
                                       last_assistant_message="Logged."), interactive=True)["withdrawn"] is False


def test_journal_false_keeps_the_turn_out_of_the_journal(project, tmp_path):
    """The signal not to journal a turn: the model's log_interaction call with journal=false."""
    rows = [opener("p1", "my medical history, please do not record this"), *activation_rows(),
            answer_row(LONG_ANSWER, "m1")]
    transcript = rows_file(tmp_path / "t.jsonl", rows)
    hook.record_turn(stop_event(project, transcript, prompt_id="p1", last_assistant_message=LONG_ANSWER),
                     interactive=True)
    rows_file(transcript, [*rows, FEEDBACK, *log_rows({"status": "withheld", "state": "not_written"},
                                                      arguments={"journal": False}), answer_row("Kept out.", "m2")])
    again = hook.record_turn(stop_event(project, transcript, prompt_id="p1", stop_hook_active=True,
                                        last_assistant_message="Kept out."), interactive=True)
    assert again == {"status": "skipped", "reason": "not_journaled", "withdrawn": True}
    assert entries(project) == []


def test_a_log_call_whose_result_never_reaches_the_transcript_is_left_to_the_model(project, tmp_path, monkeypatch):
    """#701 review: the transcript may lag; the hook reads it again, then gives the call the turn."""
    monkeypatch.setattr(hook, "TRANSCRIPT_WAIT_SECONDS", 0.1)
    transcript = rows_file(tmp_path / "t.jsonl", [
        opener("p1", "question"), *activation_rows(), *log_rows({}, result=False), answer_row("Answer.")])
    started = time.monotonic()
    result = hook.record_turn(stop_event(project, transcript, prompt_id="p1"), interactive=True)
    assert result["reason"] == "logged_by_model" and time.monotonic() - started >= 0.1
    assert not (project / "history.md").exists()


def test_a_prompt_row_written_after_the_stop_is_waited_for(project, tmp_path, monkeypatch):
    """#701 review: a lagging transcript is read again for a while before the turn counts as lost."""
    monkeypatch.setattr(hook, "TRANSCRIPT_WAIT_SECONDS", 5.0)
    transcript = rows_file(tmp_path / "t.jsonl", [opener("p1", "first"), answer_row("First.", "m1")])

    def catch_up():
        time.sleep(0.2)
        with open(transcript, "a", encoding="utf-8") as stream:
            for row in (opener("p2", "second"), answer_row("Second.", "m2")):
                stream.write(json.dumps(row) + "\n")
    writer = threading.Thread(target=catch_up)
    writer.start()
    try:
        result = hook.record_turn(stop_event(project, transcript, prompt_id="p2"), interactive=True)
    finally:
        writer.join()
    assert result["status"] == "recorded"
    [entry] = entries(project)
    assert (entry.intent, entry.outcome) == ("second", "Second.")


@pytest.mark.parametrize("origin,meta", [("host_synthetic", False), ("task_notification", False),
                                         ("peer", True), ("scheduled", True)])
def test_a_turn_the_user_did_not_type_is_logged_once_as_its_own_turn(project, tmp_path, origin, meta):
    """A notification, a message from another session or a scheduled task opens its own turn."""
    event_row = opener("p2", f"<{origin}>event</{origin}>", origin, **({"isMeta": True} if meta else {}))
    transcript = rows_file(tmp_path / "t.jsonl", [
        opener("p1", "question"), *activation_rows(), answer_row("First answer.", "m1"), event_row,
        answer_row("Event handled.", "m2")])
    first = hook.record_turn(stop_event(project, transcript, prompt_id="p1",
                                        last_assistant_message="First answer."), interactive=True)
    second = stop_event(project, transcript, prompt_id="p2", last_assistant_message="Event handled.")
    assert first["status"] == hook.record_turn(second, interactive=True)["status"] == "recorded"
    hook.record_turn(second, interactive=True)  # the same Stop again writes nothing new
    assert [(entry.intent, entry.outcome) for entry in entries(project)] == [
        ("question", "First answer."), (f"<{origin}>event</{origin}>", "Event handled.")]


def test_a_prompt_without_text_is_logged_under_a_placeholder(project, tmp_path):
    """#701 review: an image alone used to leave its turn unlogged."""
    transcript = rows_file(tmp_path / "image.jsonl", [
        opener("p1", "first question"), answer_row("First.", "m1"),
        opener("p2", [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AA=="}},
                      {"type": "text", "text": "<system-reminder>a note</system-reminder>"}]),
        answer_row("A screenshot of the settings page.", "m2")])
    turn = hook.read_turn(transcript.read_text(encoding="utf-8").splitlines())
    assert (turn.prompt, turn.answer) == ("[image]", "A screenshot of the settings page.")
    assert hook.record_turn(stop_event(project, transcript, prompt_id="p2"), interactive=True)["status"] == "recorded"


def test_a_typed_prompt_that_begins_like_a_host_row_is_a_prompt(project, tmp_path):
    """#701 review: only rows Claude Code writes itself are host rows, not a prompt the user typed."""
    typed = "Stop hook feedback: why do I keep seeing this line?"
    transcript = rows_file(tmp_path / "t.jsonl", [opener("p1", "first"), answer_row("First.", "m1"),
                                                  opener("p2", typed), answer_row("Because...", "m2")])
    assert hook.read_turn(transcript.read_text(encoding="utf-8").splitlines(), "p2").prompt == typed



def closing_call(tool_id="w1"):
    return {"type": "assistant", "message": {"id": "a" + tool_id, "content": [
        {"type": "tool_use", "id": tool_id, "name": "mcp__ccd_session__wrap_up", "input": {"summary": "box text"}}]}}


def test_a_turn_closed_by_a_call_logs_the_text_given_before_it(project, tmp_path):
    """#701 review (S9): the box of a closing call such as wrap_up is never the entry; the final
    answer given as text before it is, as the notice asks."""
    transcript = rows_file(tmp_path / "t.jsonl", [opener("p1", "question"), *activation_rows(),
                                                  answer_row("The answer.\n\n" + FOOTER, "m1"), closing_call()])
    hook.record_turn(stop_event(project, transcript, prompt_id="p1", last_assistant_message=""), interactive=True)
    [entry] = entries(project)
    assert entry.outcome == "The answer. " + FOOTER and "box text" not in entry.outcome


def test_a_turn_that_delivered_no_text_is_still_logged(project, tmp_path):
    transcript = rows_file(tmp_path / "t.jsonl", [opener("p1", "question"), closing_call()])
    assert hook.record_turn(stop_event(project, transcript, prompt_id="p1"), interactive=True)["status"] == "recorded"
    [entry] = entries(project)
    assert (entry.intent, entry.outcome) == ("question", hook.NO_ANSWER)

# --- Attribution against the delivered footer (#701 review) ---------------------------------------


def attributed(project, tmp_path, rows, answer):
    transcript = rows_file(tmp_path / "t.jsonl", [opener("p1", "question"), *rows, answer_row(answer)])
    hook.record_turn(stop_event(project, transcript, prompt_id="p1", last_assistant_message=answer), interactive=True)
    [entry] = entries(project)
    return entry.action


def test_a_footer_naming_another_agent_is_a_mismatch(project, tmp_path):
    action = attributed(project, tmp_path, activation_rows(), "Answer.\n\n**Agent**: code_reviewer · **Rules**: —")
    assert action == ("Agent: code_reviewer Persona (mismatch): software_engineer; activation=act-1; "
                      f"revision={REVISION}; action=switch")


def test_an_answer_without_a_footer_is_unverified(project, tmp_path):
    action = attributed(project, tmp_path, activation_rows(), "Answer without a footer.")
    assert action == ("Agent: software_engineer Persona (unverified): software_engineer; activation=act-1; "
                      f"revision={REVISION}; missing=footer; action=switch")


def test_a_footer_quoted_before_the_end_is_no_footer(project, tmp_path):
    action = attributed(project, tmp_path, [], "A footer looks like **Agent**: lawyer.\n\nThat was all.")
    assert action == "Agent: unattributed Persona (unverified): missing=persona; action=unspecified"


def test_a_success_in_the_turn_decides_the_action_whatever_no_change_follows(project, tmp_path):
    rows = [*activation_rows("t1"), *activation_rows("t2", "NO_CHANGE", "run_flow")]
    rows[-1]["message"]["content"][0]["content"] = json.dumps(
        {"status": "needs_execution", "persona_activation": {"status": "NO_CHANGE", "persona": PERSONA}})
    action = attributed(project, tmp_path, rows, "Answer.\n\n" + FOOTER)
    assert action.endswith("action=switch")


# --- Failures are visible, bounded and survivable (#701 review) -----------------------------------


def test_the_journal_lock_wait_is_bounded_and_its_failure_visible(project, monkeypatch, capsys):
    from src.file_lock import file_lock
    monkeypatch.setattr(hook, "LOCK_TIMEOUT_SECONDS", 0.2)
    event = json.dumps(stop_event(project, prompt_id="prompt-2")).encode()
    with file_lock(project / ".history.md.lock"):
        started = time.monotonic()
        code, out, err = run_main(monkeypatch, capsys, event, ["--interactive"])
        waited = time.monotonic() - started
    assert (code, out) == (1, "") and "journal_busy" in err and waited < 5
    assert run_main(monkeypatch, capsys, event, ["--interactive"]) == (0, "", "")  # free again: written
    assert len(entries(project)) == 1


def test_a_transcript_over_the_cap_is_a_visible_failure(project, monkeypatch, capsys):
    monkeypatch.setattr(hook, "MAX_TRANSCRIPT_BYTES", 100)
    code, _, err = run_main(monkeypatch, capsys, json.dumps(stop_event(project, prompt_id="prompt-2")).encode(),
                            ["--interactive"])
    assert code == 1 and "the transcript is over" in err
    assert not (project / "history.md").exists()


def test_the_transcript_is_read_line_by_line(tmp_path):
    """#701 review: reading the whole file into one string took about eleven times its size."""
    path = tmp_path / "long.jsonl"
    filler = json.dumps({"type": "system", "content": "x" * 1000}) + "\n"
    with open(path, "w", encoding="utf-8") as stream:
        stream.write(filler * 8000)  # 8 MB that no turn needs
        for row in (opener("p1", "question"), answer_row("Answer.")):
            stream.write(json.dumps(row) + "\n")
    size = path.stat().st_size
    tracemalloc.start()
    try:
        turn = hook.read_turn(hook._transcript_lines(str(path)), "p1")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert turn.answer == "Answer." and peak < size / 8, (peak, size)


def test_a_journal_entry_whose_meta_is_not_an_object_stops_nothing(project, tmp_path):
    """#701 review: a Meta line holding a JSON list raised AttributeError on every later turn."""
    writer = HistoryWriter(str(project / "history.md"), str(project / "history"))
    writer.append_entry("an old turn", "Agent: x", "an old answer")
    text = (project / "history.md").read_text(encoding="utf-8")
    (project / "history.md").write_text(text + "\n## 2026-11-01T00:00:00+00:00 | 0123456789ab\n"
                                        "**Intent:** odd\n**Action:** Agent: x\n**Outcome:** odd\n**Meta:** [1, 2]\n",
                                        encoding="utf-8")
    transcript = corrected_turn(tmp_path, answer_row(CORRECTED, "m2"))
    event = stop_event(project, transcript, prompt_id="p1", last_assistant_message=LONG_ANSWER)
    assert hook.record_turn(event, interactive=True)["status"] == "recorded"
    assert hook.record_turn({**event, "last_assistant_message": CORRECTED}, interactive=True)["replaced"]
    assert len(entries(project)) == 3


# --- The journal writer under a key (#701) --------------------------------------------------------


def test_a_withdrawn_entry_is_never_written_again_under_its_key(tmp_path):
    writer = HistoryWriter(str(tmp_path / "history.md"), str(tmp_path / "history"))
    written = writer.append_entry("q", "Agent: a", "first", idempotency_key="k1", supersede=lambda earlier: True)
    assert writer.withdraw("k1") == {"status": "withdrawn", "entry_id": written["entry_id"],
                                     "timestamp": written["timestamp"]}
    assert writer.withdraw("k1") == {"status": "absent"}
    again = writer.append_entry("q", "Agent: a", "second", idempotency_key="k1", supersede=lambda earlier: True)
    assert again["status"] == "withdrawn"
    assert HistoryReader(str(tmp_path / "history.md")).read_all() == []


def test_supersede_needs_a_key_and_a_busy_journal_raises_after_the_timeout(tmp_path):
    from src.file_lock import file_lock
    writer = HistoryWriter(str(tmp_path / "history.md"), str(tmp_path / "history"))
    with pytest.raises(ValueError):
        writer.append_entry("q", "Agent: a", "o", supersede=lambda earlier: True)
    writer.append_entry("q", "Agent: a", "earlier", idempotency_key="k")
    with file_lock(tmp_path / ".history.md.lock"):
        started = time.monotonic()
        with pytest.raises(JournalBusy):
            writer.append_entry("q", "Agent: a", "o", lock_timeout=0.1)
        with pytest.raises(JournalBusy):
            writer.withdraw("k", lock_timeout=0.1)
        assert time.monotonic() - started < 5
    assert writer.append_entry("q", "Agent: a", "o", lock_timeout=0.1)["status"] == "recorded"
    assert writer.withdraw("k", lock_timeout=0.1)["status"] == "withdrawn"


def test_without_supersede_a_changed_payload_is_still_a_conflict(tmp_path):
    """log_interaction's own keyed retries keep their contract."""
    from src.memory.history import MemoryWriteRejected
    writer = HistoryWriter(str(tmp_path / "history.md"), str(tmp_path / "history"))
    writer.append_entry("q", "Agent: a", "first", idempotency_key="k1")
    with pytest.raises(MemoryWriteRejected, match="idempotency_conflict"):
        writer.append_entry("q", "Agent: a", "second", idempotency_key="k1")


# --- The session decides, never settings files the server reads (#701 review) ---------------------


REGISTRATION = {"hooks": {event: [{"hooks": [{"type": "command", "timeout": 30,
                                              "command": "cd /opt/agents && .venv/bin/python -m "
                                                         "src.memory.turn_log_hook --interactive"}]}]
                          for event in ("UserPromptSubmit", "Stop")}}


@pytest.mark.parametrize("case", [
    "another CLAUDE_CONFIG_DIR", "claude --bare", "claude --restricted", "--setting-sources project,local",
    "disableAllHooks", "allowManagedHooksOnly",
])
@pytest.mark.asyncio
async def test_a_session_that_does_not_run_the_hook_keeps_log_interaction(case, model_logging, project, tmp_path,
                                                                          monkeypatch):
    """Each case leaves the registration in files the server could read while the session runs no
    hook. The server's texts are the same for every Claude Code session, whatever those files say,
    and the only thing that leaves a turn to the hook is the notice a running hook gives."""
    from unittest.mock import AsyncMock
    from src import client_instructions as ci
    from src.engine import persona

    home = tmp_path / "home"
    profile = home / ".claude"
    profile.mkdir(parents=True)
    settings = dict(REGISTRATION)
    if case in ("disableAllHooks", "allowManagedHooksOnly"):
        settings[case] = True
    (profile / "settings.json").write_text(json.dumps(settings), encoding="utf-8")
    (project / ".claude").mkdir()
    (project / ".claude" / "settings.local.json").write_text(json.dumps(REGISTRATION), encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    if case == "another CLAUDE_CONFIG_DIR":
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
    context = SimpleNamespace(root=project, client="claude-code", error=None, cooperation_roots=(project,))
    full = protocol_text.server_instructions()
    assert ci.initialization_instructions(full, context) == full
    assert "turn-log notice" in full and "Before delivery call log_interaction" in full

    async def build(agent, query, history, tier=None, selection=None):
        return SimpleNamespace(
            agent=agent, bundle_revision="b" * 64, scope=f"Scope of {agent}", persona_block="Role",
            rules_block="Rules", skills_block="Skills", implants_block="Implants", skills_loaded=[],
            implants_loaded=[], rules_loaded=[], tier="standard")
    monkeypatch.setattr(persona, "build_persona_bundle", AsyncMock(side_effect=build))

    class Router:
        async def update_cache(self, *args):
            return None
    token = ci.REQUEST_CLIENT.set(context)
    try:
        first = json.loads(await persona.load_persona(Router(), "lawyer", "task", [], None))
    finally:
        ci.REQUEST_CLIENT.reset(token)
    assert first["instruction"] == persona.APPLY_INSTRUCTION
    assert first["instruction_revision"] == persona.INSTRUCTION_REVISION


# --- The second review's redaction corpus (#701 review, attempt 3) --------------------------------
# Every secret is made at run time from a seeded generator, so this file's own text stays clean for
# the tracked-files scan. A secret item is masked when no run of its own characters longer than the
# format's kept prefix, or a third of it, is left; the items docs/memory.md names as still passing
# are pinned as passing, so that list stays true.

ALNUM = string.ascii_letters + string.digits
UPPER_DIGITS = string.ascii_uppercase + string.digits
BASE64_ALPHABET = ALNUM + "+/"
URLSAFE = ALNUM + "-_"
HEX = "0123456789abcdef"
LOWER_DIGITS = string.ascii_lowercase + string.digits


def longest_left(secret: str, masked: str) -> int:
    """The longest run of ``secret``'s own characters that ``masked`` still holds."""
    best = 0
    for start in range(len(secret)):
        length = best + 1
        while start + length <= len(secret) and secret[start:start + length] in masked:
            best = length
            length += 1
    return best


def masked_away(secret: str, masked: str, allowance: int = 0) -> bool:
    return secret not in masked and longest_left(secret, masked) <= max(allowance, len(secret) // 3)


def key_block(kind: str, seed: int, block: str = "") -> str:
    body = "\n".join(fake(64, BASE64_ALPHABET, seed + index) for index in range(6))
    marker = "PRIVATE " + "KEY" + block
    return f"-----BEGIN {kind}{marker}-----\n{body}\n-----END {kind}{marker}-----"


def corpus_secrets():
    """``{name: (text, secret, allowance)}``: the real-format cases of the review's redaction probe."""
    out = {}

    def add(name, text, secret=None, allowance=0):
        out[name] = (text, text if secret is None else secret, allowance)

    asia, aws_secret = "ASIA" + fake(16, UPPER_DIGITS, 2), fake(40, BASE64_ALPHABET, 3)
    add("AWS access key id", "AKIA" + fake(16, UPPER_DIGITS, 1), None, 4)
    add("AWS temporary key id", asia, None, 4)
    add("AWS secret key, labelled", "aws_secret_access_key=" + aws_secret, aws_secret)
    add("AWS secret key, bare in prose", "the secret access key is: " + aws_secret, aws_secret)
    add("AWS secret key, STS JSON", '{"AccessKeyId": "%s", "SecretAccessKey": "%s"}' % (asia, aws_secret), aws_secret)
    session_token = fake(400, BASE64_ALPHABET, 4)
    add("AWS session token, STS JSON", '"SessionToken": "' + session_token + '"', session_token)
    for index, prefix in enumerate(("ghp_", "gho_", "ghu_", "ghs_", "ghr_")):
        add(f"GitHub token {prefix}", prefix + fake(36, ALNUM, 10 + index), None, 4)
    add("GitHub fine-grained token", "github_pat_" + fake(22, ALNUM, 16) + "_" + fake(59, ALNUM, 17), None, 11)
    add("Slack bot token", "xoxb-" + fake(12, string.digits, 20) + "-" + fake(13, string.digits, 21) + "-"
        + fake(24, ALNUM, 22), None, 5)
    add("Slack user token", "xoxp-" + fake(12, string.digits, 23) + "-" + fake(12, string.digits, 24) + "-"
        + fake(32, HEX, 25), None, 5)
    add("Slack app-level token", "xapp-1-A" + fake(10, UPPER_DIGITS, 26) + "-" + fake(13, string.digits, 27) + "-"
        + fake(64, HEX, 28), None, 7)
    add("Slack rotating token", "xoxe.xoxp-1-" + fake(160, ALNUM, 29), None, 12)
    webhook = fake(24, ALNUM, 32)
    add("Slack webhook URL", "https://hooks.slack.com/services/T" + fake(10, UPPER_DIGITS, 30) + "/B"
        + fake(10, UPPER_DIGITS, 31) + "/" + webhook, webhook)
    add("OpenAI legacy key", "sk-" + fake(48, ALNUM, 40), None, 3)
    add("OpenAI project key", "sk-proj-" + fake(120, URLSAFE, 41), None, 8)
    add("OpenAI service-account key", "sk-svcacct-" + fake(120, URLSAFE, 42), None, 11)
    add("OpenAI admin key", "sk-admin-" + fake(120, URLSAFE, 43), None, 9)
    add("Anthropic API key", "sk-ant-api03-" + fake(93, URLSAFE, 44) + "AA", None, 13)
    add("Anthropic OAuth token", "sk-ant-oat01-" + fake(93, URLSAFE, 45) + "AA", None, 13)
    add("OpenRouter key", "sk-or-v1-" + fake(64, HEX, 46), None, 9)
    jwt = ".".join(base64.urlsafe_b64encode(part).decode().rstrip("=") for part in (
        b'{"alg":"HS256","typ":"JWT"}', b'{"sub":"1234567890","name":"Synthetic","iat":1516239022}',
        fake(32, ALNUM, 50).encode()))
    add("JWT", jwt, None, 3)
    add("JWT in an Authorization header", "Authorization: Bearer " + jwt, jwt, 3)
    for index, kind in enumerate(("RSA ", "EC ", "OPENSSH ", "", "ENCRYPTED ", "DSA ")):
        block = key_block(kind, 60 + 10 * index)
        add(f"private key ({kind.strip() or 'PKCS#8'})", block, "\n".join(block.splitlines()[1:-1]))
    pgp = key_block("PGP ", 130, " BLOCK")
    add("PGP private key block", pgp, "\n".join(pgp.splitlines()[1:-1]))
    escaped = key_block("", 140).replace("\n", "\\n")
    add("private key in a JSON string", '{"private_key": "%s\\n"}' % escaped, escaped.split("-----")[2])
    headerless = "\n".join(key_block("RSA ", 150).splitlines()[1:-1])
    add("private key body without its BEGIN line", "key material:\n" + headerless, headerless)
    add("Google API key", "AIza" + fake(35, URLSAFE, 160), None, 4)
    add("Stripe secret key", "sk_live_" + fake(24, ALNUM, 161), None, 8)
    add("Stripe restricted key", "rk_live_" + fake(24, ALNUM, 162), None, 8)
    add("Stripe webhook secret", "whsec_" + fake(32, ALNUM, 163), None, 6)
    add("GitLab token", "glpat-" + fake(20, URLSAFE, 164), None, 6)
    add("npm token", "npm_" + fake(36, ALNUM, 165), None, 4)
    add("Hugging Face token", "hf_" + fake(34, ALNUM, 166), None, 3)
    add("SendGrid key", "SG." + fake(22, URLSAFE, 167) + "." + fake(43, URLSAFE, 168), None, 3)
    add("Twilio API key", "SK" + fake(32, HEX, 169), None, 2)
    add("Telegram bot token", fake(10, string.digits, 170) + ":AA" + fake(33, URLSAFE, 171))
    add("Discord bot token", fake(24, ALNUM, 172) + "." + fake(6, ALNUM, 173) + "." + fake(38, URLSAFE, 174))
    azure = fake(86, BASE64_ALPHABET, 175)
    add("Azure storage key", "AccountKey=" + azure + "==", azure)
    datadog = fake(32, HEX, 176)
    add("32-digit hex key, unlabelled", "use " + datadog + " for the agent", datadog)
    labelled_hex = fake(32, HEX, 177)
    add("32-digit hex key, labelled", "api_key=" + labelled_hex, labelled_hex)
    password = fake(14, ALNUM, 178)
    add("database URL with a password", "postgres://deploy:" + password + "@db.example.com:5432/app", password)
    basic = base64.b64encode(("admin:" + fake(12, ALNUM, 179)).encode()).decode()
    add("HTTP Basic header", "Authorization: Basic " + basic, basic)
    curl = fake(12, ALNUM, 180)
    add("curl -u user:password", "curl -u admin:" + curl + " https://example.com", curl)
    add("cooperation capability", fake(43, URLSAFE, 181))
    add("labelled short password", "password: hunter2", "hunter2")
    sshpass = fake(10, ALNUM, 182)
    add("sshpass -p", "sshpass -p '" + sshpass + "' ssh host", sshpass)
    for length in (20, 24, 32, 40, 64):
        run = fake(length, ALNUM, 190 + length)
        add(f"random alphanumeric, {length}", "value " + run + " end", run)
    base64_run = fake(43, BASE64_ALPHABET, 300)
    add("random base64 with + and /", "value " + base64_run + "= end", base64_run)
    lower = fake(32, LOWER_DIGITS, 301)
    add("random lowercase and digits, 32", "value " + lower + " end", lower)
    return out


# docs/memory.md, "these shapes still pass": a short password in prose, and hex keys of 40 and 64 digits,
# which have the shape of commit names and SHA-256 digests.
CORPUS_PASSING = {
    "unlabelled short password in prose": ("I typed hunter2 into the form", "hunter2"),
    "40-digit hex key, unlabelled": ("value " + fake(40, HEX, 330) + " end", fake(40, HEX, 330)),
    "64-digit hex key, unlabelled": ("value " + fake(64, HEX, 354) + " end", fake(64, HEX, 354)),
}


@pytest.mark.parametrize("name", sorted(corpus_secrets()))
def test_every_secret_of_the_review_corpus_is_masked(name):
    text, secret, allowance = corpus_secrets()[name]
    masked, clean = redaction.redact(f"Deployed it.\n{text}\nAll green.")
    assert clean, name
    assert masked.startswith("Deployed it.") and masked.endswith("All green."), (name, masked)
    assert masked_away(secret, masked, allowance), (name, masked)


@pytest.mark.parametrize("name", sorted(CORPUS_PASSING))
def test_the_shapes_the_docs_name_as_passing_still_pass(name):
    text, secret = CORPUS_PASSING[name]
    assert secret in redaction.redact(text)[0]


def corpus_false_positives():
    """The review's false-positive set: text that is no secret and must stay as written."""
    session = "session_01" + fake(22, ALNUM, 400)
    return {
        "UUID lower": "session 5f0c7a2e-1b6d-4c2a-9e51-0d7b3c4a8f10 ended",
        "UUID upper": "device 5F0C7A2E-1B6D-4C2A-9E51-0D7B3C4A8F10 paired",
        "git SHA 40": "merged at " + fake(40, HEX, 401) + " on dev",
        "git SHA short": "see 93b8dd84 and 10e1e6a1e18c",
        "sha256 hex": "digest " + fake(64, HEX, 402) + " matches",
        "sha512 hex": "sha512 " + fake(128, HEX, 403),
        "unix path": "edited /Users/dev/repos/Agents/src/memory/turn_log_hook.py:610 and tests/test_turn_log_hook.py",
        "path with a long dashed component":
            "scratch /private/tmp/claude-501/-Users-dev-repos-Agents/5dd7911c-d0f8-42e2-807d-d64dbb5fcdd8/scratchpad/x.py",
        "windows path": r"C:\Users\dev\AppData\Local\agents-core\history.md",
        "long test name": "tests/test_schedules_scan.py::test_tracked_files_are_clean_except_known_fixtures passed",
        "identifier of exactly 43 characters": "def test_a_rotation_between_two_stops_keeps_one(): pass",
        "GitHub PR URL": "opened https://github.com/IEZhu/Agents/pull/773 against dev",
        "claude.ai session link": "https://claude.ai/code/" + session,
        "bare session id": session,
        "tool-use id": "the call toolu_01" + fake(22, ALNUM, 404) + " returned",
        "message id": "message msg_01" + fake(22, ALNUM, 405),
        "Google Docs URL": "https://docs.google.com/document/d/1" + fake(43, URLSAFE, 406) + "/edit",
        "YouTube URL": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "npm integrity hash": "integrity sha512-" + fake(86, BASE64_ALPHABET, 407) + "==",
        "docker image digest": "image@sha256:" + fake(64, HEX, 408),
        "CamelCase identifier with digits": "class Http2ConnectionPoolManagerFactoryBean3 extends Base",
        "python code: token variable": 'token = response.json()["access_token"]',
        "python code: password parameter": "def login(user: str, password: str = None) -> Session:",
        "python code: api_key from env": 'client = Client(api_key=os.environ["OPENAI_API_KEY"])',
        "python code: a keyword argument passed on": "client = Client(api_key=api_key, password=password)",
        "yaml docs: token key": "token: the unit a model reads",
        "prose: password is": "the password is stored hashed with argon2id",
        "prose: secret word": "secret: keep the launch date quiet",
        "cli help": "use --password PASS or set PGPASSWORD",
        "max_tokens setting": "max_tokens=4096, temperature=0.2",
        "placeholder": "password=<your password> and token=${TOKEN}",
        "ISO timestamp and version": "2026-11-27T18:52:09Z v2.1.296 mcp==1.30.0",
        "email and phone": "write to dev@example.com or +1 415 555 0100",
        "base64 PNG": "data:image/png;base64," + base64.b64encode(random.Random(409).randbytes(70)).decode(),
        "ARN": "arn:aws:iam::123456789012:role/service-role/AmazonSageMaker-ExecutionRole-20200101T000001",
        "commit trailer": "Claude-Session: https://claude.ai/code/" + session,
        "footer": "**Agent**: software_engineer | **Skills**: skill-dev-testing | **Rules**: no-fabrication",
        "entry id and receipt digest": "entry 3afab3fea63f, key digest " + fake(64, HEX, 410),
        "GitHub node id": "thread PRRT_kwDOQ2gBfc5" + fake(16, URLSAFE, 411) + " resolved",
        "target triple and model name": "built for x86_64-pc-windows-msvc with all-MiniLM-L6-v2",
        "macOS temporary directory": "/var/folders/32/rg0fhsh169zgd30_3plv74_c0000gp/T/pytest-of-dev",
    }


# The review's false positives that the line rule masks now (owner's decision 2026-11-28: a harmless
# line tail is sometimes lost): a name of a secret in code, in a placeholder or in a label of prose.
LINE_RULE_MASKS = {
    "cli help": "use --password<secret>",
    "placeholder": "password<secret>",
    "prose: password is": "the password<secret>",
    "prose: secret word": "secret<secret>",
    "python code: a keyword argument passed on": "client = Client(api_key<secret>",
    "python code: api_key from env": "client = Client(api_key<secret>",
    "python code: password parameter": "def login<secret>",
    "python code: token variable": "token<secret>",
    "yaml docs: token key": "token<secret>",
}


@pytest.mark.parametrize("name", sorted(set(corpus_false_positives()) - set(LINE_RULE_MASKS)))
def test_no_false_positive_of_the_review_corpus_is_masked(name):
    text = corpus_false_positives()[name]
    assert redaction.redact(text) == (text, True)


@pytest.mark.parametrize("name", sorted(LINE_RULE_MASKS))
def test_the_line_rule_masks_these_false_positives_after_their_label(name):
    assert redaction.redact(corpus_false_positives()[name]) == (LINE_RULE_MASKS[name], True)


ENVIRONMENT_VALUE = "s3cr3t-Deploy-pass-phrase-2026"
ENVIRONMENT = {"DEPLOY_SERVICE_TOKEN": ENVIRONMENT_VALUE, "PGPASSWORD": "hunter2", "BUILD_FLAVOUR": "plain-setting-value",
               "HOME": "/Users/dev", "GIT_AUTHOR_NAME": "Synthetic Person", "GIT_AUTHOR_EMAIL": "person@example.com",
               "AUTH_MODE": "password", "GOPRIVATE": "github.com/example/*", "SSH_KEY_NAME": "id_ed25519",
               "KEYBOARD_LAYOUT": "us-intl-altgr", "PIP_KEYRING_PROVIDER": "subprocess"}


def environment_forms():
    """``{form: (text, what must not be left)}``: one environment secret in the forms of the review's probes."""
    value, encoded = ENVIRONMENT_VALUE, base64.b64encode(ENVIRONMENT_VALUE.encode()).decode()
    blob = base64.b64encode(("user:" + value + ":x").encode()).decode()
    return {
        "raw": (f"deployed with {value} today", value),
        "split over two lines": (f"deployed with {value[:14]}\n    {value[14:]} today", value[:14]),
        "split by a space": (f"deployed with {value[:14]} {value[14:]} today", value[:14]),
        "base64": (f"deployed with {encoded} today", encoded),
        "base64, split over two lines": (f"blob {encoded[:20]}\n{encoded[20:]} end", encoded[:20]),
        "base64 inside a larger blob": ("blob " + blob, blob),
        "base64url": ("t=" + base64.urlsafe_b64encode(value.encode()).decode().rstrip("="),
                      base64.urlsafe_b64encode(value.encode()).decode().rstrip("=")),
        "hex lower": (f"deployed with {value.encode().hex()} today", value.encode().hex()),
        "hex upper": (f"deployed with {value.encode().hex().upper()} today", value.encode().hex().upper()),
        "hex with colons": ("bytes " + ":".join(f"{byte:02x}" for byte in value.encode()),
                            ":".join(f"{byte:02x}" for byte in value.encode())),
        "percent-encoded": ("u?p=" + "".join(f"%{byte:02X}" for byte in value.encode()),
                            "".join(f"%{byte:02X}" for byte in value.encode())),
        "double base64": ("x " + base64.b64encode(encoded.encode()).decode(), base64.b64encode(encoded.encode()).decode()),
        "in a JSON string": ('{"env": {"DEPLOY_SERVICE_TOKEN": "%s"}}' % value, value),
        "in a shell export": (f"export DEPLOY_SERVICE_TOKEN={value}", value),
        "JSON-escaped inside a JSON string": ('{"env": "{\\"T\\": \\"%s\\"}"}' % value, value),
        "base32": ("x " + base64.b32encode(value.encode()).decode(), base64.b32encode(value.encode()).decode()),
        "with zero-width spaces": ("deployed with " + "\u200b".join(value) + " today", value),
    }


# docs/memory.md, "these shapes still pass": forms of an environment secret the scanner does not
# decode, written without a label. The value names no secret word itself, so the line rule leaves it.
NEUTRAL_VALUE = "c0bra-Deploy-mint-phrase-2026"
NEUTRAL_ENVIRONMENT = {**ENVIRONMENT, "DEPLOY_SERVICE_TOKEN": NEUTRAL_VALUE}
ENVIRONMENT_FORMS_PASSING = {
    "concatenated pieces": (f'"{NEUTRAL_VALUE[:14]}" + "{NEUTRAL_VALUE[14:]}"', NEUTRAL_VALUE[14:]),
    "reversed": (f"deployed with {NEUTRAL_VALUE[::-1]} today", NEUTRAL_VALUE[::-1]),
    "upper-cased": (f"deployed with {NEUTRAL_VALUE.upper()} today", NEUTRAL_VALUE.upper()),
    "a 15-character prefix": (f"it starts with {NEUTRAL_VALUE[:15]}", NEUTRAL_VALUE[:15]),
    "a value under eight characters": ("the run printed hunter2 twice", "hunter2"),
}


@pytest.mark.parametrize("form", sorted(environment_forms()))
def test_an_environment_secret_is_masked_in_every_form_the_scanner_decodes(form):
    text, secret = environment_forms()[form]
    masked, clean = redaction.redact(text, redaction.environment_secrets(ENVIRONMENT))
    assert clean and masked_away(secret, masked, 5), (form, masked)


@pytest.mark.parametrize("form", sorted(ENVIRONMENT_FORMS_PASSING))
def test_the_environment_forms_the_docs_name_as_passing_still_pass(form):
    text, kept = ENVIRONMENT_FORMS_PASSING[form]
    assert kept in redaction.redact(text, redaction.environment_secrets(NEUTRAL_ENVIRONMENT))[0]


@pytest.mark.parametrize("text", ["commit as Synthetic Person today", "write to person@example.com",
                                  "the password policy works for new users", "clone github.com/example/* repos",
                                  "the key file id_ed25519 is missing", "layout us-intl-altgr set",
                                  "run it in a subprocess"])
def test_environment_names_count_by_word_so_their_values_stay(text):
    """#701 review: KEYRING, KEYBOARD, AUTHOR, GOPRIVATE, SSH_KEY_NAME and AUTH_MODE name no secret."""
    known = redaction.environment_secrets(ENVIRONMENT)
    assert sorted(secret.label for secret in known) == ["environment:DEPLOY_SERVICE_TOKEN"]
    assert redaction.redact(text, known) == (text, True)


@pytest.mark.parametrize("name,counts", [
    ("DB_PASS", True), ("MYSQL_PWD", True), ("SMTP_PASS", True), ("SENTRY_DSN", True), ("DATABASE_URL", True),
    ("REDIS_URL", True), ("PGPASSWORD", True), ("API_TOKEN", True), ("accessToken", True), ("SecretAccessKey", True),
    ("Authorization", True), ("credentials", True), ("KEYRING", False), ("KEYBOARD_LAYOUT", False),
    ("GIT_AUTHOR_NAME", False), ("GOPRIVATE", False), ("SSH_KEY_NAME", False), ("AUTH_MODE", False), ("PWD", False),
    ("PASS", False), ("max_tokens", False), ("TOKEN_TYPE", False), ("GITHUB_API_URL", False),
    # Plural and joined names (#701 review, final verification).
    ("OPENAI_API_KEYS", True), ("API_KEYS", True), ("SERVICE_TOKENS", True), ("VALID_TOKENS", True),
    ("MASTERKEY", True), ("DEPLOYKEY", True), ("AUTHKEY", True), ("MYAPIKEY", True), ("SIGNINGKEY", True),
    ("GITHUB_OAUTH", True), ("PRIVATE_PEM", True), ("SSHPASS", True), ("DBPASS", True), ("ADMIN_PW", True),
    ("ROOT_PW", True), ("PASSCODE", True), ("JWT", True), ("MAX_TOKENS", False), ("MONKEYPATCH", False),
    ("TOKENIZERS_PARALLELISM", False), ("KEYCLOAK_ADMIN", False), ("PASSWORD_STORE_DIR", False),
    ("AWS_CREDENTIAL_FILE", False), ("XAUTHORITY", False),
])
def test_a_name_names_a_secret_by_its_words(name, counts):
    assert redaction.secret_name(name) is counts


def env_file_lines():
    """The review's 190 env-file lines: 38 names in 5 spellings, each with 24 lowercase letters and digits."""
    generator = random.Random(7)
    names = ["DB_PASSWORD", "PGPASSWORD", "MYSQL_ROOT_PASSWORD", "API_TOKEN", "MY_SERVICE_TOKEN", "GITHUB_TOKEN",
             "JWT_SECRET", "SESSION_SECRET", "WEBHOOK_SECRET", "DJANGO_SECRET_KEY", "SECRET_KEY", "CLIENT_SECRET",
             "OAUTH_CLIENT_SECRET", "STRIPE_API_KEY", "API_KEY", "X_API_KEY", "ENCRYPTION_KEY", "REDIS_AUTH",
             "DB_PASS", "SMTP_PASS", "PRIVATE_KEY_PASSPHRASE", "ACCESS_KEY", "AWS_SECRET_ACCESS_KEY", "TOKEN",
             "PASSWORD", "SECRET", "token", "apiKey", "clientSecret", "accessToken", "refresh_token", "auth_token",
             "authToken", "bearerToken", "x-api-key", "Authorization", "private_key", "credentials"]
    lines = []
    for name in names:
        secret = "".join(generator.choice(LOWER_DIGITS) for _ in range(24))
        for form in ("{n}={v}", "export {n}={v}", '{n}="{v}"', "{n}: {v}", '"{n}": "{v}"'):
            lines.append((form.format(n=name, v=secret), secret))
    return lines


def test_every_env_file_line_of_the_review_is_masked():
    lines = env_file_lines()
    assert len(lines) == 190
    for line, secret in lines:
        masked, clean = redaction.redact(line)
        assert clean and secret not in masked, line


SHAPE_CLASSES = {
    # name: (make one, frame, least share masked of 100 samples)
    "AWS secret key in STS JSON": (lambda g: rnd(g, 40, BASE64_ALPHABET), '{{"SecretAccessKey": "{}"}}', 100),
    "AWS secret key in prose": (lambda g: rnd(g, 40, BASE64_ALPHABET), "the secret access key is {} ok", 100),
    "random alphanumeric 20": (lambda g: rnd(g, 20, ALNUM), "value {} end", 90),
    "random alphanumeric 24": (lambda g: rnd(g, 24, ALNUM), "value {} end", 90),
    "random alphanumeric 32": (lambda g: rnd(g, 32, ALNUM), "value {} end", 95),
    "random alphanumeric 40": (lambda g: rnd(g, 40, ALNUM), "value {} end", 98),
    "token_urlsafe(24)": (lambda g: rnd(g, 32, URLSAFE), "value {} end", 95),
    "random hex 32": (lambda g: rnd(g, 32, HEX), "value {} end", 100),
    "random lowercase and digits 32": (lambda g: rnd(g, 32, LOWER_DIGITS), "value {} end", 90),
    "base64 of 24 random bytes": (lambda g: rnd(g, 32, BASE64_ALPHABET), "value {} end", 95),
    "HTTP Basic value": (lambda g: rnd(g, 27, BASE64_ALPHABET) + "=", "Authorization: Basic {}", 100),
    "Slack xapp token": (lambda g: "xapp-1-A" + rnd(g, 10, UPPER_DIGITS) + "-" + rnd(g, 13, string.digits) + "-"
                         + rnd(g, 64, HEX), "token {} end", 100),
    "Telegram bot token": (lambda g: rnd(g, 10, string.digits) + ":AA" + rnd(g, 33, URLSAFE), "bot {} end", 100),
}
ID_CLASSES = {  # ids that look random and are no secret: none of them is masked
    "tool-use id": (lambda g: "toolu_01" + rnd(g, 22, ALNUM), "call {} done"),
    "bare session id": (lambda g: "session_01" + rnd(g, 22, ALNUM), "see {} now"),
    "request id": (lambda g: "req_011C" + rnd(g, 20, ALNUM), "id {} now"),
    "64-digit hex digest": (lambda g: rnd(g, 64, HEX), "value {} end"),
}


def rnd(generator, length, alphabet):
    return "".join(generator.choice(alphabet) for _ in range(length))


@pytest.mark.parametrize("name", sorted(SHAPE_CLASSES))
def test_secret_shapes_of_the_review_are_masked_at_their_rate(name):
    """The review's Monte Carlo classes, seeded: those it found written whole 33 to 100 times in 100."""
    make, frame, least = SHAPE_CLASSES[name]
    generator = random.Random(2026)
    secrets_ = [make(generator) for _ in range(100)]
    masked = sum(secret not in redaction.redact(frame.format(secret))[0] for secret in secrets_)
    assert masked >= least, (name, masked)


@pytest.mark.parametrize("name", sorted(ID_CLASSES))
def test_random_looking_ids_are_not_masked(name):
    make, frame = ID_CLASSES[name]
    generator = random.Random(2026)
    for _ in range(100):
        text = frame.format(make(generator))
        assert redaction.redact(text) == (text, True)


def test_a_pgp_private_key_block_keeps_no_line_of_its_body():
    generator = random.Random(11)
    body = "\n".join(rnd(generator, 64, BASE64_ALPHABET) for _ in range(12))
    marker = "PGP PRIVATE " + "KEY BLOCK"
    text = f"-----BEGIN {marker}-----\n\n{body}\n=" + rnd(generator, 4, BASE64_ALPHABET) + f"\n-----END {marker}-----"
    masked, clean = redaction.redact("Here is the key:\n" + text + "\nDone.")
    assert clean and masked.endswith("Done.")
    assert all(longest_left(line, masked) <= 4 for line in body.splitlines()), masked


def test_redaction_keeps_its_time_on_large_texts():
    """#701 review: a megabyte of prose took 1.2 s; the rules added in attempt 3 keep that order."""
    generator = random.Random(5)
    words = ["the", "journal", "entry", "is", "written", "under", "a", "key", "made", "of", "session", "and",
             "prompt", "ids", "`history.md`", "rotation", "archive", "lock", "timeout", "hook", "Stop", "event"]
    text = "\n".join(" ".join(generator.choice(words) for _ in range(14)) for _ in range(4000))  # about 300 kB
    started = time.monotonic()
    masked, clean = redaction.redact(text)
    assert clean and masked == text
    assert time.monotonic() - started < 10


def test_redaction_stops_at_its_deadline():
    """A round the deadline leaves no time for is not started: the field is not clean, so it is withheld."""
    assert redaction.redact("password: hunter2", deadline=time.monotonic() - 1) == ("password: hunter2", False)
    assert redaction.redact("password: hunter2", deadline=time.monotonic() + 60) == ("password<secret>", True)


# --- The second review's reproductions, now asserting the fixed behaviour (#701 review) -----------

FIRST_TURN = ("The first turn's answer explains the four release steps in the order the installer runs them.\n\n"
              + FOOTER)
SECOND_TURN = "The second turn's answer lists the three files that changed and why each one changed.\n\n" + FOOTER


def test_a_promptless_stop_on_a_lagging_transcript_never_replaces_the_previous_turns_entry(project, tmp_path):
    """r1: without a prompt id the hook pairs the Stop's answer only with a turn that holds it."""
    def promptless(text, row_id):  # an older Claude Code: neither the row nor the hook input has a prompt id
        row = {**opener("ignored", text), "uuid": row_id}
        del row["promptId"]
        return row
    rows = [promptless("first question", "row-1"), *activation_rows(), answer_row(FIRST_TURN, "m1")]
    transcript = rows_file(tmp_path / "t.jsonl", rows)
    assert hook.record_turn(stop_event(project, transcript, last_assistant_message=FIRST_TURN),
                            interactive=True)["status"] == "recorded"
    # The second turn ends while the transcript still lags: its rows are not there yet.
    with pytest.raises(hook.TurnNotLogged, match="does not hold the Stop's answer"):
        hook.record_turn(stop_event(project, transcript, last_assistant_message=SECOND_TURN), interactive=True)
    assert [(entry.intent, entry.outcome) for entry in entries(project)] == [("first question", flat(FIRST_TURN))]
    # Once the transcript holds the turn, the same Stop logs it as its own entry.
    rows_file(transcript, [*rows, promptless("second question", "row-2"), answer_row(SECOND_TURN, "m2")])
    assert hook.record_turn(stop_event(project, transcript, last_assistant_message=SECOND_TURN),
                            interactive=True)["status"] == "recorded"
    assert [(entry.intent, entry.outcome) for entry in entries(project)] == [
        ("first question", flat(FIRST_TURN)), ("second question", flat(SECOND_TURN))]


def test_a_hook_killed_between_the_append_and_the_archive_rewrite_keeps_the_new_answer(project, tmp_path, monkeypatch):
    """r2: the replacement is written into history.md first, the archived copy removed after it, so a
    kill between the two leaves the corrected answer, which readers show; the next write under the
    key removes the older copy."""
    monkeypatch.setattr(history_module, "HistoryWriter", SmallJournal)
    transcript = corrected_turn(tmp_path, answer_row(CORRECTED, "m2"))
    first = hook.record_turn(stop_event(project, transcript, prompt_id="p1", last_assistant_message=LONG_ANSWER),
                             interactive=True)
    [archive] = (project / "history").glob("*.md")
    assert first["entry_id"] in archive.read_text(encoding="utf-8")
    real_replace, calls = history_module._replace, []

    def killed_at_the_second_replace(path, text, *, info):
        calls.append(os.path.basename(path))
        if len(calls) == 2:
            raise KeyboardInterrupt("the client cancelled the hook at its timeout")
        return real_replace(path, text, info=info)
    monkeypatch.setattr(history_module, "_replace", killed_at_the_second_replace)
    with pytest.raises(KeyboardInterrupt):
        hook.record_turn(stop_event(project, transcript, prompt_id="p1", stop_hook_active=True,
                                    last_assistant_message=CORRECTED), interactive=True)
    monkeypatch.setattr(history_module, "_replace", real_replace)
    assert calls == ["history.md", archive.name]  # the live file first, then the archive
    reader = HistoryReader(str(project / "history.md"), str(project / "history"))
    assert [entry.outcome for entry in reader.read_recent(limit=10) if entry.intent == "question"] == [flat(CORRECTED)]
    # The same Stop again, as a retry would be: the older copy goes, the corrected one stays.
    again = hook.record_turn(stop_event(project, transcript, prompt_id="p1", stop_hook_active=True,
                                        last_assistant_message=CORRECTED), interactive=True)
    assert again["entry_id"] == first["entry_id"]
    assert [entry.outcome for entry in entries(project) if entry.intent == "question"] == [flat(CORRECTED)]


def blocks(root):
    return {entry.id + entry.timestamp: block for entry, block in
            history_module.journal_blocks(str(root / "history.md"), str(root / "history"))}


def test_supersede_and_withdraw_touch_only_the_hooks_own_keyed_entry(project, tmp_path):
    """r3, kept as a regression test: every other entry stays byte for byte."""
    writer = HistoryWriter(str(project / "history.md"), str(project / "history"), rotation_kb=1)
    transcript = corrected_turn(tmp_path, answer_row(CORRECTED, "m2"))
    writer.append_entry("model unkeyed before", "Agent: a", "x" * 300)
    writer.append_entry("model keyed before", "Agent: a", "y" * 300, idempotency_key="uuid-model-1")
    other = {**stop_event(project, transcript, prompt_id="p1", last_assistant_message=LONG_ANSWER),
             "session_id": "another-session"}
    assert hook.record_turn(other, interactive=True)["status"] == "recorded"
    first = hook.record_turn(stop_event(project, transcript, prompt_id="p1", last_assistant_message=LONG_ANSWER),
                             interactive=True)
    for index in range(8):  # push the hook's entry into an archive
        writer.append_entry(f"model filler {index}", "Agent: a", "z" * 300)
    writer.append_entry("model keyed after", "Agent: a", "w" * 300, idempotency_key="uuid-model-2")
    own = lambda: [entry for entry in entries(project) if (entry.metadata or {}).get("session_id") == SESSION]  # noqa: E731
    before = blocks(project)
    mine = [key for key in before if key.startswith(first["entry_id"])]
    assert len(mine) == 1
    again = hook.record_turn(stop_event(project, transcript, prompt_id="p1", stop_hook_active=True,
                                        last_assistant_message=CORRECTED), interactive=True)
    assert again["status"] == "recorded" and [entry.outcome for entry in own()] == [flat(CORRECTED)]
    after = blocks(project)
    assert {k: v for k, v in before.items() if k not in mine} == {
        k: v for k, v in after.items() if not k.startswith(first["entry_id"])}
    rows_file(transcript, [opener("p1", "question"), *activation_rows(), answer_row(LONG_ANSWER, "m1"), FEEDBACK,
                           *log_rows({"status": "recorded"}), answer_row("Logged.", "m3")])
    gone = hook.record_turn(stop_event(project, transcript, prompt_id="p1", stop_hook_active=True,
                                       last_assistant_message="Logged."), interactive=True)
    assert gone == {"status": "skipped", "reason": "logged_by_model", "withdrawn": True} and own() == []
    assert blocks(project) == {k: v for k, v in after.items() if not k.startswith(first["entry_id"])}


def test_withdraw_finds_the_entry_in_a_pending_first_rotation(project, tmp_path):
    """r6: a first rotation that moved history.md aside left no history.md and no archive directory."""
    transcript = rows_file(tmp_path / "t.jsonl", [opener("p1", "do not record this"), *activation_rows(),
                                                  answer_row(LONG_ANSWER, "m1")])
    hook.record_turn(stop_event(project, transcript, prompt_id="p1", last_assistant_message=LONG_ANSWER),
                     interactive=True)
    os.replace(project / "history.md", project / "history.md.rotating")
    assert not (project / "history").exists()
    rows_file(transcript, [opener("p1", "do not record this"), *activation_rows(), answer_row(LONG_ANSWER, "m1"),
                           FEEDBACK, *log_rows({"status": "withheld", "state": "not_written"},
                                               arguments={"journal": False}), answer_row("Kept out.", "m2")])
    again = hook.record_turn(stop_event(project, transcript, prompt_id="p1", stop_hook_active=True,
                                        last_assistant_message="Kept out."), interactive=True)
    assert again == {"status": "skipped", "reason": "not_journaled", "withdrawn": True}
    assert entries(project) == []


def test_a_secret_in_the_footers_place_never_reaches_the_action_line(project, tmp_path):
    """r7: only a canonical agent name counts as the footer's; any other text there is no footer."""
    token = "sk-ant-" + "api03-" + fake(32, ALNUM, 7)
    answer = "Done.\n\n**Agent**: " + token
    transcript = rows_file(tmp_path / "t.jsonl", [opener("p1", "question"), *activation_rows(), answer_row(answer, "m1")])
    hook.record_turn(stop_event(project, transcript, prompt_id="p1", last_assistant_message=answer), interactive=True)
    [entry] = entries(project)
    assert token not in entry.outcome and token not in entry.action
    assert entry.action.startswith("Agent: software_engineer Persona (unverified)") and "missing=footer" in entry.action


def test_a_malformed_receipt_comment_stops_no_later_turn(project, tmp_path, monkeypatch, capsys):
    """r13: the hook's writes skip a receipt comment cut short, as a Meta line that is no object."""
    writer = HistoryWriter(str(project / "history.md"), str(project / "history"))
    writer.append_entry("an old turn", "Agent: x", "an old answer")
    with open(project / "history.md", "a", encoding="utf-8") as stream:
        stream.write('\n<!-- agents-write-receipt {"key_digest": "cut short -->\n')
    transcript = rows_file(tmp_path / "t.jsonl", [opener("p1", "question"), answer_row("Answer.", "m1")])
    event = stop_event(project, transcript, prompt_id="p1", last_assistant_message="Answer.")
    assert run_main(monkeypatch, capsys, json.dumps(event).encode(), ["--interactive"]) == (0, "", "")
    assert [entry.intent for entry in entries(project)] == ["an old turn", "question"]


def test_a_pasted_env_file_and_its_echo_reach_the_journal_masked(project, tmp_path, monkeypatch):
    """r14: values after secret names, in every spelling, whatever their shape."""
    generator = random.Random(14)
    api_token, jwt_secret = rnd(generator, 32, HEX), rnd(generator, 28, LOWER_DIGITS)
    db_password = rnd(generator, 16, LOWER_DIGITS)
    xapp = "xapp-1-A0" + rnd(generator, 9, string.digits) + "-" + rnd(generator, 13, string.digits) + "-" + \
        rnd(generator, 64, HEX)
    prompt = ("Why does the deploy fail? My .env:\n"
              f"API_TOKEN={api_token}\nJWT_SECRET={jwt_secret}\ndb_password: {db_password}\nSLACK_APP_TOKEN={xapp}\n")
    answer = (f"The deploy fails because JWT_SECRET={jwt_secret} is shorter than 32 bytes; API_TOKEN={api_token} "
              "is fine.\n\n" + FOOTER)
    transcript = rows_file(tmp_path / "t.jsonl", [opener("p1", prompt), *activation_rows(), answer_row(answer, "m1")])
    assert hook.record_turn(stop_event(project, transcript, prompt_id="p1", last_assistant_message=answer),
                            interactive=True)["status"] == "recorded"
    text = (project / "history.md").read_text(encoding="utf-8")
    for secret in (api_token, jwt_secret, db_password, xapp):
        assert secret not in text, secret
    [entry] = entries(project)
    assert "redaction" not in entry.metadata and "JWT_SECRET<secret>" in entry.intent


def test_a_turn_whose_own_call_the_server_refused_is_not_logged(project, tmp_path, monkeypatch, capsys):
    """The protocol review's probe: a redacted call refused with history_busy left the hook writing the
    raw turn. A refused call left no entry before the hook, and the hook must not write what it may
    have been redacting: it removes its own entry and exits 1 with the call's error code."""
    private = "My neighbour Ivan Petrov of 12 Elm Street was diagnosed with PRIVATE-DETAIL-2."
    rows = [opener("p1", private), *activation_rows(), answer_row(LONG_ANSWER, "m1")]
    transcript = rows_file(tmp_path / "t.jsonl", rows)
    hook.record_turn(stop_event(project, transcript, prompt_id="p1", last_assistant_message=LONG_ANSWER),
                     interactive=True)
    rows_file(transcript, [*rows, FEEDBACK, *log_rows({"status": "error", "state": "rejected", "error": "history_busy"},
                                                      arguments={"query": "[a question about a third party]"}),
                           answer_row("Logged.", "m2")])
    event = stop_event(project, transcript, prompt_id="p1", stop_hook_active=True, last_assistant_message="Logged.")
    code, out, err = run_main(monkeypatch, capsys, json.dumps(event).encode(), ["--interactive"])
    assert (code, out) == (1, "") and "its own log_interaction call was refused (history_busy)" in err
    assert entries(project) == [] and "Petrov" not in (project / "history.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("history,reason", [
    ({"status": "recorded", "state": "durably_accepted"}, "a server older than journal=false wrote the turn"),
    ({"status": "error", "state": "rejected", "error": "history_busy"}, "was refused (history_busy), not withheld"),
])
def test_a_journal_false_call_that_was_not_withheld_is_reported(project, tmp_path, monkeypatch, capsys, history, reason):
    """The protocol review's probe: a server older than journal=false ignores it and writes the turn,
    and the hook used to answer skipped with exit 0. It now removes its own entry and exits 1."""
    rows = [opener("p1", "my diagnosis, keep it out of the journal"), *activation_rows(), answer_row(LONG_ANSWER, "m1")]
    transcript = rows_file(tmp_path / "t.jsonl", rows)
    hook.record_turn(stop_event(project, transcript, prompt_id="p1", last_assistant_message=LONG_ANSWER),
                     interactive=True)
    rows_file(transcript, [*rows, FEEDBACK, *log_rows(history, arguments={"journal": False}), answer_row("Done.", "m2")])
    event = stop_event(project, transcript, prompt_id="p1", stop_hook_active=True, last_assistant_message="Done.")
    code, out, err = run_main(monkeypatch, capsys, json.dumps(event).encode(), ["--interactive"])
    assert (code, out) == (1, "") and reason in err
    assert entries(project) == []


@pytest.mark.parametrize("value", [False, "false", "0", "no"])
def test_journal_false_counts_as_the_server_reads_it(value):
    assert hook._journal_false({"journal": value}) and not hook._journal_false({"journal": True})


def test_a_replaced_or_withdrawn_entry_is_recorded_for_user_sync(project, tmp_path, monkeypatch):
    """#701 review: the hook process shares nothing, but it records what it removes, so the next
    reconcile of a process that shares this repository's history takes it out of the shared copy."""
    from src.user_sync import history as history_sync
    recorded = []
    monkeypatch.setattr(history_sync, "record_deletion",
                        lambda path, entry_id, timestamp: recorded.append((path, entry_id, timestamp)))
    transcript = corrected_turn(tmp_path, answer_row(CORRECTED, "m2"))
    first = hook.record_turn(stop_event(project, transcript, prompt_id="p1", last_assistant_message=LONG_ANSWER),
                             interactive=True)
    again = hook.record_turn(stop_event(project, transcript, prompt_id="p1", stop_hook_active=True,
                                        last_assistant_message=CORRECTED), interactive=True)
    journal = str(project / "history.md")
    assert recorded == [(journal, first["entry_id"], first["timestamp"])]
    rows_file(transcript, [opener("p1", "question"), *activation_rows(), answer_row(LONG_ANSWER, "m1"), FEEDBACK,
                           *log_rows({"status": "recorded"}), answer_row("Logged.", "m3")])
    hook.record_turn(stop_event(project, transcript, prompt_id="p1", stop_hook_active=True,
                                last_assistant_message="Logged."), interactive=True)
    assert recorded[1:] == [(journal, again["entry_id"], again["timestamp"])]
    assert history_module._sync is None  # the hook's integration is gone once the write is done


def build_archives(root, megabytes, months=12):
    """Synthetic archives of about ``megabytes`` in all, months of 3 kB entries, written directly."""
    (root / "history").mkdir(parents=True, exist_ok=True)
    body = " ".join(["journal entry answer release notes review finding worker contract rotation"] * 38)
    per_month = megabytes * 1024 * 1024 // months
    for month in range(months):
        name = f"2025-{month + 1:02d}"
        blocks_, size, index = [], 0, 0
        while size < per_month:
            block = HistoryWriter._render_entry(f"{month:02d}{index:010x}", f"{name}-10T10:00:00+00:00",
                                                f"prompt {month}-{index}", "Agent: software_engineer", body, None, None,
                                                {"answer_timestamp": "2026.11.27 10:00:00"})
            blocks_.append(block)
            size += len(block)
            index += 1
        (root / "history" / f"{name}.md").write_text("".join(blocks_), encoding="utf-8")


def test_a_hook_write_on_a_large_journal_parses_only_what_holds_its_key(project, tmp_path, monkeypatch):
    """#701 review: every hook write parsed every archive under the journal's lock (6 s at 400 MB). A
    file without the key's entry id is skipped after a substring search of its bytes."""
    build_archives(project, 24)
    parsed = []
    real_parse = HistoryReader._parse

    def counting(content):
        parsed.append(len(content))
        return real_parse(content)
    monkeypatch.setattr(HistoryReader, "_parse", staticmethod(counting))
    transcript = corrected_turn(tmp_path, answer_row(CORRECTED, "m2"))
    started = time.monotonic()
    first = hook.record_turn(stop_event(project, transcript, prompt_id="p1", last_assistant_message=LONG_ANSWER),
                             interactive=True)
    again = hook.record_turn(stop_event(project, transcript, prompt_id="p1", stop_hook_active=True,
                                        last_assistant_message=CORRECTED), interactive=True)
    withdrawn = HistoryWriter(str(project / "history.md"), str(project / "history")).withdraw(
        hook.turn_key({"session_id": SESSION}, "p1"))
    elapsed = time.monotonic() - started
    assert first["status"] == again["status"] == "recorded" and withdrawn["status"] == "withdrawn"
    assert max(parsed) < 1024 * 1024  # never an archive: only history.md, which holds the key
    assert elapsed < 5, elapsed


def test_stale_temporary_files_of_killed_writes_are_removed(project):
    """#701 review: a write killed before its move left a full copy beside the journal for good."""
    writer = HistoryWriter(str(project / "history.md"), str(project / "history"), rotation_kb=0)
    writer.append_entry("first", "Agent: a", "rotated into an archive")
    stale = [project / ".history.md.0123456789abcdef.tmp", project / "history" / ".2025-01.md.fedcba9876543210.tmp"]
    others = [project / ".CLAUDE.md.0123456789abcdef.tmp", project / ".history.md.lock", project / "history.md.tmp"]
    for path in stale + others:
        path.write_text("x", encoding="utf-8")
    writer.append_entry("second", "Agent: a", "the next write removes the leftovers")
    assert not any(path.exists() for path in stale)
    assert all(path.exists() for path in others)


def test_a_stop_past_its_deadline_writes_nothing_and_says_so(project):
    with pytest.raises(hook.TurnNotLogged, match="ran out of time"):
        hook.record_turn(stop_event(project, prompt_id="prompt-2"), interactive=True, deadline=time.monotonic() - 1)
    assert not (project / "history.md").exists()


def test_the_command_ends_itself_before_the_clients_timeout():
    """The watchdog of the command line: exit 1 with the reason, before Claude Code would cancel it."""
    probe = ("import time; from src.memory import turn_log_hook as hook; hook._watchdog(0.2); time.sleep(30)")
    result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, timeout=60,
                            env={**os.environ, "LANGFUSE_TRACING_ENABLED": "false"})
    assert result.returncode == 1 and b"turn not logged (the hook ran out of time)" in result.stderr
    assert hook.DEADLINE_SECONDS < hook.WATCHDOG_SECONDS < hook.HARD_WATCHDOG_SECONDS < 30


# --- The final verification's low findings (#701 review, attempt 4) -------------------------------


def test_the_hard_watchdog_ends_a_command_whose_computation_holds_the_interpreter_lock():
    """One regular-expression match held the interpreter lock past the 28-second timer thread, and the
    command ran for 70 s; faulthandler's watchdog thread needs no lock."""
    probe = ("import re; from src.memory import turn_log_hook as hook; hook._watchdog(30, 0.5); "
             "re.match(r'(a|aa)+$', 'a' * 80 + 'b')")
    started = time.monotonic()
    result = subprocess.run([sys.executable, "-c", probe], cwd=ROOT, capture_output=True, timeout=120,
                            env={**os.environ, "LANGFUSE_TRACING_ENABLED": "false"})
    assert result.returncode == 1 and b"Timeout" in result.stderr
    assert time.monotonic() - started < 30


def persona_rows(tool_name, persona):
    return [{"type": "assistant", "message": {"id": "a1", "content": [
                {"type": "tool_use", "id": "t1", "name": tool_name, "input": {}}]}},
            {"type": "user", "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": json.dumps(
                    {"status": "SUCCESS", "persona": persona})}]}}]


@pytest.mark.parametrize("tool_name,read", [
    ("mcp__some-other-server__route_and_load", False), ("mcp__Agents-Core__route_and_load", True),
    ("mcp__Agents-Core-Desktop__get_agent_context", True), ("mcp__plugin_agents-core_Agents-Core__route_and_load", True),
])
def test_only_an_agents_core_servers_persona_result_attributes_the_turn(project, tmp_path, tool_name, read):
    """Final verification M3: a result of another server's tool of the same name set the attribution."""
    transcript = rows_file(tmp_path / "t.jsonl", [opener("p1", "question"), *persona_rows(tool_name, PERSONA),
                                                  answer_row("Done.", "m1")])
    hook.record_turn(stop_event(project, transcript, prompt_id="p1", last_assistant_message="Done."), interactive=True)
    [entry] = entries(project)
    assert entry.action.startswith("Agent: software_engineer" if read else "Agent: unattributed"), entry.action


def test_a_descriptor_agent_that_is_no_canonical_name_never_reaches_the_action_line(project, tmp_path):
    """Final verification E4: a token-shaped agent in the persona result was written whole."""
    odd = "sk-ant-" + "api03-" + fake(32, ALNUM, 9)
    transcript = rows_file(tmp_path / "t.jsonl", [opener("p1", "question"), *activation_rows(persona={**PERSONA, "agent": odd}),
                                                  answer_row("Done.", "m1")])
    hook.record_turn(stop_event(project, transcript, prompt_id="p1", last_assistant_message="Done."), interactive=True)
    [entry] = entries(project)
    assert odd not in entry.action and "api03" not in entry.action and entry.action.startswith("Agent: unattributed")


def test_the_persona_line_is_cleaned_as_the_turns_text_is(project, tmp_path):
    secret = fake(24, ALNUM, 11)
    persona = {**PERSONA, "activation_id": "token=" + secret}
    transcript = rows_file(tmp_path / "t.jsonl", [opener("p1", "question"), *activation_rows(persona=persona),
                                                  answer_row("Done.\n\n" + FOOTER, "m1")])
    hook.record_turn(stop_event(project, transcript, prompt_id="p1", last_assistant_message="Done.\n\n" + FOOTER),
                     interactive=True)
    [entry] = entries(project)
    assert secret not in entry.action and entry.action.startswith("Agent: software_engineer")
