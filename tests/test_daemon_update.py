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
        self.stops = 0
        self.builds = 0

    def status(self): return {"state": "ready" if self.running else "stopped"}
    def _stop(self):
        if self.fail_drain:
            self.fail_drain = False
            raise TimeoutError("drain")
        self.running = False
        self.stops += 1
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
    for name, filename in [("STATE_FILE", ".last_update.json"), ("CHECK_STAMP", ".last_update_check"),
                           ("PREPARED_MARKER", ".prepared_update.json"), ("STAGING_ROOT", ".prepared")]:
        monkeypatch.setattr(self_update, name, str(data / filename))
    from src.daemon import update

    def prepare_reindex(current, staging_dir):
        assert controller.running  # the service keeps serving while the update is built
        with pytest.raises(BlockingIOError):  # under the updater lease: no stdio server prepares meanwhile
            with file_lock(root / "data/.update.lock", blocking=False): pass
        assert self_update._subprocess_lock_fds()  # and the reindex child inherits that lease
        staged = Path(staging_dir) / "data"; staged.mkdir(parents=True, exist_ok=True)
        (staged / "skills_store.npz").write_text("new-index")
        (staged / "skills_store.json").write_text(json.dumps({"save_version": "new", "metadatas": []}))
        for name in (".skills_hash", ".implants_hash"): (staged / name).write_text("new")
        controller.builds += 1
        return True
    monkeypatch.setattr(update, "prepare_reindex", prepare_reindex)
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


def test_update_is_built_while_serving_and_the_stop_only_activates_it(installation):
    controller, root, old, target = installation
    assert offline_update(controller)["state"] == "UPDATED"
    assert controller.builds == 1 and controller.stops == 1
    assert (root / "data/skills_store.npz").read_text() == "new-index"
    assert (root / "data/.skills_hash").read_text() == "new"
    assert not (root / "data/.prepared_update.json").exists()
    assert not (root / "data/.prepared").exists()


def test_nothing_to_apply_leaves_the_service_running(installation):
    controller, root, old, target = installation
    git(root, "merge", "--ff-only", target)
    assert offline_update(controller)["state"] == "UP_TO_DATE"
    assert controller.running and controller.stops == 0 and controller.probes == 0


def test_failed_preparation_leaves_the_service_running(installation, monkeypatch):
    from src.daemon import update
    controller, root, old, target = installation
    monkeypatch.setattr(update, "prepare_reindex", lambda current, staging_dir: False)
    assert offline_update(controller)["state"] == "PREPARE_REINDEX_FAILED"
    assert controller.running and controller.stops == 0
    assert git(root, "rev-parse", "HEAD") == old
    assert not (root / "data/.prepared_update.json").exists()
    assert not (controller.directory / "maintenance.json").exists()


def test_busy_after_preparation_defers_and_the_next_run_activates_it(installation):
    controller, root, old, target = installation
    busy = {"state": "deferred", "reason": "service is busy"}
    checks = iter([None, busy])
    assert offline_update(controller, expected_target=target, precheck=lambda: next(checks)) == busy
    assert controller.running and controller.stops == 0 and controller.builds == 1
    assert git(root, "rev-parse", "HEAD") == old

    assert offline_update(controller, expected_target=target, precheck=lambda: None)["state"] == "UPDATED"
    assert controller.builds == 1  # the deferred run's build is activated, not rebuilt
    assert git(root, "rev-parse", "HEAD") == target
    assert (root / "data/skills_store.npz").read_text() == "new-index"


def test_moved_target_is_refused_before_the_service_stops(installation):
    from src.daemon.update import TargetMoved
    controller, root, old, target = installation
    with pytest.raises(TargetMoved):
        offline_update(controller, expected_target=old)
    assert controller.running and controller.stops == 0 and controller.builds == 0
    assert git(root, "rev-parse", "HEAD") == old
    assert not (root / "data/.prepared_update.json").exists()


def test_failed_activation_restores_code_and_indexes_and_restarts(installation, monkeypatch):
    controller, root, old, target = installation

    def torn_move(staging_dir, repo_root, stores):
        (Path(repo_root) / "data/skills_store.npz").write_text("torn")
        raise OSError("disk full")
    monkeypatch.setattr(self_update, "_activate_staged_stores", torn_move)

    assert offline_update(controller)["state"] == "ACTIVATE_MOVE_FAILED"
    assert git(root, "rev-parse", "HEAD") == old
    assert (root / "data/skills_store.npz").read_text() == "old-index"
    assert controller.running and controller.probes == 1
    assert not (controller.directory / "transaction.json").exists()


def test_failed_merge_keeps_the_old_code_and_restarts(installation, monkeypatch):
    controller, root, old, target = installation
    run_git = self_update._run_git

    def refuse_merge(args, cwd, timeout):
        if args[0] == "merge":
            return subprocess.CompletedProcess(args, 1, "", "merge refused")
        return run_git(args, cwd, timeout)
    monkeypatch.setattr(self_update, "_run_git", refuse_merge)

    assert offline_update(controller)["state"] == "ACTIVATE_MERGE_FAILED"
    assert git(root, "rev-parse", "HEAD") == old
    assert (root / "data/skills_store.npz").read_text() == "old-index"
    assert controller.running and controller.probes == 1
    assert not (root / "data/.prepared_update.json").exists()
    assert not (controller.directory / "transaction.json").exists()


def test_activation_that_cannot_restore_the_tree_is_rolled_back(installation, monkeypatch):
    controller, root, old, target = installation

    def stuck(repo_root, branch=None, *, git_timeout=None, embedding_model=None):
        git(root, "merge", "--ff-only", target)
        (root / "data/skills_store.npz").write_text("torn")
        return self_update.ActivationStatus.ACTIVATE_ROLLBACK_FAILED
    monkeypatch.setattr(self_update, "activate_prepared_update", stuck)

    with pytest.raises(RuntimeError, match="rollback failed"):
        offline_update(controller)
    assert git(root, "rev-parse", "HEAD") == old
    assert (root / "data/skills_store.npz").read_text() == "old-index"
    assert controller.running and controller.probes == 1
    assert not (controller.directory / "transaction.json").exists()


def test_build_replaced_after_its_check_is_refused_without_applying(installation):
    from src.daemon.update import TargetMoved
    controller, root, old, target = installation
    checks = []

    def precheck():
        checks.append(1)
        if len(checks) == 2:  # after the build: a process that ignores the shared service drops it
            (root / "data/.prepared_update.json").unlink()

    with pytest.raises(TargetMoved):
        offline_update(controller, expected_target=target, precheck=precheck)
    assert git(root, "rev-parse", "HEAD") == old
    assert controller.running and controller.stops == 1 and controller.probes == 1
    assert not (controller.directory / "transaction.json").exists()
    assert not (controller.directory / "maintenance.json").exists()


def test_tree_changed_after_the_build_restarts_without_applying(installation):
    controller, root, old, target = installation
    checks = []

    def precheck():
        checks.append(1)
        if len(checks) == 2:  # after the build passed the activation gates
            (root / "README.md").write_text("local edit")

    assert offline_update(controller, expected_target=target, precheck=precheck)["state"] == "INVALID_DIRTY"
    assert git(root, "rev-parse", "HEAD") == old
    assert controller.running and controller.probes == 1
    assert not (root / "data/.prepared_update.json").exists()


def test_build_that_activation_would_refuse_fails_before_the_stop(installation, monkeypatch):
    controller, root, old, target = installation
    monkeypatch.setattr(self_update, "_validate_prepared",
                        lambda *args: (self_update.ActivationStatus.INVALID_CROSS_DEVICE, False))

    assert offline_update(controller)["state"] == "INVALID_CROSS_DEVICE"
    assert controller.running and controller.stops == 0 and controller.builds == 1
    assert not (root / "data/.prepared_update.json").exists()
    assert not (root / "data/.prepared").exists()


def test_build_for_an_older_target_is_rebuilt_for_the_new_one(installation):
    controller, root, old, target = installation
    busy = {"state": "deferred", "reason": "service is busy"}
    checks = iter([None, busy])
    assert offline_update(controller, expected_target=target, precheck=lambda: next(checks)) == busy

    work = root.parent / "work"  # the branch moves on before the next run
    subprocess.run(["git", "clone", "--quiet", str(root.parent / "remote.git"), str(work)], check=True)
    git(work, "config", "user.email", "test@example.invalid")
    git(work, "config", "user.name", "Test")
    (work / "README.md").write_text("newer")
    git(work, "commit", "-qam", "newer")
    git(work, "push", "-q", "origin", "HEAD:main")
    newer = git(work, "rev-parse", "HEAD")

    assert offline_update(controller, expected_target=newer)["state"] == "UPDATED"
    assert controller.builds == 2
    assert git(root, "rev-parse", "HEAD") == newer


def test_recover_after_an_interrupted_activation_restores_and_drops_the_build(installation, monkeypatch):
    from src.daemon import update
    controller, root, old, target = installation

    def die(staging_dir, repo_root, stores):
        (Path(repo_root) / "data/skills_store.npz").write_text("torn")
        raise KeyboardInterrupt  # the controller dies while it moves the stores
    monkeypatch.setattr(self_update, "_activate_staged_stores", die)
    rollback = update.rollback
    monkeypatch.setattr(update, "rollback", lambda *args: None)
    with pytest.raises(KeyboardInterrupt):
        offline_update(controller)
    assert git(root, "rev-parse", "HEAD") == target
    monkeypatch.setattr(update, "rollback", rollback)

    assert recover(controller)["state"] == "rolled_back"
    assert git(root, "rev-parse", "HEAD") == old
    assert (root / "data/skills_store.npz").read_text() == "old-index"
    assert not (root / "data/.prepared_update.json").exists()
    assert not (root / "data/.prepared").exists()
    assert not (controller.directory / "transaction.json").exists()


def test_prepare_reindex_uses_the_service_interpreter_model_and_live_stores(tmp_path, monkeypatch):
    from src.daemon import update
    controller = FakeController(tmp_path, tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data/skills_store.npz").write_text("live-index")
    (tmp_path / "data/.skills_hash").write_text("live-hash")
    # The worktree has no .env; the service reads the installation's under its own settings.
    (tmp_path / ".env").write_text("EMBEDDING_PROMPTS=off\nEMBEDDING_MODEL=from-env\n")
    monkeypatch.delenv("EMBEDDING_PROMPTS", raising=False)
    calls = []

    def run(args, cwd, timeout, *, env=None):
        # Unchanged sources keep the copied store: the reindex compares content hashes.
        assert (Path(cwd) / "data/.skills_hash").read_text() == "live-hash"
        calls.append((args, cwd, env))
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(self_update, "_run_command", run)

    assert update.prepare_reindex(controller, str(tmp_path / "stage")) is True
    args, cwd, env = calls[0]
    assert args == [controller.config["python"], "-m", "src.reindex"] and cwd == str(tmp_path / "stage")
    assert (env["EMBEDDING_MODEL"], env["AGENTS_MODEL_PATH"], env["PATH"], env["AGENTS_AUTO_UPDATE"]) == \
        ("test", "/unused", "/usr/bin:/bin", "0")
    assert env["EMBEDDING_PROMPTS"] == "off"
    assert (tmp_path / "stage/data/skills_store.npz").read_text() == "live-index"
    assert not (tmp_path / "stage/data/implants_store.npz").exists()

    monkeypatch.setattr(self_update, "_run_command",
                        lambda args, cwd, timeout, *, env=None: subprocess.CompletedProcess(args, 1, "", "no model"))
    with pytest.raises(RuntimeError, match="no model"):
        update.prepare_reindex(controller, str(tmp_path / "stage"))


def test_prepare_reindex_refuses_a_redirected_data_directory(tmp_path):
    from src.daemon import update
    controller = FakeController(tmp_path, tmp_path)
    (tmp_path / "data").mkdir()
    (tmp_path / "data/skills_store.npz").write_text("live-index")
    elsewhere = tmp_path / "elsewhere"; elsewhere.mkdir()
    (tmp_path / "stage").mkdir()
    (tmp_path / "stage/data").symlink_to(elsewhere)  # a target commit may carry a `data` symlink
    with pytest.raises(RuntimeError, match="redirected"):
        update.prepare_reindex(controller, str(tmp_path / "stage"))
    assert not any(elsewhere.iterdir())


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
    assert controller.running and controller.stops == 1
    assert not (controller.directory / "maintenance.json").exists()
    assert (root / "data/.prepared_update.json").exists()  # the build waits for the next run


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
    assert controller.running and controller.stops == 0 and controller.builds == 0


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


def test_failed_file_update_leaves_the_model_switch_pending(model_switch, monkeypatch):
    from src.daemon import update
    controller, root, old, target, downloads, rebuilt = model_switch
    monkeypatch.setattr(update, "prepare_reindex", lambda current, staging_dir: False)

    assert offline_update(controller)["state"] == "PREPARE_REINDEX_FAILED"

    config = read_json(controller.directory / "service.json")
    assert config["model"] == "test" and "model_generation" not in config
    assert rebuilt == []
    assert git(root, "rev-parse", "HEAD") == old
    assert controller.running and controller.stops == 0


def test_failed_activation_leaves_the_model_switch_pending(model_switch, monkeypatch):
    controller, root, old, target, downloads, rebuilt = model_switch

    def failed_move(staging_dir, repo_root, stores):
        raise OSError("disk full")
    monkeypatch.setattr(self_update, "_activate_staged_stores", failed_move)

    assert offline_update(controller)["state"] == "ACTIVATE_MOVE_FAILED"
    assert read_json(controller.directory / "service.json")["model"] == "test"
    assert rebuilt == []
    assert git(root, "rev-parse", "HEAD") == old
    assert controller.running and controller.probes == 1


def test_update_refuses_a_service_uninstalled_meanwhile(model_switch):
    controller, root, old, target, downloads, rebuilt = model_switch
    (controller.directory / "service.json").unlink()
    with pytest.raises(RuntimeError, match="not installed"):
        offline_update(controller)
    assert downloads == [] and git(root, "rev-parse", "HEAD") == old
