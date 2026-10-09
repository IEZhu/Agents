import json
import os
from pathlib import Path
import subprocess
import sys
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


def test_each_client_names_its_app_in_its_headers_and_bridge(migration, tmp_path):
    """The daemon counts requests per app (#187); one bridge file per app keeps their headers apart."""
    home = tmp_path / "home"; home.mkdir()
    migration.config["node"] = sys.executable
    _, text, _ = migration.prepare("cursor", home=home)
    assert json.loads(text)["mcpServers"]["Agents-Core"]["headers"]["X-Agents-Client"] == "cursor"
    _, text, _ = migration.prepare("claude", home=home)
    bridge = Path(json.loads(text)["mcpServers"]["Agents-Core"]["args"][1])
    assert json.loads(bridge.read_text())["headers"]["X-Agents-Client"] == "claude-code"
    _, text, _ = migration.prepare("codex", home=home)
    bridge = Path(tomllib.loads(text)["mcp_servers"]["Agents-Core"]["args"][1])
    assert json.loads(bridge.read_text())["headers"]["X-Agents-Client"] == "codex"
    _, text, _ = migration.prepare("desktop", home=home)
    bridge = Path(json.loads(text)["mcpServers"]["Agents-Core-Desktop"]["args"][1])
    assert bridge.name == "claude-desktop-routing.json"
    assert json.loads(bridge.read_text())["headers"]["X-Agents-Client"] == "claude-desktop"


def test_user_scope_claude_gets_a_bridge_that_names_each_session_project(migration, tmp_path):
    """#253: the user scope serves every project, so a bridge per session registers its own;
    no project needs `migrate --workspace` first."""
    home = tmp_path / "home"; home.mkdir()
    migration.config["node"] = sys.executable
    _, text, secret = migration.prepare("claude", home=home)
    entry = json.loads(text)["mcpServers"]["Agents-Core"]
    assert entry["command"] == sys.executable and entry["args"][0].endswith("stdio.mjs")
    assert "url" not in entry and "headers" not in entry
    bridge = Path(entry["args"][1])
    assert bridge.name == "claude-code-auto.json"
    settings = json.loads(bridge.read_text())
    assert settings["workspace"] == "auto" and "X-Agents-Workspace" not in settings["headers"]
    assert not secret and migration.token not in text
    # A pinned project keeps its fixed identity over HTTP.
    project = tmp_path / "repo"; project.mkdir()
    _, text, _ = migration.prepare("claude", project, home=home)
    pinned = json.loads(text)["projects"][str(project.resolve())]["mcpServers"]["Agents-Core"]
    assert pinned["type"] == "http" and pinned["headers"]["X-Agents-Workspace"]


def test_user_scope_claude_without_node_stays_http(migration, tmp_path):
    home = tmp_path / "home"; home.mkdir()
    migration.config["node"] = str(tmp_path / "missing-node")
    _, text, _ = migration.prepare("claude", home=home)
    entry = json.loads(text)["mcpServers"]["Agents-Core"]
    assert entry["type"] == "http" and "X-Agents-Workspace" not in entry["headers"]
    _, text, _ = migration.prepare("codex", home=home)
    entry = tomllib.loads(text)["mcp_servers"]["Agents-Core"]
    assert entry["url"].endswith("/mcp") and "X-Agents-Workspace" not in entry["http_headers"]


def test_user_scope_codex_gets_the_same_bridge(migration, tmp_path):
    """#253: Codex starts a stdio server without `cwd` in the session's working directory."""
    home = tmp_path / "home"; home.mkdir()
    (home / ".codex").mkdir()
    (home / ".codex/config.toml").write_text(
        'model = "test"\n[mcp_servers."Agents-Core"]\nurl = "http://127.0.0.1:8765/mcp"\ncwd = "/elsewhere"\n'
        '[mcp_servers."Agents-Core".http_headers]\nAuthorization = "Bearer old"\n[mcp_servers.other]\ncommand = "safe"\n')
    migration.config["node"] = sys.executable
    _, text, secret = migration.prepare("codex", home=home)
    parsed = tomllib.loads(text)
    entry = parsed["mcp_servers"]["Agents-Core"]
    assert entry["command"] == sys.executable and entry["args"][0].endswith("stdio.mjs")
    # A fixed cwd would start every session's bridge in the same directory.
    assert not {"url", "http_headers", "cwd"} & set(entry)
    assert parsed["model"] == "test" and parsed["mcp_servers"]["other"] == {"command": "safe"}
    settings = json.loads(Path(entry["args"][1]).read_text())
    assert Path(entry["args"][1]).name == "codex-auto.json"
    assert settings["workspace"] == "auto" and settings["headers"]["X-Agents-Client"] == "codex"
    assert not secret and migration.token not in text
    # A project's own .codex/config.toml keeps its fixed identity over HTTP.
    project = tmp_path / "repo"; project.mkdir()
    _, text, _ = migration.prepare("codex", project, home=home)
    pinned = tomllib.loads(text)["mcp_servers"]["Agents-Core"]
    assert pinned["url"].endswith("/mcp") and pinned["http_headers"]["X-Agents-Workspace"]


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



def test_conftest_clears_inherited_client_config_overrides(tmp_path):
    # An inherited CLAUDE_CONFIG_DIR made migration tests rewrite the developer's
    # real Claude profile. Import conftest in a child that inherits all four.
    overrides = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "AGENTS_CURSOR_MCP_CONFIG", "AGENTS_CLAUDE_DESKTOP_CONFIG")
    env = {**os.environ, **{name: str(tmp_path / "inherited" / name) for name in overrides}}
    home = tmp_path / "home"
    code = (
        "import json, os, sys\n"
        "import tests.conftest\n"
        "from src.client_paths import client_config_path\n"
        "print(json.dumps({'left': [n for n in sys.argv[1:] if n in os.environ],"
        " 'claude': str(client_config_path('claude', home=os.environ['TEST_HOME']))}))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, *overrides], cwd=Path(__file__).resolve().parents[1],
        env={**env, "TEST_HOME": str(home)}, capture_output=True, text=True, check=True,
    )
    report = json.loads(result.stdout.strip().splitlines()[-1])
    assert report == {"left": [], "claude": str(home / ".claude.json")}
