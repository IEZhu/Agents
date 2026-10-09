"""Alternate client profiles must not silently keep a standalone model process."""
import json
from pathlib import Path
import sys
import tomllib

import pytest

from src.daemon.clients import ClientMigration
from src.daemon.audit import inventory
from src.daemon.state import atomic_private, write_json
from src.client_paths import client_config_path, client_home


@pytest.fixture
def setup(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    for key in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "AGENTS_CURSOR_MCP_CONFIG", "AGENTS_CLAUDE_DESKTOP_CONFIG"):
        monkeypatch.delenv(key, raising=False)
    state = tmp_path / "state"
    write_json(state / "service.json", {"installation": str(tmp_path), "port": 8765, "node": "/usr/bin/true"})
    atomic_private(state / "token", "test-profile-token")
    return home, state, ClientMigration(state)


@pytest.mark.parametrize("client,variable,filename", [
    ("claude", "CLAUDE_CONFIG_DIR", ".claude.json"),
    ("codex", "CODEX_HOME", "config.toml"),
])
def test_profile_migration_changes_selected_file_only(setup, tmp_path, monkeypatch, client, variable, filename):
    home, state, migration = setup
    profile = tmp_path / "alternate profile"
    profile.mkdir()
    target = profile / filename
    default = home / (".claude.json" if client == "claude" else ".codex/config.toml")
    original = '{"mcpServers":{"Agents-Core":{"command":"python","args":["server.py"]},"other":{"command":"safe"}}}' if client == "claude" else '[mcp_servers."Agents-Core"]\ncommand="python"\n[mcp_servers.other]\ncommand="safe"\n'
    atomic_private(default, original)
    atomic_private(target, original)
    monkeypatch.setenv(variable, str(profile))
    # With a recorded Node the user scope gets the bridge that registers each session's project (#253).
    migration.config["node"] = sys.executable
    change = migration.prepare(client)
    assert change[0] == target
    backup = migration.apply([change])
    assert default.read_text() == original
    result = json.loads(target.read_text()) if client == "claude" else tomllib.loads(target.read_text())
    servers = result["mcpServers" if client == "claude" else "mcp_servers"]
    assert servers["Agents-Core"]["command"] == sys.executable
    assert servers["Agents-Core"]["args"][0].endswith("stdio.mjs")
    assert "url" not in servers["Agents-Core"]
    assert servers["other"] == {"command": "safe"}
    assert target.stat().st_mode & 0o777 == 0o600
    migration.restore(backup)
    assert target.read_text() == original


def test_audit_includes_standard_and_environment_profiles(setup, tmp_path, monkeypatch):
    home, state, migration = setup
    alternate = tmp_path / "claude-alt"
    write_json(home / ".claude.json", {"mcpServers": {"Agents-Core": {"url": migration.url}}})
    write_json(alternate / ".claude.json", {"mcpServers": {"Agents-Core": {"command": "python", "env": {"SECRET": "must-not-print"}}}})
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(alternate))
    rows = inventory()
    assert {row["transport"] for row in rows if row.get("agents_core")} == {"http", "stdio"}
    assert "must-not-print" not in json.dumps(rows)


@pytest.mark.parametrize("client", ["codex", "claude", "cursor", "desktop", "claude-project", "claude-deny-desktop"])
def test_exact_file_override_for_each_client_is_backed_up_and_remembered(setup, tmp_path, client):
    home, state, migration = setup
    project = tmp_path / "project"
    project.mkdir()
    target = tmp_path / "custom" / ("work.config.toml" if client == "codex" else "nonstandard.json")
    original = '# Keep this comment\n' if client == "codex" else '{"unrelated":true}'
    atomic_private(target, original)
    change = migration.prepare(client, project, config_path=target)
    backup = migration.apply([change])
    registered = json.loads((state / "client-configs.json").read_text())["configs"]
    assert {"client": client, "path": str(target), "workspace": str(project)} in registered
    assert target.read_text() != original
    if client != "claude-deny-desktop":
        assert any(row["path"] == str(target) and row.get("agents_core") for row in inventory(directory=state))
    migration.restore(backup)
    assert target.read_text() == original
    assert (state / "client-configs.json").exists()


@pytest.mark.parametrize("client,variable", [("cursor", "AGENTS_CURSOR_MCP_CONFIG"), ("desktop", "AGENTS_CLAUDE_DESKTOP_CONFIG")])
def test_exact_file_environment_can_be_overridden(setup, tmp_path, monkeypatch, client, variable):
    _, _, migration = setup
    selected = tmp_path / "selected.json"
    explicit = tmp_path / "explicit.json"
    monkeypatch.setenv(variable, str(selected))
    assert migration.prepare(client)[0] == selected
    assert migration.prepare(client, config_path=explicit)[0] == explicit


def test_workspace_configs_ignore_global_locations_but_keep_explicit_paths(setup, tmp_path, monkeypatch):
    _, _, migration = setup
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex-alt"))
    monkeypatch.setenv("AGENTS_CURSOR_MCP_CONFIG", str(tmp_path / "cursor-alt.json"))
    assert migration.prepare("codex", project)[0] == project / ".codex/config.toml"
    assert migration.prepare("cursor", project)[0] == project / ".cursor/mcp.json"
    assert migration.prepare("codex", project, config_path=tmp_path / "custom.toml")[0] == tmp_path / "custom.toml"


@pytest.mark.parametrize("override", [None, "", "~/.claude", "~/profile with spaces"])
def test_claude_config_home_distinguishes_explicit_default_directory(setup, monkeypatch, override):
    home, _, _ = setup
    monkeypatch.setenv("HOME", str(home))
    if override is not None:
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", override)
    expected = Path(override).expanduser() if override else home / ".claude"
    assert client_home("claude") == expected
    assert client_config_path("claude") == (expected / ".claude.json" if override else home / ".claude.json")
    assert client_config_path("claude-deny-desktop") == expected / "settings.json"


def test_audit_follows_alternate_project_and_plugin_scopes_and_named_codex_profiles(setup, tmp_path, monkeypatch):
    _, _, _ = setup
    claude = tmp_path / "claude-alt"
    codex = tmp_path / "codex-alt"
    project = tmp_path / "alternate-only-project"
    project.mkdir()
    write_json(claude / ".claude.json", {"projects": {str(project): {"mcpServers": {"local": {"command": "node"}}}}})
    write_json(project / ".mcp.json", {"mcpServers": {"project": {"command": "node"}}})
    for path in (claude, codex):
        write_json(path / "plugins/cache/example/.mcp.json", {"mcpServers": {path.name: {"command": "python"}}})
    atomic_private(codex / "work.config.toml", '[mcp_servers."Agents-Core"]\ncommand="python"\n')
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(claude))
    monkeypatch.setenv("CODEX_HOME", str(codex))
    rows = inventory()
    assert {"local", "project", "claude-alt", "codex-alt", "Agents-Core"} <= {row.get("server") for row in rows}
    assert any(row["scope"] == "codex:profile" and row["transport"] == "stdio" for row in rows)


def test_remembered_profile_is_audited_after_environment_disappears(setup, tmp_path, monkeypatch):
    home, state, migration = setup
    profile = tmp_path / "claude-alt"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
    migration.apply([migration.prepare("claude")])
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")
    assert any(row["path"] == str(profile / ".claude.json") for row in inventory(directory=state))
    # A later default-profile migration retains the earlier alternate registration.
    second = ClientMigration(state)
    second.apply([second.prepare("claude")])
    rows = inventory(directory=state)
    assert {str(home / ".claude.json"), str(profile / ".claude.json")} <= {row["path"] for row in rows}


def test_custom_file_concurrent_edit_and_symlink_are_not_overwritten(setup, tmp_path):
    _, state, migration = setup
    target = tmp_path / "custom.json"
    target.write_text('{}')
    change = migration.prepare("cursor", config_path=target)
    target.write_text('{"changed":true}')
    with pytest.raises(ValueError, match="changed after preparation"):
        migration.apply([change])
    assert target.read_text() == '{"changed":true}'
    assert not (state / "client-configs.json").exists()
    alias = tmp_path / "alias.json"
    alias.symlink_to(target)
    change = migration.prepare("cursor", config_path=alias)
    with pytest.raises(ValueError, match="symlink"):
        migration.apply([change])
    assert target.read_text() == '{"changed":true}'


def test_internal_inventory_cannot_be_selected_as_a_client_file(setup):
    _, state, migration = setup
    with pytest.raises(ValueError, match="collides"):
        migration.apply([migration.prepare("cursor", config_path=state / "client-configs.json")])


def test_audit_reports_invalid_known_registry_and_keeps_other_files(setup):
    home, state, migration = setup
    write_json(state / "client-configs.json", {"version": 1, "configs": [{"path": "relative"}]})
    write_json(home / ".claude.json", {"mcpServers": {"other": {"command": "safe"}}})
    rows = inventory(directory=state)
    assert any(row.get("error") and row["scope"] == "managed" for row in rows)
    assert any(row.get("server") == "other" for row in rows)


def test_cli_migration_and_audit_forward_explicit_paths_and_workspace(setup, tmp_path, capsys):
    from src.daemon.control import main
    _, state, _ = setup
    project = tmp_path / "project"
    project.mkdir()
    target = tmp_path / "custom.json"
    main(["--state", str(state), "migrate", "--clients", "claude", "--workspace", str(project), "--client-config", f"claude={target}"])
    capsys.readouterr()
    entry = json.loads(target.read_text())["projects"][str(project)]["mcpServers"]["Agents-Core"]
    assert entry["headers"]["X-Agents-Workspace"]
    main(["--state", str(state), "audit", "--workspace", str(project), "--client-config", f"claude={target}", "--client-config", f"claude={target}"])
    rows = json.loads(capsys.readouterr().out)
    assert len([row for row in rows if row["path"] == str(target)]) == 1


@pytest.mark.parametrize("args", [
    ["--clients", "claude", "--client-config", "codex=/unused"],
    ["--clients", "unknown"],
    ["--clients", "claude", "--client-config", "claude=/one", "--client-config", "claude=/two"],
    ["--clients", "claude", "--client-config", "claude="],
])
def test_invalid_cli_selection_fails_before_client_writes(setup, args):
    from src.daemon.control import main
    home, state, _ = setup
    with pytest.raises(SystemExit):
        main(["--state", str(state), "migrate", *args])
    assert not (home / ".claude.json").exists()
    assert not (state / "backups").exists()


@pytest.mark.parametrize("client,name", [("codex", "codex.json"), ("cursor", "cursor.toml"), ("desktop", "desktop.toml")])
def test_audit_uses_client_type_for_arbitrary_filenames(setup, tmp_path, client, name):
    _, state, migration = setup
    target = tmp_path / name
    migration.apply([migration.prepare(client, config_path=target)])
    rows = inventory(directory=state)
    assert any(row["path"] == str(target) and row.get("agents_core") for row in rows)
    assert not any(row.get("error") for row in rows)


def test_audit_reports_symlink_loop_and_continues(setup, tmp_path):
    home, state, _ = setup
    write_json(home / ".claude.json", {"mcpServers": {"other": {"command": "safe"}}})
    loop = tmp_path / "loop.json"
    loop.symlink_to(loop)
    rows = inventory(client_configs=[("cursor", loop)], directory=state)
    assert any(row["path"] == str(loop) and row.get("error") for row in rows)
    assert any(row.get("server") == "other" for row in rows)


def test_registry_write_failure_rolls_back_client_and_preserves_inventory(setup, tmp_path, monkeypatch):
    from src.daemon import clients
    _, state, migration = setup
    migration.apply([migration.prepare("claude")])
    registry = state / "client-configs.json"
    original_registry = registry.read_bytes()
    target = tmp_path / "alternate.json"
    target.write_text('{"mcpServers":{"Agents-Core":{"command":"python"}}}')
    original = target.read_bytes()
    second = ClientMigration(state)
    change = second.prepare("cursor", config_path=target)
    original_write = clients.atomic_private
    def fail_registry(path, content):
        if path == registry:
            raise OSError("simulated inventory write failure")
        return original_write(path, content)
    monkeypatch.setattr(clients, "atomic_private", fail_registry)
    with pytest.raises(OSError, match="inventory write failure"):
        second.apply([change])
    assert target.read_bytes() == original
    assert registry.read_bytes() == original_registry


def test_later_profile_migration_does_not_block_restoring_earlier_backup(setup, tmp_path):
    _, state, migration = setup
    first, second = tmp_path / "first.json", tmp_path / "second.json"
    first.write_text('{"mcpServers":{"Agents-Core":{"command":"python"}}}')
    original = first.read_bytes()
    backup = migration.apply([migration.prepare("cursor", config_path=first)])
    later = ClientMigration(state)
    later.apply([later.prepare("cursor", config_path=second)])
    later.restore(backup)
    assert first.read_bytes() == original
    assert json.loads(second.read_text())["mcpServers"]["Agents-Core"]["url"]
    assert any(row["path"] == str(first) and row.get("transport") == "stdio" for row in inventory(directory=state))
