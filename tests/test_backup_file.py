"""Verify unique backups and cleanup without touching live client files."""
import importlib.util
import os
from pathlib import Path
import stat
import subprocess
import sys

import pytest


HELPER = Path(__file__).resolve().parents[1] / "scripts/_helpers/backup_file.py"
spec = importlib.util.spec_from_file_location("backup_file_helper", HELPER)
helper = importlib.util.module_from_spec(spec)
spec.loader.exec_module(helper)


def test_repeated_backups_preserve_bytes_and_prior_snapshot(tmp_path):
    config = tmp_path / "client !profile!" / "mcp.json"
    config.parent.mkdir()
    original = b'{"mcpServers": {}}\r\n'
    config.write_bytes(original)
    prior = config.with_name(config.name + ".backup.1700000000")
    prior.write_bytes(b"Earlier backup\r\n")

    first = helper.backup_file(config)
    second = helper.backup_file(config)

    assert first != second and first != prior and second != prior
    assert first.parent == second.parent == config.parent
    assert first.read_bytes() == second.read_bytes() == config.read_bytes() == original
    assert prior.read_bytes() == b"Earlier backup\r\n"
    assert set(config.parent.glob(config.name + ".backup.*")) == {prior, first, second}
    if os.name != "nt":
        assert stat.S_IMODE(first.stat().st_mode) == 0o600
        assert stat.S_IMODE(second.stat().st_mode) == 0o600


def test_copy_failure_removes_partial_backup_and_preserves_existing_files(tmp_path, monkeypatch):
    config = tmp_path / "mcp.json"
    original = b'{"mcpServers": {}}\r\n'
    config.write_bytes(original)
    prior = config.with_name(config.name + ".backup.1700000000")
    prior.write_bytes(b"Earlier backup\r\n")

    def fail_after_partial_copy(source, destination):
        destination.write(source.read(3))
        raise OSError("simulated copy failure")

    monkeypatch.setattr(helper.shutil, "copyfileobj", fail_after_partial_copy)

    with pytest.raises(OSError, match="simulated copy failure"):
        helper.backup_file(config)

    assert config.read_bytes() == original
    assert prior.read_bytes() == b"Earlier backup\r\n"
    assert set(config.parent.glob(config.name + ".backup.*")) == {prior}


def test_missing_source_does_not_reserve_a_backup(tmp_path):
    with pytest.raises(FileNotFoundError):
        helper.backup_file(tmp_path / "missing.json")
    assert not list(tmp_path.iterdir())


def test_cli_reports_created_backup(tmp_path):
    config = tmp_path / "config with spaces.json"
    config.write_bytes(b"Original bytes\r\n")

    result = subprocess.run([sys.executable, str(HELPER), str(config)],
                            capture_output=True, text=True, timeout=10)

    assert result.returncode == 0, result.stdout + result.stderr
    backup = Path(result.stdout.strip().removeprefix("Backup created: "))
    assert backup.read_bytes() == config.read_bytes() == b"Original bytes\r\n"


def test_cli_reports_backup_failure(tmp_path):
    config = tmp_path / "missing.json"

    result = subprocess.run([sys.executable, str(HELPER), str(config)],
                            capture_output=True, text=True, timeout=10)

    assert result.returncode == 1
    assert "ERROR: Failed to back up" in result.stderr
    assert str(config) in result.stderr
    assert not list(tmp_path.iterdir())
