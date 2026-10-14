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


def test_only_dedicated_hook_group_is_owned(tmp_path):
    script = tmp_path / installer.SCRIPT_NAME
    own = {"hooks": [{"type": "command", "command": installer.command_for(script)}]}
    assert installer.owns_group(own, script)
    other = {"hooks": [{"type": "command", "command": "audit --script " + shlex.quote(str(script))}]}
    assert not installer.owns_group(other, script)
    own["hooks"].append({"type": "command", "command": "other"})
    assert not installer.owns_group(own, script)


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
