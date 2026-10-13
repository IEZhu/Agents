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
def no_overrides(monkeypatch, tmp_path):
    for name in ("CLAUDE_CONFIG_DIR", "CLAUDE_CODE_PLUGIN_CACHE_DIR", claude_plugin.FORCE_ENV):
        monkeypatch.delenv(name, raising=False)
    # The machine's own managed settings stay out of the tests.
    managed = tmp_path / "managed"
    monkeypatch.setattr(claude_plugin, "MANAGED_DIRECTORIES", {"darwin": managed, "linux": managed})
    monkeypatch.setattr(claude_plugin, "MANAGED_PREFERENCES", tmp_path / "managed-preferences")
    return managed


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


def load_helper():
    import importlib.util
    path = Path(__file__).resolve().parents[1] / "scripts/_helpers/claude_code_plugin.py"
    spec = importlib.util.spec_from_file_location("claude_code_plugin_helper", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


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


@pytest.mark.parametrize("source", ["file", "drop-in", "server-cache", "plist", "user-plist"])
def test_managed_settings_that_disable_the_plugin_write_the_entry(tmp_path, no_overrides, source):
    """F2: managed false blocks the plugin, so Claude Code needs the entry."""
    import getpass
    import plistlib
    install_plugin(tmp_path / ".claude")
    disabled = {"enabledPlugins": {PLUGIN_ID: False}}
    preferences = tmp_path / "managed-preferences"
    if source == "file":
        no_overrides.mkdir(); write_json(no_overrides / "managed-settings.json", disabled)
    elif source == "drop-in":
        (no_overrides / "managed-settings.d").mkdir(parents=True)
        write_json(no_overrides / "managed-settings.d/50-plugins.json", disabled)
    elif source == "server-cache":
        write_json(tmp_path / ".claude/remote-settings.json", disabled)
    else:
        target = preferences / (getpass.getuser() if source == "user-plist" else "") / "com.anthropic.claudecode.plist"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(plistlib.dumps(disabled))
    platform = "darwin" if "plist" in source else None
    assert claude_plugin.enabled_ids(home=tmp_path, environ={}, platform=platform) == []
    if "plist" not in source:
        assert claude_plugin.skip_reason(home=tmp_path, environ={}) is None


def test_managed_settings_that_leave_it_on_or_cannot_be_read(tmp_path, no_overrides):
    install_plugin(tmp_path / ".claude")
    no_overrides.mkdir()
    write_json(no_overrides / "managed-settings.json", {"enabledPlugins": {PLUGIN_ID: True}})
    assert claude_plugin.skip_reason(home=tmp_path, environ={})
    (no_overrides / "managed-settings.json").write_text("{broken")
    assert claude_plugin.skip_reason(home=tmp_path, environ={}) is None


@pytest.mark.parametrize("layer", ["settings.json", "settings.local.json"])
def test_a_known_project_that_disables_the_plugin_writes_the_entry(tmp_path, layer):
    install_plugin(tmp_path / ".claude")
    project = tmp_path / "repo"; (project / ".claude").mkdir(parents=True)
    write_json(project / ".claude" / layer, {"enabledPlugins": {PLUGIN_ID: False}})
    assert claude_plugin.skip_reason(home=tmp_path, environ={})
    assert claude_plugin.skip_reason(home=tmp_path, environ={}, workspaces=[project]) is None


def test_windows_never_skips(tmp_path, monkeypatch):
    """F2: the plugin's start-server.sh does not run on Windows."""
    helper = load_helper()
    install_plugin(tmp_path / ".claude")
    assert claude_plugin.skip_reason(home=tmp_path, environ={})
    monkeypatch.setattr(claude_plugin.os, "name", "nt")
    assert claude_plugin.skip_reason(home=tmp_path, environ={}) is None
    assert helper.main(["--profile", str(tmp_path / ".claude")]) == 1


def test_the_helper_checks_the_profile_it_is_given(tmp_path, capsys):
    helper = load_helper()
    profile = tmp_path / "work"
    install_plugin(profile)
    assert helper.main(["--profile", str(profile)]) == 0 and PLUGIN_ID in capsys.readouterr().out
    assert helper.main(["--profile", str(profile), "--force-claude-code-mcp"]) == 1
    assert helper.main(["--profile", str(tmp_path / "empty")]) == 1


def test_the_settings_env_block_is_followed(tmp_path):
    """F7: CLAUDE_CODE_PLUGIN_CACHE_DIR and CLAUDE_CONFIG_DIR in the user settings' env block."""
    profile = tmp_path / ".claude"
    install_plugin(profile)
    cache = tmp_path / "plugin-cache"
    (profile / "plugins").rename(cache)
    assert claude_plugin.skip_reason(home=tmp_path, environ={}) is None
    write_json(profile / "settings.json", {"env": {"CLAUDE_CODE_PLUGIN_CACHE_DIR": str(cache)}})
    assert claude_plugin.skip_reason(home=tmp_path, environ={})
    # Two different roots: uncertain, so the entry is written.
    assert claude_plugin.skip_reason(home=tmp_path, environ={"CLAUDE_CODE_PLUGIN_CACHE_DIR": str(tmp_path)}) is None
    write_json(profile / "settings.json", {"env": {"CLAUDE_CODE_PLUGIN_CACHE_DIR": str(cache),
                                                   "CLAUDE_CONFIG_DIR": str(tmp_path / "elsewhere")}})
    assert claude_plugin.skip_reason(home=tmp_path, environ={}) is None


def test_the_profile_of_a_claude_json(tmp_path):
    assert claude_plugin.profile_for_config(tmp_path / ".claude.json", home=tmp_path) == tmp_path / ".claude"
    assert claude_plugin.profile_for_config(tmp_path / "w/.claude.json", home=tmp_path) == tmp_path / "w"


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


def test_migrate_still_writes_the_shared_project_file_beside_the_plugin(setup, tmp_path, capsys):
    """F4: a project's .mcp.json is shared with collaborators, who may not have the plugin."""
    from src.daemon.control import main
    state, home = setup
    install_plugin(home / ".claude")
    workspace = tmp_path / "repo"; workspace.mkdir()
    (workspace / ".mcp.json").write_text('{"mcpServers": {}}')

    assert main(["--state", str(state), "migrate", "--clients", "claude", "--workspace", str(workspace)]) == 0

    result = json.loads(capsys.readouterr().out)
    assert result["files"] == [str(workspace / ".mcp.json")] and result["claude_code"].startswith("skipped")
    assert not (home / ".claude.json").exists()
    assert "Agents-Core" in json.loads((workspace / ".mcp.json").read_text())["mcpServers"]


def test_migrate_checks_the_profile_it_writes(setup, tmp_path, capsys):
    """F1: the plugin in profile A does not stop migrate of profile B, and the reverse."""
    from src.daemon.control import main
    state, home = setup
    install_plugin(home / ".claude")
    other = tmp_path / "claude-work"; other.mkdir()

    assert main(["--state", str(state), "migrate", "--clients", "claude",
                 "--client-config", f"claude={other / '.claude.json'}"]) == 0

    result = json.loads(capsys.readouterr().out)
    assert "claude_code" not in result and result["files"] == [str(other / ".claude.json")]
    assert "Agents-Core" in json.loads((other / ".claude.json").read_text())["mcpServers"]

    install_plugin(other)
    (home / ".claude/plugins/installed_plugins.json").unlink()
    assert main(["--state", str(state), "migrate", "--clients", "claude",
                 "--client-config", f"claude={other / '.claude.json'}"]) == 0
    assert json.loads(capsys.readouterr().out)["claude_code"].startswith("skipped")
    assert main(["--state", str(state), "migrate", "--clients", "claude"]) == 0
    assert json.loads(capsys.readouterr().out)["files"] == [str(home / ".claude.json")]


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


def bearer_entry():
    return {"mcpServers": {"Agents-Core": {"type": "http", "url": URL, "headers": {"Authorization": "Bearer " + TOKEN}}}}


def test_the_upkeep_decides_per_recorded_profile(setup, tmp_path):
    """F1: the plugin in the default profile leaves a recorded second profile to the upkeep."""
    state, home = setup
    install_plugin(home / ".claude")
    write_json(home / ".claude.json", bearer_entry())
    other = tmp_path / "claude-work" / ".claude.json"; other.parent.mkdir()
    write_json(other, bearer_entry())
    write_json(state / "client-configs.json",
               {"version": 1, "configs": [{"client": "claude", "path": str(other), "workspace": None}]})

    report = registrations.upgrade(state, home)["claude"]

    assert report["status"] == "upgraded" and report["files"] == [str(other)]
    assert report["plugin"][0]["path"] == str(home / ".claude.json")
    assert json.loads((home / ".claude.json").read_text()) == bearer_entry()
    assert json.loads(other.read_text()) != bearer_entry()


def test_a_project_that_disables_the_plugin_keeps_the_user_entry_current(setup, tmp_path):
    """F2: a registered project's local settings turn the plugin off there, so the entry stays."""
    state, home = setup
    install_plugin(home / ".claude")
    write_json(home / ".claude.json", bearer_entry())
    root = tmp_path / "repo"; (root / ".claude").mkdir(parents=True)
    WorkspaceRegistry(state).register(root)
    write_json(root / ".claude/settings.local.json", {"enabledPlugins": {PLUGIN_ID: False}})

    assert registrations.upgrade(state, home)["claude"]["status"] == "upgraded"


def test_a_forced_profile_stays_forced_in_the_upkeep(setup, capsys):
    """F3: --force-claude-code-mcp of migrate is kept per profile and honoured by the upkeep."""
    from src.daemon.control import main
    state, home = setup
    install_plugin(home / ".claude")

    assert main(["--state", str(state), "migrate", "--clients", "claude", "--force-claude-code-mcp"]) == 0
    assert str(home / ".claude.json") in json.loads(capsys.readouterr().out)["files"]
    assert registrations.forced_profiles(state) == {str(home / ".claude")}

    write_json(home / ".claude.json", bearer_entry())  # an older transport, as after a change of port or token
    assert registrations.upgrade(state, home)["claude"]["status"] == "upgraded"
    assert main(["--state", str(state), "migrate", "--clients", "claude"]) == 0
    assert "claude_code" not in json.loads(capsys.readouterr().out)


def test_registrations_run_takes_the_force_flag(setup, capsys):
    """F3, F6: `registrations run --force-claude-code-mcp` parses, upgrades and keeps the choice."""
    from src.daemon.control import main
    state, home = setup
    install_plugin(home / ".claude")
    write_json(home / ".claude.json", bearer_entry())

    main(["--state", str(state), "registrations", "run"])
    assert json.loads(capsys.readouterr().out)["clients"]["claude"]["status"] == "skipped"
    main(["--state", str(state), "registrations", "run", "--force-claude-code-mcp"])

    result = json.loads(capsys.readouterr().out)
    assert result["clients"]["claude"]["status"] == "upgraded"
    assert result["claude_code_forced"] == [str(home / ".claude")]
    assert claude_plugin.FORCE_ENV not in __import__("os").environ


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


def test_each_recorded_profile_is_warned_about(setup, tmp_path, capsys):
    """F5: the warning covers every recorded Claude Code profile."""
    state, home = setup
    other = tmp_path / "claude-work"; other.mkdir()
    install_plugin(other)
    write_json(other / ".claude.json", {"mcpServers": {"Agents-Core": {"command": "python", "args": ["s.py"]}}})
    write_json(state / "client-configs.json", {"version": 1, "configs": [
        {"client": "claude", "path": str(other / ".claude.json"), "workspace": None}]})

    warnings = status(state, capsys)["warnings"]

    assert [warning["profile"] for warning in warnings] == [str(other)]


def test_the_web_ui_overview_carries_the_warning(setup):
    """F5: the state the web UI reads (renderRegistrations) holds the warnings."""
    from src.daemon import overview
    state, home = setup
    install_plugin(home / ".claude")
    write_json(home / ".claude.json", {"mcpServers": {"Agents-Core": {"command": "python", "args": ["s.py"]}}})

    report = overview.registration_report(state, home)

    assert report["warnings"][0]["warning"] == "duplicate_claude_code_server"
    assert "claude mcp remove Agents-Core --scope user" in report["warnings"][0]["message"]
