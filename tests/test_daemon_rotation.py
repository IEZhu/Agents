import json

import pytest

from src.daemon.clients import ClientMigration
from src.daemon.rotation import rotate_token
from src.daemon.state import atomic_private, write_json
from tests.test_daemon_update import installation


@pytest.mark.parametrize("fail_ready", [False, True])
def test_rotation_is_coherent_or_restores_token_and_clients(installation, tmp_path, fail_ready):
    controller, _, _, _ = installation
    old = "old-private-token-" * 4
    write_json(controller.directory / "service.json", controller.config)
    atomic_private(controller.directory / "token", old + "\n")
    target = tmp_path / "client.json"
    content = json.dumps({"headers": {"Authorization": "Bearer " + old}})
    ClientMigration(controller.directory).apply([(target, content, True)])
    controller.fail_ready = 1 if fail_ready else 0
    if fail_ready:
        with pytest.raises(RuntimeError, match="warmup"):
            rotate_token(controller)
    else:
        assert rotate_token(controller)["state"] == "rotated"
    token = (controller.directory / "token").read_text().strip()
    assert (token == old) == fail_ready
    assert json.loads(target.read_text())["headers"]["Authorization"] == "Bearer " + token
    assert controller.running
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
