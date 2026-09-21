from contextlib import nullcontext
from pathlib import Path
import sys

import pytest

from src.daemon import clients, control
from src.daemon.state import write_json
from src.file_lock import file_lock


@pytest.mark.parametrize("entrypoint", ["setup", "controller"])
@pytest.mark.parametrize("barrier", ["control_lock", "maintenance"])
def test_migration_checks_admission_before_reading_token_or_preparing_files(tmp_path, monkeypatch, entrypoint, barrier):
    root = tmp_path / "install"
    state = tmp_path / "state"
    home = tmp_path / "home"
    home.mkdir()
    write_json(root / "data/.shared-service.json", {"directory": str(state)})
    write_json(state / "service.json", {"installation": str(root)})
    if barrier == "maintenance":
        write_json(state / "maintenance.json", {"operation": "token_rotate"})

    def unexpected_prepare(*args, **kwargs):
        pytest.fail("Migration read state before acquiring admission")

    monkeypatch.setattr(clients, "ClientMigration", unexpected_prepare)
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(sys, "path", sys.path.copy())
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setenv("CLAUDE_CONFIG_PATH", str(home / "Library/Application Support/Claude/claude_desktop_config.json"))
    monkeypatch.setenv("MCP_PYTHON", sys.executable)
    monkeypatch.setenv("MCP_SERVER", str(root / "src/server.py"))
    script = (Path(__file__).resolve().parents[1] / "scripts/init_repo.sh").read_text()
    injection = script.split('    python -c "\n', 1)[1].split('\n" &&', 1)[0]
    lease = file_lock(state / "control.lock", blocking=False) if barrier == "control_lock" else nullcontext()

    with lease, pytest.raises(BlockingIOError if barrier == "control_lock" else RuntimeError):
        if entrypoint == "setup":
            exec(compile(injection, "init_repo.sh:inject_mcp_config", "exec"), {})
        else:
            control.main(["--state", str(state), "migrate", "--clients", "desktop"])
