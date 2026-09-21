from unittest.mock import MagicMock

import pytest

from src.daemon import control
from src.daemon.clients import ClientMigration
from src.daemon.state import read_json


@pytest.mark.parametrize("cache_state", ["missing_reference", "reference_is_directory", "missing_snapshot"])
def test_install_reports_missing_model_cache_before_writing_service(tmp_path, monkeypatch, cache_state):
    root = tmp_path / "install"
    root.mkdir()
    monkeypatch.setattr(control, "__file__", str(root / "src/daemon/control.py"))
    monkeypatch.setattr(control.socket, "socket", MagicMock())
    monkeypatch.setattr(control.shutil, "which", lambda name: "/usr/bin/" + name)
    plist = tmp_path / "launchagent.plist"
    monkeypatch.setattr(control.Controller, "plist", property(lambda self: plist))
    cache = tmp_path / "cache"
    monkeypatch.setenv("FASTEMBED_CACHE_DIR", str(cache))
    reference = cache / "models--qdrant--multilingual-e5-large-onnx/refs/main"
    if cache_state == "reference_is_directory":
        reference.mkdir(parents=True)
    elif cache_state == "missing_snapshot":
        reference.parent.mkdir(parents=True)
        reference.write_text("missing-revision\n")
    controller = control.Controller(tmp_path / "state")

    with pytest.raises(RuntimeError, match="The e5-large model must be cached before service installation"):
        controller.install()

    assert not (controller.directory / "service.json").exists()
    assert not (controller.directory / "token").exists()
    assert not (root / "data/.shared-service.json").exists()
    assert not plist.exists()


@pytest.mark.parametrize("node_source", ["relative", "absolute", "discovered_relative", "missing"])
def test_install_persists_node_path_for_other_working_directories(tmp_path, monkeypatch, node_source):
    root = tmp_path / "install"
    root.mkdir()
    monkeypatch.setattr(control, "__file__", str(root / "src/daemon/control.py"))
    monkeypatch.setattr(control.socket, "socket", MagicMock())
    monkeypatch.setattr(control.Controller, "plist", property(lambda self: tmp_path / "launchagent.plist"))
    cache = tmp_path / "cache"
    model = cache / "models--qdrant--multilingual-e5-large-onnx"
    (model / "refs").mkdir(parents=True)
    (model / "refs/main").write_text("cached-revision\n")
    (model / "snapshots/cached-revision").mkdir(parents=True)
    monkeypatch.setenv("FASTEMBED_CACHE_DIR", str(cache))
    node = tmp_path / "bin/node"
    node.parent.mkdir()
    node.touch()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(control.shutil, "which", lambda name: "/usr/bin/git" if name == "git" else
                        "bin/node" if node_source == "discovered_relative" else None)
    selected = {"relative": "bin/node", "absolute": str(node)}.get(node_source)
    controller = control.Controller(tmp_path / "state")

    controller.install(node=selected)

    expected = None if node_source == "missing" else str(node)
    assert read_json(controller.directory / "service.json")["node"] == expected
    monkeypatch.chdir(root)
    migration = ClientMigration(controller.directory)
    if expected is None:
        with pytest.raises(ValueError, match="Node executable"):
            migration.bridge()
    else:
        assert migration.bridge()["command"] == expected
