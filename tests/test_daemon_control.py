from unittest.mock import MagicMock

import pytest

from src.daemon import control


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
