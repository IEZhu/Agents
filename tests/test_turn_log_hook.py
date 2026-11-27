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
    """#701 review: thread-close's record and tags and autodev-retro's tag need the model's own call."""
    calls = flat(protocol_text.part("finish", "hook-calls"))
    for phrase in ("when a flow or the user asks for it", "record, tags or files", "to log the turn redacted",
                   "journal=false", "the hook then leaves that turn to the call",
                   "Add a time line only when such a call returns a timestamp."):
        assert phrase in calls, phrase
    assert "a call a flow or the user asks for is still made, its timestamp alone giving a time line" in \
        protocol_text.bundle_pointer(True)


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
    assert "the latest SUCCESS decides over the server instructions" in rule
    assert rule in protocol_text.bundle_instruction()
    for text in (protocol_text.bundle_pointer(), protocol_text.bundle_section_pointer(),
                 protocol_text.no_change_instruction()):
        assert "footer and log_interaction unless this turn's Agents-Core turn-log notice" in text
    template = flat(protocol_text.template())
    assert ("When this turn's prompt carries the Agents-Core turn-log notice of the session's own Stop hook, "
            "or the server instructions or the latest `SUCCESS`, which decides over them, say that this "
            "client's Stop hook logs each turn, follow that notice or instruction in place of the "
            "`log_interaction` steps of this protocol") in template
    reminder = flat(protocol_text.memory_reminder())
    assert "Unless this turn's Agents-Core turn-log notice, or the server instructions or the latest SUCCESS" in reminder
    # The turn-end sentence no longer tells a hooked session to log (#701 review).
    assert "log it and include the footer" not in reminder and "is logged like any other" in reminder


def test_the_notice_states_what_the_hook_records_and_what_the_model_still_does():
    notice = protocol_text.turn_log_notice()
    assert notice.startswith("Agents-Core turn log: this session's Stop hook records this turn")
    for phrase in ("still ends with the footer", "journal=false", "when a flow or the user asks for it",
                   "Add a time line only when such a call returns a timestamp",
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
    (FAKE_OPENAI_PROJECT_KEY, "sk-proj-<secret>"),
    ("xoxb-1234567890-abcdefghij", "xoxb-<secret>"),
    ("AIzaSyA1234567890abcdefghijklmnopqrstu", "AIza<secret>"),
    (FAKE_JWT, "eyJ<secret>"),
    ("aws_secret_access_key=wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY", "aws_secret_access_key=<secret>"),
    (f'"capability":"{FAKE_CAPABILITY}"', '"capability":"<secret>"'),
    ('{"password":"hunter2"}', '{"password":"<secret>"}'),
    ("password=short1", "password=<secret>"),
    ("ghp_abcdefghijklmnop1234", "ghp_***"),
])
def test_the_hook_masks_common_secret_formats(secret, kept):
    masked, clean = hook.redact(f"before {secret} after")
    assert clean and masked == f"before {kept} after"


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
    assert hook.redact(f"use {token} now") == (f"use {hook.MASK} now", True)


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
    assert clean and leak not in masked and hook.MASK in masked


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
    hook.record_turn(stop_event(project, last_assistant_message=f"Used {value} to deploy.\n\n{FOOTER}"))
    [entry] = entries(project)
    assert value not in entry.outcome and entry.outcome.startswith(f"Used {hook.MASK} to deploy.")


def test_text_the_scanner_cannot_clean_is_withheld_and_the_entry_marked(switches, project, monkeypatch):
    """The hook never writes a text its detector could not clean; the turn stays, marked."""
    from src.schedules import scan
    monkeypatch.setattr(scan, "MAX_TEXT_LENGTH", 60)  # the answer is longer: unscannable
    hook.record_turn(stop_event(project, last_assistant_message="x" * 80 + "\n\n" + FOOTER))
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


@pytest.mark.parametrize("history,logged", [
    ({"status": "recorded"}, True), ({"status": "duplicate"}, True),
    ({"status": "queued"}, True),  # a daemon older than this hook accepted the write
    ({"status": "error", "state": "unknown", "error": "history_timeout"}, True),  # it may still commit
    ({"status": "error", "state": "rejected", "error": "history_busy"}, False),
])
def test_a_turn_the_model_logged_or_may_have_logged_is_left_to_it(project, tmp_path, history, logged):
    transcript = rows_file(tmp_path / "t.jsonl", [
        opener("p1", "question"), *activation_rows(), *log_rows(history), answer_row("Answer.\n\n" + FOOTER)])
    result = hook.record_turn(stop_event(project, transcript, prompt_id="p1"), interactive=True)
    assert (result["status"] == "skipped") is logged
    if logged:
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
