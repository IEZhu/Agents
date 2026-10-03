from pathlib import Path
import json
import subprocess
import sys

import pytest

from src import self_update
from src.daemon.state import write_json, read_json
from src.daemon.update import offline_update, recover
from src.daemon.bootstrap import assert_service_safe
from src.file_lock import file_lock
from src.model_migration import DEFAULT_MODEL, GENERATION


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


class FakeController:
    def __init__(self, root, directory):
        self.directory = directory
        self.config = {"installation": str(root), "git": "/usr/bin/git", "model": "test",
                       "model_cache": "/unused", "model_artifact": "test", "model_path": "/unused",
                       "path": "/usr/bin:/bin", "autostart": True, "python": sys.executable,
                       "model_generation": GENERATION}
        self.running = True
        self.fail_ready = 0
        self.fail_drain = False
        self.probes = 0

    def status(self): return {"state": "ready" if self.running else "stopped"}
    def _stop(self):
        if self.fail_drain:
            self.fail_drain = False
            raise TimeoutError("drain")
        self.running = False
    def _start(self, probation=None):
        assert probation
        # Probation must acquire a normal reader lease without deadlocking.
        with file_lock(Path(self.config["installation"]) / "data/.sessions.lock", shared=True, blocking=False): pass
        assert_service_safe(self.directory, probation=probation)
        self.running = True
        self.probes += 1
    def wait_ready(self):
        if self.fail_ready:
            self.fail_ready -= 1
            raise RuntimeError("candidate warmup failed")
        return {"state": "ready", "pid": 123}
    def write_plist(self): pass


@pytest.fixture
def installation(tmp_path, monkeypatch):
    root = tmp_path / "install"; root.mkdir()
    git(root, "init", "-b", "main")
    git(root, "config", "user.email", "test@example.invalid")
    git(root, "config", "user.name", "Test")
    (root / ".gitignore").write_text("data/\n")
    (root / "README.md").write_text("old")
    git(root, "add", "."); git(root, "commit", "-m", "old")
    old = git(root, "rev-parse", "HEAD")
    (root / "README.md").write_text("new")
    git(root, "commit", "-am", "new")
    target = git(root, "rev-parse", "HEAD")
    remote = tmp_path / "remote.git"
    subprocess.run(["git", "clone", "--bare", str(root), str(remote)], check=True, capture_output=True)
    git(root, "reset", "--hard", old)
    git(root, "remote", "add", "origin", str(remote))
    data = root / "data"; data.mkdir()
    (data / "skills_store.npz").write_text("old-index")
    directory = tmp_path / "state"; directory.mkdir()
    controller = FakeController(root, directory)
    write_json(directory / "service.json", controller.config)
    for name, filename in [("STATE_FILE", ".last_update.json"), ("CHECK_STAMP", ".last_update_check")]:
        monkeypatch.setattr(self_update, name, str(data / filename))
    def reindex(root, timeout):
        assert not controller.running
        with pytest.raises(BlockingIOError):
            with file_lock(Path(root) / "data/.sessions.lock", shared=True, blocking=False): pass
        (Path(root) / "data/skills_store.npz").write_text("new-index")
        return True
    monkeypatch.setattr(self_update, "_run_reindex", reindex)
    # Changes to os.environ made by the controller should not escape this test.
    for key in ("AGENTS_AUTO_UPDATE", "EMBEDDING_MODEL", "FASTEMBED_CACHE_DIR", "AGENTS_MODEL_ARTIFACT", "AGENTS_MODEL_PATH", "PATH"):
        monkeypatch.setenv(key, __import__('os').environ.get(key, ""))
    return controller, root, old, target


def test_update_probes_after_writer_lease_released(installation):
    controller, root, old, target = installation
    result = offline_update(controller)
    assert result["state"] == "UPDATED"
    assert git(root, "rev-parse", "HEAD") == target
    assert controller.probes == 1
    assert not (controller.directory / "transaction.json").exists()


def test_failed_ready_restores_code_and_indexes(installation):
    controller, root, old, target = installation
    controller.fail_ready = 1
    assert offline_update(controller)["state"] == "rolled_back"
    assert git(root, "rev-parse", "HEAD") == old
    assert (root / "data/skills_store.npz").read_text() == "old-index"
    assert controller.probes == 2


def test_failed_rollback_is_not_retried_and_keeps_recovery_barriers(installation, monkeypatch):
    from src.daemon import update
    controller, _, _, _ = installation
    controller.fail_ready = 1
    failure = RuntimeError("Code rollback failed; maintenance retained")
    attempts = []

    def fail_restore(*args):
        attempts.append(1)
        raise failure

    monkeypatch.setattr(update, "restore_files", fail_restore)
    with pytest.raises(RuntimeError) as caught:
        offline_update(controller)

    assert caught.value is failure
    assert len(attempts) == 1
    assert (controller.directory / "transaction.json").exists()
    assert (controller.directory / "maintenance.json").exists()
    assert not controller.running


def test_stdio_reader_defers_update_and_restores_runtime(installation):
    controller, root, old, target = installation
    with file_lock(root / "data/.sessions.lock", shared=True):
        with pytest.raises(BlockingIOError): offline_update(controller)
    assert git(root, "rev-parse", "HEAD") == old
    assert controller.running
    assert not (controller.directory / "maintenance.json").exists()


def test_drain_timeout_preserves_runtime_and_tree(installation):
    controller, root, old, target = installation
    controller.fail_drain = True
    with pytest.raises(TimeoutError): offline_update(controller)
    assert controller.running
    assert git(root, "rev-parse", "HEAD") == old
    assert not (controller.directory / "transaction.json").exists()


def test_recovery_before_mutation_and_fail_closed_journal(installation):
    controller, root, old, target = installation
    write_json(controller.directory / "transaction.json", {"was_running": True, "phase": "draining"})
    with pytest.raises(RuntimeError): assert_service_safe(controller.directory)
    assert recover(controller)["state"] == "recovered_before_mutation"
    assert controller.running


def test_dependency_changes_rejected_before_merge(installation):
    controller, root, old, target = installation
    git(root, "reset", "--hard", target)
    (root / "requirements.txt").write_text("new-package==1\n")
    git(root, "add", "."); git(root, "commit", "-m", "dependencies")
    git(root, "push", "origin", "main")
    git(root, "reset", "--hard", old)
    with pytest.raises(RuntimeError, match="Dependency manifests"):
        offline_update(controller)
    assert git(root, "rev-parse", "HEAD") == old
    assert controller.running


@pytest.fixture
def model_switch(installation, monkeypatch):
    """An installation whose service.json predates the current model generation."""
    from src.daemon import control, update
    controller, root, old, target = installation
    controller.config = {key: value for key, value in controller.config.items() if key != "model_generation"}
    write_json(controller.directory / "service.json", controller.config)
    downloads = []

    def switched(config):
        assert controller.running  # the download happens before the service stops
        with pytest.raises(BlockingIOError):  # and under the control lock
            with file_lock(controller.directory / "control.lock", blocking=False): pass
        downloads.append(config["model"])
        return {**config, "model": DEFAULT_MODEL, "model_generation": GENERATION,
                "model_artifact": "export@rev", "model_path": "/cache/local/copy"}

    rebuilt = []

    def reindex(current):
        assert not current.running
        with pytest.raises(BlockingIOError):  # the leases stay held while the stores rebuild
            with file_lock(root / "data/.sessions.lock", shared=True, blocking=False): pass
        assert read_json(current.directory / "service.json")["model"] == DEFAULT_MODEL
        rebuilt.append(current.config["model"])
        (root / "data/skills_store.npz").write_text("default-model-index")

    monkeypatch.setattr(control, "switched_model_config", switched)
    monkeypatch.setattr(update, "reindex", reindex)
    return controller, root, old, target, downloads, rebuilt


def test_update_switches_the_service_to_the_default_model_once(model_switch):
    controller, root, old, target, downloads, rebuilt = model_switch
    result = offline_update(controller)

    assert result["state"] == "UPDATED" and result["model"] == DEFAULT_MODEL and result["model_from"] == "test"
    assert git(root, "rev-parse", "HEAD") == target
    config = read_json(controller.directory / "service.json")
    assert (config["model"], config["model_path"], config["model_generation"]) == (DEFAULT_MODEL, "/cache/local/copy", GENERATION)
    assert config["autostart"] is True and config["installation"] == str(root)
    assert rebuilt == [DEFAULT_MODEL]
    assert (root / "data/skills_store.npz").read_text() == "default-model-index"
    assert controller.probes == 1
    assert not (controller.directory / "transaction.json").exists()

    # A later update keeps the model, including one the operator chose after the switch.
    assert offline_update(controller)["state"] == "UP_TO_DATE"
    assert downloads == ["test"] and rebuilt == [DEFAULT_MODEL]


def test_up_to_date_installation_still_switches_its_model(model_switch):
    controller, root, old, target, downloads, rebuilt = model_switch
    git(root, "merge", "--ff-only", target)

    result = offline_update(controller)

    assert result["state"] == "UP_TO_DATE" and result["model"] == DEFAULT_MODEL
    assert read_json(controller.directory / "service.json")["model"] == DEFAULT_MODEL
    assert controller.probes == 1


@pytest.mark.parametrize("failure", ["probation", "reindex"])
def test_failed_model_switch_restores_model_code_and_indexes(model_switch, monkeypatch, failure):
    from src.daemon import update
    controller, root, old, target, downloads, rebuilt = model_switch
    if failure == "probation":
        controller.fail_ready = 1
        assert offline_update(controller)["state"] == "rolled_back"
    else:
        def broken(current):
            (root / "data/skills_store.npz").write_text("torn")
            raise RuntimeError("reindex failed")
        monkeypatch.setattr(update, "reindex", broken)
        with pytest.raises(RuntimeError, match="reindex failed"):
            offline_update(controller)

    config = read_json(controller.directory / "service.json")
    assert config["model"] == "test" and "model_generation" not in config
    assert controller.config["model"] == "test"
    assert git(root, "rev-parse", "HEAD") == old
    assert (root / "data/skills_store.npz").read_text() == "old-index"
    assert controller.running
    assert not (controller.directory / "transaction.json").exists()


def test_recover_restores_an_interrupted_model_switch(model_switch, monkeypatch):
    from src.daemon import update
    controller, root, old, target, downloads, rebuilt = model_switch
    git(root, "merge", "--ff-only", target)

    def crash(current):
        raise KeyboardInterrupt  # the controller dies; rollback is left to recover
    monkeypatch.setattr(update, "reindex", crash)
    rollback = update.rollback
    monkeypatch.setattr(update, "rollback", lambda *args: None)
    with pytest.raises(KeyboardInterrupt):
        offline_update(controller)
    assert read_json(controller.directory / "service.json")["model"] == DEFAULT_MODEL
    monkeypatch.setattr(update, "rollback", rollback)

    controller.config = read_json(controller.directory / "service.json")
    assert recover(controller)["state"] == "rolled_back"
    assert read_json(controller.directory / "service.json")["model"] == "test"
    assert (root / "data/skills_store.npz").read_text() == "old-index"
    assert not (controller.directory / "transaction.json").exists()


def test_rolled_back_file_update_leaves_the_model_switch_pending(model_switch, monkeypatch):
    controller, root, old, target, downloads, rebuilt = model_switch
    monkeypatch.setattr(self_update, "_run_reindex", lambda root, timeout: False)  # REINDEX_FAILED, rolled back

    offline_update(controller)

    config = read_json(controller.directory / "service.json")
    assert config["model"] == "test" and "model_generation" not in config
    assert rebuilt == []
    assert git(root, "rev-parse", "HEAD") == old
    assert not (controller.directory / "transaction.json").exists()


def test_update_refuses_a_service_uninstalled_meanwhile(model_switch):
    controller, root, old, target, downloads, rebuilt = model_switch
    (controller.directory / "service.json").unlink()
    with pytest.raises(RuntimeError, match="not installed"):
        offline_update(controller)
    assert downloads == [] and git(root, "rev-parse", "HEAD") == old
