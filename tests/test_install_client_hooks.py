"""Normal setup choices and detection, with no live profiles or package installs."""
import json
from pathlib import Path
import subprocess
import sys

import pytest

from scripts import install_client_hooks as installer


@pytest.fixture
def profile(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: tmp_path))
    for name in ('CODEX_HOME', 'CLAUDE_CONFIG_DIR', 'AGENTS_CURSOR_MCP_CONFIG',
                 'AGENTS_CLAUDE_DESKTOP_CONFIG', 'AGENTS_ANTIGRAVITY_MCP_CONFIG', 'XDG_CONFIG_HOME'):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv('APPDATA', str(tmp_path / 'AppData/Roaming'))
    monkeypatch.setenv('LOCALAPPDATA', str(tmp_path / 'AppData/Local'))
    monkeypatch.setattr(installer.shutil, 'which', lambda command: None)
    monkeypatch.setattr(installer, 'ensure_claude_title_sdk', lambda: None)
    return tmp_path


def test_absent_clients_do_not_create_profiles(profile, capsys):
    assert installer.main(['--yes']) == 0
    assert list(profile.iterdir()) == []
    assert 'No supported clients' in capsys.readouterr().out


@pytest.mark.parametrize('client', ['codex', 'claude', 'cursor', 'desktop', 'antigravity'])
def test_detected_profiles_receive_supported_hooks_without_cli(profile, client):
    directory = {'codex': profile / '.codex', 'claude': profile / '.claude',
                 'cursor': profile / '.cursor', 'desktop': installer.client_config_path('desktop').parent,
                 'antigravity': profile / '.gemini/config'}[client]
    directory.mkdir(parents=True)
    assert installer.main(['--yes']) == 0
    if client == 'desktop':
        assert 'Agents-Readiness' in json.loads(installer.client_config_path('desktop').read_text())['mcpServers']
        assert (profile / '.claude/settings.json').exists()  # Code-tab profile
    else:
        target = directory / ('settings.json' if client == 'claude' else 'hooks.json')
        assert target.exists()
        if client == 'codex':
            assert 'Interrupt' in json.loads(target.read_text())['hooks']


def test_profile_override_preserves_user_hooks_and_repeat(profile, monkeypatch, capsys):
    selected = profile / 'alternate profile'
    selected.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(selected))
    target = selected / 'hooks.json'
    custom = {'hooks': {'SessionStart': [{'matcher': 'resume', 'hooks': [{'command': 'user-owned'}]}]}}
    target.write_text(json.dumps(custom))
    assert installer.main(['--yes']) == 0
    hooks = json.loads(target.read_text())['hooks']
    assert hooks['SessionStart'][0] == custom['hooks']['SessionStart'][0]
    original, stamp = target.read_bytes(), target.stat().st_mtime_ns
    backups = sorted(selected.glob('*.backup.*'))
    assert 'review/trust' in capsys.readouterr().out
    assert installer.main(['--yes']) == 0
    assert target.read_bytes() == original and target.stat().st_mtime_ns == stamp
    assert sorted(selected.glob('*.backup.*')) == backups
    assert not (profile / '.codex').exists()
    assert 'review/trust' not in capsys.readouterr().out


def test_interactive_choices_are_independent(profile, monkeypatch):
    (profile / '.codex').mkdir()
    replies = iter(['n', 'y'])
    monkeypatch.setattr('builtins.input', lambda prompt: next(replies))
    assert installer.main([]) == 0
    hooks = json.loads((profile / '.codex/hooks.json').read_text())['hooks']
    assert len(hooks['SessionStart']) == 1 and 'Interrupt' in hooks
    assert not (profile / '.codex/hooks/agents-core-session-readiness.py').exists()


def test_explicit_skip_overrides_yes(profile):
    (profile / '.codex').mkdir()
    assert installer.main(['--yes', '--skip-readiness', '--skip-thread-status']) == 0
    assert not (profile / '.codex/hooks.json').exists()


def test_bad_hook_json_fails_without_success_or_secret_output(profile, capsys):
    (profile / '.codex').mkdir()
    target = profile / '.codex/hooks.json'
    content = b'{"private-token": "never-print-this", broken'
    target.write_bytes(content)
    assert installer.main(['--yes']) == 1
    output = capsys.readouterr()
    assert 'ERROR:' in output.err
    assert 'never-print-this' not in output.err + output.out
    assert 'codex configured' not in output.out
    assert target.read_bytes() == content


def test_sdk_dependency_failure_still_installs_stdlib_claude_hook(profile, monkeypatch, capsys):
    (profile / '.claude').mkdir()
    def fail():
        raise subprocess.CalledProcessError(1, ['fake-pip'])
    monkeypatch.setattr(installer, 'ensure_claude_title_sdk', fail)
    assert installer.main(['--yes', '--skip-readiness']) == 0
    settings = json.loads((profile / '.claude/settings.json').read_text())
    assert 'UserPromptSubmit' in settings['hooks']
    assert (profile / '.claude/hooks/agents-core-thread-status.py').is_file()
    output = capsys.readouterr()
    assert 'WARNING:' in output.err and 'CLI title writes' in output.err
    assert 'claude configured' in output.out


def test_sdk_uses_pinned_extra_and_hook_interpreter(monkeypatch):
    monkeypatch.setattr(installer.metadata, 'version', lambda package: 'missing-version')
    calls = []
    monkeypatch.setattr(installer.subprocess, 'run', lambda command, **kwargs: calls.append(command))
    installer.ensure_claude_title_sdk()
    assert calls == [[sys.executable, '-m', 'pip', 'install', 'claude-agent-sdk==0.2.168']]


@pytest.mark.parametrize('client,variable,native', [
    ('cursor', 'AGENTS_CURSOR_MCP_CONFIG', '.cursor'),
    ('antigravity', 'AGENTS_ANTIGRAVITY_MCP_CONFIG', '.gemini/config'),
])
@pytest.mark.parametrize('default_exists', [False, True])
def test_alternate_mcp_profile_never_mutates_inactive_native_hooks(profile, monkeypatch, capsys, client, variable, native, default_exists):
    other = profile / 'alternate profile/mcp.json'
    other.parent.mkdir()
    other.write_bytes(b'{"mcpServers":{}}')
    monkeypatch.setenv(variable, str(other))
    target = profile / native / 'hooks.json'
    if default_exists:
        target.parent.mkdir(parents=True)
        target.write_bytes(b'{"personal":"unchanged"}')
    assert installer.main(['--yes']) == 0
    if default_exists:
        assert target.read_bytes() == b'{"personal":"unchanged"}'
    else:
        assert not target.parent.exists()
    assert other.read_bytes() == b'{"mcpServers":{}}'
    assert 'alternate MCP file' in capsys.readouterr().out
