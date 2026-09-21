import json
from pathlib import Path
import subprocess
import tomllib

import pytest

from src.daemon.clients import ClientMigration, replace_toml_server
from src.daemon.state import write_json, atomic_private


@pytest.fixture
def migration(tmp_path):
    state = tmp_path / "service"; state.mkdir(mode=0o700)
    write_json(state / "service.json", {"installation": str(tmp_path), "port": 8765, "node": "/usr/bin/true"})
    atomic_private(state / "token", "private-token-for-tests-only" * 2)
    return ClientMigration(state)


def test_toml_replaces_transport_and_preserves_other_servers():
    original = '''# User settings
model = "test"
[mcp_servers."Agents-Core"]
command = "python"
args = ["server.py"]
[mcp_servers."Agents-Core".env]
KEY = "value"
[mcp_servers.other]
command = "safe"
'''
    result = replace_toml_server(original, "Agents-Core", {"url": "http://127.0.0.1:8765/mcp", "http_headers": {"Authorization": "secret"}})
    parsed = tomllib.loads(result)
    assert parsed["mcp_servers"]["other"] == {"command": "safe"}
    assert "env" not in parsed["mcp_servers"]["Agents-Core"]
    assert "command" not in parsed["mcp_servers"]["Agents-Core"]
    assert result.startswith("# User settings")


def test_migration_backup_and_conflicting_restore(migration, tmp_path):
    home = tmp_path / "home"; home.mkdir()
    project = tmp_path / "repo"; project.mkdir()
    original = {"mcpServers": {"other": {"command": "safe"}}, "projects": {str(project): {"otherPolicy": True}}}
    write_json(home / ".claude.json", original)
    change = migration.prepare("claude", project, home=home)
    backup = migration.apply([change])
    path = change[0]
    result = json.loads(path.read_text())
    assert result["projects"][str(project)]["otherPolicy"]
    assert result["projects"][str(project)]["mcpServers"]["Agents-Core"]["type"] == "http"
    assert path.stat().st_mode & 0o777 == 0o600
    assert (backup / "changes.json").stat().st_mode & 0o777 == 0o600
    migration.restore(backup)
    assert json.loads(path.read_text()) == original
    with pytest.raises(ValueError, match="changed"): migration.restore(backup)


def test_tracked_codex_uses_string_helper_without_secret(migration, tmp_path):
    root = tmp_path / "tracked"; root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    path = root / ".codex/config.toml"; path.parent.mkdir()
    path.write_text('[mcp_servers."Agents-Core"]\ncommand="python"\n')
    subprocess.run(["git", "-C", str(root), "add", ".codex/config.toml"], check=True)
    _, text, secret = migration.prepare("codex", root)
    entry = tomllib.loads(text)["mcp_servers"]["Agents-Core"]
    assert isinstance(entry["http_headers_helper"], str)
    assert migration.token not in text
    assert not secret


def test_prepared_callback_runs_before_writes(migration, tmp_path):
    target = tmp_path / "config.json"
    target.write_text("original")
    def callback(backup):
        assert target.read_text() == "original"
        assert (backup / "changes.json").exists()
        raise RuntimeError("simulated controller crash before mutation")
    with pytest.raises(RuntimeError):
        migration.apply([(target, "new", False)], on_prepared=callback)
    assert target.read_text() == "original"


def test_changed_configuration_is_rejected_before_migration(migration, tmp_path):
    home = tmp_path / "home"; home.mkdir()
    change = migration.prepare("claude", home=home)
    path = home / ".claude.json"
    path.write_text('{"userChanged": true}')
    with pytest.raises(ValueError, match="changed after preparation"):
        migration.apply([change])
    assert json.loads(path.read_text()) == {"userChanged": True}


def test_restore_preserves_original_bytes_and_restores_remaining_files(migration, tmp_path):
    first = tmp_path / "non-utf8.json"
    second = tmp_path / "second.json"
    first.write_bytes(b"\xfforiginal bytes")
    second.write_bytes(b"second original")
    backup = migration.apply([(first, "{}", False), (second, "{}", False)])

    migration.restore(backup)

    assert first.read_bytes() == b"\xfforiginal bytes"
    assert second.read_bytes() == b"second original"
