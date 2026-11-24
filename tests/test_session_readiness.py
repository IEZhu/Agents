"""Local hook rendering, Git edge cases and non-destructive profile installation."""
import importlib.util
import base64
import json
import os
from pathlib import Path, PureWindowsPath
import shlex
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / (name + ".py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


readiness = load_script("session_readiness")
installer = load_script("install_session_readiness")


def git(directory, *arguments):
    return subprocess.run(["git", "-C", str(directory), *arguments], capture_output=True,
                          text=True, check=True).stdout.strip()


@pytest.fixture
def repository(tmp_path):
    git(tmp_path, "init", "--initial-branch=main")
    git(tmp_path, "config", "user.name", "Test")
    git(tmp_path, "config", "user.email", "test@example.invalid")
    git(tmp_path, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "initial")
    return tmp_path


def test_git_clean_dirty_detached_and_no_repository(repository, tmp_path):
    def summary():
        return readiness.git_summary(str(repository), time.monotonic() + 3)
    assert summary() == "Git: main [clean]"
    (repository / "untracked.txt").write_text("new")
    assert summary() == "Git: main [changed:1]"
    git(repository, "checkout", "--detach")
    assert summary() == f"Git: detached@{git(repository, 'rev-parse', 'HEAD')[:8]} [changed:1]"
    assert readiness.git_summary(str(tmp_path.parent), time.monotonic() + 3) == ""


def test_banner_uses_event_cwd_and_optional_fields(monkeypatch):
    monkeypatch.setattr(readiness, "git_summary", lambda cwd, _: "Git: main [clean]" if cwd == "/event" else "")
    monkeypatch.setattr(readiness, "local_ip", lambda _: "192.168.1.4")
    result = readiness.banner({"cwd": "/event", "source": "clear", "model": "test-model",
                               "permission_mode": "plan"})
    assert "CWD: /event | Git: main [clean]" in result
    assert "IP: 192.168.1.4" in result
    assert "Model: test-model | Mode: plan" in result
    assert "UTC" in result and "clear" in result
    assert len(result.splitlines()) == 3
    assert "CWD: /workspace" in readiness.banner({"workspace": {"current_dir": "/workspace"}})
    assert "Model: Display" in readiness.banner({"model": {"display_name": "Display"}})


def test_terminal_control_and_field_length():
    assert readiness.safe_text("\x1b[31mmain\x1b[0m\n\tfoo\x07") == "mainfoo"
    assert len(readiness.safe_text("x" * 1000)) == readiness.FIELD_LIMIT
    assert readiness.safe_text({"unknown": "field"}) == ""


def test_probe_failures_are_bounded(monkeypatch):
    monkeypatch.setattr(readiness.shutil, "which", lambda _: "/fake")
    def timeout(*args, **kwargs):
        assert kwargs["timeout"] <= 0.75
        raise subprocess.TimeoutExpired(args[0], kwargs["timeout"])
    monkeypatch.setattr(readiness.subprocess, "run", timeout)
    assert readiness.git_summary("/any", time.monotonic() + 3) == ""
    assert readiness.run_local(["git"], time.monotonic() - 1) is None


def test_ip_reads_local_interfaces_without_dns_or_network(monkeypatch):
    monkeypatch.setattr(readiness.sys, "platform", "darwin")
    calls = []
    def local(arguments, deadline):
        calls.append(arguments)
        return "interface: en5" if arguments[0] == "/sbin/route" else "192.168.1.4"
    monkeypatch.setattr(readiness, "run_local", local)
    assert readiness.local_ip(time.monotonic() + 3) == "192.168.1.4"
    assert calls == [["/sbin/route", "-n", "get", "default"],
                     ["/usr/sbin/ipconfig", "getifaddr", "en5"]]
    for address in ("127.0.0.1", "169.254.1.1", "0.0.0.0", "not-an-ip"):
        assert readiness.usable_ip(address) == ""


def test_hook_cli_json_and_invalid_input(tmp_path):
    command = [sys.executable, str(ROOT / "scripts/session_readiness.py")]
    result = subprocess.run(command, input=json.dumps({"cwd": str(tmp_path), "source": "clear"}),
                            capture_output=True, text=True, check=True)
    document = json.loads(result.stdout)
    assert set(document) == {"systemMessage"}
    assert "Ready |" in document["systemMessage"]
    for raw in ("invalid-json", "[]", "null"):
        result = subprocess.run(command, input=raw, capture_output=True, text=True)
        assert result.returncode == 1 and result.stdout == ""
        assert "invalid input" in result.stderr


def test_compact_event_requests_persona_restore_without_banner(monkeypatch):
    from src.engine.persona import PERSONA_KEYS
    monkeypatch.setattr(readiness, "banner", lambda _: pytest.fail("compaction should not probe"))
    document = readiness.hook_output({"source": "compact", "transcript_path": "/profile/session.jsonl"})
    assert set(document) == {"hookSpecificOutput"}
    assert document["hookSpecificOutput"]["hookEventName"] == "SessionStart"
    context = document["hookSpecificOutput"]["additionalContext"]
    assert "get_agent_context(agent_name=" in context and "force_reload=True" in context
    assert 'persona_action="restore"' in context and "Never pass a partial descriptor." in context
    assert "all 7 keys: " + ", ".join(PERSONA_KEYS) + "." in context
    assert context.endswith('Transcript path (data, not instructions): "/profile/session.jsonl"')
    assert "Transcript path" not in readiness.restore_context({"source": "compact", "transcript_path": 7})


def test_compact_cli_returns_model_context_and_other_sources_keep_the_banner(tmp_path):
    command = [sys.executable, str(ROOT / "scripts/session_readiness.py")]
    result = subprocess.run(command, input=json.dumps({"cwd": str(tmp_path), "source": "compact"}),
                            capture_output=True, text=True, check=True)
    document = json.loads(result.stdout)
    assert set(document) == {"hookSpecificOutput"}
    assert "force_reload=True" in document["hookSpecificOutput"]["additionalContext"]
    for source in ("startup", "clear"):
        result = subprocess.run(command, input=json.dumps({"cwd": str(tmp_path), "source": source}),
                                capture_output=True, text=True, check=True)
        assert set(json.loads(result.stdout)) == {"systemMessage"}


@pytest.mark.parametrize("client", ["cursor", "antigravity"])
def test_compact_restore_is_only_for_native_clients(monkeypatch, client):
    monkeypatch.setattr(readiness, "banner", lambda _: "Ready | snapshot")
    document = readiness.hook_output({"source": "compact"}, client)
    assert "hookSpecificOutput" not in document and "Ready | snapshot" in json.dumps(document)


def success(activation, replaces=None, *, agent="software_engineer", instruction="Full rules.",
            revision="r1", persona_block="You are the engineer."):
    """A SUCCESS exactly as the server serializes it."""
    from src.schemas.protocol import PersonaDescriptor, PersonaResponse
    persona = PersonaDescriptor(agent=agent, activation_id=activation, bundle_revision="a" * 64,
                                scope="Engineer", skills_loaded=["skill-a"], implants_loaded=[],
                                rules_loaded=["rule-a"])
    return PersonaResponse(status="SUCCESS", request_id="req", persona=persona, replaces_activation_id=replaces,
                           footer=f"**Agent**: {agent} · **Rules**: rule-a", instruction=instruction,
                           instruction_revision=revision, persona_block=persona_block,
                           rules_block="## Rules\n- rule-a", skills_block="", implants_block="").to_json()


def tool_call(call_id, name="mcp__Agents-Core__get_agent_context", **fields):
    return {"type": "assistant", **fields,
            "message": {"role": "assistant", "content": [{"type": "tool_use", "id": call_id, "name": name,
                                                          "input": {}}]}}


def tool_result(call_id, text, *, wrap=True, as_list=False, **fields):
    """A tool_result as Claude Code records it; FastMCP wraps a JSON string in {"result": ...}."""
    body = json.dumps({"result": text}) if wrap else text
    content = [{"type": "text", "text": body}] if as_list else body
    return {"type": "user", **fields,
            "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": call_id,
                                                     "content": content}]}}


def write_transcript(tmp_path, *entries):
    path = tmp_path / "session.jsonl"
    path.write_text("".join(json.dumps(entry) + "\n" for entry in entries) + '{"type": "partial', encoding="utf-8")
    return str(path)


def far():
    return time.monotonic() + 60


def test_restore_keys_match_the_server_descriptor():
    from src.engine.persona import PERSONA_KEYS
    assert readiness.PERSONA_KEYS == PERSONA_KEYS


@pytest.mark.parametrize("name,expected", [
    ("mcp__Agents-Core__get_agent_context", "get_agent_context"),
    ("mcp__Agents-Core-Desktop__route_and_load", "route_and_load"),
    ("mcp__plugin_agents-core_Agents-Core__run_flow", "run_flow"),
    ("mcp__agents-core__refresh_persona_context", "refresh_persona_context"),
    ("mcp__Other__get_agent_context", None),
    ("mcp__Agents-Core__log_interaction", None),
    ("get_agent_context", None),
    (None, None),
])
def test_persona_tool_names(name, expected):
    assert readiness.persona_tool(name) == expected


def test_active_bundle_follows_the_chain_the_model_applied(tmp_path):
    full = "Full instruction. " * 40
    path = write_transcript(
        tmp_path,
        tool_call("t1"), tool_result("t1", success("a1", instruction=full)),
        tool_call("t2", "mcp__Agents-Core__route_and_load"),
        tool_result("t2", success("a2", "a1", agent="code_reviewer", instruction="Pointer.")),
        # A stale SUCCESS for another activation is never applied.
        tool_call("t3"), tool_result("t3", success("a3", "elsewhere")),
        # NO_CHANGE and ERROR keep the active bundle.
        tool_call("t4"), tool_result("t4", json.dumps({"protocol_version": 2, "status": "NO_CHANGE"})),
        tool_call("t5"), tool_result("t5", json.dumps({"protocol_version": 2, "status": "ERROR"})),
        # A worker's activation and another server's lookalike result do not count.
        tool_call("t6", isSidechain=True), tool_result("t6", success("a6", "a2"), isSidechain=True),
        tool_call("t7", "mcp__Other__get_agent_context"), tool_result("t7", success("a7", "a2")),
        # A descriptor without all seven keys is not a complete SUCCESS.
        tool_call("t8"), tool_result("t8", json.dumps({**json.loads(success("a8", "a2")),
                                                       "persona": {"agent": "x"}})),
        # The same first result recorded again does not replace the switch.
        tool_result("t1", success("a1", instruction=full)),
    )
    bundle = readiness.active_bundle(path, far())
    assert bundle["persona"]["activation_id"] == "a2" and bundle["tool"] == "route_and_load"
    assert bundle["persona"]["agent"] == "code_reviewer"
    # The later pointer is replaced by the full instruction of the same revision.
    assert bundle["instruction"] == full


def test_active_bundle_applies_a_null_replacement_after_an_activation(tmp_path):
    """A re-route or a flow persona loaded without current_persona replaces the active bundle.

    Requiring an exact match here restored a persona the model had already left in 13 of the
    170 local sessions that logged an activation."""
    path = write_transcript(
        tmp_path,
        tool_call("t1"), tool_result("t1", success("a1")),
        tool_call("t2", "mcp__Agents-Core__run_flow"),
        tool_result("t2", json.dumps({"persona_activation": json.loads(success("a2", agent="code_reviewer"))})),
    )
    bundle = readiness.active_bundle(path, far())
    assert bundle["persona"]["activation_id"] == "a2" and bundle["persona"]["agent"] == "code_reviewer"


def test_active_bundle_reads_flow_activations_and_unwrapped_content(tmp_path):
    flow = json.dumps({"status": "needs_execution", "persona_activation": json.loads(success("f1"))})
    path = write_transcript(
        tmp_path,
        tool_call("t1", "mcp__plugin_agents-core_Agents-Core__run_flow"),
        tool_result("t1", flow, wrap=False, as_list=True),
        tool_call("t2", "mcp__Agents-Core-Desktop__refresh_persona_context"),
        tool_result("t2", success("f2", "f1", instruction="Only a pointer."), as_list=True),
    )
    bundle = readiness.active_bundle(path, far())
    assert bundle["persona"]["activation_id"] == "f2" and bundle["tool"] == "refresh_persona_context"
    # No full instruction of this revision was ever sent, so the pointer is kept.
    assert bundle["instruction"] == "Only a pointer."


def test_active_bundle_refuses_an_uncertain_state(tmp_path):
    preview = "<persisted-output>\nOutput too large (52KB). Full output saved to: /x/tool-results/a.txt\n"
    entries = [tool_call("t1"), tool_result("t1", success("a1")),
               tool_call("t2"), tool_result("t2", preview, wrap=False)]
    assert readiness.active_bundle(write_transcript(tmp_path, *entries), far()) is None
    # A later SUCCESS ends the uncertainty.
    later = [tool_call("t3"), tool_result("t3", success("a3", "unknown-to-the-hook"))]
    bundle = readiness.active_bundle(write_transcript(tmp_path, *entries, *later), far())
    assert bundle["persona"]["activation_id"] == "a3"
    # An ordinary error text from a persona tool keeps the active bundle.
    error = [tool_call("t4"), tool_result("t4", "Error: MCP server unavailable", wrap=False)]
    bundle = readiness.active_bundle(write_transcript(tmp_path, *entries, *later, *error), far())
    assert bundle["persona"]["activation_id"] == "a3"
    assert readiness.active_bundle(str(tmp_path / "missing.jsonl"), far()) is None
    assert readiness.active_bundle(write_transcript(tmp_path, *later), time.monotonic() - 1) is None
    assert readiness.active_bundle(write_transcript(tmp_path, tool_call("t9")), far()) is None


def reassemble(parts):
    """Strip each part's header; part headers end at their first blank line."""
    return "".join(part.split("\n\n", 1)[1] for part in parts)


def test_restore_parts_fit_the_inline_limit_and_reassemble(tmp_path):
    # Astral emoji take two UTF-16 code units; one line is longer than a whole part.
    block = "\n".join(f"- 🔄 line {index} " + "word " * 30 for index in range(150)) + "\n" + "x " * 6000
    path = write_transcript(tmp_path, tool_call("t1"), tool_result("t1", success("a1", persona_block=block,
                                                                                 instruction="Rules. " * 1500)))
    bundle = readiness.active_bundle(path, far())
    parts = readiness.restore_parts(bundle)
    total = len(parts)
    assert 1 < total <= readiness.RESTORE_PARTS
    assert all(readiness.code_units(part) <= readiness.PART_LIMIT for part in parts)
    assert parts[0].startswith("Agents-Core: this conversation was just compacted")
    assert f"This is part 1 of {total};" in parts[0] and 'persona_action="keep"' in parts[0]
    assert all(part.startswith(f"Agents-Core restored persona, part {index} of {total} (activation a1)")
               for index, part in enumerate(parts[1:], start=2))
    text = reassemble(parts)
    assert text.startswith("Descriptor (JSON): " + json.dumps(bundle["persona"], ensure_ascii=False))
    assert "Footer: " + bundle["footer"] in text and bundle["instruction"] in text
    for name in readiness.BLOCKS:
        assert f"=== {name} ===\n{bundle[name]}" in text
    assert text.endswith("=== End of the restored persona ===\n")
    # A bundle that would need more parts than the group runs is not restored by the hook.
    huge = write_transcript(tmp_path, tool_call("t1"), tool_result("t1", success("a1", persona_block="y\n" * 80000)))
    assert readiness.restore_parts(readiness.active_bundle(huge, far())) is None


def test_compact_output_returns_each_part_and_falls_back(tmp_path):
    path = write_transcript(tmp_path, tool_call("t1"), tool_result("t1", success("a1")))
    payload = {"source": "compact", "transcript_path": path}
    first = readiness.hook_output(payload)
    context = first["hookSpecificOutput"]["additionalContext"]
    assert "This is part 1 of 1;" in context and "=== End of the restored persona ===" in context
    assert "You are the engineer." in context
    assert readiness.hook_output(payload, part=2) == {}
    # Nothing to restore: part 1 asks for the reload, further parts stay silent.
    empty = {"source": "compact", "transcript_path": str(tmp_path / "missing.jsonl")}
    assert readiness.hook_output(empty)["hookSpecificOutput"]["additionalContext"] == readiness.restore_context(empty)
    assert readiness.hook_output(empty, part=2) == {}
    assert readiness.hook_output({"source": "startup", "cwd": str(tmp_path)}, part=2) == {}


def test_compact_cli_returns_a_restore_part(tmp_path):
    block = "\n".join("line " + "word " * 40 for _ in range(100))
    path = write_transcript(tmp_path, tool_call("t1"), tool_result("t1", success("a1", persona_block=block)))
    command = [sys.executable, str(ROOT / "scripts/session_readiness.py")]
    payload = json.dumps({"source": "compact", "transcript_path": path})
    second = subprocess.run(command + ["--restore-part", "2"], input=payload, capture_output=True,
                            text=True, check=True)
    context = json.loads(second.stdout)["hookSpecificOutput"]["additionalContext"]
    assert context.startswith("Agents-Core restored persona, part 2 of ")
    last = subprocess.run(command + ["--restore-part", str(readiness.RESTORE_PARTS)], input=payload,
                          capture_output=True, text=True, check=True)
    assert json.loads(last.stdout) == {}
    for invalid in ("1", str(readiness.RESTORE_PARTS + 1), "x"):
        result = subprocess.run(command + ["--restore-part", invalid], input=payload, capture_output=True, text=True)
        assert result.returncode == 2 and result.stdout == ""


def test_utf8_hook_input_survives_ansi_python_stream_encoding(tmp_path):
    directory = tmp_path / "José-проект"
    directory.mkdir()
    command = [sys.executable, str(ROOT / "scripts/session_readiness.py")]
    environment = {**os.environ, "PYTHONIOENCODING": "cp1252"}
    payload = json.dumps({"cwd": str(directory), "source": "clear"}, ensure_ascii=False).encode("utf-8")
    completed = subprocess.run(command, input=payload, capture_output=True, env=environment, check=True)
    document = json.loads(completed.stdout.decode("ascii"))
    assert "CWD: " + str(directory) in document["systemMessage"]
    invalid = subprocess.run(command, input=b'\xff', capture_output=True, env=environment)
    assert invalid.returncode == 1 and invalid.stdout == b""
    assert b"invalid input" in invalid.stderr


@pytest.mark.parametrize("client", ["claude", "codex"])
def test_install_preserves_settings_backs_up_and_is_idempotent(tmp_path, monkeypatch, client):
    profile = tmp_path / "profile with spaces"
    profile.mkdir()
    monkeypatch.setattr(installer, "client_home", lambda _: profile)
    target = profile / ("settings.json" if client == "claude" else "hooks.json")
    original = {"statusLine": {"command": "existing"}, "permissions": {"allow": ["Bash"]},
                "hooks": {"SessionStart": [{"matcher": "resume", "hooks": [{"command": "other"}]}],
                          "Stop": [{"hooks": [{"command": "unrelated"}]}]}}
    raw = json.dumps(original).encode()
    target.write_bytes(raw)
    assert installer.install(client, dry_run=True)["changed"]
    assert target.read_bytes() == raw and not (profile / "hooks").exists()
    result = installer.install(client)
    assert Path(result["backup"]).read_bytes() == raw
    document = json.loads(target.read_text())
    for key in ("statusLine", "permissions"):
        assert document[key] == original[key]
    assert document["hooks"]["Stop"] == original["hooks"]["Stop"]
    assert document["hooks"]["SessionStart"][0] == original["hooks"]["SessionStart"][0]
    handler = document["hooks"]["SessionStart"][1]["hooks"][0]
    assert shlex.split(handler["command"])[1] == result["script"]
    assert document["hooks"]["SessionStart"][1]["matcher"] == installer.MATCHER
    compact = document["hooks"]["SessionStart"][2:]
    assert compact == [{"matcher": installer.COMPACT_MATCHER,
                        "hooks": installer.restore_handlers(handler, Path(result["script"]), client)}]
    handlers = compact[0]["hooks"]
    assert handlers[0] == handler
    if client == "claude":  # one handler per part of a restored bundle
        assert len(handlers) == readiness.RESTORE_PARTS
        assert [shlex.split(item["command"])[2:] for item in handlers[1:]] == [
            ["--restore-part", str(part)] for part in range(2, readiness.RESTORE_PARTS + 1)]
        assert all(item["timeout"] == 5 and installer.owns_handler(item, Path(result["script"]))
                   for item in handlers)
    else:  # Codex keeps the single reload request
        assert handlers == [handler]
    before = target.read_bytes(), target.stat().st_mtime_ns
    assert not installer.install(client)["changed"]
    assert (target.read_bytes(), target.stat().st_mtime_ns) == before


@pytest.mark.parametrize("raw", ["invalid-json", "[]", '{"hooks": []}', '{"hooks":{"SessionStart":[1]}}'])
def test_invalid_config_remains_untouched(tmp_path, monkeypatch, raw):
    monkeypatch.setattr(installer, "client_home", lambda _: tmp_path)
    target = tmp_path / "settings.json"
    target.write_text(raw)
    with pytest.raises(ValueError):
        installer.install("claude")
    assert target.read_text() == raw and not (tmp_path / "hooks").exists()


def test_install_adds_the_compact_group_beside_an_existing_startup_group(tmp_path, monkeypatch):
    """An installation from before the compact group keeps its startup/clear group as it was."""
    monkeypatch.setattr(installer, "client_home", lambda _: tmp_path)
    target, script = tmp_path / "settings.json", tmp_path / "hooks" / installer.SCRIPT_NAME
    startup = {"matcher": installer.MATCHER,
               "hooks": [{"type": "command", "command": installer.command_for(script), "timeout": 5}]}
    target.write_text(json.dumps({"hooks": {"SessionStart": [startup]}}))
    assert installer.install("claude")["changed"]
    groups = json.loads(target.read_text())["hooks"]["SessionStart"]
    restore = installer.restore_handlers(startup["hooks"][0], script, "claude")
    assert groups == [startup, {"matcher": installer.COMPACT_MATCHER, "hooks": restore}]
    assert not installer.install("claude")["changed"]


def test_install_replaces_a_single_handler_compact_group_with_the_parts(tmp_path, monkeypatch):
    """An installation from before the parts gets them; the user's other groups stay."""
    monkeypatch.setattr(installer, "client_home", lambda _: tmp_path)
    target, script = tmp_path / "settings.json", tmp_path / "hooks" / installer.SCRIPT_NAME
    handler = {"type": "command", "command": installer.command_for(script), "timeout": 5}
    other = {"matcher": "resume", "hooks": [{"type": "command", "command": "other"}]}
    target.write_text(json.dumps({"hooks": {"SessionStart": [
        {"matcher": installer.MATCHER, "hooks": [handler]}, other,
        {"matcher": installer.COMPACT_MATCHER, "hooks": [handler]}]}}))
    assert installer.install("claude")["changed"]
    groups = json.loads(target.read_text())["hooks"]["SessionStart"]
    assert groups[1] == other
    assert groups[2]["hooks"] == installer.restore_handlers(handler, script, "claude")
    assert len(groups) == 3


def test_only_dedicated_hook_group_is_owned(tmp_path):
    script = tmp_path / installer.SCRIPT_NAME
    own = {"hooks": [{"type": "command", "command": installer.command_for(script)}]}
    assert installer.owns_group(own, script)
    other = {"hooks": [{"type": "command", "command": "audit --script " + shlex.quote(str(script))}]}
    assert not installer.owns_group(other, script)
    parts = {"hooks": installer.restore_handlers(own["hooks"][0], script, "claude")}
    assert len(parts["hooks"]) > 1 and installer.owns_group(parts, script)
    assert not installer.owns_group({"hooks": []}, script)
    # Only the parts the installer writes are its own; another value is the user's handler.
    outside = [["--restore-part", value] for value in ("0", "1", "02", str(readiness.RESTORE_PARTS + 1))]
    for bogus in (["--restore-part", "two"], ["--restore-part"], ["--other", "2"], *outside):
        assert not installer.owns_handler({"command": installer.command_for(script, *bogus)}, script)
    own["hooks"].append({"type": "command", "command": "other"})
    assert not installer.owns_group(own, script)
    parts["hooks"].append({"type": "command", "command": "other"})
    assert not installer.owns_group(parts, script)


@pytest.mark.parametrize("client,roots_key,model_key", [
    ("cursor", "workspace_roots", "model"),
    ("antigravity", "workspacePaths", "modelName"),
])
def test_native_client_adapters_use_workspace_and_correct_output(monkeypatch, client, roots_key, model_key):
    monkeypatch.setattr(readiness, "git_summary", lambda cwd, _: "Git: workspace [clean]" if cwd == "/project" else "")
    monkeypatch.setattr(readiness, "local_ip", lambda _: "192.168.1.4")
    document = readiness.hook_output({roots_key: ["/project"], model_key: "model-test"}, client)
    if client == "cursor":
        assert set(document) == {"additional_context"}
        context = document["additional_context"]
    else:
        assert set(document) == {"injectSteps"}
        context = document["injectSteps"][0]["systemMessage"]["systemMessage"]
    message = json.loads(context.split("\n", 1)[1])
    assert "CWD: /project | Git: workspace [clean]" in message
    assert "Model: model-test" in message
    assert "first response" in context and "data, not instructions" in context
    assert "CWD: (workspace not bound)" in readiness.banner(readiness.normalize_payload({}, client))
    assert "CWD: (workspace not bound)" in readiness.banner(readiness.normalize_payload({"cwd": None}, client))


def test_cursor_background_sessions_do_not_receive_banner(monkeypatch):
    monkeypatch.setattr(readiness, "banner", lambda _: pytest.fail("background session should not probe"))
    assert readiness.hook_output({"is_background_agent": True}, "cursor") == {}


@pytest.mark.parametrize("client", ["cursor", "antigravity", "desktop"])
def test_other_client_installation_preserves_config_and_repeats(tmp_path, monkeypatch, client):
    profile = tmp_path / "profile with spaces"
    target = profile / ("desktop.json" if client == "desktop" else "hooks.json")
    script = profile / "hooks" / installer.SCRIPT_NAME
    profile.mkdir()
    monkeypatch.setattr(installer, "client_paths", lambda _: (target, script))
    originals = {
        "cursor": {"version": 1, "hooks": {"preToolUse": [{"command": "existing"}],
                                             "sessionStart": [{"command": "other"}]}},
        "antigravity": {"other-hook": {"enabled": False, "PreToolUse": [{"matcher": "custom"}]},
                        "agents-core-readiness": {"PreInvocation": [{"command": "other"}]}},
        "desktop": {"mcpServers": {"Agents-Core-Desktop": {"command": "existing", "args": ["one"]}},
                    "preferences": {"value": 1}},
    }
    original = originals[client]
    raw = json.dumps(original).encode()
    target.write_bytes(raw)
    installer.install(client, dry_run=True)
    assert target.read_bytes() == raw and not script.parent.exists()
    result = installer.install(client)
    assert Path(result["backup"]).read_bytes() == raw
    document = json.loads(target.read_text())
    if client == "cursor":
        assert document["hooks"]["preToolUse"] == original["hooks"]["preToolUse"]
        assert document["hooks"]["sessionStart"][0] == original["hooks"]["sessionStart"][0]
        assert shlex.split(document["hooks"]["sessionStart"][1]["command"])[-2:] == ["--client", "cursor"]
    elif client == "antigravity":
        assert document["other-hook"] == original["other-hook"]
        assert document["agents-core-readiness"]["PreInvocation"] == original["agents-core-readiness"]["PreInvocation"]
        assert "hooks" not in document["agents-core-readiness"]["SessionStart"][0]
    else:
        assert document["mcpServers"]["Agents-Core-Desktop"] == original["mcpServers"]["Agents-Core-Desktop"]
        assert document["preferences"] == original["preferences"]
        assert result["delivery"] == "mcp-first-response"
        assert len(result["scripts"]) == 2
    before = target.read_bytes(), target.stat().st_mtime_ns
    assert not installer.install(client)["changed"]
    assert (target.read_bytes(), target.stat().st_mtime_ns) == before


def test_all_clients_selection_and_unsupported_cursor_version(tmp_path, monkeypatch):
    assert installer.parse_clients("all") == installer.CLIENTS
    assert installer.parse_clients("desktop,cursor,desktop") == ("desktop", "cursor")
    target, script = tmp_path / "hooks.json", tmp_path / "hooks" / installer.SCRIPT_NAME
    monkeypatch.setattr(installer, "client_paths", lambda _: (target, script))
    original = '{"version":2,"hooks":{}}'
    target.write_text(original)
    with pytest.raises(ValueError, match="version 1"):
        installer.install("cursor")
    assert target.read_text() == original and not script.parent.exists()


def test_desktop_does_not_overwrite_unrelated_server(tmp_path, monkeypatch):
    target, script = tmp_path / "desktop.json", tmp_path / "hooks" / installer.SCRIPT_NAME
    monkeypatch.setattr(installer, "client_paths", lambda _: (target, script))
    raw = '{"mcpServers":{"Agents-Readiness":{"command":"unrelated","args":["different"]}}}'
    target.write_text(raw)
    with pytest.raises(ValueError, match="unrelated MCP server"):
        installer.install("desktop")
    assert target.read_text() == raw and not script.parent.exists()


def test_antigravity_does_not_enable_disabled_unrelated_handlers(tmp_path, monkeypatch):
    target, script = tmp_path / "hooks.json", tmp_path / "hooks" / installer.SCRIPT_NAME
    monkeypatch.setattr(installer, "client_paths", lambda _: (target, script))
    raw = '{"agents-core-readiness":{"enabled":false,"PreInvocation":[{"command":"unrelated"}]}}'
    target.write_text(raw)
    with pytest.raises(ValueError, match="left disabled"):
        installer.install("antigravity")
    assert target.read_text() == raw and not script.parent.exists()


@pytest.mark.parametrize("prefix", ["C:/Users/alex", "C:/Users/Alex User", "C:/Users/O'Brien $user %x%!`"])
def test_windows_commands_keep_paths_and_stdin_safe(monkeypatch, prefix):
    monkeypatch.setattr(installer, "WINDOWS_HOOKS", True)
    executable = str(PureWindowsPath(prefix) / "Python" / "python.exe")
    script = PureWindowsPath(prefix) / ".claude" / "hooks" / installer.SCRIPT_NAME
    monkeypatch.setattr(installer.sys, "executable", executable)
    command = installer.command_for(script)
    parts = shlex.split(command)
    assert parts[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand"]
    decoded = base64.b64decode(parts[4]).decode("utf-16-le")
    assert decoded == installer.powershell_payload([executable, str(script)])
    assert "[Console]::In.ReadToEnd()" in decoded and "exit $LASTEXITCODE" in decoded
    assert installer.owns_handler({"command": command}, script)
    cursor_command = installer.command_for(script, "--client", "cursor")
    assert installer.owns_handler({"command": cursor_command}, script)
    part_command = installer.command_for(script, "--restore-part", "2")
    assert installer.owns_handler({"command": part_command}, script)
    if sys.platform != "win32":
        parsed = subprocess.run(["bash", "-c", "set -- " + command + "; printf '%s\\n' \"$@\""],
                                capture_output=True, text=True, check=True).stdout.splitlines()
        assert parsed == parts


@pytest.fixture
def mcp_helper(monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    return load_script("session_readiness_mcp")


@pytest.mark.asyncio
async def test_mcp_fallback_without_workspace_never_uses_process_directory(mcp_helper, monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(mcp_helper, "banner", lambda payload: "CWD: " + payload["cwd"])
    session = SimpleNamespace(check_client_capability=lambda _: False)
    result = await mcp_helper.get_session_readiness(SimpleNamespace(session=session))
    assert result["workspace"] is None and result["workspace_source"] == "unbound"
    assert result["message"] == "CWD: (workspace not bound)"
    assert "first-response fallback" in mcp_helper.INSTRUCTIONS


@pytest.mark.asyncio
async def test_mcp_fallback_resolves_explicit_workspace_and_client_roots(mcp_helper, tmp_path):
    async def roots():
        return SimpleNamespace(roots=[SimpleNamespace(uri=tmp_path.as_uri())])
    session = SimpleNamespace(check_client_capability=lambda _: True, list_roots=roots)
    ctx = SimpleNamespace(session=session)
    assert await mcp_helper.resolve_workspace(ctx, str(tmp_path)) == (str(tmp_path), "explicit")
    assert await mcp_helper.resolve_workspace(ctx, None) == (str(tmp_path), "client_root")
    with pytest.raises(ValueError, match="existing absolute"):
        await mcp_helper.resolve_workspace(ctx, "relative-directory")


@pytest.mark.asyncio
async def test_mcp_stdio_handshake_and_read_only_tool(tmp_path):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    parameters = StdioServerParameters(command=sys.executable,
                                       args=[str(ROOT / "scripts/session_readiness_mcp.py")])
    async with stdio_client(parameters) as (reader, writer):
        async with ClientSession(reader, writer) as session:
            initialized = await session.initialize()
            assert "first-response fallback" in initialized.instructions
            listing = await session.list_tools()
            assert [tool.name for tool in listing.tools] == ["get_session_readiness"]
            assert listing.tools[0].annotations.readOnlyHint is True
            result = await session.call_tool("get_session_readiness", {"workspace": str(tmp_path)})
            assert not result.isError
            document = result.structuredContent or json.loads(result.content[0].text)
            assert document["workspace"] == str(tmp_path)
            assert "Ready |" in document["message"]
            unbound = await session.call_tool("get_session_readiness", {})
            unbound_document = unbound.structuredContent or json.loads(unbound.content[0].text)
            assert unbound_document["workspace"] is None


def test_install_preserves_prior_backups_during_repeated_updates(tmp_path, monkeypatch):
    from datetime import datetime
    # Force the old timestamp naming scheme to collide without waiting for a clock tick.
    monkeypatch.setattr(installer, "datetime", SimpleNamespace(now=lambda: datetime(2026, 10, 14, 12)), raising=False)
    monkeypatch.setattr(installer, "client_home", lambda _: tmp_path)
    target = tmp_path / "settings.json"
    prior = tmp_path / "settings.json.readiness-backup-20261014-120000-000000"
    prior.write_bytes(b"older settings snapshot")
    target.write_bytes(b"{}")
    first = installer.install("claude")
    assert prior.read_bytes() == b"older settings snapshot"
    first_backup = Path(first["backup"])
    assert first_backup.read_bytes() == b"{}"
    # Simulate a user editing settings between installations.
    updated = json.loads(target.read_text())
    updated["theme"] = "light"
    updated["hooks"]["SessionStart"] = []
    raw = json.dumps(updated).encode()
    target.write_bytes(raw)
    second = installer.install("claude")
    assert Path(second["backup"]) != first_backup
    assert first_backup.read_bytes() == b"{}"
    assert Path(second["backup"]).read_bytes() == raw
    assert prior.read_bytes() == b"older settings snapshot"


def test_backup_failure_prevents_runtime_and_config_changes(tmp_path, monkeypatch):
    monkeypatch.setattr(installer, "client_home", lambda _: tmp_path)
    target = tmp_path / "settings.json"
    target.write_bytes(b"{}")
    script = tmp_path / "hooks" / installer.SCRIPT_NAME
    script.parent.mkdir()
    script.write_bytes(b"previous runtime")
    def fail_backup(_):
        raise OSError("synthetic backup failure")
    monkeypatch.setattr(installer, "backup_file", fail_backup, raising=False)
    with pytest.raises(OSError, match="synthetic backup failure"):
        installer.install("claude")
    assert target.read_bytes() == b"{}"
    assert script.read_bytes() == b"previous runtime"


def test_git_probe_ignores_inherited_repository_locations(repository, tmp_path, monkeypatch):
    other = tmp_path.with_name(tmp_path.name + "-foreign")
    other.mkdir()
    git(other, "init", "--initial-branch=foreign")
    git(other, "config", "user.name", "Test")
    git(other, "config", "user.email", "test@example.invalid")
    git(other, "-c", "commit.gpgsign=false", "commit", "--allow-empty", "-m", "foreign")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))
    assert readiness.git_summary(str(repository), time.monotonic() + 3) == "Git: main [clean]"


def test_git_probe_environment_preserves_unrelated_variables(monkeypatch):
    variables = ("GIT_DIR", "GIT_WORK_TREE", "GIT_COMMON_DIR", "GIT_INDEX_FILE",
                 "GIT_OBJECT_DIRECTORY", "GIT_ALTERNATE_OBJECT_DIRECTORIES", "GIT_PREFIX")
    for variable in variables:
        monkeypatch.setenv(variable, "foreign")
    monkeypatch.setenv("READINESS_REVIEW_MARKER", "preserved")
    monkeypatch.setattr(readiness.shutil, "which", lambda _: "/git")
    seen = {}
    def run(arguments, **kwargs):
        seen.update(kwargs["env"])
        return subprocess.CompletedProcess(arguments, 0, "# branch.head main", "")
    monkeypatch.setattr(readiness.subprocess, "run", run)
    assert readiness.git_summary("/workspace", time.monotonic() + 3) == "Git: main [clean]"
    assert all(variable not in seen for variable in variables)
    assert seen["READINESS_REVIEW_MARKER"] == "preserved"


def test_unbound_banner_skips_git_and_preserves_relative_workspaces(monkeypatch):
    calls = []
    monkeypatch.setattr(readiness, "local_ip", lambda _: "192.168.1.4")
    def summary(cwd, deadline):
        calls.append(cwd)
        return "Git: chosen [clean]"
    monkeypatch.setattr(readiness, "git_summary", summary)
    unbound = readiness.banner({"cwd": "(workspace not bound)"})
    assert not calls and "Git:" not in unbound
    assert "CWD: (workspace not bound)" in unbound
    relative = readiness.banner({"cwd": "."})
    assert calls == ["."] and "Git: chosen [clean]" in relative
