"""MCP config injection must keep user fields and refuse configs it cannot parse.

Covers both injectors: the inline Python in ``scripts/init_repo.sh`` (extracted
the same way as in ``test_daemon_migration_guards.py``) and
``scripts/_helpers/inject_mcp.py`` used by ``init_repo.bat``.
"""
import importlib
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
NIX_LD = "/run/current-system/sw/share/nix-ld/lib"


def _run_shell_injection(tmp_path, monkeypatch, config=None, nixos=False):
    """Write ``config`` (None re-runs on the current file) and inject into it."""
    path = tmp_path / "mcp.json"
    if config is not None:
        path.write_text(json.dumps(config), encoding="utf-8")
    monkeypatch.setenv("CLAUDE_CONFIG_PATH", str(path))
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

    def run(config=None):
        """Write ``config`` (None re-runs on the current file) and inject into it."""
        if config is not None:
            path.write_text(json.dumps(config), encoding="utf-8")
        monkeypatch.setattr(sys, "argv", ["inject_mcp.py", str(path), "/venv/bin/python", "/srv/server.py"])
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
