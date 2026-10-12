"""Run the installers' real MCP setup hooks against disposable client profiles."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
BACKUP_TIMESTAMP = "1700000000"


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
    # The pre-fix collision regression must run before this helper exists.
    backup_helper = ROOT / "scripts/_helpers/backup_file.py"
    if backup_helper.exists():
        shutil.copyfile(backup_helper, helpers / backup_helper.name)
    for name in ("routing-protocol-core.md", "memory-routing.md",
                 "legacy/memory-routing-v1.md", "legacy/memory-routing-v2.md",
                 "legacy/memory-routing-v3.md", "legacy/memory-routing-v4.md",
                 "legacy/memory-routing-v5.md", "legacy/memory-routing-v6.md", "legacy/memory-routing-v7.md", "legacy/memory-routing-v8.md"):
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
    if kind == "cmd":
        # Client detection must not pick up applications installed on the test host.
        system32 = Path(os.environ["SystemRoot"]) / "System32"
        env["PATH"] = os.pathsep.join(map(str, (system32, system32 / "WindowsPowerShell/v1.0")))

    if kind == "bash":
        source = (ROOT / "scripts/init_repo.sh").read_text()
        functions = between(source, "print_header() {", "# Fatal error handler")
        hooks = between(source, "# Resolve client paths", "# ============== Pre-flight Checks")
        setup = between(source, "    # --- Detect Cursor ---", "    # --- Configure Codex instructions ---")
        script = checkout / "profiles.sh"
        script.write_text("set -e\nCONFIGURED_ENVS=()\n"
                          'python() { "$PYTHON_ABS" "$@"; }\n'
                          f'date() {{ printf "%s\\n" "{BACKUP_TIMESTAMP}"; }}\n'
                          "check_command() { return 1; }\n" + functions + hooks + setup
                          + 'printf \'CONFIGURED_ENVS=%s\\n\' "${CONFIGURED_ENVS[*]}"\n')
        command = [interpreter, str(script)]
    else:
        source = (ROOT / "scripts/init_repo.bat").read_text()
        setup = between(source, "REM Resolve the same effective client paths", "REM --- Configure Codex instructions ---")
        # Freeze the old timestamp computation; new setup may no longer use it.
        setup = "\n".join(
            f'set "BACKUP_TS={BACKUP_TIMESTAMP}"'
            if line.startswith("for /f %%T ") and 'do set "BACKUP_TS=' in line else line
            for line in setup.splitlines()) + "\n"
        fatal_handler = source[source.index("REM ============== Fatal Error Handler =============="):]
        script = checkout / "profiles.bat"
        script.write_bytes(("@echo off\nsetlocal enabledelayedexpansion\nset \"CONFIGURED_ENVS=\"\n" + setup
                            + "echo CONFIGURED_ENVS=!CONFIGURED_ENVS!\nexit /b 0\n"
                            + fatal_handler).replace("\n", "\r\n").encode())
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
    defaults = {"claude": home / ".claude.json", "cursor": home / ".cursor/mcp.json", "desktop": desktop,
                "antigravity": home / ".gemini/config/mcp_config.json"}
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
                 "desktop": home / f"custom desktop {suffix}/registration.json",
                 "antigravity": home / f"custom antigravity {suffix}/registration.json"}
        overrides = {"CLAUDE_CONFIG_DIR": str(claude_home),
                     "AGENTS_CURSOR_MCP_CONFIG": str(paths["cursor"]),
                     "AGENTS_CLAUDE_DESKTOP_CONFIG": str(paths["desktop"]),
                     "AGENTS_ANTIGRAVITY_MCP_CONFIG": str(paths["antigravity"]),
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
        backups = list(path.parent.glob(path.name + ".backup.*"))
        assert backups and all(json.loads(backup.read_text()) == original for backup in backups)
    # ...and Claude Code, the app's Code tab included, denies it.
    assert set(json.loads((claude_home / "settings.json").read_text())["permissions"]["deny"]) == {
        "mcp__Agents-Core-Desktop__*", "mcp__Agents_Core_Desktop__*"}
    assert target.read_bytes().startswith(b"Selected personal instructions\r\n")
    assert (templates / "routing-protocol-core.md").read_bytes().strip() in target.read_bytes()
    assert reminder.read_bytes() == (templates / "memory-routing.md").read_bytes()
    assert {path: path.read_bytes() for path in preserved} == preserved


@pytest.mark.parametrize("suffix", ["profile", "profile!literal", "profile !segment!"])
def test_explicit_paths_create_missing_parent_directories(profile_installer, suffix):
    run, home, checkout, _, _ = profile_installer
    claude_home = home / f"new claude {suffix}"
    cursor = home / f"new cursor {suffix}/mcp.json"
    desktop = home / f"new desktop {suffix}/mcp.json"
    antigravity = home / f"new antigravity {suffix}/mcp.json"

    result = run({"CLAUDE_CONFIG_DIR": str(claude_home), "AGENTS_CURSOR_MCP_CONFIG": str(cursor),
                  "AGENTS_CLAUDE_DESKTOP_CONFIG": str(desktop),
                  "AGENTS_ANTIGRAVITY_MCP_CONFIG": str(antigravity), "segment": "expanded-value"})

    assert result.returncode == 0, result.stdout + result.stderr
    for path, name in ((claude_home / ".claude.json", "Agents-Core"), (cursor, "Agents-Core"),
                       (desktop, "Agents-Core-Desktop"), (antigravity, "Agents-Core")):
        assert json.loads(path.read_text())["mcpServers"][name]["args"] == [str(checkout / "src/server.py")]
    assert not (home / ".claude.json").exists()
    assert not (home / ".claude").exists()
    assert not (home / ".cursor").exists()
    assert not (home / ".gemini").exists()


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


def test_antigravity_existing_directory_creates_config_and_reinstalls(profile_installer):
    run, home, checkout, _, _ = profile_installer
    config = home / ".gemini/config/mcp_config.json"
    config.parent.mkdir(parents=True)

    first = run({})

    assert first.returncode == 0, first.stdout + first.stderr
    expected = {"mcpServers": {"Agents-Core": {"command": sys.executable,
                                             "args": [str(checkout / "src/server.py")]}}}
    assert json.loads(config.read_text()) == expected
    configured = config.read_bytes()

    second = run({})

    assert second.returncode == 0, second.stdout + second.stderr
    assert config.read_bytes() == configured
    assert "CONFIGURED_ENVS= Antigravity" in second.stdout or "CONFIGURED_ENVS=Antigravity" in second.stdout
    assert any(backup.read_bytes() == configured for backup in config.parent.glob(config.name + ".backup.*"))


def test_absent_antigravity_is_skipped_without_creating_config(profile_installer):
    run, home, _, _, _ = profile_installer

    result = run({})

    assert result.returncode == 0, result.stdout + result.stderr
    assert "Antigravity not detected" in result.stdout
    assert "Configuring Antigravity MCP" not in result.stdout
    assert not (home / ".gemini").exists()


@pytest.mark.parametrize("profile_installer", ["cmd"], indirect=True)
def test_windows_antigravity_on_path_creates_default_config(profile_installer):
    run, home, checkout, _, _ = profile_installer
    binaries = checkout / "fake client commands"
    binaries.mkdir()
    # WHERE discovers this stub. Setup must not actually launch the client.
    (binaries / "agy.cmd").write_bytes(b"@exit /b 99\r\n")
    system32 = Path(os.environ["SystemRoot"]) / "System32"
    path = os.pathsep.join(map(str, (binaries, system32, system32 / "WindowsPowerShell/v1.0")))

    result = run({"PATH": path})

    assert result.returncode == 0, result.stdout + result.stderr
    config = home / ".gemini/config/mcp_config.json"
    assert json.loads(config.read_text())["mcpServers"]["Agents-Core"]["args"] == [str(checkout / "src/server.py")]
    assert "CONFIGURED_ENVS= Antigravity" in result.stdout


@pytest.mark.parametrize("client", ["cursor", "desktop", "claude", "antigravity"])
@pytest.mark.parametrize("original", [b'{"mcpServers":', b'["preserve", "this"]',
                                     b'{"mcpServers": [], "keep": true}'],
                         ids=["malformed-json", "non-object-root", "non-object-servers"])
def test_invalid_client_config_is_preserved_and_not_reported_as_configured(profile_installer, client, original):
    run, home, _, _, desktop = profile_installer
    paths = {"cursor": home / ".cursor/mcp.json", "desktop": desktop,
             "claude": home / ".claude.json", "antigravity": home / ".gemini/config/mcp_config.json"}
    config = paths[client]
    config.parent.mkdir(parents=True, exist_ok=True)
    config.write_bytes(original)
    previous_backup = config.with_name(config.name + ".backup." + BACKUP_TIMESTAMP)
    previous_backup.write_bytes(b"Existing backup\r\n")

    result = run({})

    assert config.read_bytes() == original
    assert previous_backup.read_bytes() == b"Existing backup\r\n"
    backups = set(config.parent.glob(config.name + ".backup.*")) - {previous_backup}
    assert backups and all(backup.read_bytes() == original for backup in backups)
    assert "ERROR:" in result.stdout + result.stderr
    assert "Failed to update" in result.stdout
    if sys.platform == "win32":
        assert result.returncode == 0, result.stdout + result.stderr
        label = {"cursor": "Cursor", "desktop": "Claude Desktop", "claude": "Claude Code",
                 "antigravity": "Antigravity"}[client]
        configured = next(line.removeprefix("CONFIGURED_ENVS=") for line in result.stdout.splitlines()
                          if line.startswith("CONFIGURED_ENVS="))
        assert label not in configured and label.replace(" ", "-") not in configured
    else:
        assert result.returncode != 0, result.stdout + result.stderr
        # This marker is printed only when the real setup block completes.
        assert "CONFIGURED_ENVS=" not in result.stdout
        assert not (home / ".claude/CLAUDE.md").exists()


def custom_client_profile(home, client, suffix="profile !segment!"):
    """Select one disposable client profile with shell-sensitive path characters."""
    directory = home / f"custom {client} {suffix}"
    if client == "claude":
        return directory / ".claude.json", {"CLAUDE_CONFIG_DIR": str(directory),
                                             "segment": "expanded-value"}
    variable = {"cursor": "AGENTS_CURSOR_MCP_CONFIG", "desktop": "AGENTS_CLAUDE_DESKTOP_CONFIG",
                "antigravity": "AGENTS_ANTIGRAVITY_MCP_CONFIG"}[client]
    config = directory / "registration.json"
    return config, {variable: str(config), "segment": "expanded-value"}


@pytest.mark.parametrize("client", ["cursor", "desktop", "claude", "antigravity"])
@pytest.mark.parametrize("suffix", ["profile!literal", "profile !segment!"])
def test_repeated_mcp_setup_preserves_backups_when_timestamps_collide(profile_installer, client, suffix):
    run, home, checkout, _, _ = profile_installer
    config, overrides = custom_client_profile(home, client, suffix)
    config.parent.mkdir(parents=True)
    original = b'{"mcpServers": {"other": {"command": "keep"}}, "keep": "before"}\r\n'
    config.write_bytes(original)
    previous_backup = config.with_name(config.name + ".backup." + BACKUP_TIMESTAMP)
    previous_backup.write_bytes(b"Earlier client configuration\r\n")

    first = run(overrides)

    assert first.returncode == 0, first.stdout + first.stderr
    assert previous_backup.read_bytes() == b"Earlier client configuration\r\n"
    first_backups = set(config.parent.glob(config.name + ".backup.*"))
    new_backups = first_backups - {previous_backup}
    assert len(new_backups) == 1
    assert next(iter(new_backups)).read_bytes() == original
    document = json.loads(config.read_bytes())
    name = "Agents-Core-Desktop" if client == "desktop" else "Agents-Core"
    assert document["mcpServers"][name]["args"] == [str(checkout / "src/server.py")]
    assert document["mcpServers"]["other"] == {"command": "keep"}
    assert document["keep"] == "before"
    configured = config.read_bytes()
    preserved = {backup: backup.read_bytes() for backup in first_backups}

    second = run(overrides)

    assert second.returncode == 0, second.stdout + second.stderr
    assert config.read_bytes() == configured
    assert {backup: backup.read_bytes() for backup in preserved} == preserved
    second_backups = set(config.parent.glob(config.name + ".backup.*")) - first_backups
    assert len(second_backups) == 1
    assert next(iter(second_backups)).read_bytes() == configured


@pytest.mark.parametrize("client", ["cursor", "desktop", "claude", "antigravity"])
def test_backup_failure_preserves_client_config_and_is_not_reported_configured(profile_installer, client):
    run, home, checkout, _, _ = profile_installer
    config, overrides = custom_client_profile(home, client)
    config.parent.mkdir(parents=True)
    original = b'{"mcpServers": {"other": {"command": "keep"}}, "keep": true}\r\n'
    config.write_bytes(original)
    previous_backup = config.with_name(config.name + ".backup." + BACKUP_TIMESTAMP)
    previous_backup.write_bytes(b"Earlier client configuration\r\n")
    helper = checkout / "scripts/_helpers/backup_file.py"
    helper.write_text('import sys\nsys.stderr.write("ERROR: simulated backup failure\\n")\nsys.exit(1)\n',
                      encoding="utf-8")

    result = run(overrides)

    assert "ERROR: simulated backup failure" in result.stdout + result.stderr
    assert config.read_bytes() == original
    assert previous_backup.read_bytes() == b"Earlier client configuration\r\n"
    assert set(config.parent.glob(config.name + ".backup.*")) == {previous_backup}
    if sys.platform == "win32":
        configured = [line.removeprefix("CONFIGURED_ENVS=") for line in result.stdout.splitlines()
                      if line.startswith("CONFIGURED_ENVS=")]
        if configured:
            label = {"cursor": "Cursor", "desktop": "Claude Desktop", "claude": "Claude Code",
                     "antigravity": "Antigravity"}[client]
            assert label not in configured[0] and label.replace(" ", "-") not in configured[0]
        else:
            assert result.returncode != 0, result.stdout + result.stderr
    else:
        assert result.returncode != 0, result.stdout + result.stderr
        assert "CONFIGURED_ENVS=" not in result.stdout


@pytest.mark.parametrize("override", [None, "custom antigravity/mcp.json", "profile!literal/mcp.json",
                                     "profile !segment!/mcp.json"])
def test_client_config_paths_reports_effective_antigravity_path(tmp_path, override):
    home = tmp_path / "isolated home"
    env = dict(os.environ, HOME=str(home), USERPROFILE=str(home))
    env.pop("AGENTS_ANTIGRAVITY_MCP_CONFIG", None)
    expected = home / ".gemini/config/mcp_config.json"
    if override:
        expected = home / override
        env["AGENTS_ANTIGRAVITY_MCP_CONFIG"] = str(expected)

    result = subprocess.run([sys.executable, str(ROOT / "scripts/_helpers/client_config_paths.py")],
                            env=env, capture_output=True, text=True, encoding="utf-8", timeout=20)

    assert result.returncode == 0, result.stdout + result.stderr
    paths = dict(line.split("=", 1) for line in result.stdout.splitlines())
    assert paths["ANTIGRAVITY_CONFIG"] == str(expected)
    assert not home.exists()


@pytest.mark.parametrize("profile_installer", ["cmd"], indirect=True)
@pytest.mark.parametrize("missing", ["helper", "MCP_SETTINGS_FILE", "CLAUDE_DESKTOP_CONFIG",
                                     "CLAUDE_CODE_DIR", "CLAUDE_CODE_MCP", "CLAUDE_CODE_SETTINGS",
                                     "ANTIGRAVITY_CONFIG"])
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
                 "CLAUDE_CODE_SETTINGS": home / ".claude/settings.json",
                 "ANTIGRAVITY_CONFIG": home / ".gemini/config/mcp_config.json"}
        helper.write_text("\n".join(f"print({str(key + '=' + str(path))!r})"
                                    for key, path in paths.items() if key != missing) + "\n",
                          encoding="utf-8")
        expected_key = missing

    result = run({missing: "stale inherited path"})

    assert result.returncode == 1, result.stdout + result.stderr
    assert "FATAL: init_repo.bat aborted unexpectedly" in result.stdout
    assert "Exit code : 1" in result.stdout
    assert f"Context   : Failed to resolve {expected_key}" in result.stdout
    assert "!_FATAL_CTX!" not in result.stdout and "!_FATAL_EC!" not in result.stdout
    assert not list(home.iterdir())
