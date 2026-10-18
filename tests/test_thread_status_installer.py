"""Focused safety and compatibility checks for the opt-in hook installer."""
import argparse
import base64
import importlib.util
import json
from pathlib import Path
import shlex

import pytest

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("install_thread_status", ROOT / "scripts/install_thread_status.py")
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


@pytest.fixture
def installation(tmp_path, monkeypatch):
    source = tmp_path / "source"
    for name, content in (("scripts/thread_status.py", b"# runtime\n"),
                          ("src/thread_status.py", b"# formatter\n"),
                          ("src/codex_thread_titles.py", b"# Codex API adapter\n")):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
    monkeypatch.setattr(installer, "ROOT", source)
    profile = tmp_path / "profile with spaces"
    profile.mkdir()
    target, script = profile / "settings.json", profile / "hooks" / installer.SCRIPT_NAME
    monkeypatch.setattr(installer, "client_paths", lambda _: (target, script))
    return target, script, source


@pytest.mark.parametrize("client", installer.CLIENTS)
def test_formats_preservation_copy_and_idempotence(installation, client):
    target, script, source = installation
    event = "sessionStart" if client == "cursor" else "SessionStart"
    unrelated = {"command": "other"} if client == "cursor" else {"matcher": "resume", "hooks": [{"command": "other"}]}
    original = {"permissions": {"allow": ["Bash"]}, "hooks": {event: [unrelated], "Stop": [{"command": "stop-other"}]}}
    raw = json.dumps(original).encode()
    target.write_bytes(raw)
    result = installer.install(client)
    assert result["changed"] and Path(result["backup"]).read_bytes() == raw
    document = json.loads(target.read_text())
    assert document["permissions"] == original["permissions"]
    assert document["hooks"]["Stop"] == original["hooks"]["Stop"]
    assert document["hooks"][event][0] == unrelated
    events = ((event,) if client == "cursor" else (event, "UserPromptSubmit", "Interrupt")
              if client == "codex" else (event, "UserPromptSubmit"))
    assert set(document["hooks"]) == {*events, "Stop"}
    for name in events:
        entry = document["hooks"][name][-1]
        handler = entry if client == "cursor" else entry["hooks"][0]
        if installer.helpers.WINDOWS_HOOKS:
            arguments = shlex.split(handler["command"])
            assert arguments[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand"]
            payload = base64.b64decode(arguments[4]).decode("utf-16-le")
            assert payload == installer.helpers.powershell_payload([installer.sys.executable, str(script), "--client", client])
        else:
            assert shlex.split(handler["command"])[1:] == [str(script), "--client", client]
        assert handler["timeout"] == (3 if name == "Interrupt" else 5)
    if client == "cursor":
        assert document["version"] == 1
    assert script.read_bytes() == (source / "scripts/thread_status.py").read_bytes()
    assert script.with_name(installer.CORE_NAME).read_bytes() == (source / "src/thread_status.py").read_bytes()
    assert script.with_name(installer.CODEX_NAME).read_bytes() == (source / "src/codex_thread_titles.py").read_bytes()
    before = {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in (target, script, script.with_name(installer.CORE_NAME), script.with_name(installer.CODEX_NAME))}
    assert not installer.install(client)["changed"]
    assert {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in before} == before


@pytest.mark.parametrize("client", installer.CLIENTS)
@pytest.mark.parametrize("existing", [False, True])
def test_dry_run_never_writes(installation, client, existing):
    target, script, _ = installation
    if existing:
        target.write_text('{"unrelated":true}')
    before = {path: path.read_bytes() for path in target.parent.rglob("*") if path.is_file()}
    assert installer.install(client, dry_run=True)["changed"]
    assert {path: path.read_bytes() for path in target.parent.rglob("*") if path.is_file()} == before
    assert not script.parent.exists()


@pytest.mark.parametrize("raw", ["bad", "[]", '{"hooks":[]}', '{"hooks":{"SessionStart":[1]}}',
                                     '{"hooks":{"UserPromptSubmit":[{"hooks":42}]}}'])
def test_invalid_config_is_untouched(installation, raw):
    target, script, _ = installation
    target.write_text(raw)
    with pytest.raises(ValueError):
        installer.install("claude")
    assert target.read_text() == raw and not script.parent.exists()


@pytest.mark.parametrize("version", [2, True, "1"])
def test_cursor_rejects_invalid_version(installation, version):
    target, script, _ = installation
    raw = json.dumps({"version": version})
    target.write_text(raw)
    with pytest.raises(ValueError, match="version 1"):
        installer.install("cursor")
    assert target.read_text() == raw and not script.parent.exists()


def test_mixed_groups_preserve_other_handlers_and_remove_owned_duplicates(installation):
    target, script, _ = installation
    owned = {"type": "command", "command": installer.command_for(script, "--client", "claude")}
    other = {"command": "audit --script " + shlex.quote(str(script))}
    target.write_text(json.dumps({"hooks": {"SessionStart": [
        {"matcher": "resume", "hooks": [owned, other]}, {"hooks": [owned]}, {"hooks": [owned]}]}}))
    installer.install("claude")
    groups = json.loads(target.read_text())["hooks"]["SessionStart"]
    assert groups[0] == {"matcher": "resume", "hooks": [other]}
    assert len(groups) == 2
    assert sum(installer.owns_handler(item, script) for group in groups for item in group["hooks"]) == 1
    assert not installer.install("claude")["changed"]


def test_backups_are_unique_and_failure_precedes_writes(installation, monkeypatch):
    target, script, source = installation
    target.write_text('{"unrelated":true}')
    first = installer.install("claude")["backup"]
    target.write_text('{"unrelated":false}')
    second = installer.install("claude")["backup"]
    assert first != second
    assert json.loads(Path(first).read_text()) == {"unrelated": True}
    assert json.loads(Path(second).read_text()) == {"unrelated": False}
    (source / "scripts/thread_status.py").write_bytes(b"# update\n")
    target.write_text('{"unrelated":42}')
    before = target.read_bytes(), script.read_bytes()
    def failure(_):
        raise OSError("backup failed")
    monkeypatch.setattr(installer, "backup_file", failure)
    with pytest.raises(OSError, match="backup failed"):
        installer.install("claude")
    assert (target.read_bytes(), script.read_bytes()) == before


@pytest.mark.parametrize("kind", ["config", "runtime", "core", "codex", "hooks-directory", "profile-directory"])
def test_symlinks_are_refused_before_writes(installation, tmp_path, kind):
    target, script, _ = installation
    external = tmp_path / "external"
    external.mkdir()
    if kind == "profile-directory":
        target.parent.rmdir()
        target.parent.symlink_to(external, target_is_directory=True)
    elif kind == "hooks-directory":
        script.parent.symlink_to(external, target_is_directory=True)
    else:
        victim = external / "victim"
        victim.write_text("original")
        link = {"config": target, "runtime": script, "core": script.with_name(installer.CORE_NAME),
                "codex": script.with_name(installer.CODEX_NAME)}[kind]
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(victim)
    before = {path: path.read_bytes() for path in external.rglob("*") if path.is_file()}
    with pytest.raises(ValueError, match="symbolic link"):
        installer.install("claude")
    assert {path: path.read_bytes() for path in external.rglob("*") if path.is_file()} == before


def test_client_selection_and_profile_overrides(tmp_path, monkeypatch):
    assert installer.parse_clients("all") == installer.CLIENTS
    assert installer.parse_clients("codex,claude,codex") == ("codex", "claude")
    for value in ("", "all,codex", "desktop"):
        with pytest.raises(argparse.ArgumentTypeError):
            installer.parse_clients(value)
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "claude-profile"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-profile"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    assert installer.client_paths("claude")[0] == tmp_path / "claude-profile/settings.json"
    assert installer.client_paths("codex")[0] == tmp_path / "codex-profile/hooks.json"
    assert installer.client_paths("cursor")[0] == tmp_path / ".cursor/hooks.json"


def test_windows_quoting_and_ownership(tmp_path, monkeypatch):
    monkeypatch.setattr(installer.helpers, "WINDOWS_HOOKS", True)
    script = tmp_path / "O'Brien $user %x%!`" / installer.SCRIPT_NAME
    for client in installer.CLIENTS:
        command = installer.command_for(script, "--client", client)
        arguments = shlex.split(command)
        assert arguments[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand"]
        payload = base64.b64decode(arguments[4]).decode("utf-16-le")
        assert payload == installer.helpers.powershell_payload([installer.sys.executable, str(script), "--client", client])
        assert installer.owns_handler({"command": command}, script)
        assert not installer.owns_handler({"command": command + " extra"}, script)


def test_unrelated_program_and_shell_suffix_are_not_owned(installation):
    _, script, _ = installation
    for command in (shlex.join(["audit", str(script), "--client", "claude"]),
                    installer.command_for(script, "--client", "claude") + " && echo extra"):
        assert not installer.owns_handler({"command": command}, script)


def test_cli_install_and_error_reporting(installation, capsys):
    target, script, _ = installation
    assert installer.main(["--clients", "claude"]) == 0
    assert json.loads(capsys.readouterr().out)["changed"]
    assert target.exists() and script.exists()
    target.write_text("broken")
    assert installer.main(["--clients", "claude"]) == 1
    assert "ERROR (claude)" in capsys.readouterr().err
