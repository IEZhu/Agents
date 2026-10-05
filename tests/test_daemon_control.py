from pathlib import Path
import plistlib
from unittest.mock import MagicMock

import pytest

from src.daemon import control
from src.daemon.clients import ClientMigration
from src.daemon.state import read_json
from src.engine.embedding_prompts import COMPLETE, local_copy, pinned_revision
from src.model_migration import DEFAULT_MODEL, GENERATION



@pytest.fixture(autouse=True)
def no_real_scheduler(monkeypatch):
    """``install`` removes the scheduled sync run (#168); tests never reach the real scheduler."""
    calls = []
    monkeypatch.setattr(control, "stop_scheduled_sync", lambda installation=None: calls.append(installation) or "unchanged")
    return calls

def _default_model_copy(cache):
    """A published plain-file copy of the default model, as materialize() leaves it."""
    copy = Path(local_copy(DEFAULT_MODEL, str(cache)))
    copy.mkdir(parents=True)
    (copy / COMPLETE).write_text("revision")
    return copy


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

    with pytest.raises(RuntimeError, match="The intfloat/multilingual-e5-large model must be cached before service installation"):
        controller.install(model="intfloat/multilingual-e5-large")

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
    _default_model_copy(cache)
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


def test_install_treats_blank_cache_dir_as_default(tmp_path, monkeypatch):
    # `FASTEMBED_CACHE_DIR=` from env.example must not resolve to the working directory.
    root = tmp_path / "install"
    root.mkdir()
    monkeypatch.setattr(control, "__file__", str(root / "src/daemon/control.py"))
    monkeypatch.setattr(control.socket, "socket", MagicMock())
    monkeypatch.setattr(control.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(control.Controller, "plist", property(lambda self: tmp_path / "launchagent.plist"))
    home = tmp_path / "home"
    cache = home / ".cache/fastembed"
    _default_model_copy(cache)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("FASTEMBED_CACHE_DIR", "")
    monkeypatch.chdir(tmp_path)
    controller = control.Controller(tmp_path / "state")

    controller.install()

    assert read_json(controller.directory / "service.json")["model_cache"] == str(cache)


def _installable(tmp_path, monkeypatch):
    root = tmp_path / "install"
    root.mkdir()
    monkeypatch.setattr(control, "__file__", str(root / "src/daemon/control.py"))
    monkeypatch.setattr(control.socket, "socket", MagicMock())
    monkeypatch.setattr(control.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(control.Controller, "plist", property(lambda self: tmp_path / "launchagent.plist"))
    cache = tmp_path / "cache"
    monkeypatch.setenv("FASTEMBED_CACHE_DIR", str(cache))
    return control.Controller(tmp_path / "state"), cache


def test_install_pins_the_default_models_plain_file_copy(tmp_path, monkeypatch):
    controller, cache = _installable(tmp_path, monkeypatch)
    copy = _default_model_copy(cache)

    controller.install()

    config = read_json(controller.directory / "service.json")
    assert config["model"] == DEFAULT_MODEL
    assert config["model_path"] == str(copy)
    assert config["model_artifact"] == pinned_revision(DEFAULT_MODEL)  # what the standalone fingerprint uses
    assert config["model_generation"] == GENERATION


def test_install_refuses_an_incomplete_default_model_copy(tmp_path, monkeypatch):
    controller, cache = _installable(tmp_path, monkeypatch)
    _default_model_copy(cache).joinpath(COMPLETE).unlink()  # an interrupted download

    with pytest.raises(RuntimeError, match=f"The {DEFAULT_MODEL} model must be downloaded"):
        controller.install()
    assert not (controller.directory / "service.json").exists()


def test_install_keeps_e5_selectable_by_its_cached_snapshot(tmp_path, monkeypatch):
    controller, cache = _installable(tmp_path, monkeypatch)
    model = cache / "models--qdrant--multilingual-e5-large-onnx"
    (model / "refs").mkdir(parents=True)
    (model / "refs/main").write_text("cached-revision\n")
    (model / "snapshots/cached-revision").mkdir(parents=True)

    controller.install(model="intfloat/multilingual-e5-large")

    config = read_json(controller.directory / "service.json")
    assert config["model"] == "intfloat/multilingual-e5-large"
    assert config["model_path"] == str(model / "snapshots/cached-revision")
    assert config["model_artifact"] == "models--qdrant--multilingual-e5-large-onnx:cached-revision"


def test_install_refuses_a_model_it_cannot_pin(tmp_path, monkeypatch):
    controller, _cache = _installable(tmp_path, monkeypatch)
    with pytest.raises(RuntimeError, match="cannot pin"):
        controller.install(model="sentence-transformers/all-MiniLM-L6-v2")


@pytest.mark.parametrize("nonce", ["-dash-first", "_underscore-first", "plain"])
def test_probation_nonce_reaches_serve_whatever_its_first_character(tmp_path, monkeypatch, nonce):
    # secrets.token_urlsafe can start with "-"; as a separate argument argparse read it as an option (#183).
    plist = tmp_path / "launchagent.plist"
    monkeypatch.setattr(control.Controller, "plist", property(lambda self: plist))
    controller = control.Controller(tmp_path / "state")
    controller.config = {"python": "/venv/bin/python", "installation": str(tmp_path), "path": "/usr/bin"}
    controller.write_plist(probation=nonce)
    arguments = plistlib.loads(plist.read_bytes())["ProgramArguments"]
    assert arguments[:3] == ["/venv/bin/python", "-m", "src.daemon"]
    received = {}
    monkeypatch.setattr("src.daemon.bootstrap.serve",
                        lambda directory, probation: received.update(probation=probation))

    control.main(arguments[3:])  # what launchd passes after `python -m src.daemon`

    assert received == {"probation": nonce}


def test_install_removes_the_scheduled_sync_run(tmp_path, monkeypatch, no_real_scheduler):
    """The daemon runs the sync loop itself (#167); a job from before would compete with it."""
    root = tmp_path / "install"
    root.mkdir()
    monkeypatch.setattr(control, "__file__", str(root / "src/daemon/control.py"))
    monkeypatch.setattr(control.socket, "socket", MagicMock())
    monkeypatch.setattr(control.shutil, "which", lambda name: "/usr/bin/" + name)
    monkeypatch.setattr(control.Controller, "plist", property(lambda self: tmp_path / "launchagent.plist"))
    monkeypatch.setattr(control, "pin_model", lambda model, cache: {"model_artifact": "a", "model_path": "p"})
    result = control.Controller(tmp_path / "state").install()
    assert result["scheduled_sync"] == "unchanged"
    assert no_real_scheduler == [root]
