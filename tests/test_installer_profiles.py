"""Run the installers' real MCP setup hooks against disposable client profiles."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


def between(source, start, end):
    first = source.index(start)
    return source[first:source.index(end, first)]


@pytest.fixture(params=["bash", "cmd"], ids=["unix", "windows"])
def profile_installer(request, tmp_path):
    kind = request.param
    if kind == "cmd" and sys.platform != "win32":
        pytest.skip("requires native cmd.exe")
    if kind == "bash" and sys.platform == "win32":
        pytest.skip("Unix hook runs on Unix; Windows exercises native cmd.exe")
    interpreter = os.environ.get("COMSPEC") if kind == "cmd" else shutil.which("bash")
    if not interpreter:
        pytest.skip(f"{kind} is unavailable")

    checkout = tmp_path / "installation with spaces"
    helpers, templates = checkout / "scripts/_helpers", checkout / "scripts/templates"
    helpers.mkdir(parents=True)
    (templates / "legacy").mkdir(parents=True)
    (checkout / "src").mkdir()
    for name in ("__init__.py", "client_paths.py"):
        shutil.copyfile(ROOT / "src" / name, checkout / "src" / name)
    for name in ("inject_mcp.py", "inject_claude_md.py", "migrate_routing_memory.py", "client_config_paths.py",
                 "deny_desktop_mcp.py"):
        shutil.copyfile(ROOT / "scripts/_helpers" / name, helpers / name)
    for name in ("routing-protocol-core.md", "memory-routing.md",
                 "legacy/memory-routing-v1.md", "legacy/memory-routing-v2.md",
                 "legacy/memory-routing-v3.md", "legacy/memory-routing-v4.md",
                 "legacy/memory-routing-v5.md", "legacy/memory-routing-v6.md"):
        shutil.copyfile(ROOT / "scripts/templates" / name, templates / name)
    home = tmp_path / "isolated home"
    home.mkdir()
    cwd = tmp_path / "unrelated working directory"
    cwd.mkdir()
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home),
               APPDATA=str(home / "AppData/Roaming"), LOCALAPPDATA=str(home / "AppData/Local"),
               REPO_ROOT=str(checkout),
               PYTHON_ABS=sys.executable, SERVER_ABS=str(checkout / "src/server.py"),
               HELPERS=str(helpers), ROUTING_TEMPLATE=str(templates / "routing-protocol-core.md"),
               IS_NIXOS="false", NIX_LD_LIB_PATH="", PYTHONUTF8="1",
               RED="", GREEN="", YELLOW="", BLUE="", CYAN="", NC="")
    for name in ("CLAUDE_CONFIG_DIR", "AGENTS_CURSOR_MCP_CONFIG", "AGENTS_CLAUDE_DESKTOP_CONFIG",
                 "AGENTS_ANTIGRAVITY_MCP_CONFIG",
                 "XDG_CONFIG_HOME", "PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"):
        env.pop(name, None)

    if kind == "bash":
        source = (ROOT / "scripts/init_repo.sh").read_text()
        functions = between(source, "print_header() {", "# Fatal error handler")
        hooks = between(source, "# Resolve client paths", "# ============== Pre-flight Checks")
        setup = between(source, "    # --- Detect Cursor ---", "    # --- Configure Codex instructions ---")
        script = checkout / "profiles.sh"
        script.write_text("set -e\nCONFIGURED_ENVS=()\n"
                          'python() { "$PYTHON_ABS" "$@"; }\n'
                          "check_command() { return 1; }\n" + functions + hooks + setup)
        command = [interpreter, str(script)]
    else:
        source = (ROOT / "scripts/init_repo.bat").read_text()
        setup = between(source, "REM Resolve the same effective client paths", "REM --- Configure Codex instructions ---")
        fatal_handler = source[source.index("REM ============== Fatal Error Handler =============="):]
        script = checkout / "profiles.bat"
        script.write_bytes(("@echo off\nsetlocal enabledelayedexpansion\n" + setup
                            + "exit /b 0\n" + fatal_handler).replace("\n", "\r\n").encode())
        command = [interpreter, "/d", "/c", str(script)]

    def run(overrides):
        return subprocess.run(command, cwd=cwd, env={**env, **overrides}, input="y\ny\n",
                              text=True, encoding="utf-8", errors="replace", capture_output=True, timeout=20)

    desktop = home / ("AppData/Roaming/Claude/claude_desktop_config.json" if kind == "cmd" else
                      "Library/Application Support/Claude/claude_desktop_config.json" if sys.platform == "darwin" else
                      ".config/Claude/claude_desktop_config.json")
    return run, home, checkout, templates, desktop


@pytest.mark.parametrize("profile_kind", ["defaults", "custom-profiles", "single-exclamation", "paired-exclamations"])
def test_installer_configures_effective_paths_and_preserves_other_profiles(profile_installer, profile_kind):
    run, home, checkout, templates, desktop = profile_installer
    defaults = {"claude": home / ".claude.json", "cursor": home / ".cursor/mcp.json", "desktop": desktop}
    paths = defaults
    claude_home = home / ".claude"
    overrides = {}
    custom = profile_kind != "defaults"
    if custom:
        suffix = {"single-exclamation": "profile!literal",
                  "paired-exclamations": "profile !segment!"}.get(profile_kind, "profile")
        claude_home = home / f"alternate claude {suffix}"
        paths = {"claude": claude_home / ".claude.json",
                 "cursor": home / f"custom cursor {suffix}/registration.json",
                 "desktop": home / f"custom desktop {suffix}/registration.json"}
        overrides = {"CLAUDE_CONFIG_DIR": str(claude_home),
                     "AGENTS_CURSOR_MCP_CONFIG": str(paths["cursor"]),
                     "AGENTS_CLAUDE_DESKTOP_CONFIG": str(paths["desktop"]),
                     # Make accidental cmd expansion change the path visibly.
                     "segment": "expanded-value"}

    original = {"mcpServers": {"other": {"command": "unrelated"},
                               "Agents-Core": {"command": "old-python", "disabled": True}},
                "projects": {"unrelated-project": {"otherPolicy": True}}}
    for path in {*defaults.values(), *paths.values()}:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(original))
    default_instructions = home / ".claude/CLAUDE.md"
    default_instructions.parent.mkdir(exist_ok=True)
    default_instructions.write_bytes(b"Default personal instructions\r\n")
    target = claude_home / "CLAUDE.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"Selected personal instructions\r\n")
    reminder = claude_home / "memory/feedback_agents_core_routing.md"
    reminder.parent.mkdir()
    reminder.write_bytes((templates / "legacy" / "memory-routing-v1.md").read_bytes())
    preserved = {path: path.read_bytes() for path in [*defaults.values(), default_instructions]} if custom else {}

    result = run(overrides)

    assert result.returncode == 0, result.stdout + result.stderr
    for client, path in paths.items():
        document = json.loads(path.read_text())
        # The desktop app's entry has its own name, taking over the old one (#231).
        name = "Agents-Core-Desktop" if client == "desktop" else "Agents-Core"
        entry = document["mcpServers"][name]
        assert entry["command"] == sys.executable
        assert entry["args"] == [str(checkout / "src/server.py")]
        assert entry["disabled"] is True
        assert set(document["mcpServers"]) == {name, "other"}
        assert document["mcpServers"]["other"] == original["mcpServers"]["other"]
        assert document["projects"] == original["projects"]
    # ...and Claude Code, the app's Code tab included, denies it.
    assert set(json.loads((claude_home / "settings.json").read_text())["permissions"]["deny"]) == {
        "mcp__Agents-Core-Desktop__*", "mcp__Agents_Core_Desktop__*"}
    assert target.read_bytes().startswith(b"Selected personal instructions\r\n")
    assert (templates / "routing-protocol-core.md").read_bytes().strip() in target.read_bytes()
    assert reminder.read_bytes() == (templates / "memory-routing.md").read_bytes()
    assert {path: path.read_bytes() for path in preserved} == preserved


def test_explicit_paths_create_missing_parent_directories(profile_installer):
    run, home, checkout, _, _ = profile_installer
    claude_home = home / "new claude profile"
    cursor = home / "new cursor profile/mcp.json"
    desktop = home / "new desktop profile/mcp.json"

    result = run({"CLAUDE_CONFIG_DIR": str(claude_home), "AGENTS_CURSOR_MCP_CONFIG": str(cursor),
                  "AGENTS_CLAUDE_DESKTOP_CONFIG": str(desktop)})

    assert result.returncode == 0, result.stdout + result.stderr
    for path, name in ((claude_home / ".claude.json", "Agents-Core"), (cursor, "Agents-Core"),
                       (desktop, "Agents-Core-Desktop")):
        assert json.loads(path.read_text())["mcpServers"][name]["args"] == [str(checkout / "src/server.py")]
    assert not (home / ".claude.json").exists()
    assert not (home / ".claude").exists()
    assert not (home / ".cursor").exists()


def test_a_desktop_only_installation_configures_claude_code_for_the_code_tab(profile_installer):
    """The desktop app's Code tab runs Claude Code with the default profile (#231)."""
    run, home, checkout, _, desktop = profile_installer
    desktop.parent.mkdir(parents=True)

    result = run({})

    assert result.returncode == 0, result.stdout + result.stderr
    assert set(json.loads(desktop.read_text())["mcpServers"]) == {"Agents-Core-Desktop"}
    registry = json.loads((home / ".claude.json").read_text())["mcpServers"]
    assert registry["Agents-Core"]["args"] == [str(checkout / "src/server.py")]
    assert set(json.loads((home / ".claude/settings.json").read_text())["permissions"]["deny"]) == {
        "mcp__Agents-Core-Desktop__*", "mcp__Agents_Core_Desktop__*"}


@pytest.mark.parametrize("profile_installer", ["cmd"], indirect=True)
@pytest.mark.parametrize("missing", ["helper", "MCP_SETTINGS_FILE", "CLAUDE_DESKTOP_CONFIG",
                                     "CLAUDE_CODE_DIR", "CLAUDE_CODE_MCP", "CLAUDE_CODE_SETTINGS"])
def test_windows_path_resolution_failure_reports_context_before_writes(profile_installer, missing):
    run, home, checkout, _, desktop = profile_installer
    helper = checkout / "scripts/_helpers/client_config_paths.py"
    if missing == "helper":
        helper.unlink()
        expected_key = "MCP_SETTINGS_FILE"
    else:
        paths = {"MCP_SETTINGS_FILE": home / ".cursor/mcp.json",
                 "CLAUDE_DESKTOP_CONFIG": desktop,
                 "CLAUDE_CODE_DIR": home / ".claude",
                 "CLAUDE_CODE_MCP": home / ".claude.json",
                 "CLAUDE_CODE_SETTINGS": home / ".claude/settings.json"}
        helper.write_text("\n".join(f"print({str(key + '=' + str(path))!r})"
                                    for key, path in paths.items() if key != missing) + "\n",
                          encoding="utf-8")
        expected_key = missing

    result = run({})

    assert result.returncode == 1, result.stdout + result.stderr
    assert "FATAL: init_repo.bat aborted unexpectedly" in result.stdout
    assert "Exit code : 1" in result.stdout
    assert f"Context   : Failed to resolve {expected_key}" in result.stdout
    assert "!_FATAL_CTX!" not in result.stdout and "!_FATAL_EC!" not in result.stdout
    assert not list(home.iterdir())
