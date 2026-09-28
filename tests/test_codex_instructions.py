"""Codex installation updates only its effective global managed instruction block."""
import importlib
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def codex(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts" / "_helpers"))
    helper = importlib.import_module("install_codex_instructions")
    injector = importlib.import_module("inject_claude_md")
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(helper.shutil, "which", lambda command: None)
    template = ROOT / "scripts" / "templates" / "routing-protocol-core.md"
    return helper, injector, home, template


def test_absent_codex_is_skipped_without_creating_home(codex, capsys):
    helper, _, home, template = codex
    assert helper.main([str(template)]) == 0
    assert not (home / ".codex").exists()
    assert "Codex not detected" in capsys.readouterr().out


@pytest.mark.parametrize("detection", ["directory", "empty_env", "custom_home", "cli"])
def test_detected_codex_receives_protocol_2(codex, tmp_path, monkeypatch, capsys, detection):
    helper, injector, home, template = codex
    destination = home / ".codex"
    if detection in ("directory", "empty_env"):
        destination.mkdir()
    if detection == "empty_env":
        monkeypatch.setenv("CODEX_HOME", "")
    elif detection == "custom_home":
        destination = tmp_path / "custom codex home"
        monkeypatch.setenv("CODEX_HOME", str(destination))
    elif detection == "cli":
        monkeypatch.setattr(helper.shutil, "which", lambda command: "/bin/codex")

    assert helper.main([str(template)]) == 0
    target = destination / "AGENTS.md"
    content = target.read_text(encoding="utf-8")
    assert template.read_text(encoding="utf-8").strip() in content
    assert content.count(injector.MARKER_BEGIN) == 1
    assert not list(destination.glob("*.backup.*"))
    output = capsys.readouterr().out
    assert "configured" in output and str(target) in output and "fresh Codex session" in output
    if detection == "custom_home":
        assert not (home / ".codex").exists()


def test_custom_home_tilde_expands(codex, monkeypatch):
    helper, _, home, template = codex
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("CODEX_HOME", "~/custom-codex")
    assert helper.main([str(template)]) == 0
    assert (home / "custom-codex" / "AGENTS.md").exists()


@pytest.mark.parametrize("legacy_markers", [False, True])
def test_v1_migration_preserves_user_bytes_and_repeat_is_noop(codex, capsys, legacy_markers):
    helper, injector, home, template = codex
    destination = home / ".codex"
    destination.mkdir()
    target = destination / "AGENTS.md"
    prefix, suffix = b"# Personal rules\r\nUse Spanish.\r\n\r\n", b"\r\n\r\nKeep this\r\n"
    begin = injector.LEGACY_MARKER_BEGIN if legacy_markers else injector.MARKER_BEGIN
    end = injector.LEGACY_MARKER_END if legacy_markers else injector.MARKER_END
    old_template = (template.parent / "routing-protocol-v1.md").read_bytes()
    original = prefix + begin.encode() + b"\n" + old_template + b"\n" + end.encode() + suffix
    target.write_bytes(original)

    assert helper.main([str(template)]) == 0
    updated = target.read_bytes()
    assert updated.startswith(prefix) and updated.endswith(suffix)
    assert template.read_bytes().strip() in updated
    assert updated.count(injector.MARKER_BEGIN.encode()) == 1
    backups = list(destination.glob("AGENTS.md.backup.*"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    capsys.readouterr()

    assert helper.main([str(template)]) == 0
    assert target.read_bytes() == updated
    assert list(destination.glob("AGENTS.md.backup.*")) == backups
    output = capsys.readouterr().out
    assert "already current" in output and str(target) in output and "fresh Codex session" in output


def test_existing_unmarked_instructions_are_preserved(codex):
    helper, injector, home, template = codex
    destination = home / ".codex"
    destination.mkdir()
    target = destination / "AGENTS.md"
    target.write_bytes(b"My custom instructions without final newline")
    assert helper.main([str(template)]) == 0
    assert target.read_bytes().startswith(
        b"My custom instructions without final newline\n" + injector.MARKER_BEGIN.encode()
    )


@pytest.mark.parametrize("override_content", [b"", b" \r\n\t", b"Override instructions"])
def test_only_effective_instruction_file_is_updated(codex, override_content):
    helper, _, home, template = codex
    destination = home / ".codex"
    destination.mkdir()
    normal, override = destination / "AGENTS.md", destination / "AGENTS.override.md"
    normal.write_bytes(b"Normal instructions")
    override.write_bytes(override_content)
    assert helper.main([str(template)]) == 0
    effective, inactive = (override, normal) if override_content.strip() else (normal, override)
    assert template.read_bytes().strip() in effective.read_bytes()
    assert inactive.read_bytes() == (b"Normal instructions" if override_content.strip() else override_content)
    assert not list(destination.glob(inactive.name + ".backup.*"))


@pytest.mark.parametrize("failure", ["missing_source", "invalid_markers", "symlink", "write_error"])
def test_failure_reports_error_without_rewriting(codex, tmp_path, monkeypatch, capsys, failure):
    helper, injector, home, template = codex
    destination = home / ".codex"
    destination.mkdir()
    target = destination / "AGENTS.md"
    original = b"Personal instructions"
    if failure == "missing_source":
        template = tmp_path / "missing.md"
    elif failure == "invalid_markers":
        original += b"\n" + injector.MARKER_BEGIN.encode()
    elif failure == "write_error":
        def fail_inject(*args):
            raise PermissionError("target is not writable")
        monkeypatch.setattr(helper, "inject", fail_inject)
    if failure == "symlink":
        linked = tmp_path / "personal.md"
        linked.write_bytes(original)
        try:
            target.symlink_to(linked)
        except OSError as exc:
            if sys.platform == "win32" and exc.winerror == 1314:
                pytest.skip("Windows account lacks symlink creation privilege")
            raise
    else:
        target.write_bytes(original)

    assert helper.main([str(template)]) == 1
    assert target.read_bytes() == original
    assert not list(destination.glob("*.backup.*"))
    if failure == "symlink":
        assert target.is_symlink()
    error = capsys.readouterr().err
    assert "ERROR" in error and "rerun the installer" in error


def test_cli_reports_usage_or_absent_codex_without_side_effects(tmp_path):
    helper_path = ROOT / "scripts" / "_helpers" / "install_codex_instructions.py"
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), PATH="", CODEX_HOME="")
    for arguments, code, expected in (
        ([], 1, "Usage:"),
        ([str(ROOT / "scripts" / "templates" / "routing-protocol-core.md")], 0, "Codex not detected"),
    ):
        result = subprocess.run(
            [sys.executable, str(helper_path), *arguments],
            env=env, text=True, capture_output=True, timeout=10,
        )
        assert result.returncode == code, result.stdout + result.stderr
        assert expected in result.stdout + result.stderr
    assert not (tmp_path / ".codex").exists()
