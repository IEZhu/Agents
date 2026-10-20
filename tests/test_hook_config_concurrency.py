"""Both hook installers refuse a client edit made after the configuration snapshot."""

import pytest

from scripts import install_session_readiness, install_thread_status


@pytest.mark.parametrize('installer,runtime_only', [(install_session_readiness, False),
    (install_thread_status, False), (install_thread_status, True)], ids=['readiness', 'status', 'status-runtime-update'])
def test_concurrent_hook_config_edit_is_preserved(tmp_path, monkeypatch, installer, runtime_only):
    target = tmp_path / 'profile/hooks.json'
    script = tmp_path / 'profile/hooks' / installer.SCRIPT_NAME
    target.parent.mkdir()
    target.write_text('{}')
    monkeypatch.setattr(installer, 'client_paths', lambda client: (target, script))
    if runtime_only:
        installer.install('codex')
        script.write_bytes(b'# Old runtime requiring update\n')
    previous_runtime = script.read_bytes() if script.exists() else None
    backup = installer.backup_file
    concurrent = b'{"hooks":{},"concurrent":"keep this edit"}'
    def change_after_backup(path):
        result = backup(path)
        target.write_bytes(concurrent)
        return result
    monkeypatch.setattr(installer, 'backup_file', change_after_backup)
    with pytest.raises(ValueError, match='changed during setup'):
        installer.install('codex')
    assert target.read_bytes() == concurrent
    assert (script.read_bytes() if script.exists() else None) == previous_runtime


@pytest.mark.parametrize('installer', [install_session_readiness, install_thread_status], ids=['readiness', 'status'])
def test_edit_after_runtime_copy_is_not_overwritten(tmp_path, monkeypatch, installer):
    target = tmp_path / 'profile/hooks.json'
    script = target.parent / 'hooks' / installer.SCRIPT_NAME
    target.parent.mkdir()
    target.write_text('{}')
    monkeypatch.setattr(installer, 'client_paths', lambda client: (target, script))
    write = installer.atomic_write
    concurrent = b'{"personal":"newer client edit"}'
    def edit_after_runtime_copy(path, content):
        write(path, content)
        if path != target:
            target.write_bytes(concurrent)
    monkeypatch.setattr(installer, 'atomic_write', edit_after_runtime_copy)
    with pytest.raises(ValueError, match='changed during setup'):
        installer.install('codex')
    assert target.read_bytes() == concurrent
