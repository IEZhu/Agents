import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tomllib

import pytest

from src.daemon.clients import ClientMigration, replace_toml_server
from src.daemon.state import write_json, atomic_private


ROOT = Path(__file__).resolve().parents[1]
ROUTING_BEGIN = b"# >>> Agents-Core Routing Protocol (managed by init_repo) >>>"
ROUTING_END = b"# <<< Agents-Core Routing Protocol (managed by init_repo) <<<"


@pytest.fixture
def migration(tmp_path):
    state = tmp_path / "service"; state.mkdir(mode=0o700)
    template = tmp_path / "scripts/templates/routing-protocol-core.md"
    template.parent.mkdir(parents=True)
    shutil.copyfile(ROOT / "scripts/templates/routing-protocol-core.md", template)
    # Any existing absolute file stands in for Node: the bridge entry only names it.
    write_json(state / "service.json", {"installation": str(tmp_path), "port": 8765, "node": sys.executable})
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
    if os.name == "posix":
        assert path.stat().st_mode & 0o777 == 0o600
        assert (backup / "changes.json").stat().st_mode & 0o777 == 0o600
    else:  # no mode bits: the backup directory gets an owner-only DACL, the client file keeps its own
        from src.daemon.bootstrap import _private_file
        assert _private_file(backup / "changes.json")
    migration.restore(backup)
    assert json.loads(path.read_text()) == original
    with pytest.raises(ValueError, match="changed"): migration.restore(backup)


def test_tracked_codex_gets_a_pinned_bridge_without_secret(migration, tmp_path):
    root = tmp_path / "tracked"; root.mkdir()
    subprocess.run(["git", "init", str(root)], check=True, capture_output=True)
    path = root / ".codex/config.toml"; path.parent.mkdir()
    path.write_text('[mcp_servers."Agents-Core"]\ncommand="python"\n')
    subprocess.run(["git", "-C", str(root), "add", ".codex/config.toml"], check=True)
    home = tmp_path / "home"; (home / ".codex").mkdir(parents=True)
    _, text, secret = migration.prepare("codex", root, home=home)
    entry = tomllib.loads(text)["mcp_servers"]["Agents-Core"]
    assert entry["args"][0].endswith("stdio.mjs") and "http_headers_helper" not in entry
    assert json.loads(Path(entry["args"][1]).read_text())["headers"]["X-Agents-Workspace"]
    assert migration.token not in text
    assert not secret
    # Under a user layer that is still HTTP it stays HTTP, and the helper keeps the token out.
    (home / ".codex/config.toml").write_text('[mcp_servers."Agents-Core"]\nurl = "http://127.0.0.1:8765/mcp"\n')
    _, text, secret = migration.prepare("codex", root, home=home)
    entry = tomllib.loads(text)["mcp_servers"]["Agents-Core"]
    assert entry["url"].endswith("/mcp") and isinstance(entry["http_headers_helper"], str)
    assert migration.token not in text and not secret


def codex_merge(user, project):
    """The entry Codex runs: it merges a server's table field by field across its layers,
    a project's .codex/config.toml over the user's."""
    return {**user, **project}


def test_codex_layers_merge_into_one_transport(migration, tmp_path):
    """#292: a stdio user layer merged with an HTTP project layer is an entry Codex refuses
    ("url is not supported for stdio"), and every thread of that project stops resuming."""
    home = tmp_path / "home"; (home / ".codex").mkdir(parents=True)
    project = tmp_path / "repo"; project.mkdir()
    for node in (sys.executable, str(tmp_path / "missing-node")):
        migration.config["node"] = node
        user = tomllib.loads(migration.prepare("codex", home=home)[1])["mcp_servers"]["Agents-Core"]
        pinned = tomllib.loads(migration.prepare("codex", project, home=home)[1])["mcp_servers"]["Agents-Core"]
        merged = codex_merge(user, pinned)
        if node == sys.executable:
            assert {"command", "args"} <= set(merged)
            assert not {"url", "http_headers", "http_headers_helper"} & set(merged)
            settings = json.loads(Path(merged["args"][1]).read_text())
            assert settings["headers"]["X-Agents-Workspace"] and "workspace" not in settings
        else:  # without Node every layer stays HTTP
            assert merged["url"].endswith("/mcp") and "command" not in merged
            assert merged["http_headers"]["X-Agents-Workspace"]


def test_a_codex_project_layer_takes_the_transport_of_the_user_layer(migration, tmp_path):
    """#292: `migrate --workspace` must not put a bridge under a user layer that is still HTTP."""
    home = tmp_path / "home"; (home / ".codex").mkdir(parents=True)
    (home / ".codex/config.toml").write_text(
        '[mcp_servers."Agents-Core"]\nurl = "http://127.0.0.1:8765/mcp"\n')
    project = tmp_path / "repo"; project.mkdir()
    migration.config["node"] = sys.executable
    pinned = tomllib.loads(migration.prepare("codex", project, home=home)[1])["mcp_servers"]["Agents-Core"]
    assert pinned["url"].endswith("/mcp") and pinned["http_headers"]["X-Agents-Workspace"]
    # An explicit transport decides alone: the upgrade moves every layer in one transaction.
    pinned = tomllib.loads(migration.prepare("codex", project, home=home, transport="bridge")[1])
    assert pinned["mcp_servers"]["Agents-Core"]["args"][0].endswith("stdio.mjs")


def test_cursor_layers_get_bridges(migration, tmp_path):
    """#292: Cursor's layers share one transport too; the user layer names each session's project."""
    home = tmp_path / "home"; home.mkdir()
    project = tmp_path / "repo"; project.mkdir()
    migration.config["node"] = sys.executable
    user = json.loads(migration.prepare("cursor", home=home)[1])["mcpServers"]["Agents-Core"]
    assert user["args"][0].endswith("stdio.mjs") and Path(user["args"][1]).name == "cursor-auto.json"
    assert json.loads(Path(user["args"][1]).read_text())["workspace"] == "auto"
    _, text, secret = migration.prepare("cursor", project, home=home)
    pinned = json.loads(text)["mcpServers"]["Agents-Core"]
    assert "url" not in pinned and json.loads(Path(pinned["args"][1]).read_text())["headers"]["X-Agents-Workspace"]
    assert not secret and migration.token not in text
    migration.config["node"] = str(tmp_path / "missing-node")
    user = json.loads(migration.prepare("cursor", home=home)[1])["mcpServers"]["Agents-Core"]
    assert user["url"].endswith("/mcp") and "command" not in user


def test_each_client_names_its_app_in_its_headers_and_bridge(migration, tmp_path):
    """The daemon counts requests per app (#187); one bridge file per app keeps their headers apart."""
    home = tmp_path / "home"; home.mkdir()
    migration.config["node"] = sys.executable
    _, text, _ = migration.prepare("cursor", home=home)
    bridge = Path(json.loads(text)["mcpServers"]["Agents-Core"]["args"][1])
    assert json.loads(bridge.read_text())["headers"]["X-Agents-Client"] == "cursor"
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
    _, text, _ = migration.prepare("antigravity", home=home)
    bridge = Path(json.loads(text)["mcpServers"]["Agents-Core"]["args"][1])
    assert bridge.name == "antigravity-auto.json"
    assert json.loads(bridge.read_text())["headers"]["X-Agents-Client"] == "antigravity"


def test_stdio_servers_get_their_bridge_configuration_where_startup_looks(migration, tmp_path):
    """#266: `src.startup` hands a stdio session to the bridge with `bridges/stdio-auto.json`."""
    from src.daemon.state import read_json
    path = migration.directory / "bridges" / "stdio-auto.json"
    migration.config["node"] = str(tmp_path / "missing-node")
    assert migration.stdio_bridge() is None and not path.exists()  # stdio servers serve standalone
    migration.config["node"] = sys.executable
    assert migration.stdio_bridge() == path
    assert read_json(path) == {"url": "http://127.0.0.1:8765/mcp", "workspace": "auto",
                               "headers": {"Authorization": "Bearer " + migration.token, "X-Agents-Client": "stdio"}}
    written = path.stat().st_mtime_ns
    os.utime(path, ns=(written - 10**9, written - 10**9))
    migration.stdio_bridge()  # unchanged: left alone
    assert path.stat().st_mtime_ns == written - 10**9
    migration.token = "rotated-" + migration.token
    migration.stdio_bridge()
    assert read_json(path)["headers"]["Authorization"] == "Bearer " + migration.token


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
    change = migration.prepare("codex", home=home)
    _, text, secret = change
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
    # A project's own .codex/config.toml keeps its fixed identity, through the same transport (#292).
    migration.apply([change])
    project = tmp_path / "repo"; project.mkdir()
    _, text, _ = migration.prepare("codex", project, home=home)
    pinned = tomllib.loads(text)["mcp_servers"]["Agents-Core"]
    assert pinned["command"] == sys.executable and "url" not in pinned
    settings = json.loads(Path(pinned["args"][1]).read_text())
    assert settings["headers"]["X-Agents-Workspace"] and "workspace" not in settings


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



def test_user_scope_antigravity_gets_auto_workspace_bridge(migration, tmp_path):
    """#257: Antigravity gets a stdio bridge with auto_workspace for user scope, and fixed workspace for projects."""
    home = tmp_path / "home"; home.mkdir()
    migration.config["node"] = sys.executable
    change = migration.prepare("antigravity", home=home)
    registry_file = home / ".gemini/config/config.json"
    assert not registry_file.exists()  # registration stays in the staged transaction
    backup = migration.apply([change])
    entry = json.loads(Path(change[0]).read_text())["mcpServers"]["Agents-Core"]
    assert entry["command"] == sys.executable and entry["args"][0].endswith("stdio.mjs")
    bridge = Path(entry["args"][1])
    assert bridge.name == "antigravity-auto.json"
    settings = json.loads(bridge.read_text())
    assert settings["workspace"] == "auto" and "X-Agents-Workspace" not in settings["headers"]
    assert settings["headers"]["X-Agents-Client"] == "antigravity"
    assert not change[2] and migration.token not in change[1]
    # #260: User scope also provisions the Antigravity plugin with PreToolUse hook for signed workspace binding.
    plugin_file = home / ".gemini/config/plugins/agents-core/plugin.json"
    hooks_file = home / ".gemini/config/plugins/agents-core/hooks.json"
    assert plugin_file.exists() and hooks_file.exists()
    assert json.loads(registry_file.read_text())["plugins"]["agents-core"] == {"enabled": True}
    journal_paths = {record["path"] for record in json.loads((backup / "changes.json").read_text())}
    assert {str(change[0]), str(registry_file), str(plugin_file), str(hooks_file)} <= journal_paths
    manifest = json.loads(plugin_file.read_text())
    assert manifest["name"] == "agents-core"
    hooks = json.loads(hooks_file.read_text())
    hook_entry = hooks["agents-core-workspace"]["PreToolUse"][0]
    assert hook_entry["matcher"] == "call_mcp_tool|mcp_Agents-Core_.*|mcp_Agents_Core_.*"
    cmd = hook_entry["hooks"][0]["command"]
    assert sys.executable in cmd and "antigravity_hook.mjs" in cmd and str(bridge) in cmd
    # Restore removes the provisioned plugin files when they were created by migration
    migration.restore(backup)
    assert not plugin_file.exists() and not hooks_file.exists()
    assert not registry_file.exists()
    assert not (home / ".gemini/config/plugins/agents-core/rules/AGENTS.md").exists()
    # A pinned project gets a workspace bridge with its workspace identity.
    project = tmp_path / "repo"; project.mkdir()
    _, text, _ = migration.prepare("antigravity", project, home=home)
    pinned_bridge = Path(json.loads(text)["mcpServers"]["Agents-Core"]["args"][1])
    pinned_settings = json.loads(pinned_bridge.read_text())
    assert pinned_settings.get("workspace") != "auto"
    assert pinned_settings["headers"]["X-Agents-Workspace"]


def test_conftest_clears_inherited_client_config_overrides(tmp_path):
    # An inherited CLAUDE_CONFIG_DIR made migration tests rewrite the developer's
    # real Claude profile. Import conftest in a child that inherits all five.
    overrides = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "AGENTS_CURSOR_MCP_CONFIG", "AGENTS_CLAUDE_DESKTOP_CONFIG", "AGENTS_ANTIGRAVITY_MCP_CONFIG")
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


def test_provision_antigravity_plugin_merges_and_preserves_customizations(migration, tmp_path):
    home = tmp_path / "home"
    plugin_dir = home / ".gemini/config/plugins/agents-core"
    plugin_dir.mkdir(parents=True)
    # Existing plugin.json with custom description or rules
    write_json(plugin_dir / "plugin.json", {"name": "agents-core", "description": "Custom", "version": "1.0.0"})
    # Existing hooks.json with a user hook
    write_json(plugin_dir / "hooks.json", {"user-hook": {"enabled": True, "Stop": []}})
    registry_file = home / ".gemini/config/config.json"
    original_registry = {
        "theme": "custom",
        "plugins": {
            "other-plugin": {"enabled": False, "custom": ["keep"]},
            "agents-core": {"enabled": False, "custom": {"option": "keep"}},
        },
    }
    # Byte-exact restore includes the existing registry's formatting.
    registry_file.write_text(json.dumps(original_registry, indent=4) + "\n\n")
    original_registry_bytes = registry_file.read_bytes()

    migration.config["node"] = sys.executable
    changes = migration.provision_antigravity_plugin(home=home)
    backup = migration.apply(changes)

    manifest = json.loads((plugin_dir / "plugin.json").read_text())
    assert manifest["version"] == "1.0.0"
    assert manifest["description"] == "Custom"

    hooks = json.loads((plugin_dir / "hooks.json").read_text())
    assert "user-hook" in hooks
    assert "agents-core-workspace" in hooks
    assert hooks["agents-core-workspace"]["PreToolUse"][0]["matcher"] == "call_mcp_tool|mcp_Agents-Core_.*|mcp_Agents_Core_.*"
    updated_registry = json.loads(registry_file.read_text())
    assert updated_registry == {
        **original_registry,
        "plugins": {
            **original_registry["plugins"],
            "agents-core": {"enabled": True, "custom": {"option": "keep"}},
        },
    }

    # Restoring backup reverts the workspace hook while preserving the user hook
    migration.restore(backup)
    restored_hooks = json.loads((plugin_dir / "hooks.json").read_text())
    assert "user-hook" in restored_hooks
    assert "agents-core-workspace" not in restored_hooks
    assert registry_file.read_bytes() == original_registry_bytes

    # When node is not a valid file, it returns empty changes and does nothing
    empty_home = tmp_path / "empty_home"
    migration.config["node"] = str(tmp_path / "nonexistent-node")
    changes = migration.provision_antigravity_plugin(home=empty_home)
    assert changes == []
    assert not (empty_home / ".gemini").exists()

    # Invalid JSON or non-object values raise ValueError naming the file
    migration.config["node"] = sys.executable
    (plugin_dir / "plugin.json").write_text("invalid json {")
    with pytest.raises(ValueError, match="Invalid JSON.*plugin.json"):
        migration.provision_antigravity_plugin(home=home)
    (plugin_dir / "plugin.json").write_text("[1, 2, 3]")
    with pytest.raises(ValueError, match="must be a JSON object"):
        migration.provision_antigravity_plugin(home=home)
    write_json(plugin_dir / "plugin.json", {"name": "agents-core"})
    (plugin_dir / "hooks.json").write_text("invalid hooks {")
    with pytest.raises(ValueError, match="Invalid JSON.*hooks.json"):
        migration.provision_antigravity_plugin(home=home)
    (plugin_dir / "hooks.json").write_text('"not an object"')
    with pytest.raises(ValueError, match="must be a JSON object"):
        migration.provision_antigravity_plugin(home=home)


@pytest.mark.parametrize("document", [
    [], "invalid root", None,
    {"plugins": []}, {"plugins": "invalid plugins"}, {"plugins": None},
    {"plugins": {"agents-core": []}},
    {"plugins": {"agents-core": "invalid entry"}},
    {"plugins": {"agents-core": None}},
    {"plugins": {"agents-core": False}},
])
def test_antigravity_rejects_non_object_plugin_registry_before_apply(migration, tmp_path, document):
    home = tmp_path / "home"
    registry_file = home / ".gemini/config/config.json"
    write_json(registry_file, document)
    original = registry_file.read_bytes()

    with pytest.raises(ValueError, match="must be a JSON object"):
        migration.prepare("antigravity", home=home)

    assert registry_file.read_bytes() == original
    assert not (home / ".gemini/config/mcp_config.json").exists()
    assert not (home / ".gemini/config/plugins/agents-core").exists()
    assert migration.staged_changes == {}


def test_antigravity_rejects_invalid_json_plugin_registry_before_apply(migration, tmp_path):
    home = tmp_path / "home"
    registry_file = home / ".gemini/config/config.json"
    registry_file.parent.mkdir(parents=True)
    registry_file.write_text("invalid json {")

    with pytest.raises(ValueError, match="Invalid JSON.*config.json"):
        migration.prepare("antigravity", home=home)

    assert registry_file.read_text() == "invalid json {"
    assert not (home / ".gemini/config/mcp_config.json").exists()
    assert not (home / ".gemini/config/plugins/agents-core").exists()


@pytest.mark.parametrize("edit_when", ["before_apply", "during_apply"])
@pytest.mark.parametrize("existing_registry", [False, True])
def test_antigravity_registry_concurrent_edit_preserves_user_changes_and_rolls_back(
    migration, tmp_path, edit_when, existing_registry,
):
    home = tmp_path / "home"
    registry_file = home / ".gemini/config/config.json"
    if existing_registry:
        write_json(registry_file, {"plugins": {"other": {"enabled": True}}})
    change = migration.prepare("antigravity", home=home)
    user_edit = {"plugins": {"other": {"enabled": False}}, "userChanged": True}

    if edit_when == "before_apply":
        write_json(registry_file, user_edit)
        callback = None
        error = "changed after preparation"
    else:
        def callback(_backup):
            write_json(registry_file, user_edit)
        error = "changed during migration"

    with pytest.raises(ValueError, match=error):
        migration.apply([change], on_prepared=callback)

    assert json.loads(registry_file.read_text()) == user_edit
    assert not change[0].exists()
    plugin_dir = home / ".gemini/config/plugins/agents-core"
    assert not (plugin_dir / "plugin.json").exists()
    assert not (plugin_dir / "hooks.json").exists()
    assert str(change[0]) in migration.staged_changes


def test_antigravity_restore_rejects_user_edited_plugin_registry(migration, tmp_path):
    home = tmp_path / "home"
    change = migration.prepare("antigravity", home=home)
    backup = migration.apply([change])
    registry_file = home / ".gemini/config/config.json"
    user_edit = {"plugins": {"agents-core": {"enabled": False}}, "userChanged": True}
    write_json(registry_file, user_edit)

    with pytest.raises(ValueError, match="changed after migration"):
        migration.restore(backup)

    assert json.loads(registry_file.read_text()) == user_edit
    assert change[0].exists()
    assert (home / ".gemini/config/plugins/agents-core/plugin.json").exists()
    assert (home / ".gemini/config/plugins/agents-core/hooks.json").exists()


def test_repeated_antigravity_migration_preserves_settings_without_duplicate_hooks(migration, tmp_path):
    home = tmp_path / "home"
    registry_file = home / ".gemini/config/config.json"
    original_registry = {
        "userSettings": {"theme": "custom"},
        "plugins": {"other-plugin": {"enabled": True}, "agents-core": {"custom": "keep"}},
    }
    write_json(registry_file, original_registry)
    plugin_dir = home / ".gemini/config/plugins/agents-core"
    write_json(plugin_dir / "plugin.json", {"name": "agents-core", "description": "Custom"})
    write_json(plugin_dir / "hooks.json", {"user-hook": {"enabled": True, "Stop": []}})
    change = migration.prepare("antigravity", home=home)
    migration.apply([change])
    paths = [change[0], registry_file, plugin_dir / "plugin.json", plugin_dir / "hooks.json",
             plugin_dir / "rules/AGENTS.md"]
    first_contents = {path: path.read_bytes() for path in paths}

    second_change = migration.prepare("antigravity", home=home)
    migration.apply([second_change])

    assert {path: path.read_bytes() for path in paths} == first_contents
    registry = json.loads(registry_file.read_text())
    assert registry["userSettings"] == original_registry["userSettings"]
    assert registry["plugins"] == {
        "other-plugin": {"enabled": True}, "agents-core": {"custom": "keep", "enabled": True},
    }
    assert json.loads((plugin_dir / "plugin.json").read_text())["description"] == "Custom"
    hooks = json.loads((plugin_dir / "hooks.json").read_text())
    assert set(hooks) == {"user-hook", "agents-core-workspace"}
    assert hooks["user-hook"] == {"enabled": True, "Stop": []}
    assert len(hooks["agents-core-workspace"]["PreToolUse"]) == 1
    assert len(hooks["agents-core-workspace"]["PreToolUse"][0]["hooks"]) == 1


@pytest.mark.parametrize("home_form", ["absolute", "relative", "directory_alias"])
def test_antigravity_config_override_can_share_plugin_registry_and_restores_once(
    migration, tmp_path, monkeypatch, home_form,
):
    home = tmp_path / "home"
    registry_file = home / ".gemini/config/config.json"
    registry_file.parent.mkdir(parents=True)
    original = {
        "userSettings": {"theme": "custom"},
        "plugins": {"other-plugin": {"enabled": False}, "agents-core": {"custom": "keep"}},
        "mcpServers": {"other": {"command": "safe", "args": ["keep"]}},
    }
    registry_file.write_text(json.dumps(original, indent=4) + "\n\n")
    original_bytes = registry_file.read_bytes()
    monkeypatch.chdir(tmp_path)
    supplied_home = Path("home") if home_form == "relative" else home
    if home_form == "directory_alias":
        supplied_home = tmp_path / "home-alias"
        try:
            supplied_home.symlink_to(home, target_is_directory=True)
        except OSError:
            pytest.skip("Directory symlinks are unavailable")
    change = migration.prepare(
        "antigravity", home=supplied_home, config_path=registry_file,
    )
    backup = migration.apply([change])

    document = json.loads(registry_file.read_text())
    assert document["userSettings"] == original["userSettings"]
    assert document["mcpServers"]["other"] == original["mcpServers"]["other"]
    entry = document["mcpServers"]["Agents-Core"]
    assert entry["command"] == sys.executable and entry["args"][0].endswith("stdio.mjs")
    assert document["plugins"] == {
        "other-plugin": {"enabled": False}, "agents-core": {"custom": "keep", "enabled": True},
    }
    journal = json.loads((backup / "changes.json").read_text())
    assert sum(record["path"] == str(registry_file) for record in journal) == 1
    rules_file = home / ".gemini/config/plugins/agents-core/rules/AGENTS.md"
    template = Path(migration.config["installation"]) / "scripts/templates/routing-protocol-core.md"
    assert template.read_bytes().strip() in rules_file.read_bytes()

    migration.restore(backup)

    assert registry_file.read_bytes() == original_bytes
    plugin_dir = home / ".gemini/config/plugins/agents-core"
    assert not (plugin_dir / "plugin.json").exists()
    assert not (plugin_dir / "hooks.json").exists()
    assert not (plugin_dir / "rules/AGENTS.md").exists()


@pytest.mark.parametrize("config_target", ["default", "registry", "referent"])
def test_antigravity_registry_file_symlink_is_rejected(migration, tmp_path, config_target):
    home = tmp_path / "home"
    registry_file = home / ".gemini/config/config.json"
    referent = home / "original-config.json"
    write_json(referent, {"userSettings": {"theme": "keep"}, "plugins": {"other": {"enabled": True}}})
    original_bytes = referent.read_bytes()
    registry_file.parent.mkdir(parents=True)
    try:
        registry_file.symlink_to(referent)
    except OSError:
        pytest.skip("File symlinks are unavailable")
    config_path = {"default": None, "registry": registry_file, "referent": referent}[config_target]
    change = migration.prepare("antigravity", home=home, config_path=config_path)

    with pytest.raises(ValueError, match="cannot be a symlink"):
        migration.apply([change])

    assert registry_file.is_symlink()
    assert referent.read_bytes() == original_bytes
    assert not (home / ".gemini/config/mcp_config.json").exists()
    plugin_dir = home / ".gemini/config/plugins/agents-core"
    assert not (plugin_dir / "plugin.json").exists()
    assert not (plugin_dir / "hooks.json").exists()


def test_antigravity_shared_registry_edit_during_preparation_is_preserved(migration, tmp_path, monkeypatch):
    home = tmp_path / "home"
    registry_file = home / ".gemini/config/config.json"
    write_json(registry_file, {
        "userSettings": {"theme": "original"},
        "plugins": {"other-plugin": {"enabled": True}},
        "mcpServers": {"other": {"command": "original"}},
    })
    user_edit = {
        "userSettings": {"theme": "changed", "language": "custom"},
        "plugins": {"other-plugin": {"enabled": False}, "agents-core": {"custom": "added"}},
        "mcpServers": {"other": {"command": "changed"}, "added": {"command": "keep"}},
    }
    read_config = migration.read_config
    edited = False

    def edit_after_first_snapshot(path, **kwargs):
        nonlocal edited
        document = read_config(path, **kwargs)
        if Path(path) == registry_file and not edited:
            edited = True
            write_json(registry_file, user_edit)
        return document

    monkeypatch.setattr(migration, "read_config", edit_after_first_snapshot)
    change = migration.prepare("antigravity", home=home, config_path=registry_file)
    assert edited and json.loads(registry_file.read_text()) == user_edit

    with pytest.raises(ValueError, match="changed after preparation"):
        migration.apply([change])

    assert json.loads(registry_file.read_text()) == user_edit
    plugin_dir = home / ".gemini/config/plugins/agents-core"
    assert not (plugin_dir / "plugin.json").exists()
    assert not (plugin_dir / "hooks.json").exists()


def test_antigravity_migration_installs_protocol_in_active_plugin_rules(migration, tmp_path):
    home = tmp_path / "home"
    rules_file = home / ".gemini/config/plugins/agents-core/rules/AGENTS.md"
    change = migration.prepare("antigravity", home=home)
    assert not rules_file.exists()  # preparation must not enable instructions early

    def inspect_prepared_backup(backup):
        journal = json.loads((backup / "changes.json").read_text())
        paths = [record["path"] for record in journal]
        registry_file = home / ".gemini/config/config.json"
        assert str(rules_file) in paths
        assert paths.index(str(rules_file)) < paths.index(str(registry_file))
        assert not rules_file.exists() and not registry_file.exists() and not change[0].exists()
        assert not (rules_file.parents[1] / "plugin.json").exists()
        assert not (rules_file.parents[1] / "hooks.json").exists()

    backup = migration.apply([change], on_prepared=inspect_prepared_backup)

    content = rules_file.read_bytes()
    template = Path(migration.config["installation"]) / "scripts/templates/routing-protocol-core.md"
    assert template.read_bytes().strip() in content
    assert content.count(ROUTING_BEGIN) == content.count(ROUTING_END) == 1
    journal = json.loads((backup / "changes.json").read_text())
    assert sum(record["path"] == str(rules_file) for record in journal) == 1
    assert json.loads((home / ".gemini/config/config.json").read_text())["plugins"]["agents-core"]["enabled"] is True

    migration.restore(backup)

    assert not rules_file.exists()


def test_antigravity_user_edited_rules_block_restore_without_partial_removals(migration, tmp_path):
    home = tmp_path / "home"
    change = migration.prepare("antigravity", home=home)
    backup = migration.apply([change])
    rules_file = home / ".gemini/config/plugins/agents-core/rules/AGENTS.md"
    user_edit = b"My new personal rules\r\n"
    rules_file.write_bytes(user_edit)

    with pytest.raises(ValueError, match="changed after migration"):
        migration.restore(backup)

    assert rules_file.read_bytes() == user_edit
    assert change[0].exists()
    assert (home / ".gemini/config/config.json").exists()
    assert (rules_file.parents[1] / "plugin.json").exists()
    assert (rules_file.parents[1] / "hooks.json").exists()


@pytest.mark.parametrize("legacy", [False, True])
def test_antigravity_rules_upgrade_preserves_personal_bytes_and_restores_them(migration, tmp_path, legacy):
    home = tmp_path / "home"
    rules_file = home / ".gemini/config/plugins/agents-core/rules/AGENTS.md"
    rules_file.parent.mkdir(parents=True)
    prefix = b"# Personal rules\r\nPreserve my language choice.\r\n"
    suffix = b"\r\n# Additional rules\r\nPreserve these bytes too.\r\n"
    begin = ROUTING_BEGIN.replace(b"init_repo)", b"init_repo.sh)") if legacy else ROUTING_BEGIN
    end = ROUTING_END.replace(b"init_repo)", b"init_repo.sh)") if legacy else ROUTING_END
    original = prefix + begin + b"\nStale managed protocol\n" + end + suffix
    rules_file.write_bytes(original)
    change = migration.prepare("antigravity", home=home)
    assert rules_file.read_bytes() == original

    backup = migration.apply([change])

    updated = rules_file.read_bytes()
    assert updated.startswith(prefix) and updated.endswith(suffix)
    assert updated.count(ROUTING_BEGIN) == updated.count(ROUTING_END) == 1
    template = Path(migration.config["installation"]) / "scripts/templates/routing-protocol-core.md"
    assert template.read_bytes().strip() in updated
    assert b"Stale managed protocol" not in updated

    migration.restore(backup)

    assert rules_file.read_bytes() == original


@pytest.mark.parametrize("original", [
    b"Personal rules without a newline",
    b"Personal rules\r\n",
])
def test_antigravity_rules_append_protocol_without_replacing_personal_rules(migration, tmp_path, original):
    home = tmp_path / "home"
    rules_file = home / ".gemini/config/plugins/agents-core/rules/AGENTS.md"
    rules_file.parent.mkdir(parents=True)
    rules_file.write_bytes(original)

    backup = migration.apply([migration.prepare("antigravity", home=home)])

    content = rules_file.read_bytes()
    assert content.startswith(original)
    assert content.count(ROUTING_BEGIN) == content.count(ROUTING_END) == 1
    migration.restore(backup)
    assert rules_file.read_bytes() == original


@pytest.mark.parametrize("shape", ["begin_only", "end_only", "reversed", "duplicate", "mixed", "legacy_begin_only"])
def test_antigravity_malformed_rules_markers_refuse_entire_migration(migration, tmp_path, shape):
    home = tmp_path / "home"
    rules_file = home / ".gemini/config/plugins/agents-core/rules/AGENTS.md"
    rules_file.parent.mkdir(parents=True)
    original = {
        "begin_only": ROUTING_BEGIN,
        "end_only": ROUTING_END,
        "reversed": ROUTING_END + b"\n" + ROUTING_BEGIN,
        "duplicate": ROUTING_BEGIN + b"\n" + ROUTING_BEGIN + b"\n" + ROUTING_END,
        "mixed": ROUTING_BEGIN + b"\n" + ROUTING_END + b"\n" + ROUTING_BEGIN.replace(b"init_repo)", b"init_repo.sh)"),
        "legacy_begin_only": ROUTING_BEGIN.replace(b"init_repo)", b"init_repo.sh)"),
    }[shape]
    rules_file.write_bytes(original)

    with pytest.raises(ValueError, match="marker"):
        migration.prepare("antigravity", home=home)

    assert rules_file.read_bytes() == original
    assert not (home / ".gemini/config/mcp_config.json").exists()
    assert not (home / ".gemini/config/config.json").exists()
    assert not (rules_file.parents[1] / "plugin.json").exists()
    assert not (rules_file.parents[1] / "hooks.json").exists()
    assert not migration.staged_changes
    assert not (migration.directory / "bridges/antigravity-auto.json").exists()


@pytest.mark.parametrize("edit_when", ["before_apply", "during_apply"])
def test_antigravity_rules_concurrent_edit_preserves_user_bytes_and_rolls_back(migration, tmp_path, edit_when):
    home = tmp_path / "home"
    rules_file = home / ".gemini/config/plugins/agents-core/rules/AGENTS.md"
    rules_file.parent.mkdir(parents=True)
    rules_file.write_bytes(b"Original personal rules\r\n")
    change = migration.prepare("antigravity", home=home)
    user_edit = b"Concurrent personal rules\r\n"
    if edit_when == "before_apply":
        rules_file.write_bytes(user_edit)
        callback = None
        error = "changed after preparation"
    else:
        def callback(_backup):
            rules_file.write_bytes(user_edit)
        error = "changed during migration"

    with pytest.raises(ValueError, match=error):
        migration.apply([change], on_prepared=callback)

    assert rules_file.read_bytes() == user_edit
    assert not change[0].exists()
    assert not (home / ".gemini/config/config.json").exists()
    assert not (rules_file.parents[1] / "plugin.json").exists()
    assert not (rules_file.parents[1] / "hooks.json").exists()
    assert str(change[0]) in migration.staged_changes


def test_antigravity_rules_symlink_is_rejected_without_replacing_personal_target(migration, tmp_path):
    home = tmp_path / "home"
    rules_file = home / ".gemini/config/plugins/agents-core/rules/AGENTS.md"
    rules_file.parent.mkdir(parents=True)
    referent = home / "personal-rules.md"
    referent.write_bytes(b"Personal rules\r\n")
    try:
        rules_file.symlink_to(referent)
    except OSError:
        pytest.skip("File symlinks are unavailable")
    change = migration.prepare("antigravity", home=home)

    with pytest.raises(ValueError, match="cannot be a symlink"):
        migration.apply([change])

    assert rules_file.is_symlink() and referent.read_bytes() == b"Personal rules\r\n"
    assert not change[0].exists()
    assert not (home / ".gemini/config/config.json").exists()


def test_antigravity_missing_installation_protocol_refuses_migration(migration, tmp_path):
    template = Path(migration.config["installation"]) / "scripts/templates/routing-protocol-core.md"
    template.unlink()
    home = tmp_path / "home"

    with pytest.raises((OSError, ValueError), match="routing-protocol-core"):
        migration.prepare("antigravity", home=home)

    assert not (home / ".gemini").exists()
    assert not migration.staged_changes
    assert not (migration.directory / "bridges/antigravity-auto.json").exists()


@pytest.mark.parametrize("asset", ["plugin.json", "hooks.json", "rules/AGENTS.md"])
def test_antigravity_mcp_override_cannot_overwrite_plugin_asset(migration, tmp_path, asset):
    home = tmp_path / "home"
    target = home / ".gemini/config/plugins/agents-core" / asset
    target.parent.mkdir(parents=True)
    original = b"Personal rules\r\n" if asset.endswith(".md") else b'{"custom": "keep"}\n'
    target.write_bytes(original)

    with pytest.raises(ValueError, match="Antigravity MCP config cannot share plugin asset"):
        migration.prepare("antigravity", home=home, config_path=target)

    assert target.read_bytes() == original
    assert not (home / ".gemini/config/config.json").exists()
    assert not (migration.directory / "bridges/antigravity-auto.json").exists()
    assert not migration.staged_changes


def test_staged_changes_do_not_leak_to_other_clients(migration, tmp_path):
    home = tmp_path / "home"; home.mkdir()
    migration.config["node"] = sys.executable
    # Prepare antigravity (stages plugin changes)
    antigravity_change = migration.prepare("antigravity", home=home)
    # Prepare cursor (different client)
    cursor_change = migration.prepare("cursor", home=home)
    # Apply only cursor change
    migration.apply([cursor_change])
    # Antigravity plugin files must not have been created
    plugin_file = home / ".gemini/config/plugins/agents-core/plugin.json"
    assert not plugin_file.exists()
    registry_file = home / ".gemini/config/config.json"
    assert not registry_file.exists()
    assert not (home / ".gemini/config/plugins/agents-core/rules/AGENTS.md").exists()
    # Now apply antigravity change
    migration.apply([antigravity_change])
    assert plugin_file.exists()
    assert json.loads(registry_file.read_text())["plugins"]["agents-core"]["enabled"] is True
    assert (home / ".gemini/config/plugins/agents-core/rules/AGENTS.md").exists()


def test_staged_changes_preserved_on_failed_apply(migration, tmp_path):
    home = tmp_path / "home"; home.mkdir()
    migration.config["node"] = sys.executable
    change = migration.prepare("antigravity", home=home)
    # Simulate a failure during apply (e.g. on_prepared callback raises)
    def failing_callback(_backup):
        raise RuntimeError("simulated callback failure")

    with pytest.raises(RuntimeError, match="simulated callback failure"):
        migration.apply([change], on_prepared=failing_callback)

    plugin_file = home / ".gemini/config/plugins/agents-core/plugin.json"
    assert not plugin_file.exists()
    assert str(change[0]) in migration.staged_changes

    # Retry applying the change without failure
    migration.apply([change])
    assert plugin_file.exists()
    assert str(change[0]) not in migration.staged_changes



def test_the_service_start_keeps_the_stdio_bridge_configuration_and_survives_a_failure(migration, caplog):
    """`serve` writes it at every start, so an installation from before #266 gets it; a failure
    leaves stdio servers standalone, never the service down."""
    from src.daemon import bootstrap
    from src.daemon.state import read_json
    config = read_json(migration.directory / "service.json")
    write_json(migration.directory / "service.json", {**config, "node": sys.executable})
    bootstrap.stdio_bridge(migration.directory)
    assert (migration.directory / "bridges/stdio-auto.json").is_file()
    (migration.directory / "service.json").unlink()
    bootstrap.stdio_bridge(migration.directory)  # logs and returns
    assert "serve standalone" in caplog.text


def test_desktop_migration_lifts_only_the_standalone_denial(migration, tmp_path):
    """#421: the service serves the desktop entry, so its migration removes the #231 denial."""
    from src.client_paths import DESKTOP_DENY_RULES
    settings = tmp_path / "claude" / "settings.json"
    atomic_private(settings, json.dumps({"model": "m", "permissions": {
        "deny": ["Bash(rm *)", *DESKTOP_DENY_RULES], "allow": ["x"]}}))
    change = migration.prepare("claude-deny-desktop", config_path=settings)
    assert json.loads(change[1]) == {"model": "m", "permissions": {"deny": ["Bash(rm *)"], "allow": ["x"]}}
    migration.apply([change])
    assert json.loads(settings.read_text()) == json.loads(change[1])
    atomic_private(settings, json.dumps({"model": "m", "permissions": {"deny": list(DESKTOP_DENY_RULES)}}))
    assert json.loads(migration.prepare("claude-deny-desktop", config_path=settings)[1]) == {"model": "m"}


@pytest.mark.parametrize("content", [None, {"permissions": {"deny": ["Bash(rm *)"]}}, {"permissions": []}])
def test_desktop_migration_writes_no_settings_without_a_denial(migration, tmp_path, content):
    settings = tmp_path / "claude" / "settings.json"
    if content is not None:
        atomic_private(settings, json.dumps(content))
    assert migration.prepare("claude-deny-desktop", config_path=settings) is None


def test_migrate_desktop_skips_settings_without_a_denial_and_cleans_a_denied_one(migration, tmp_path, capsys):
    from src.client_paths import DESKTOP_DENY_RULES
    from src.daemon import control
    desktop, settings = tmp_path / "desktop.json", tmp_path / "claude" / "settings.json"
    argv = ["--state", str(migration.directory), "migrate", "--clients", "desktop",
            "--client-config", f"desktop={desktop}", "--client-config", f"claude-deny-desktop={settings}"]
    control.main(argv)
    assert json.loads(capsys.readouterr().out)["files"] == [str(desktop)]
    assert not settings.exists()
    assert "Agents-Core-Desktop" in json.loads(desktop.read_text())["mcpServers"]
    atomic_private(settings, json.dumps({"permissions": {"deny": list(DESKTOP_DENY_RULES)}}))
    control.main(argv)
    assert str(settings) in json.loads(capsys.readouterr().out)["files"]
    assert json.loads(settings.read_text()) == {}
