"""MCP config injection must keep user fields and refuse configs it cannot parse.

Covers both injectors: the inline Python in ``scripts/init_repo.sh`` (extracted
the same way as in ``test_daemon_migration_guards.py``) and
``scripts/_helpers/inject_mcp.py`` used by ``init_repo.bat``.
"""
import codecs
import importlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
NIX_LD = "/run/current-system/sw/share/nix-ld/lib"


def _run_shell_injection(tmp_path, monkeypatch, config=None, nixos=False, client="claude"):
    """Write ``config`` (None re-runs on the current file) and inject into it."""
    path = tmp_path / "mcp.json"
    if config is not None:
        path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_PATH", str(path))
    monkeypatch.setenv("MCP_CLIENT", client)
    monkeypatch.setenv("MCP_PYTHON", "/venv/bin/python")
    # No data/.shared-service.json under this install, so the stdio path runs.
    monkeypatch.setenv("MCP_SERVER", str(tmp_path / "install/src/server.py"))
    monkeypatch.setenv("MCP_IS_NIXOS", "true" if nixos else "false")
    monkeypatch.setenv("MCP_NIX_LD_LIB_PATH", NIX_LD)
    script = (REPO_ROOT / "scripts/init_repo.sh").read_text()
    injection = script.split('    python -c "\n', 1)[1].split('\n" &&', 1)[0]
    exec(compile(injection, "init_repo.sh:inject_mcp_config", "exec"), {})
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.mark.parametrize("entry", ["x", ["x"], None])
def test_shell_injection_replaces_non_object_entry(tmp_path, monkeypatch, entry):
    config = _run_shell_injection(tmp_path, monkeypatch, {"mcpServers": {"Agents-Core": entry}})
    assert config["mcpServers"]["Agents-Core"] == {
        "command": "/venv/bin/python",
        "args": [str(tmp_path / "install/src/server.py")],
    }


def test_shell_injection_replaces_non_object_env_on_nixos(tmp_path, monkeypatch):
    config = _run_shell_injection(
        tmp_path, monkeypatch, {"mcpServers": {"Agents-Core": {"env": []}}}, nixos=True
    )
    assert config["mcpServers"]["Agents-Core"]["env"] == {"LD_LIBRARY_PATH": NIX_LD}


def test_shell_injection_prepends_nix_ld_path_once(tmp_path, monkeypatch):
    original = {
        "mcpServers": {
            "Agents-Core": {"cwd": "/work", "env": {"LD_LIBRARY_PATH": "/opt/lib", "A": "1"}},
            "other": {"command": "other"},
        }
    }
    first = _run_shell_injection(tmp_path, monkeypatch, original, nixos=True)
    second = _run_shell_injection(tmp_path, monkeypatch, nixos=True)

    assert first == second
    entry = second["mcpServers"]["Agents-Core"]
    assert entry["cwd"] == "/work"
    assert entry["env"] == {"LD_LIBRARY_PATH": f"{NIX_LD}:/opt/lib", "A": "1"}
    assert second["mcpServers"]["other"] == {"command": "other"}


def test_shell_injection_keeps_ld_path_that_already_has_nix_ld(tmp_path, monkeypatch):
    value = f"/opt/lib:{NIX_LD}:/usr/lib"
    config = _run_shell_injection(
        tmp_path,
        monkeypatch,
        {"mcpServers": {"Agents-Core": {"env": {"LD_LIBRARY_PATH": value}}}},
        nixos=True,
    )
    assert config["mcpServers"]["Agents-Core"]["env"]["LD_LIBRARY_PATH"] == value


@pytest.mark.parametrize("original", [[{"mcpServers": {}}], {"mcpServers": "x"}, {"mcpServers": []}])
def test_shell_injection_refuses_unexpected_shapes(tmp_path, monkeypatch, original):
    with pytest.raises(SystemExit) as exc:
        _run_shell_injection(tmp_path, monkeypatch, original)

    assert exc.value.code == 1
    assert json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8")) == original


@pytest.fixture
def run_helper(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "scripts" / "_helpers"))
    inject_mcp = importlib.import_module("inject_mcp")
    path = tmp_path / "mcp.json"

    def run(config=None, *options):
        """Write ``config`` (None re-runs on the current file) and inject into it."""
        if config is not None:
            path.write_text(json.dumps(config), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["inject_mcp.py", str(path), "/venv/bin/python", "/srv/server.py", *options])
        inject_mcp.main()
        return json.loads(path.read_text(encoding="utf-8"))

    return run


def test_helper_preserves_user_fields_across_reruns(run_helper):
    original = {
        "mcpServers": {
            "Agents-Core": {"command": "old", "cwd": "/work", "env": {"A": "1"}},
            "other": {"command": "other"},
        }
    }
    run_helper(original)
    config = run_helper()

    assert config["mcpServers"]["Agents-Core"] == {
        "command": "/venv/bin/python",
        "args": ["/srv/server.py"],
        "cwd": "/work",
        "env": {"A": "1"},
    }
    assert config["mcpServers"]["other"] == {"command": "other"}


def test_helper_replaces_non_object_entry(run_helper):
    config = run_helper({"mcpServers": {"Agents-Core": "x"}})
    assert config["mcpServers"]["Agents-Core"] == {"command": "/venv/bin/python", "args": ["/srv/server.py"]}


@pytest.mark.parametrize("original", [[{"mcpServers": {}}], {"mcpServers": "x"}, {"mcpServers": []}])
def test_helper_refuses_unexpected_shapes(run_helper, tmp_path, original):
    with pytest.raises(SystemExit) as exc:
        run_helper(original)

    assert exc.value.code == 1
    assert json.loads((tmp_path / "mcp.json").read_text(encoding="utf-8")) == original


@pytest.mark.parametrize("client", ["claude", "cursor", "desktop", "antigravity"])
def test_shared_shell_injection_uses_explicit_client_and_exact_destination(tmp_path, monkeypatch, client):
    from src.daemon.state import atomic_private, write_json

    home = tmp_path / "home"
    home.mkdir()
    installation = tmp_path / "installation"
    state = tmp_path / "state"
    selected_profile = home / "custom claude profile"
    selected_profile.mkdir()
    # The basename and directory deliberately do not identify a client.
    destination = tmp_path / "custom registrations" / "selected.json"
    destination.parent.mkdir()
    original = {"mcpServers": {"Agents-Core": {"command": "old-python", "args": ["old.py"], "disabled": True},
                                "other": {"command": "unrelated"}}, "userSetting": True}
    destination.write_text(json.dumps(original))
    defaults = [home / ".claude.json", home / ".cursor/mcp.json",
                home / "Library/Application Support/Claude/claude_desktop_config.json"]
    for path in defaults:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"Inactive default file\r\n")
    before = {path: path.read_bytes() for path in defaults}
    write_json(installation / "data/.shared-service.json", {"directory": str(state)})
    write_json(state / "service.json", {"installation": str(installation), "port": 8765, "node": sys.executable})
    atomic_private(state / "token", "private-test-token-only")
    write_json(selected_profile / "settings.json", {"permissions": {"deny": ["existing-rule"]}, "userSetting": True})
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(selected_profile))
    monkeypatch.setenv("CLAUDE_CONFIG_PATH", str(destination))
    monkeypatch.setenv("MCP_CLIENT", client)
    monkeypatch.setenv("MCP_PYTHON", sys.executable)
    monkeypatch.setenv("MCP_SERVER", str(installation / "src/server.py"))
    script = (REPO_ROOT / "scripts/init_repo.sh").read_text()
    injection = script.split('    python -c "\n', 1)[1].split('\n" &&', 1)[0]

    with pytest.raises(SystemExit) as error:
        exec(compile(injection, "init_repo.sh:inject_mcp_config", "exec"), {})

    assert error.value.code == 0
    result = json.loads(destination.read_text())
    name = "Agents-Core-Desktop" if client == "desktop" else "Agents-Core"
    entry = result["mcpServers"][name]
    assert entry["disabled"] is True
    assert result["userSetting"] is True
    assert result["mcpServers"]["other"] == original["mcpServers"]["other"]
    assert {path: path.read_bytes() for path in defaults} == before
    if client == "desktop":
        assert "Agents-Core" not in result["mcpServers"]
        assert entry["command"] == sys.executable
        assert entry["args"][0] == str(installation / "bridge/stdio.mjs")
        settings = json.loads((selected_profile / "settings.json").read_text())
        assert settings["userSetting"] is True
        assert set(settings["permissions"]["deny"]) == {
            "existing-rule", "mcp__Agents-Core-Desktop__*", "mcp__Agents_Core_Desktop__*"}
        assert not (home / ".claude/settings.json").exists()
    elif client in ("claude", "antigravity"):
        # The user scope serves every project: a bridge per session names its own (#253).
        assert entry["command"] == sys.executable
        assert entry["args"][0] == str(installation / "bridge/stdio.mjs")
        assert json.loads(Path(entry["args"][1]).read_text())["workspace"] == "auto"
        assert "url" not in entry
    else:
        assert entry["url"] == "http://127.0.0.1:8765/mcp"
        assert "command" not in entry and "args" not in entry


# --- the Claude desktop app's entry (#231) -------------------------------------------------

DESKTOP_ORIGINAL = {"mcpServers": {"Agents-Core": {"command": "old", "env": {"A": "1"}},
                                   "other": {"command": "other"}}}


def test_shell_injection_names_the_desktop_entry_and_takes_over_the_old_one(tmp_path, monkeypatch):
    """The app injects its servers into its Code-tab sessions; Claude Code denies this name (#231)."""
    from src.client_paths import DESKTOP_SERVER
    config = _run_shell_injection(tmp_path, monkeypatch, DESKTOP_ORIGINAL, client="desktop")
    assert set(config["mcpServers"]) == {DESKTOP_SERVER, "other"}
    assert config["mcpServers"][DESKTOP_SERVER] == {
        "command": "/venv/bin/python", "args": [str(tmp_path / "install/src/server.py")], "env": {"A": "1"}}
    assert _run_shell_injection(tmp_path, monkeypatch, client="desktop") == config  # idempotent


def test_helper_names_the_desktop_entry_and_takes_over_the_old_one(run_helper):
    config = run_helper(DESKTOP_ORIGINAL, "--desktop")
    assert set(config["mcpServers"]) == {"Agents-Core-Desktop", "other"}
    assert config["mcpServers"]["Agents-Core-Desktop"] == {
        "command": "/venv/bin/python", "args": ["/srv/server.py"], "env": {"A": "1"}}
    assert run_helper(None, "--desktop") == config  # idempotent
    assert run_helper(None)["mcpServers"]["Agents-Core"] == {"command": "/venv/bin/python",
                                                             "args": ["/srv/server.py"]}  # default name


def test_with_both_names_the_old_entry_takes_over_as_migrate_does(run_helper):
    config = run_helper({"mcpServers": {"Agents-Core": {"env": {"A": "1"}},
                                        "Agents-Core-Desktop": {"disabled": True}}}, "--desktop")
    assert config["mcpServers"] == {"Agents-Core-Desktop": {
        "env": {"A": "1"}, "command": "/venv/bin/python", "args": ["/srv/server.py"]}}


def test_helper_reads_a_config_saved_with_a_bom(run_helper, tmp_path):
    (tmp_path / "mcp.json").write_bytes(codecs.BOM_UTF8 + json.dumps({"mcpServers": {"other": {}}}).encode())
    assert set(run_helper(None)["mcpServers"]) == {"Agents-Core", "other"}


BRIDGE = {"command": "/usr/bin/node", "args": ["/install/bridge/stdio.mjs", "/state/bridges/claude-desktop-routing.json"]}


@pytest.mark.parametrize("installed", [True, False])
def test_setup_keeps_the_shared_services_entries_only_while_it_is_installed(tmp_path, monkeypatch, installed):
    """migrate's bridge stays while the service is installed; after uninstall setup writes stdio again."""
    monkeypatch.syspath_prepend(str(REPO_ROOT / "scripts" / "_helpers"))
    inject_mcp = importlib.import_module("inject_mcp")
    server = tmp_path / "install" / "src" / "server.py"
    if installed:
        (tmp_path / "install" / "data").mkdir(parents=True)
        (tmp_path / "install" / "data" / ".shared-service.json").write_text("{}")
    path = tmp_path / "claude_desktop_config.json"
    path.write_text(json.dumps({"mcpServers": {"Agents-Core-Desktop": BRIDGE, "Agents-Core": {"command": "old"}}}))
    monkeypatch.setattr(sys, "argv", ["inject_mcp.py", str(path), "/venv/bin/python", str(server), "--desktop"])
    inject_mcp.main()
    servers = json.loads(path.read_text())["mcpServers"]
    if installed:
        assert servers == {"Agents-Core-Desktop": BRIDGE}  # the older duplicate goes
    else:
        assert servers == {"Agents-Core-Desktop": {"command": "/venv/bin/python", "args": [str(server)]}}


@pytest.fixture
def deny_helper(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(REPO_ROOT / "scripts" / "_helpers"))
    deny = importlib.import_module("deny_desktop_mcp")
    path = tmp_path / "claude profile" / "settings.json"

    def run(settings=None):
        if settings is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(settings), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["deny_desktop_mcp.py", str(path)])
        deny.main()
        return json.loads(path.read_text(encoding="utf-8"))

    return run


def test_deny_helper_adds_the_desktop_rules_once_and_keeps_the_rest(deny_helper):
    from src.client_paths import DESKTOP_DENY_RULES
    original = {"permissions": {"deny": ["existing-rule"], "allow": ["x"]}, "userSetting": True}
    settings = deny_helper(original)
    assert settings["permissions"] == {"deny": ["existing-rule", *DESKTOP_DENY_RULES], "allow": ["x"]}
    assert settings["userSetting"] is True
    assert deny_helper() == settings


def test_deny_helper_reads_settings_saved_with_a_bom(deny_helper, tmp_path):
    """PowerShell 5.1 writes UTF-8 with a BOM; the rewrite has none."""
    from src.client_paths import DESKTOP_DENY_RULES
    path = tmp_path / "claude profile" / "settings.json"
    path.parent.mkdir(parents=True)
    path.write_bytes(codecs.BOM_UTF8 + json.dumps({"userSetting": True}).encode())
    assert deny_helper() == {"userSetting": True, "permissions": {"deny": list(DESKTOP_DENY_RULES)}}
    assert not path.read_bytes().startswith(codecs.BOM_UTF8)
    assert [entry.name for entry in path.parent.iterdir()] == ["settings.json"]  # no temporary file left


def test_deny_helper_creates_missing_settings(deny_helper, tmp_path):
    from src.client_paths import DESKTOP_DENY_RULES
    assert deny_helper() == {"permissions": {"deny": list(DESKTOP_DENY_RULES)}}


@pytest.mark.parametrize("original", [[], {"permissions": []}, {"permissions": {"deny": "x"}}])
def test_deny_helper_refuses_unexpected_shapes(deny_helper, tmp_path, original):
    with pytest.raises(SystemExit) as exc:
        deny_helper(original)
    assert exc.value.code == 1
    assert json.loads((tmp_path / "claude profile" / "settings.json").read_text(encoding="utf-8")) == original


def test_shell_injection_keeps_the_shared_services_entry_while_it_is_installed(tmp_path, monkeypatch):
    if sys.platform == "darwin":
        pytest.skip("on macOS the installed service takes the ClientMigration branch")
    (tmp_path / "install" / "data").mkdir(parents=True)
    (tmp_path / "install" / "data" / ".shared-service.json").write_text("{}")
    with pytest.raises(SystemExit) as exc:
        _run_shell_injection(tmp_path, monkeypatch, {"mcpServers": {"Agents-Core-Desktop": BRIDGE,
                                                                    "Agents-Core": {"command": "old"}}},
                             client="desktop")
    assert exc.value.code == 0
    assert json.loads((tmp_path / "mcp.json").read_text())["mcpServers"] == {"Agents-Core-Desktop": BRIDGE}
