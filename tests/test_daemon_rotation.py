import json
from pathlib import Path

import pytest

from src.daemon.clients import ClientMigration
from src.daemon.rotation import rotate_token
from src.daemon.state import atomic_private, write_json
from tests.test_daemon_update import installation


@pytest.mark.parametrize("fail_ready", [False, True])
@pytest.mark.parametrize("initial_state", ["ready", "starting", "draining", "stopped"])
def test_rotation_is_coherent_or_restores_token_and_clients(installation, tmp_path, monkeypatch, fail_ready, initial_state):
    controller, _, _, _ = installation
    old = "old-private-token-" * 4
    write_json(controller.directory / "service.json", controller.config)
    atomic_private(controller.directory / "token", old + "\n")
    target = tmp_path / "client.json"
    content = json.dumps({"headers": {"Authorization": "Bearer " + old}})
    ClientMigration(controller.directory).apply([(target, content, True)])
    controller.running = initial_state != "stopped"
    monkeypatch.setattr(controller, "status", lambda: {"state": initial_state})
    controller.fail_ready = 1 if fail_ready else 0
    should_rollback = fail_ready and controller.running
    if should_rollback:
        with pytest.raises(RuntimeError, match="warmup"):
            rotate_token(controller)
    else:
        assert rotate_token(controller)["state"] == "rotated"
    token = (controller.directory / "token").read_text().strip()
    assert (token == old) == should_rollback
    assert json.loads(target.read_text())["headers"]["Authorization"] == "Bearer " + token
    assert controller.running == (initial_state != "stopped")
    assert not (controller.directory / "transaction.json").exists()
    assert not (controller.directory / "maintenance.json").exists()


def test_repeated_rotation_updates_token_and_managed_client(installation, tmp_path):
    controller, _, _, _ = installation
    old = "old-private-token-" * 4
    write_json(controller.directory / "service.json", controller.config)
    token_path = controller.directory / "token"
    atomic_private(token_path, old + "\n")
    target = tmp_path / "client.json"
    content = json.dumps({"headers": {"Authorization": "Bearer " + old}})
    ClientMigration(controller.directory).apply([(target, content, True)])
    tokens = {old}

    for _ in range(2):
        assert rotate_token(controller) == {"state": "rotated", "files": 2}
        token = token_path.read_text().strip()
        assert token not in tokens
        tokens.add(token)
        assert json.loads(target.read_text())["headers"]["Authorization"] == "Bearer " + token
        assert controller.running
        assert not (controller.directory / "transaction.json").exists()
        assert not (controller.directory / "maintenance.json").exists()


@pytest.mark.parametrize("project_scoped", [False, True])
def test_custom_profile_rotation_survives_removed_environment(installation, tmp_path, monkeypatch, project_scoped):
    controller, _, _, _ = installation
    controller.config["port"] = 8765
    write_json(controller.directory / "service.json", controller.config)
    atomic_private(controller.directory / "token", "original-profile-token")
    profile = tmp_path / "alternate-claude"
    project = tmp_path / "repo" if project_scoped else None
    if project:
        project.mkdir()
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(profile))
    migration = ClientMigration(controller.directory)
    migration.apply([migration.prepare("claude", project)])
    config_path = profile / ".claude.json"

    def profile_entry():
        document = json.loads(config_path.read_text())
        scope = document["projects"][str(project.resolve())] if project else document
        return scope["mcpServers"]["Agents-Core"]

    before = profile_entry()
    bridge_path = Path(before["args"][1]) if project is None else None
    monkeypatch.delenv("CLAUDE_CONFIG_DIR")

    assert rotate_token(controller)["state"] == "rotated"

    token = (controller.directory / "token").read_text().strip()
    entry = profile_entry()
    assert token != "original-profile-token"
    if project:
        assert entry["headers"]["Authorization"] == "Bearer " + token
        assert entry["headers"]["X-Agents-Workspace"] == before["headers"]["X-Agents-Workspace"]
        assert entry["url"] == before["url"]
    else:
        assert entry == before
        assert "headers" not in entry and "url" not in entry
        assert token not in config_path.read_text()
        settings = json.loads(bridge_path.read_text())
        assert settings["headers"]["Authorization"] == "Bearer " + token
        assert settings["workspace"] == "auto"
        assert settings["url"] == "http://127.0.0.1:8765/mcp"
        assert settings["headers"]["X-Agents-Client"] == "claude-code"
        assert "X-Agents-Workspace" not in settings["headers"]


def test_rotation_rewrites_the_stdio_bridge_configuration(installation):
    """#266: a stdio server of the installation reaches the service with the rotated token."""
    import sys
    controller, _, _, _ = installation
    controller.config.update(node=sys.executable, port=8765)
    write_json(controller.directory / "service.json", controller.config)
    atomic_private(controller.directory / "token", "old-private-token-" * 4 + "\n")
    path = ClientMigration(controller.directory).stdio_bridge()
    assert rotate_token(controller)["state"] == "rotated"
    token = (controller.directory / "token").read_text().strip()
    assert json.loads(path.read_text())["headers"]["Authorization"] == "Bearer " + token
