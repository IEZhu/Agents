"""The agents-core Claude Code plugin and the writers of Claude Code's Agents-Core entry (#367)."""
import json
from pathlib import Path
import sys
import tomllib

import pytest

from src import claude_plugin
from src.daemon import registrations
from src.daemon.state import atomic_private, write_json
from src.daemon.workspaces import WorkspaceRegistry

TOKEN = "private-token-for-tests-only" * 2
URL = "http://127.0.0.1:8765/mcp"
PLUGIN_ID = "agents-core@claude-plugins-official"


@pytest.fixture(autouse=True)
def no_overrides(monkeypatch):
    for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_PLUGIN_CACHE_DIR", claude_plugin.FORCE_ENV):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    state = tmp_path / "service"; state.mkdir(mode=0o700)
    home = tmp_path / "home"; home.mkdir()
    write_json(state / "service.json", {"installation": str(tmp_path / "install"), "port": 8765,
                                        "node": sys.executable})
    atomic_private(state / "token", TOKEN)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return state, home


def install_plugin(profile, *, enabled=True, scope="user", plugin_id=PLUGIN_ID, settings=True):
    """Claude Code's own record of an installed plugin, as /plugin writes it."""
    (profile / "plugins").mkdir(parents=True, exist_ok=True)
    (profile / "plugins/installed_plugins.json").write_text(json.dumps({"version": 2, "plugins": {plugin_id: [
        {"scope": scope, "installPath": str(profile / "plugins/cache/x/agents-core/0.1.1"), "version": "0.1.1"}]}}))
    if settings:
        (profile / "settings.json").write_text(json.dumps({"enabledPlugins": {plugin_id: enabled}}))


def http_codex():
    return (f'[mcp_servers."Agents-Core"]\nurl = "{URL}"\n'
            f'[mcp_servers."Agents-Core".http_headers]\nAuthorization = "Bearer {TOKEN}"\n')


# --- detection ---------------------------------------------------------------------------------

def test_an_enabled_user_scope_install_skips_the_entry(tmp_path):
    install_plugin(tmp_path / ".claude")
    reason = claude_plugin.skip_reason(home=tmp_path, environ={})
    assert PLUGIN_ID in reason and "--force-claude-code-mcp" in reason


@pytest.mark.parametrize("case", ["absent", "disabled", "project-scope", "another-plugin", "unreadable",
                                  "not-a-mapping"])
def test_anything_else_writes_the_entry_as_before(tmp_path, case):
    profile = tmp_path / ".claude"
    if case == "disabled":
        install_plugin(profile, enabled=False)
    elif case == "project-scope":
        install_plugin(profile, scope="project")
    elif case == "another-plugin":
        install_plugin(profile, plugin_id="agents-core-extra@market")
    elif case == "unreadable":
        install_plugin(profile)
        (profile / "plugins/installed_plugins.json").write_text("{not json")
    elif case == "not-a-mapping":
        install_plugin(profile)
        (profile / "settings.json").write_text('["enabledPlugins"]')
    assert claude_plugin.skip_reason(home=tmp_path, environ={}) is None


def test_an_install_the_user_settings_do_not_mention_is_enabled(tmp_path):
    """defaultEnabled is true unless the manifest says otherwise; ours does not."""
    install_plugin(tmp_path / ".claude", settings=False)
    assert claude_plugin.skip_reason(home=tmp_path, environ={})


def test_the_profile_and_plugins_root_overrides_are_followed(tmp_path):
    profile = tmp_path / "work-profile"
    install_plugin(profile)
    assert claude_plugin.skip_reason(home=tmp_path, environ={}) is None
    assert claude_plugin.skip_reason(home=tmp_path, environ={"CLAUDE_CONFIG_DIR": str(profile)})
    cache = tmp_path / "plugin-root"
    (profile / "plugins").rename(cache)
    assert claude_plugin.skip_reason(home=tmp_path, environ={"CLAUDE_CONFIG_DIR": str(profile)}) is None
    assert claude_plugin.skip_reason(home=tmp_path, environ={
        "CLAUDE_CONFIG_DIR": str(profile), "CLAUDE_CODE_PLUGIN_CACHE_DIR": str(cache)})


@pytest.mark.parametrize("value", ["1", "true", "yes"])
def test_force_registers_anyway(tmp_path, value):
    install_plugin(tmp_path / ".claude")
    assert claude_plugin.skip_reason(home=tmp_path, environ={}, force=True) is None
    assert claude_plugin.skip_reason(home=tmp_path, environ={claude_plugin.FORCE_ENV: value}) is None


def test_detection_never_writes_claude_codes_files(tmp_path):
    profile = tmp_path / ".claude"
    install_plugin(profile)
    before = {path: path.read_bytes() for path in profile.rglob("*") if path.is_file()}
    claude_plugin.skip_reason(home=tmp_path, environ={})
    claude_plugin.duplicate_warning(home=tmp_path, environ={})
    assert {path: path.read_bytes() for path in profile.rglob("*") if path.is_file()} == before


# --- migrate -----------------------------------------------------------------------------------

def test_migrate_writes_no_claude_code_entry_beside_the_plugin(setup, capsys):
    from src.daemon.control import main
    state, home = setup
    install_plugin(home / ".claude")
    (home / ".cursor").mkdir()

    assert main(["--state", str(state), "migrate", "--clients", "codex,claude,cursor"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["claude_code"].startswith("skipped: the " + PLUGIN_ID)
    assert set(result["files"]) == {str(home / ".codex/config.toml"), str(home / ".cursor/mcp.json")}
    assert not (home / ".claude.json").exists()
    assert "Agents-Core" in tomllib.loads((home / ".codex/config.toml").read_text())["mcp_servers"]
    assert "Agents-Core" in json.loads((home / ".cursor/mcp.json").read_text())["mcpServers"]


def test_migrate_of_claude_code_alone_changes_nothing_beside_the_plugin(setup, tmp_path, capsys):
    from src.daemon.control import main
    state, home = setup
    install_plugin(home / ".claude")
    workspace = tmp_path / "repo"; workspace.mkdir()
    (workspace / ".mcp.json").write_text('{"mcpServers": {}}')

    assert main(["--state", str(state), "migrate", "--clients", "claude", "--workspace", str(workspace)]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["files"] == [] and "backup" not in result and result["claude_code"].startswith("skipped")
    assert not (home / ".claude.json").exists()
    assert json.loads((workspace / ".mcp.json").read_text()) == {"mcpServers": {}}


@pytest.mark.parametrize("plugin", ["absent", "disabled", "forced"])
def test_migrate_writes_the_claude_code_entry_otherwise(setup, capsys, plugin):
    from src.daemon.control import main
    state, home = setup
    if plugin != "absent":
        install_plugin(home / ".claude", enabled=plugin == "forced")
    extra = ["--force-claude-code-mcp"] if plugin == "forced" else []

    assert main(["--state", str(state), "migrate", "--clients", "claude", *extra]) == 0

    result = json.loads(capsys.readouterr().out)
    assert "claude_code" not in result and str(home / ".claude.json") in result["files"]
    assert "Agents-Core" in json.loads((home / ".claude.json").read_text())["mcpServers"]


# --- the service's registration upkeep ---------------------------------------------------------

def test_the_upkeep_leaves_claude_code_to_the_plugin(setup):
    state, home = setup
    install_plugin(home / ".claude")
    claude = {"mcpServers": {"Agents-Core": {"type": "http", "url": URL, "headers": {"Authorization": "Bearer " + TOKEN}}}}
    write_json(home / ".claude.json", claude)
    (home / ".codex").mkdir()
    (home / ".codex/config.toml").write_text(http_codex())

    report = registrations.upgrade(state, home)

    assert report["claude"]["status"] == "skipped" and PLUGIN_ID in report["claude"]["reason"]
    assert json.loads((home / ".claude.json").read_text()) == claude
    assert report["codex"]["status"] == "upgraded"


def test_the_upkeep_is_unchanged_without_the_plugin_or_when_forced(setup, monkeypatch):
    state, home = setup
    write_json(home / ".claude.json", {"mcpServers": {"Agents-Core": {
        "type": "http", "url": URL, "headers": {"Authorization": "Bearer " + TOKEN}}}})
    assert registrations.upgrade(state, home)["claude"]["status"] == "upgraded"

    install_plugin(home / ".claude")
    write_json(home / ".claude.json", {"mcpServers": {"Agents-Core": {
        "type": "http", "url": URL, "headers": {"Authorization": "Bearer " + TOKEN}}}})
    monkeypatch.setenv(claude_plugin.FORCE_ENV, "1")
    assert registrations.upgrade(state, home)["claude"]["status"] == "upgraded"


# --- the duplicate warning in `registrations status` -------------------------------------------

def status(state, capsys):
    from src.daemon.control import main
    main(["--state", str(state), "registrations", "status"])
    return json.loads(capsys.readouterr().out)


@pytest.mark.parametrize("where", ["user", "local", "project"])
def test_status_warns_about_a_second_claude_code_server(setup, tmp_path, capsys, where):
    state, home = setup
    install_plugin(home / ".claude")
    root = tmp_path / "repo"; root.mkdir()
    entry = {"command": "python", "args": ["server.py"]}
    if where == "user":
        write_json(home / ".claude.json", {"mcpServers": {"Agents-Core": entry}})
    elif where == "local":
        write_json(home / ".claude.json", {"projects": {str(root): {"mcpServers": {"Agents-Core": entry}}}})
    else:
        WorkspaceRegistry(state).register(root)
        write_json(root / ".mcp.json", {"mcpServers": {"Agents-Core": entry}})

    warnings = status(state, capsys)["warnings"]

    assert [warning["warning"] for warning in warnings] == ["duplicate_claude_code_server"]
    assert "claude mcp remove Agents-Core --scope user" in warnings[0]["message"]
    assert "disable the agents-core plugin" in warnings[0]["message"]
    assert warnings[0]["registrations"][0]["scope"].split(":")[0] == where


@pytest.mark.parametrize("single", ["plugin-only", "entry-only", "plugin-disabled"])
def test_status_does_not_warn_with_a_single_registration(setup, capsys, single):
    state, home = setup
    if single != "entry-only":
        install_plugin(home / ".claude", enabled=single == "plugin-only")
    if single != "plugin-only":
        write_json(home / ".claude.json", {"mcpServers": {"Agents-Core": {"command": "python", "args": ["s.py"]}}})

    assert "warnings" not in status(state, capsys)
