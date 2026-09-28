"""Execute the real installer instruction hooks without dependency or MCP setup.

Each harness contains the installer's own template selector, skip-MCP guard, and
Codex hook. Only unrelated installation stages are omitted. Temporary checkout
and home paths contain spaces to exercise the shell/batch argument boundaries.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
BEGIN = b"# >>> Agents-Core Routing Protocol (managed by init_repo) >>>"


def between(source, start, end):
    first = source.index(start)
    return source[first:source.index(end, first)]


@pytest.fixture(params=["bash", "cmd"], ids=["unix", "windows"])
def installer_hook(request, tmp_path):
    kind = request.param
    if kind == "cmd" and sys.platform != "win32":
        pytest.skip("requires native cmd.exe")
    if kind == "bash" and sys.platform == "win32":
        pytest.skip("Unix hook runs on Unix; Windows exercises native cmd.exe")
    interpreter = os.environ.get("COMSPEC") if kind == "cmd" else shutil.which("bash")
    if not interpreter:
        pytest.skip(f"{kind} is unavailable")

    checkout = tmp_path / "checkout with spaces"
    helpers = checkout / "scripts" / "_helpers"
    templates = checkout / "scripts" / "templates"
    helpers.mkdir(parents=True)
    templates.mkdir()
    for name in ("install_codex_instructions.py", "inject_claude_md.py"):
        shutil.copyfile(ROOT / "scripts" / "_helpers" / name, helpers / name)
    for name in ("routing-protocol-core.md", "routing-protocol-v1.md"):
        shutil.copyfile(ROOT / "scripts" / "templates" / name, templates / name)
    codex_home = tmp_path / "custom codex home"
    profile = tmp_path / "isolated profile"
    profile.mkdir()
    env = dict(os.environ, HOME=str(profile), USERPROFILE=str(profile),
               CODEX_HOME=str(codex_home), REPO_ROOT=str(checkout),
               PYTHON_ABS=sys.executable, HELPERS=str(helpers),
               RED="", GREEN="", YELLOW="", BLUE="", CYAN="", NC="",
               PYTHONUTF8="1")
    for name in ("PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "__PYVENV_LAUNCHER__"):
        env.pop(name, None)

    if kind == "bash":
        source = (ROOT / "scripts" / "init_repo.sh").read_text(encoding="utf-8")
        selector = between(source, 'PERSONA_PROTOCOL="${AGENTS_PERSONA_PROTOCOL:-2}"',
                           "# Canonical managed-section markers")
        functions = between(source, "print_header() {", "# Fatal error handler")
        guard = between(source, 'if [ "$SKIP_MCP" = true ]; then',
                        "    # Track which environments were configured")
        hook = between(source, "    # --- Configure Codex instructions ---",
                       "    # --- Summary ---")
        closing = between(source, "    # --- Summary ---",
                          "# ============== Final Summary ==============")
        script = checkout / "instructions.sh"
        script.write_text("set -e\nCONFIGURED_ENVS=()\n" + functions + selector + guard + hook + closing,
                          encoding="utf-8")
        command = [interpreter, str(script)]
    else:
        source = (ROOT / "scripts" / "init_repo.bat").read_text(encoding="utf-8")
        selector = between(source, 'set "PERSONA_PROTOCOL=2"',
                           "REM ============== Pre-flight Checks")
        guard = between(source, 'if "%SKIP_MCP%"=="true" (', 'set "CONFIGURED_ENVS="')
        hook = between(source, "REM --- Configure Codex instructions ---", "REM --- MCP Summary ---")
        closing = between(source, "\n:mcp_done\n", "REM ============== Final Summary ==============")
        script = checkout / "instructions.bat"
        script.write_bytes(("@echo off\nsetlocal enabledelayedexpansion\n" + selector + guard + hook
                            + closing + "exit /b 0\n").replace("\n", "\r\n").encode("utf-8"))
        command = [interpreter, "/d", "/c", script.name]

    def run(version=None, skip_mcp=False):
        process_env = dict(env, SKIP_MCP="true" if skip_mcp else "false")
        if version is None:
            process_env.pop("AGENTS_PERSONA_PROTOCOL", None)
        else:
            process_env["AGENTS_PERSONA_PROTOCOL"] = str(version)
        return subprocess.run(command, cwd=checkout, env=process_env, text=True,
                              encoding="utf-8", errors="replace", capture_output=True, timeout=15)

    return run, codex_home, templates


@pytest.mark.parametrize("version", [None, 1, 2], ids=["default", "v1", "v2"])
def test_installer_hook_selects_requested_protocol(installer_hook, version):
    run, codex_home, templates = installer_hook
    result = run(version)
    assert result.returncode == 0, result.stdout + result.stderr
    target = codex_home / "AGENTS.md"
    template = templates / ("routing-protocol-v1.md" if version == 1 else "routing-protocol-core.md")
    assert template.read_bytes().strip() in target.read_bytes()
    assert target.read_bytes().count(BEGIN) == 1
    assert str(target) in result.stdout and "configured" in result.stdout
    assert "fresh Codex session" in result.stdout
    assert not list(codex_home.glob("*.backup.*"))


def test_installer_hook_migrates_preserves_and_repeats_without_backup_growth(installer_hook):
    run, codex_home, templates = installer_hook
    codex_home.mkdir()
    target = codex_home / "AGENTS.md"
    original = b"# Personal rules\r\nKeep these bytes.\r\n"
    target.write_bytes(original)
    for version in (1, 2, 1):
        result = run(version)
        assert result.returncode == 0, result.stdout + result.stderr
        template = templates / ("routing-protocol-v1.md" if version == 1 else "routing-protocol-core.md")
        updated = target.read_bytes()
        assert updated.startswith(original) and template.read_bytes().strip() in updated
        assert updated.count(BEGIN) == 1
        backups = sorted(codex_home.glob("AGENTS.md.backup.*"))
        assert backups
        mtime = target.stat().st_mtime_ns
        repeated = run(version)
        assert repeated.returncode == 0, repeated.stdout + repeated.stderr
        assert "already current" in repeated.stdout
        assert target.read_bytes() == updated and target.stat().st_mtime_ns == mtime
        assert sorted(codex_home.glob("AGENTS.md.backup.*")) == backups


def test_installer_hook_respects_override_precedence(installer_hook):
    run, codex_home, templates = installer_hook
    codex_home.mkdir()
    normal, override = codex_home / "AGENTS.md", codex_home / "AGENTS.override.md"
    normal.write_bytes(b"Normal personal rules")
    override.write_bytes(b"Override personal rules")
    result = run()
    assert result.returncode == 0, result.stdout + result.stderr
    assert normal.read_bytes() == b"Normal personal rules"
    assert override.read_bytes().startswith(b"Override personal rules\n")
    assert (templates / "routing-protocol-core.md").read_bytes().strip() in override.read_bytes()
    assert str(override) in result.stdout
    assert not list(codex_home.glob("AGENTS.md.backup.*"))


def test_installer_skip_mcp_guard_prevents_instruction_updates(installer_hook):
    run, codex_home, _ = installer_hook
    codex_home.mkdir()
    target = codex_home / "AGENTS.md"
    target.write_bytes(b"Personal rules")
    result = run(skip_mcp=True)
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Skipping MCP configuration" in result.stdout
    assert "Checking Codex global instructions" not in result.stdout
    assert target.read_bytes() == b"Personal rules"
    assert not list(codex_home.glob("*.backup.*"))


@pytest.mark.parametrize("failure", ["missing_template", "invalid_markers"])
def test_installer_hook_reports_failures_without_rewriting(installer_hook, failure):
    run, codex_home, templates = installer_hook
    codex_home.mkdir()
    target = codex_home / "AGENTS.md"
    original = b"Personal rules"
    if failure == "missing_template":
        (templates / "routing-protocol-core.md").unlink()
    else:
        original += b"\n" + BEGIN
    target.write_bytes(original)
    result = run()
    output = result.stdout + result.stderr
    # Both full installers treat client setup failures as actionable warnings.
    assert "ERROR: Could not configure Codex instructions" in output
    assert "Failed to configure Codex instructions" in output
    assert "Codex global instructions configured:" not in output
    assert target.read_bytes() == original
    assert not list(codex_home.glob("*.backup.*"))
