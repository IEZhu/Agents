"""Codex registration in isolated profiles; never launch a server or client."""
from contextlib import nullcontext
import base64
import importlib
import json
from pathlib import Path
import sys
import tomllib

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def codex_mcp(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / 'scripts/_helpers'))
    helper = importlib.import_module('install_codex_mcp')
    home = tmp_path / 'codex profile'
    home.mkdir()
    monkeypatch.setenv('CODEX_HOME', str(home))
    server = tmp_path / 'checkout with spaces/src/server.py'
    server.parent.mkdir(parents=True)
    server.write_text('# Stub server; never executed by registration tests.\n')
    return helper, home, [sys.executable, str(server)]


def test_new_registration_is_absolute_and_repeat_does_not_touch_file(codex_mcp):
    helper, home, args = codex_mcp
    assert helper.main(args) == 0
    target = home / 'config.toml'
    content = target.read_bytes()
    assert tomllib.loads(content.decode())['mcp_servers']['Agents-Core'] == {
        'command': args[0], 'args': [args[1]]}
    stamp = target.stat().st_mtime_ns
    assert helper.main(args) == 0
    assert target.read_bytes() == content and target.stat().st_mtime_ns == stamp
    assert not list(home.glob('*.backup.*'))


def test_updates_paths_preserving_options_other_servers_and_snapshots(codex_mcp):
    helper, home, args = codex_mcp
    target = home / 'config.toml'
    original = b'''# Personal configuration\nmodel = "custom"\n[mcp_servers.other]\ncommand = "other"\n[mcp_servers."Agents-Core"]\ncommand = "old"\nargs = ["old.py"]\nstartup_timeout_sec = 90\n[mcp_servers."Agents-Core".env]\nCUSTOM = "keep"\n'''
    target.write_bytes(original)
    assert helper.main(args) == 0
    data = tomllib.loads(target.read_text())
    assert data['model'] == 'custom'
    assert data['mcp_servers']['other'] == {'command': 'other'}
    assert data['mcp_servers']['Agents-Core']['env'] == {'CUSTOM': 'keep'}
    assert data['mcp_servers']['Agents-Core']['startup_timeout_sec'] == 90
    assert b'# Personal configuration' in target.read_bytes()
    assert [p.read_bytes() for p in home.glob('config.toml.backup.*')] == [original]
    updated = target.read_bytes()
    Path(args[1] + '.new').write_text('# Alternate stub\n')
    assert helper.main([*args[:1], args[1] + '.new']) == 0
    assert sorted(p.read_bytes() for p in home.glob('config.toml.backup.*')) == sorted([original, updated])


@pytest.mark.parametrize('entry', ['url = "http://localhost:8765/mcp"\nhttp_headers = { X = "keep" }',
                                   'command = "node"\nargs = ["/old/bridge/stdio.mjs", "config"]'])
def test_preserves_existing_service_transport_without_rewriting(codex_mcp, entry):
    helper, home, args = codex_mcp
    target = home / 'config.toml'
    content = ('[mcp_servers."Agents-Core"]\n' + entry + '\n').encode()
    target.write_bytes(content)
    assert helper.main(args) == 0
    assert target.read_bytes() == content
    assert not list(home.glob('*.backup.*'))


@pytest.mark.parametrize('content', [b'broken = [', b'\xff', b'mcp_servers = 3',
                                    b'mcp_servers = { "Agents-Core" = { command = "old" } }',
                                    b'[mcp_servers]\n"Agents-Core" = 2'])
def test_invalid_or_unsupported_config_is_unchanged(codex_mcp, content, capsys):
    helper, home, args = codex_mcp
    target = home / 'config.toml'
    target.write_bytes(content)
    assert helper.main(args) == 1
    assert target.read_bytes() == content
    assert not list(home.glob('*.backup.*'))
    assert 'ERROR:' in capsys.readouterr().err


def test_failed_backup_preserves_config(codex_mcp, monkeypatch):
    helper, home, args = codex_mcp
    target = home / 'config.toml'
    target.write_bytes(b'model = "keep"\n')
    def fail(*unused):
        raise PermissionError('backup refused')
    monkeypatch.setattr(helper, 'backup_file', fail)
    assert helper.main(args) == 1
    assert target.read_bytes() == b'model = "keep"\n'


def test_failed_replace_preserves_original_and_backup(codex_mcp, monkeypatch):
    helper, home, args = codex_mcp
    target = home / 'config.toml'
    original = b'model = "keep"\n'
    target.write_bytes(original)
    def fail(*unused):
        raise PermissionError('replace refused')
    monkeypatch.setattr(helper.os, 'replace', fail)
    assert helper.main(args) == 1
    assert target.read_bytes() == original
    assert [p.read_bytes() for p in home.glob('*.backup.*')] == [original]
    assert not list(home.glob('.config.toml.*'))


def test_absent_client_has_distinct_skip_status(codex_mcp, monkeypatch):
    helper, home, args = codex_mcp
    monkeypatch.delenv('CODEX_HOME')
    monkeypatch.setattr(helper, 'detect_codex_home', lambda: None)
    assert helper.main(args) == 2
    assert not (home / 'config.toml').exists()


def test_toml_multiline_content_is_never_silently_removed(codex_mcp):
    helper, home, args = codex_mcp
    target = home / 'config.toml'
    content = b'''model = "keep"\n[mcp_servers."Agents-Core"]\ncommand = "old"\n[mcp_servers.other]\nnote = """\n[mcp_servers.'Agents-Core']\nprivate-looking text\n"""\n'''
    target.write_bytes(content)
    assert helper.main(args) == 1
    assert target.read_bytes() == content
    assert not list(home.glob('*.backup.*'))


def test_config_symlink_is_refused(codex_mcp, tmp_path):
    helper, home, args = codex_mcp
    destination = tmp_path / 'personal.toml'
    destination.write_bytes(b'model = "keep"\n')
    try:
        (home / 'config.toml').symlink_to(destination)
    except OSError as error:
        if sys.platform == 'win32' and error.winerror == 1314:
            pytest.skip('No Windows symlink creation privilege')
        raise
    assert helper.main(args) == 1
    assert destination.read_bytes() == b'model = "keep"\n'


def test_detects_default_profile_without_cli(codex_mcp, monkeypatch):
    helper, home, args = codex_mcp
    detection = importlib.import_module('scripts._helpers.install_codex_instructions')
    monkeypatch.delenv('CODEX_HOME')
    monkeypatch.setattr(Path, 'home', classmethod(lambda cls: home))
    monkeypatch.setattr(detection.shutil, 'which', lambda command: None)
    profile = home / '.codex'
    profile.mkdir()
    assert helper.main(args) == 0
    assert (profile / 'config.toml').exists()


def test_changed_config_is_not_overwritten(codex_mcp, monkeypatch):
    helper, home, args = codex_mcp
    target = home / 'config.toml'
    target.write_bytes(b'model = "original"\n')
    backup = helper.backup_file
    def edit_after_backup(path):
        result = backup(path)
        path.write_bytes(b'model = "concurrent edit"\n')
        return result
    monkeypatch.setattr(helper, 'backup_file', edit_after_backup)
    assert helper.main(args) == 1
    assert target.read_bytes() == b'model = "concurrent edit"\n'


@pytest.mark.parametrize('existing', ['', '/user/lib'])
def test_nixos_libraries_preserve_user_env_and_repeat_is_noop(codex_mcp, existing):
    helper, home, args = codex_mcp
    target = home / 'config.toml'
    if existing:
        target.write_text('[mcp_servers."Agents-Core".env]\nLD_LIBRARY_PATH = "/user/lib"\nCUSTOM = "keep"\n')
    args = [*args, '--ld-library-path', '/nix/ld/lib']
    assert helper.main(args) == 0
    environment = tomllib.loads(target.read_text())['mcp_servers']['Agents-Core']['env']
    assert environment['LD_LIBRARY_PATH'] == ('/nix/ld/lib:/user/lib' if existing else '/nix/ld/lib')
    if existing:
        assert environment['CUSTOM'] == 'keep'
    content, stamp = target.read_bytes(), target.stat().st_mtime_ns
    backups = list(home.glob('*.backup.*'))
    assert helper.main(args) == 0
    assert target.read_bytes() == content and target.stat().st_mtime_ns == stamp
    assert list(home.glob('*.backup.*')) == backups


def test_missing_server_is_refused_without_creating_config(codex_mcp):
    helper, home, args = codex_mcp
    Path(args[1]).unlink()
    assert helper.main(args) == 1
    assert not (home / 'config.toml').exists()


@pytest.fixture
def shared_codex(codex_mcp):
    helper, home, args = codex_mcp
    installation = Path(args[1]).parents[1]
    directory = installation.parent / 'shared service'
    directory.mkdir()
    (installation / 'data').mkdir()
    (installation / 'data/.shared-service.json').write_text(json.dumps({'directory': str(directory)}))
    (directory / 'service.json').write_text(json.dumps({'installation': str(installation), 'port': 8187}))
    (directory / 'token').write_text('test-only-token')
    return helper, home, args, installation, directory


@pytest.mark.parametrize('with_node', [False, True], ids=['http', 'bridge'])
@pytest.mark.parametrize('line_ending', [b'\n', b'\r\n'], ids=['lf', 'crlf'])
def test_new_codex_uses_installed_shared_service(shared_codex, with_node, line_ending, capsys):
    helper, home, args, installation, directory = shared_codex
    if with_node:
        config = json.loads((directory / 'service.json').read_text())
        config['node'] = sys.executable  # Only checked for existence; never executed.
        (directory / 'service.json').write_text(json.dumps(config))
    target = home / 'config.toml'
    original = '# Personal setting\nmodel = "keep"\n[mcp_servers.other]\ncommand = "other"\n[mcp_servers."Agents-Core"]\ncommand = "old"\nargs = ["old.py"]\nstartup_timeout_sec = 90\n'
    target.write_bytes(original.encode().replace(b'\n', line_ending))
    original_bytes = target.read_bytes()
    assert helper.main(args) == 0
    document = tomllib.loads(target.read_text())
    entry = document['mcp_servers']['Agents-Core']
    assert document['model'] == 'keep'
    assert document['mcp_servers']['other'] == {'command': 'other'}
    assert entry['startup_timeout_sec'] == 90
    if with_node:
        assert entry['command'] == sys.executable and entry['args'][0] == str(installation / 'bridge/stdio.mjs')
        bridge = json.loads(Path(entry['args'][1]).read_text())
        assert bridge['workspace'] == 'auto' and bridge['headers']['X-Agents-Client'] == 'codex'
        assert 'url' not in entry
    else:
        assert entry['url'] == 'http://127.0.0.1:8187/mcp'
        assert entry['http_headers']['X-Agents-Client'] == 'codex'
        assert 'command' not in entry and 'args' not in entry
    records = json.loads((directory / 'client-configs.json').read_text())['configs']
    assert any(record['client'] == 'codex' and record['path'] == str(target) for record in records)
    journal = json.loads(next((directory / 'backups').glob('*/changes.json')).read_text())
    assert any(record['path'] == str(target) and base64.b64decode(record['before']) == original_bytes for record in journal)
    assert 'test-only-token' not in capsys.readouterr().out


@pytest.mark.parametrize('barrier', ['control.lock', 'maintenance.json', 'transaction.json'])
def test_shared_codex_checks_admission_before_reading_service_state(shared_codex, monkeypatch, barrier):
    helper, home, args, _, directory = shared_codex
    target = home / 'config.toml'
    target.write_bytes(b'model = "keep"\n')
    if barrier != 'control.lock':
        (directory / barrier).write_text('{"operation":"busy"}')
    def unexpected_migration(*unused):
        pytest.fail('Shared service state read before admission')
    monkeypatch.setattr(helper, 'ClientMigration', unexpected_migration)
    lease = helper.file_lock(directory / barrier, blocking=False) if barrier == 'control.lock' else nullcontext()
    with lease:
        assert helper.main(args) == 1
    assert target.read_bytes() == b'model = "keep"\n'
    assert not (directory / 'backups').exists()


@pytest.mark.parametrize('broken', ['marker-json', 'marker-directory', 'service', 'token', 'service-shape'])
def test_unusable_shared_state_never_falls_back_to_standalone(shared_codex, broken, capsys):
    helper, home, args, installation, directory = shared_codex
    target = home / 'config.toml'
    target.write_bytes(b'model = "keep"\n')
    marker = installation / 'data/.shared-service.json'
    if broken == 'marker-json':
        marker.write_text('private-value = invalid json')
    elif broken == 'marker-directory':
        marker.write_text('{"directory":"relative-private-value"}')
    elif broken == 'service-shape':
        (directory / 'service.json').write_text('{"installation":"private-value"}')
    else:
        (directory / ('service.json' if broken == 'service' else 'token')).unlink()
    assert helper.main(args) == 1
    assert target.read_bytes() == b'model = "keep"\n'
    assert 'private-value' not in capsys.readouterr().err


@pytest.mark.parametrize('entry', ['url = "http://localhost:8765/mcp"',
                                  'command = "node"\nargs = ["/old/bridge/stdio.mjs", "config"]'])
def test_existing_transport_preserved_even_when_shared_state_unavailable(shared_codex, entry):
    helper, home, args, installation, _ = shared_codex
    (installation / 'data/.shared-service.json').write_text('invalid')
    target = home / 'config.toml'
    content = ('[mcp_servers."Agents-Core"]\n' + entry + '\n').encode()
    target.write_bytes(content)
    assert helper.main(args) == 0
    assert target.read_bytes() == content


def test_shared_migration_refuses_toml_that_would_lose_other_settings(shared_codex):
    helper, home, args, _, _ = shared_codex
    target = home / 'config.toml'
    content = b'''[mcp_servers.other]\nnote = """\n[mcp_servers.'Agents-Core']\nprivate-looking text\n"""\n'''
    target.write_bytes(content)
    assert helper.main(args) == 1
    assert target.read_bytes() == content


@pytest.mark.parametrize('entry', ['url = "http://localhost:5432/personal"',
                                  'command = "node"\nargs = ["/personal/bridge/stdio.mjs", "config"]'])
@pytest.mark.parametrize('initial', ['standalone', 'absent', 'empty'])
@pytest.mark.parametrize('dotdot', [False, True])
def test_shared_migration_preserves_transport_changed_since_initial_read(shared_codex, monkeypatch, entry, initial, dotdot):
    helper, home, args, _, _ = shared_codex
    if dotdot:
        (home / 'nested').mkdir()
        monkeypatch.setenv('CODEX_HOME', str(home / 'nested/..'))
    target = home / 'config.toml'
    if initial == 'standalone':
        target.write_text('[mcp_servers."Agents-Core"]\ncommand = "old"\nargs = ["old.py"]\n')
    elif initial == 'empty':
        target.write_bytes(b'')
    concurrent = ('[mcp_servers."Agents-Core"]\n' + entry + '\n').encode()
    migration_type = helper.ClientMigration
    class ConcurrentMigration(migration_type):
        def prepare(self, *args, **kwargs):
            target.write_bytes(concurrent)
            return super().prepare(*args, **kwargs)
    monkeypatch.setattr(helper, 'ClientMigration', ConcurrentMigration)
    assert helper.main(args) == 1
    assert target.read_bytes() == concurrent


@pytest.mark.parametrize('initial', [None, b'', b'model = "keep"\n'])
def test_unchanged_non_normalized_profile_can_migrate(shared_codex, monkeypatch, initial):
    helper, home, args, _, _ = shared_codex
    (home / 'nested').mkdir()
    monkeypatch.setenv('CODEX_HOME', str(home / 'nested/..'))
    target = home / 'config.toml'
    if initial is not None:
        target.write_bytes(initial)
    assert helper.main(args) == 0
    assert tomllib.loads(target.read_text())['mcp_servers']['Agents-Core']['url'] == 'http://127.0.0.1:8187/mcp'
