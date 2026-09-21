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


@pytest.mark.parametrize("barrier", ["control_lock", "maintenance.json", "transaction.json"])
def test_restore_checks_admission_before_stopping_or_restoring(tmp_path, monkeypatch, barrier):
    state = tmp_path / "state"
    write_json(state / "service.json", {"installation": str(tmp_path)})
    if barrier != "control_lock":
        write_json(state / barrier, {"operation": "update"})

    monkeypatch.setattr(control.Controller, "_stop", lambda self: pytest.fail("Stopped before admission"))
    monkeypatch.setattr(clients, "ClientMigration", lambda *args: pytest.fail("Restored before admission"))
    lease = file_lock(state / "control.lock", blocking=False) if barrier == "control_lock" else nullcontext()

    with lease, pytest.raises(BlockingIOError if barrier == "control_lock" else RuntimeError):
        control.main(["--state", str(state), "restore-clients", str(tmp_path / "backup")])


def test_restore_holds_control_lock_through_stop_and_file_restoration(tmp_path, monkeypatch):
    state = tmp_path / "state"
    write_json(state / "service.json", {"installation": str(tmp_path)})
    operations = []

    def check_lock(operation):
        with pytest.raises(BlockingIOError):
            with file_lock(state / "control.lock", blocking=False):
                pass
        operations.append(operation)

    class Migration:
        def __init__(self, directory):
            check_lock("read_state")
        def restore(self, backup):
            check_lock("restore")

    monkeypatch.setattr(control.Controller, "_stop", lambda self: check_lock("stop"))
    monkeypatch.setattr(clients, "ClientMigration", Migration)

    control.main(["--state", str(state), "restore-clients", str(tmp_path / "backup")])

    assert operations == ["stop", "read_state", "restore"]
    with file_lock(state / "control.lock", blocking=False):
        pass
