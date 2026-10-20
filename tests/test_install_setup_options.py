"""Optional shared setup with private disposable state and no service operations."""
import json
from pathlib import Path
import sys

import pytest

from scripts import install_setup_options as setup


@pytest.fixture
def profile(tmp_path, monkeypatch):
    root = tmp_path / 'installation with spaces'
    (root / 'data').mkdir(parents=True)
    directory = tmp_path / 'private service'
    directory.mkdir()
    events = []

    class Controller:
        config = {}
        state = 'stopped'
        fail_cooperation = False

        def __init__(self, selected):
            assert Path(selected) == directory
            self.directory = directory

        def install(self, **kwargs):
            events.append(('install', kwargs))
            self.config = {'installation': str(root), 'port': 9876}

        def status(self):
            enabled = (root / '.env').is_file() and 'AGENTS_COOPERATION_ENABLED=1' in (root / '.env').read_text()
            return {'state': self.state, 'install_root': self.config.get('installation'),
                    'cooperation': 'ready' if enabled and not self.fail_cooperation else 'disabled'}

        def start(self):
            events.append(('start', None))
            self.state = 'ready'
            return self.status()

        def restart(self):
            events.append(('restart', None))
            self.state = 'ready'
            return self.status()

    controller = Controller(directory)
    monkeypatch.setattr(setup, 'ROOT', root)
    monkeypatch.setattr(setup, 'Controller', lambda selected: controller)
    monkeypatch.setattr(setup, 'state_dir', lambda installation: directory)
    monkeypatch.setattr(setup.sys, 'platform', 'darwin')
    monkeypatch.setattr(setup.hooks, 'main', lambda flags: events.append(('hooks', flags)) or 0)
    def refresh(selected, **kwargs):
        assert selected == directory
        events.append(('registrations', kwargs))
        return {'clients': {'claude': {'status': 'current'}}}
    monkeypatch.setattr(setup.registrations, 'run', refresh)
    return root, controller, events


def existing(profile):
    root, controller, _ = profile
    controller.config = {'installation': str(root), 'port': 9876, 'model': 'user-choice'}
    controller.state = 'ready'
    return controller


def test_yes_installs_all_and_enables_cooperation_before_first_start(profile):
    root, controller, events = profile
    assert setup.main(['--yes']) == 0
    assert [event[0] for event in events] == ['hooks', 'install', 'start', 'registrations']
    assert events[1][1]['python'] == sys.executable
    assert (root / '.env').read_text() == 'AGENTS_COOPERATION_ENABLED=1\n'
    assert controller.status()['cooperation'] == 'ready'


def test_existing_ready_service_is_not_reinstalled_or_restarted(profile):
    root, controller, events = profile
    existing(profile)
    (root / '.env').write_text('AGENTS_COOPERATION_ENABLED=1\n')
    original = dict(controller.config)
    stamp = (root / '.env').stat().st_mtime_ns
    assert setup.main(['--yes']) == 0
    assert [event[0] for event in events] == ['hooks', 'registrations']
    assert controller.config == original
    assert (root / '.env').stat().st_mtime_ns == stamp


def test_cooperation_choice_preserves_comments_crlf_and_restarts_once(profile):
    root, _, events = profile
    existing(profile)
    (root / '.env').write_bytes(b'# Keep this\r\nPRIVATE="do-not-print"\r\nexport AGENTS_COOPERATION_ENABLED=0 # choice\r\n')
    assert setup.main(['--yes', '--skip-daemon']) == 0
    assert [event[0] for event in events] == ['hooks', 'restart']
    assert (root / '.env').read_bytes() == b'# Keep this\r\nPRIVATE="do-not-print"\r\nexport AGENTS_COOPERATION_ENABLED=1 # choice\r\n'


@pytest.mark.parametrize('flags', [['--yes', '--skip-daemon', '--skip-cooperation'],
                                  ['--skip-cooperation', '--skip-daemon', '--yes']])
def test_explicit_shared_skips_win_and_preserve_state(profile, flags):
    root, _, events = profile
    assert setup.main(flags) == 0
    assert events == [('hooks', ['--yes'])]
    assert not (root / '.env').exists()


def test_skip_hooks_still_installs_daemon_and_cooperation(profile):
    _, _, events = profile
    assert setup.main(['--yes', '--skip-hooks']) == 0
    assert [event[0] for event in events] == ['install', 'start', 'registrations']


def test_skip_cooperation_does_not_change_env_or_disable_existing_setting(profile):
    root, _, events = profile
    existing(profile)
    content = b'# unchanged\nAGENTS_COOPERATION_ENABLED=1\n'
    (root / '.env').write_bytes(content)
    assert setup.main(['--yes', '--skip-cooperation']) == 0
    assert (root / '.env').read_bytes() == content
    assert 'restart' not in [event[0] for event in events]


def test_declined_daemon_without_existing_service_skips_cooperation(profile, monkeypatch):
    root, _, events = profile
    monkeypatch.setattr('builtins.input', lambda prompt: 'n')
    assert setup.main([]) == 0
    assert events == [('hooks', [])]
    assert not (root / '.env').exists()


def test_choices_are_independent_for_existing_daemon(profile, monkeypatch):
    _, _, events = profile
    existing(profile)
    replies = iter(['n', 'y'])
    monkeypatch.setattr('builtins.input', lambda prompt: next(replies))
    assert setup.main([]) == 0
    assert [event[0] for event in events] == ['hooks', 'restart']


def test_eof_does_not_install_daemon(profile, monkeypatch):
    def eof(prompt):
        raise EOFError
    monkeypatch.setattr('builtins.input', eof)
    assert setup.main([]) == 0
    assert profile[2] == [('hooks', [])]


def test_unsupported_linux_keeps_hooks_without_service_state(profile, monkeypatch, capsys):
    monkeypatch.setattr(setup.sys, 'platform', 'linux')
    assert setup.main(['--yes']) == 0
    assert profile[2] == [('hooks', ['--yes'])]
    assert not (profile[0] / '.env').exists()
    assert 'macOS and Windows' in capsys.readouterr().out


@pytest.mark.parametrize('phase', ['install', 'start', 'restart'])
def test_service_failure_preserves_independent_hooks_and_redacts_exception(profile, monkeypatch, capsys, phase):
    root, controller, events = profile
    if phase == 'restart':
        existing(profile)
    def fail(**kwargs):
        raise RuntimeError('PRIVATE_TOKEN_NEVER_PRINT')
    monkeypatch.setattr(controller, phase, fail)
    assert setup.main(['--yes']) == 1
    assert events[0][0] == 'hooks'
    output = capsys.readouterr()
    assert 'ERROR:' in output.err and 'PRIVATE_TOKEN_NEVER_PRINT' not in output.err + output.out
    assert 'registrations' not in [event[0] for event in events]
    if phase == 'install':
        assert not (root / '.env').exists()


def test_cooperation_failure_is_not_hidden_by_ordinary_readiness(profile, capsys):
    profile[1].fail_cooperation = True
    assert setup.main(['--yes']) == 1
    output = capsys.readouterr()
    assert 'cooperation ready.' not in output.out
    assert 'ERROR:' in output.err


def test_wrong_installation_is_refused_before_service_or_env_mutations(profile, capsys):
    root, controller, events = profile
    controller.config = {'installation': str(root.parent / 'another-installation')}
    assert setup.main(['--yes']) == 1
    assert events == [('hooks', ['--yes'])]
    assert not (root / '.env').exists()


def test_maintenance_is_refused_before_status_and_env_writes(profile, monkeypatch):
    root, controller, events = profile
    existing(profile)
    (controller.directory / 'maintenance.json').write_text('{"active":true}')
    monkeypatch.setattr(controller, 'status', lambda: pytest.fail('must admit before status/token read'))
    assert setup.main(['--yes']) == 1
    assert events == [('hooks', ['--yes'])]
    assert not (root / '.env').exists()


def test_failed_managed_registration_is_reported_without_private_reason(profile, monkeypatch, capsys):
    monkeypatch.setattr(setup.registrations, 'run', lambda *args, **kwargs:
                        {'clients': {'codex': {'status': 'failed', 'reason': 'PRIVATE_CONFIG'}}})
    assert setup.main(['--yes']) == 1
    output = capsys.readouterr()
    assert 'codex failed' in output.out
    assert 'PRIVATE_CONFIG' not in output.out + output.err


def test_busy_registration_refresh_retries_only_busy(profile, monkeypatch):
    calls = []
    def refresh(*args, **kwargs):
        calls.append(kwargs)
        if len(calls) < 3:
            raise BlockingIOError
        return {'clients': {}}
    monkeypatch.setattr(setup.registrations, 'run', refresh)
    monkeypatch.setattr(setup.time, 'sleep', lambda seconds: None)
    assert setup.main(['--yes', '--force-claude-code-mcp']) == 0
    assert len(calls) == 3 and all(call['force_claude_code'] for call in calls)


def test_shared_service_marker_uses_existing_private_directory(profile):
    root, controller, _ = profile
    existing(profile)
    (root / 'data/.shared-service.json').write_text(json.dumps({'directory': str(controller.directory)}))
    assert setup.controller_for_installation() is controller


def test_hook_failure_does_not_skip_independent_daemon_setup(profile, monkeypatch):
    monkeypatch.setattr(setup.hooks, 'main', lambda flags: 1)
    assert setup.main(['--yes']) == 1
    assert [event[0] for event in profile[2]] == ['install', 'start', 'registrations']


@pytest.mark.parametrize('kind', ['bash', 'cmd'])
@pytest.mark.parametrize('skip_mcp', [False, True])
def test_native_launchers_forward_all_independent_options(tmp_path, kind, skip_mcp):
    import os
    import shutil
    import subprocess
    if kind == 'cmd' and sys.platform != 'win32':
        pytest.skip('requires native cmd.exe')
    if kind == 'bash' and sys.platform == 'win32':
        pytest.skip('Windows exercises native cmd.exe')
    command = os.environ.get('COMSPEC') if kind == 'cmd' else shutil.which('bash')
    if not command:
        pytest.skip('native interpreter unavailable')
    repository = Path(__file__).resolve().parents[1]
    root = tmp_path / 'installation with spaces'
    (root / 'scripts').mkdir(parents=True)
    capture = tmp_path / 'captured.json'
    (root / 'scripts/install_setup_options.py').write_text(
        'import json, os, sys\nfrom pathlib import Path\n'
        'Path(os.environ["SETUP_CAPTURE"]).write_text(json.dumps(sys.argv[1:]))\n')
    def section(source, begin, end):
        start = source.index(begin)
        return source[start:source.index(end, start)]
    if kind == 'bash':
        source = (repository / 'scripts/init_repo.sh').read_text(encoding='utf-8')
        arguments = section(source, '# ============== Parse Arguments', '# ============== Helper Functions')
        options = section(source, '# ============== Client Lifecycle Hooks', '# ============== Sync Between Machines')
        script = root / 'setup.sh'
        script.write_text('set -e\nprint_step() { :; }\n' + arguments + options)
        invocation = [command, str(script)]
    else:
        source = (repository / 'scripts/init_repo.bat').read_text(encoding='utf-8')
        arguments = section(source, 'REM ============== Parse Arguments', '\n:args_done\n') + '\n:args_done\n'
        options = section(source, 'REM ============== Client Lifecycle Hooks', 'REM ============== Sync Between Machines')
        script = root / 'setup.bat'
        script.write_bytes(('@echo off\nsetlocal EnableDelayedExpansion\n' + arguments + options
                            + '\nexit /b 0\n').replace('\n', '\r\n').encode())
        invocation = [command, '/d', '/c', str(script)]
    flags = ['--yes', '--skip-hooks', '--skip-daemon', '--skip-cooperation',
             '--skip-readiness', '--skip-thread-status', '--force-claude-code-mcp']
    if skip_mcp:
        flags.append('--skip-mcp')
    environment = {**os.environ, 'REPO_ROOT': str(root), 'PYTHON_ABS': sys.executable,
                   'SETUP_CAPTURE': str(capture)}
    environment.pop('AGENTS_ASSUME_YES', None)
    environment.pop('AGENTS_FORCE_CLAUDE_CODE_MCP', None)
    result = subprocess.run(invocation + flags, env=environment, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr + result.stdout
    if skip_mcp:
        assert not capture.exists()
    else:
        assert set(json.loads(capture.read_text())) == set(flags)
