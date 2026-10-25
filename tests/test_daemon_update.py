from pathlib import Path
import json
import os
import shutil
import subprocess
import sys
from types import SimpleNamespace

import pytest

from src import self_update
from src.daemon.state import write_json, read_json
from src.daemon.update import offline_update, recover
from src.daemon.update import prepare_reindex as real_prepare_reindex
from src.daemon.bootstrap import assert_service_safe
from src.file_lock import file_lock
from src import executables
from src.model_migration import DEFAULT_MODEL, GENERATION


# By absolute path: an update gives the test process the service's PATH, which on Windows has no git.
GIT = shutil.which("git")


def git(root, *args):
    return subprocess.run([GIT, "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


class FakeController:
    def __init__(self, root, directory):
        self.directory = directory
        # Where the controller looks up a tool without a recorded path: the test host's PATH.
        self.config = {"installation": str(root), "git": GIT, "model": "test",
                       "model_cache": "/unused", "model_artifact": "test", "model_path": "/unused",
                       "path": os.environ["PATH"], "autostart": True, "python": sys.executable,
                       "model_generation": GENERATION}
        self.running = True
        self.fail_ready = 0
        self.fail_drain = False
        self.probes = 0
        self.stops = 0
        self.builds = 0
        self.staged_with = []  # the interpreter each prepared reindex was given (None: the service's)

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
        return {"state": "ready", "pid": 123,
                "agents_core_commit": git(Path(self.config["installation"]), "rev-parse", "HEAD")}
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
    subprocess.run([GIT, "clone", "--bare", str(root), str(remote)], check=True, capture_output=True)
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

    def prepare_reindex(current, staging_dir, python=None):
        controller.staged_with.append(python)
        assert controller.running  # the service keeps serving while the update is built
        with pytest.raises(BlockingIOError):  # under the updater lease: no stdio server prepares meanwhile
            with file_lock(root / "data/.update.lock", blocking=False): pass
        # The reindex child inherits that lease; on Windows it runs in a job that ends with this process.
        assert self_update._subprocess_lock_fds() or os.name == "nt"
        # Without the control lock: stop, restart and disable work during a long build.
        with file_lock(controller.directory / "control.lock", blocking=False): pass
        staged = Path(staging_dir) / "data"; staged.mkdir(parents=True, exist_ok=True)
        (staged / "skills_store.npz").write_text("new-index")
        (staged / "skills_store.json").write_text(json.dumps({"save_version": "new", "metadatas": []}))
        for name in (".skills_hash", ".implants_hash"): (staged / name).write_text("new")
        controller.builds += 1
        return True
    monkeypatch.setattr(update, "prepare_reindex", prepare_reindex)
    # The update channel comes from the installation's .env, not the developer's shell (#381).
    from src import update_channel
    for key in update_channel.KEYS:
        monkeypatch.delenv(key, raising=False)
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


def test_pinned_runtime_source_refuses_before_build_or_stop(installation, tmp_path):
    controller, root, old, _ = installation
    source = str(tmp_path / "pinned-source")
    controller.config["runtime_root"] = source
    write_json(controller.directory / "service.json", controller.config)
    with pytest.raises(RuntimeError, match="explicit owner-controlled runtime migration"):
        offline_update(controller)
    assert git(root, "rev-parse", "HEAD") == old
    assert controller.running and controller.stops == controller.builds == 0
    assert read_json(controller.directory / "service.json")["runtime_root"] == source
    assert not (controller.directory / "maintenance.json").exists()


def test_runtime_source_changed_during_build_refuses_before_stop(installation, monkeypatch, tmp_path):
    from src.daemon import update
    controller, root, old, _ = installation
    build = update.prepare_reindex

    def build_and_pin(current, staging_dir):
        result = build(current, staging_dir)
        write_json(controller.directory / "service.json", {**controller.config, "runtime_root": str(tmp_path)})
        return result
    monkeypatch.setattr(update, "prepare_reindex", build_and_pin)
    with pytest.raises(RuntimeError, match="runtime_root"):
        offline_update(controller)
    assert git(root, "rev-parse", "HEAD") == old and controller.stops == 0
    assert controller.running and controller.builds == 1


def test_ready_service_with_the_wrong_commit_rolls_back(installation):
    controller, root, old, _ = installation
    # An old launcher can start successfully while still executing its pinned source.
    controller.wait_ready = lambda: {"state": "ready", "pid": 123, "agents_core_commit": old}
    result = offline_update(controller)
    assert result["state"] == "rolled_back" and result["health"]["commit_verified"]
    assert git(root, "rev-parse", "HEAD") == old
    assert (root / "data/skills_store.npz").read_text() == "old-index"
    assert controller.running and controller.stops == controller.probes == 2
    assert not (controller.directory / "maintenance.json").exists()


def test_candidate_without_commit_identity_rolls_back_to_legacy_health(installation):
    controller, root, old, _ = installation
    controller.wait_ready = lambda: {"state": "ready", "pid": 123}
    result = offline_update(controller)
    assert result["state"] == "rolled_back"
    assert result["health"]["commit_verified"] is False
    assert result["health"]["commit_verification"] == "legacy health has no agents_core_commit"
    assert git(root, "rev-parse", "HEAD") == old and controller.running


def test_candidate_with_unknown_identity_is_not_a_legacy_rollback(installation):
    controller, root, old, _ = installation
    controller.wait_ready = lambda: {"state": "ready", "agents_core_commit": None}
    with pytest.raises(RuntimeError, match="source commit unknown"):
        offline_update(controller)
    assert git(root, "rev-parse", "HEAD") == old
    assert not controller.running
    assert (controller.directory / "transaction.json").exists()
    assert (controller.directory / "maintenance.json").exists()


def test_runtime_root_equal_to_installation_can_update(installation):
    controller, root, _, target = installation
    controller.config["runtime_root"] = str(root)
    write_json(controller.directory / "service.json", controller.config)
    result = offline_update(controller)
    assert result["state"] == "UPDATED" and result["health"]["agents_core_commit"] == target


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

    def stuck(repo_root, branch=None, *, git_timeout=None, embedding_model=None, policy=None):
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


def test_embedding_settings_changed_during_the_build_discard_it_without_a_restart(installation, monkeypatch):
    from src.daemon import update
    controller, root, old, target = installation
    build = update.prepare_reindex

    def build_while_env_changes(current, staging_dir):
        built = build(current, staging_dir)
        (root / ".env").write_text("EMBEDDING_PROMPTS=off\n")  # the restarted service would re-embed
        return built
    monkeypatch.setattr(update, "prepare_reindex", build_while_env_changes)
    monkeypatch.delenv("EMBEDDING_PROMPTS", raising=False)

    assert offline_update(controller)["state"] == "INVALID_EMBEDDING_INPUTS"
    assert controller.running and controller.stops == 0
    assert not (root / "data/.prepared_update.json").exists() and not (root / "data/.prepared").exists()

    monkeypatch.setattr(update, "prepare_reindex", build)  # the next run builds with the new settings
    assert offline_update(controller)["state"] == "UPDATED"
    assert controller.builds == 2 and git(root, "rev-parse", "HEAD") == target


def test_embedding_settings_changed_before_activation_keep_the_old_code(installation, monkeypatch):
    from src.daemon.update import TargetMoved
    controller, root, old, target = installation
    monkeypatch.delenv("EMBEDDING_PROMPTS", raising=False)
    checks = []

    def precheck():
        checks.append(1)
        if len(checks) == 2:  # after the build recorded its settings, before the stop
            (root / ".env").write_text("EMBEDDING_PROMPTS=off\n")

    with pytest.raises(TargetMoved, match="embedding settings"):
        offline_update(controller, expected_target=target, precheck=precheck)
    assert git(root, "rev-parse", "HEAD") == old
    assert (root / "data/skills_store.npz").read_text() == "old-index"
    assert controller.running and controller.stops == 1 and controller.probes == 1
    assert not (controller.directory / "transaction.json").exists()

    assert offline_update(controller, expected_target=target, precheck=lambda: None)["state"] == "UPDATED"
    assert controller.builds == 2  # the next run builds for the new settings


def test_target_commit_cannot_redirect_the_build_record_into_the_live_env(installation):
    controller, root, old, target = installation
    (root / ".env").write_text("SECRET=keep\n")
    work = root.parent / "work"  # the target plants a symlink where a build record could be written
    subprocess.run([GIT, "clone", "--quiet", str(root.parent / "remote.git"), str(work)], check=True)
    git(work, "config", "user.email", "test@example.invalid")
    git(work, "config", "user.name", "Test")
    for name in (".embedding-inputs", "prepared-build.json"):
        (work / name).symlink_to("../../../.env")
    git(work, "add", "-A")
    git(work, "commit", "-qm", "symlinks")
    git(work, "push", "-q", "origin", "HEAD:main")

    assert offline_update(controller)["state"] == "UPDATED"
    assert (root / ".env").read_text() == "SECRET=keep\n"
    assert (controller.directory / "prepared-build.json").is_file()


@pytest.mark.parametrize("line, rebuilt", [("EMBEDDING_PROMPTS=off", True), ("LANGFUSE_HOST=https://elsewhere", False)])
def test_deferred_build_is_reused_only_under_the_same_embedding_settings(installation, monkeypatch, line, rebuilt):
    controller, root, old, target = installation
    monkeypatch.delenv("EMBEDDING_PROMPTS", raising=False)
    busy = {"state": "deferred", "reason": "service is busy"}
    checks = iter([None, busy])
    assert offline_update(controller, expected_target=target, precheck=lambda: next(checks)) == busy

    (root / ".env").write_text(line + "\n")
    assert offline_update(controller, expected_target=target, precheck=lambda: None)["state"] == "UPDATED"
    assert controller.builds == (2 if rebuilt else 1)


def test_service_reconfigured_during_the_build_defers_without_a_restart(installation, monkeypatch):
    from src.daemon import update
    controller, root, old, target = installation
    build = update.prepare_reindex

    def build_while_reconfigured(current, staging_dir):
        write_json(controller.directory / "service.json", {**controller.config, "model": "chosen-meanwhile"})
        return build(current, staging_dir)
    monkeypatch.setattr(update, "prepare_reindex", build_while_reconfigured)

    result = offline_update(controller, expected_target=target, precheck=lambda: None)
    assert result == {"state": "deferred", "reason": "service configuration changed during the build"}
    assert controller.running and controller.stops == 0
    assert git(root, "rev-parse", "HEAD") == old


def test_service_uninstalled_during_the_build_is_left_alone(installation, monkeypatch):
    from src.daemon import update
    controller, root, old, target = installation
    build = update.prepare_reindex

    def build_while_uninstalled(current, staging_dir):
        (controller.directory / "service.json").unlink()
        return build(current, staging_dir)
    monkeypatch.setattr(update, "prepare_reindex", build_while_uninstalled)

    with pytest.raises(RuntimeError, match="not installed"):
        offline_update(controller)
    assert controller.stops == 0 and git(root, "rev-parse", "HEAD") == old


def test_build_for_an_older_target_is_rebuilt_for_the_new_one(installation):
    controller, root, old, target = installation
    busy = {"state": "deferred", "reason": "service is busy"}
    checks = iter([None, busy])
    assert offline_update(controller, expected_target=target, precheck=lambda: next(checks)) == busy

    work = root.parent / "work"  # the branch moves on before the next run
    subprocess.run([GIT, "clone", "--quiet", str(root.parent / "remote.git"), str(work)], check=True)
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
    from src.engine import config
    monkeypatch.setattr(config, "AUTO_UPDATE_REINDEX_TIMEOUT", 4321)
    calls = []

    def run(args, cwd, timeout, *, env=None):
        # Unchanged sources keep the copied store: the reindex compares content hashes.
        assert (Path(cwd) / "data/.skills_hash").read_text() == "live-hash"
        calls.append((args, cwd, env, timeout))
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(self_update, "_run_command", run)

    assert update.prepare_reindex(controller, str(tmp_path / "stage")) is True
    args, cwd, env, timeout = calls[0]
    assert args == [controller.config["python"], "-m", "src.reindex"] and cwd == str(tmp_path / "stage")
    assert timeout == 4321  # AGENTS_AUTO_UPDATE_REINDEX_TIMEOUT
    # The reindex runs with system directories, never the installer's PATH (#244).
    assert (env["EMBEDDING_MODEL"], env["AGENTS_MODEL_PATH"], env["PATH"], env["AGENTS_AUTO_UPDATE"]) == \
        ("test", "/unused", executables.service_path(), "0")
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


@pytest.mark.parametrize("failure_kind", ["wrong_commit", "warmup", "interrupt"])
def test_failed_restored_probation_stops_service_and_retains_recovery(installation, monkeypatch, failure_kind):
    from src.daemon import update
    controller, root, old, target = installation
    controller.fail_ready = 1
    wait_ready = controller.wait_ready
    failure = KeyboardInterrupt("restored warmup interrupted") if failure_kind == "interrupt" else RuntimeError("restored warmup failed")
    restores = []
    restore_files = update.restore_files

    def restore_once(*args):
        restores.append(1)
        return restore_files(*args)

    def restored_ready():
        if controller.probes == 1:
            return wait_ready()
        if failure_kind == "wrong_commit":
            return {"state": "ready", "agents_core_commit": target}
        raise failure

    monkeypatch.setattr(update, "restore_files", restore_once)
    controller.wait_ready = restored_ready
    with pytest.raises(KeyboardInterrupt if failure_kind == "interrupt" else RuntimeError) as caught:
        offline_update(controller)

    if failure_kind != "wrong_commit":
        assert caught.value is failure
    assert git(root, "rev-parse", "HEAD") == old
    assert (root / "data/skills_store.npz").read_text() == "old-index"
    assert restores == [1]
    assert not controller.running and controller.probes == 2 and controller.stops == 3
    journal = read_json(controller.directory / "transaction.json")
    assert journal["old_sha"] == old and journal["target_sha"] == target and journal["phase"] == "probation"
    assert (controller.directory / "maintenance.json").exists()
    # Startup consumes the single-use nonce before health verification.
    assert not (controller.directory / "probation.json").exists()
    with pytest.raises(RuntimeError, match="maintenance"):
        assert_service_safe(controller.directory)


def test_failed_restored_probation_preserves_primary_error_when_stop_fails(installation):
    controller, root, old, _ = installation
    controller.fail_ready = 1
    wait_ready, stop = controller.wait_ready, controller._stop
    failure = RuntimeError("restored health verification failed")
    stop_attempts = []

    def restored_ready():
        if controller.probes == 1:
            return wait_ready()
        raise failure

    def fail_restored_stop():
        stop_attempts.append(controller.probes)
        if controller.probes == 2:
            raise TimeoutError("restored drain timed out")
        stop()

    controller.wait_ready, controller._stop = restored_ready, fail_restored_stop
    with pytest.raises(RuntimeError) as caught:
        offline_update(controller)

    assert caught.value is failure
    assert any("TimeoutError" in note and "recovery barriers retained" in note for note in failure.__notes__)
    assert stop_attempts == [0, 1, 2] and controller.probes == 2
    assert controller.running  # Failed stopping cannot establish process quiescence.
    assert git(root, "rev-parse", "HEAD") == old
    assert (root / "data/skills_store.npz").read_text() == "old-index"
    assert (controller.directory / "transaction.json").exists()
    assert (controller.directory / "maintenance.json").exists()
    with pytest.raises(RuntimeError, match="maintenance"):
        assert_service_safe(controller.directory)


def test_failed_rollback_recovery_retains_barriers_until_verified(installation):
    controller, root, old, _ = installation
    wait_ready = controller.wait_ready
    controller.wait_ready = lambda: {"state": "ready", "agents_core_commit": None}
    with pytest.raises(RuntimeError, match="source commit unknown"):
        offline_update(controller)
    with pytest.raises(RuntimeError, match="source commit unknown"):
        recover(controller)

    assert not controller.running and controller.probes == 3 and controller.stops == 5
    assert git(root, "rev-parse", "HEAD") == old
    assert (root / "data/skills_store.npz").read_text() == "old-index"
    assert read_json(controller.directory / "transaction.json")["old_sha"] == old
    assert (controller.directory / "maintenance.json").exists()
    with pytest.raises(RuntimeError, match="maintenance"):
        assert_service_safe(controller.directory)

    controller.wait_ready = wait_ready
    result = recover(controller)
    assert result["state"] == "rolled_back" and result["health"]["commit_verified"]
    assert controller.running and controller.probes == 4 and controller.stops == 6
    assert not any((controller.directory / name).exists()
                   for name in ("transaction.json", "maintenance.json", "probation.json"))


@pytest.mark.parametrize("failure_kind", ["wrong_commit", "unknown_commit", "warmup", "interrupt"])
def test_failed_recovery_before_mutation_stops_unverified_runtime(installation, failure_kind):
    controller, root, old, target = installation
    wait_ready = controller.wait_ready
    failure = KeyboardInterrupt("recovery interrupted") if failure_kind == "interrupt" else RuntimeError("recovery warmup failed")
    write_json(controller.directory / "transaction.json",
               {"was_running": True, "phase": "draining", "runtime_sha": old})
    write_json(controller.directory / "maintenance.json", {"operation": "update"})

    def failed_ready():
        if failure_kind in ("wrong_commit", "unknown_commit"):
            return {"state": "ready", "agents_core_commit": target if failure_kind == "wrong_commit" else None}
        raise failure

    controller.wait_ready = failed_ready
    with pytest.raises(KeyboardInterrupt if failure_kind == "interrupt" else RuntimeError) as caught:
        recover(controller)

    if failure_kind in ("warmup", "interrupt"):
        assert caught.value is failure
    assert not controller.running and controller.stops == 2 and controller.probes == 1
    assert git(root, "rev-parse", "HEAD") == old
    assert (root / "data/skills_store.npz").read_text() == "old-index"
    assert read_json(controller.directory / "transaction.json")["runtime_sha"] == old
    assert (controller.directory / "maintenance.json").exists()
    with pytest.raises(RuntimeError, match="maintenance"):
        assert_service_safe(controller.directory)

    controller.wait_ready = wait_ready
    assert recover(controller)["state"] == "recovered_before_mutation"
    assert controller.running and controller.stops == 3 and controller.probes == 2
    assert not any((controller.directory / name).exists()
                   for name in ("transaction.json", "maintenance.json", "probation.json"))


def test_recovery_before_mutation_accepts_legacy_health(installation):
    controller, _, old, _ = installation
    write_json(controller.directory / "transaction.json",
               {"was_running": True, "phase": "draining", "runtime_sha": old})
    write_json(controller.directory / "maintenance.json", {"operation": "update"})
    controller.wait_ready = lambda: {"state": "ready"}
    assert recover(controller)["state"] == "recovered_before_mutation"
    assert controller.running and controller.stops == 1 and controller.probes == 1
    assert not (controller.directory / "transaction.json").exists()
    assert not (controller.directory / "maintenance.json").exists()


def test_failed_pre_mutation_restoration_stops_unverified_runtime(installation):
    controller, root, old, _ = installation
    wait_ready = controller.wait_ready
    controller.wait_ready = lambda: {"state": "ready", "agents_core_commit": None}
    # A normal stdio reader makes activation's writer lease fail after the stop.
    with file_lock(root / "data/.sessions.lock", shared=True):
        with pytest.raises(RuntimeError, match="source commit unknown"):
            offline_update(controller)

    assert not controller.running and controller.stops == 2 and controller.probes == 1
    assert git(root, "rev-parse", "HEAD") == old
    assert (root / "data/skills_store.npz").read_text() == "old-index"
    journal = read_json(controller.directory / "transaction.json")
    assert journal["runtime_sha"] == old and "old_sha" not in journal
    assert (controller.directory / "maintenance.json").exists()
    with pytest.raises(RuntimeError, match="maintenance"):
        assert_service_safe(controller.directory)

    controller.wait_ready = wait_ready
    assert recover(controller)["state"] == "recovered_before_mutation"
    assert controller.running and not (controller.directory / "maintenance.json").exists()


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


def test_a_missing_manifest_fails_closed_before_build_or_stop(installation):
    """A requirements change whose revisions have no pyproject.toml cannot be verified."""
    controller, root, old, target = installation
    git(root, "reset", "--hard", target)
    (root / "requirements.txt").write_text("new-package==1\n")
    git(root, "add", "."); git(root, "commit", "-m", "dependencies")
    git(root, "push", "origin", "main")
    git(root, "reset", "--hard", old)
    with pytest.raises(RuntimeError, match="pyproject.toml must exist in both revisions"):
        offline_update(controller)
    assert git(root, "rev-parse", "HEAD") == old
    assert controller.running and controller.stops == 0 and controller.builds == 0


def publish_pyproject_update(root, target, before, after):
    """Put a manifest on the installed base and publish an exact target change."""
    git(root, "reset", "--hard", target)
    (root / "pyproject.toml").write_text(before)
    git(root, "add", "pyproject.toml")
    git(root, "commit", "-qm", "installed manifest")
    base = git(root, "rev-parse", "HEAD")
    (root / "pyproject.toml").write_text(after)
    git(root, "commit", "-qam", "target manifest")
    changed = git(root, "rev-parse", "HEAD")
    git(root, "push", "origin", "main")
    git(root, "reset", "--hard", base)
    return base, changed


def test_metadata_and_new_extra_can_activate_without_environment_maintenance(installation):
    from tests.test_daemon_update_dependencies import MANIFEST
    controller, root, _, target = installation
    after = MANIFEST.replace('version = "1.0"', 'version = "2.0"')
    after += 'thread-status = ["claude-agent-sdk==0.2.168"]\n'
    after += '\n[tool.setuptools.package-data]\n"src.context_compaction" = ["cursor_ui.js"]\n'
    _, changed = publish_pyproject_update(root, target, MANIFEST, after)
    result = offline_update(controller)
    assert result["state"] == "UPDATED" and result["health"]["agents_core_commit"] == changed
    assert git(root, "rev-parse", "HEAD") == changed and controller.stops == 1


def test_an_environment_change_for_a_service_outside_venv_refuses_before_build_or_stop(installation):
    """The service's interpreter must be the installation's .venv: only that one can be switched (#181)."""
    from tests.test_daemon_update_dependencies import MANIFEST
    controller, root, _, target = installation
    git(root, "reset", "--hard", target)
    (root / "requirements.txt").write_text("mcp==1.28.1\n")
    git(root, "add", "requirements.txt"); git(root, "commit", "-qm", "requirements")
    git(root, "push", "-q", "origin", "main")
    base, _ = publish_pyproject_update(root, git(root, "rev-parse", "HEAD"), MANIFEST,
                                       MANIFEST.replace("httpx>=0.28,<0.29", "httpx>=1"))
    with pytest.raises(RuntimeError, match="not the installation's interpreter"):
        offline_update(controller)
    assert git(root, "rev-parse", "HEAD") == base and controller.stops == controller.builds == 0


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


# --- Update channel: switching branches inside the transaction (#381) -------

def _channel(root):
    from tests.test_update_channel import push_dev
    return push_dev(root)


def _upstream(root, branch):
    return (git(root, "config", f"branch.{branch}.remote"), git(root, "config", f"branch.{branch}.merge"))


def test_a_switch_whose_activation_failed_reports_no_branch_change(installation, monkeypatch):
    """CodeRabbit on #394: the result named a switch that a failed activation had undone."""
    from src import update_channel
    controller, root, old, target = installation
    _channel(root)
    update_channel.set_branch(root, "dev")
    run_git = self_update._run_git

    def refuse_checkout(args, cwd, timeout):
        if args[0] == "checkout" and "-B" in args:
            return subprocess.CompletedProcess(args, 1, "", "checkout refused")
        return run_git(args, cwd, timeout)
    monkeypatch.setattr(self_update, "_run_git", refuse_checkout)

    result = offline_update(controller)
    assert result["state"] == "ACTIVATE_MERGE_FAILED"
    assert "branch" not in result and "branch_from" not in result
    assert git(root, "symbolic-ref", "--short", "HEAD") == "main" and git(root, "rev-parse", "HEAD") == old


def test_channel_set_dev_switches_the_next_update_to_dev(installation):
    from src import update_channel
    controller, root, old, target = installation
    tip = _channel(root)
    update_channel.set_branch(root, "dev")
    result = offline_update(controller)
    assert result["state"] == "UPDATED" and result["branch"] == "dev" and result["branch_from"] == "main"
    assert git(root, "symbolic-ref", "--short", "HEAD") == "dev" and git(root, "rev-parse", "HEAD") == tip
    assert _upstream(root, "dev") == ("origin", "refs/heads/dev")
    # Built while serving, then one stop for activation and probation.
    assert controller.builds == 1 and controller.stops == 1 and controller.probes == 1
    assert (root / "data/skills_store.npz").read_text() == "new-index"
    assert git(root, "rev-parse", "main") == old  # the previous branch stays where it was
    assert update_channel.show(root, network=False)["checked_out"] == "dev"
    assert not (controller.directory / "transaction.json").exists()
    assert offline_update(controller)["state"] == "UP_TO_DATE"


def test_failed_probation_after_a_switch_returns_to_main_with_the_previous_stores(installation):
    from src import update_channel
    controller, root, old, target = installation
    _channel(root)
    update_channel.set_branch(root, "dev")
    controller.fail_ready = 1
    result = offline_update(controller)
    assert result["state"] == "rolled_back"
    assert git(root, "symbolic-ref", "--short", "HEAD") == "main" and git(root, "rev-parse", "HEAD") == old
    assert (root / "data/skills_store.npz").read_text() == "old-index"
    assert subprocess.run([GIT, "-C", str(root), "rev-parse", "--verify", "--quiet", "refs/heads/dev"],
                          capture_output=True).returncode == 1  # the switch created it; the rollback removed it
    assert not (controller.directory / "transaction.json").exists() and controller.running


def test_a_downgrade_is_refused_until_it_is_confirmed(installation):
    from src import update_channel
    controller, root, old, target = installation
    tip = _channel(root)
    update_channel.set_branch(root, "dev")
    assert offline_update(controller)["state"] == "UPDATED"
    stops = controller.stops
    # A hand-edited setting back to main, previous dev: HEAD (dev) has a commit origin/main lacks.
    env = root / ".env"
    env.write_text(env.read_text().replace("AGENTS_AUTO_UPDATE_BRANCH=dev", "AGENTS_AUTO_UPDATE_BRANCH=main")
                   .replace("AGENTS_AUTO_UPDATE_PREVIOUS_BRANCH=main", "AGENTS_AUTO_UPDATE_PREVIOUS_BRANCH=dev"))
    assert offline_update(controller)["state"] == "SKIPPED_DOWNGRADE"
    assert git(root, "symbolic-ref", "--short", "HEAD") == "dev" and controller.stops == stops
    update_channel.set_branch(root, "main", allow_downgrade=True)
    result = offline_update(controller)
    assert result["state"] == "UPDATED" and result["branch"] == "main"
    assert git(root, "symbolic-ref", "--short", "HEAD") == "main" and git(root, "rev-parse", "HEAD") == target
    assert _upstream(root, "main") == ("origin", "refs/heads/main")
    assert git(root, "rev-parse", "dev") == tip


def test_a_third_branch_is_left_alone(installation):
    from src import update_channel
    controller, root, old, target = installation
    _channel(root)
    update_channel.set_branch(root, "dev")
    git(root, "checkout", "--quiet", "-b", "experiment")
    assert offline_update(controller)["state"] == "SKIPPED_WRONG_BRANCH"
    assert git(root, "symbolic-ref", "--short", "HEAD") == "experiment" and controller.stops == 0


def test_an_exported_branch_overrides_env_for_one_command(installation, monkeypatch):
    from src import update_channel
    controller, root, old, target = installation
    _channel(root)
    update_channel.set_branch(root, "dev")
    monkeypatch.setenv("AGENTS_AUTO_UPDATE_BRANCH", "main")
    result = offline_update(controller)
    assert result["state"] == "UPDATED" and "branch" not in result
    assert "overrides .env (dev)" in result["note"]
    assert git(root, "symbolic-ref", "--short", "HEAD") == "main" and git(root, "rev-parse", "HEAD") == target


# --- Review round 1 (#381) ---------------------------------------------------

def test_an_exported_main_never_moves_a_dev_installation_after_a_release(installation, monkeypatch):
    """txn-1: on dev with .env dev / previous main, an exported main (a release that contains HEAD)
    leaves the checkout on dev and says that the environment overrode .env."""
    from src import update_channel
    from tests.test_update_channel import release
    controller, root, old, target = installation
    tip = _channel(root)
    release(root)
    git(root, "checkout", "--quiet", "-B", "dev", tip)
    (root / ".env").write_text("AGENTS_AUTO_UPDATE_BRANCH=dev\nAGENTS_AUTO_UPDATE_PREVIOUS_BRANCH=main\n")
    monkeypatch.setenv("AGENTS_AUTO_UPDATE_BRANCH", "main")
    result = offline_update(controller)
    assert result["state"] == "SKIPPED_WRONG_BRANCH" and "overrides .env (dev)" in result["note"]
    assert git(root, "symbolic-ref", "--short", "HEAD") == "dev" and git(root, "rev-parse", "HEAD") == tip
    assert controller.stops == 0 and update_channel.read(root, environ={}).branch == "dev"


def test_main_after_a_release_switches_to_dev_without_a_confirmation(installation):
    """txn-2: a release merge commit on main does not make main -> dev a downgrade."""
    from src import update_channel
    from tests.test_update_channel import release
    controller, root, old, target = installation
    tip = _channel(root)
    merge = release(root)
    git(root, "merge", "--quiet", "--ff-only", merge)
    assert update_channel.set_branch(root, "dev")["downgrade"] is False
    result = offline_update(controller)
    assert result["state"] == "UPDATED" and result["branch"] == "dev"
    assert git(root, "symbolic-ref", "--short", "HEAD") == "dev" and git(root, "rev-parse", "HEAD") == tip


def test_a_channel_saved_during_the_build_defers_the_activation(installation, monkeypatch):
    """txn-3: the owner saves main again while the main -> dev switch builds: nothing switches."""
    from src import update_channel
    from src.daemon import update
    controller, root, old, target = installation
    _channel(root)
    update_channel.set_branch(root, "dev")
    real = update.prepare

    def prepare_then_save(*args, **kwargs):
        status = real(*args, **kwargs)
        update_channel.set_branch(root, "main")  # the page or `channel set`, under the channel lock only
        return status
    monkeypatch.setattr(update, "prepare", prepare_then_save)
    result = offline_update(controller)
    assert result == {"state": "deferred", "reason": "update channel changed during the build"}
    assert git(root, "symbolic-ref", "--short", "HEAD") == "main" and git(root, "rev-parse", "HEAD") == old
    assert controller.stops == 0
    # fixes-1: the refused switch build goes; an up-to-date channel would never run this again.
    assert self_update._read_prepared_marker() is None and not any((root / "data/.prepared").glob("*"))
    monkeypatch.setattr(update, "prepare", real)
    result = offline_update(controller)
    assert result["state"] == "UPDATED" and "branch" not in result
    assert git(root, "symbolic-ref", "--short", "HEAD") == "main" and git(root, "rev-parse", "HEAD") == target


def test_failed_probation_restores_the_target_branchs_upstream(installation):
    """txn-5: the switch rewrote branch.dev.remote/merge; the controller's rollback restores them."""
    from src import update_channel
    controller, root, old, target = installation
    _channel(root)
    git(root, "branch", "dev", old)
    git(root, "config", "branch.dev.remote", "upstream-x")
    git(root, "config", "branch.dev.merge", "refs/heads/foo")
    update_channel.set_branch(root, "dev")
    controller.fail_ready = 1
    assert offline_update(controller)["state"] == "rolled_back"
    assert git(root, "symbolic-ref", "--short", "HEAD") == "main" and git(root, "rev-parse", "dev") == old
    assert _upstream(root, "dev") == ("upstream-x", "refs/heads/foo")


# --- Dependencies: the target's Python environment (#181) ----------------------

@pytest.fixture
def venv_installation(installation, tmp_path, monkeypatch):
    """A service on the installation's setup ``.venv`` with both manifests; fake venv/pip builds."""
    from src import environments
    from tests.test_environments import PROJECT, FakeBuilder, make_environment
    controller, root, old, target = installation
    base = tmp_path / "base/bin/python3"
    base.parent.mkdir(parents=True)
    base.write_text("base interpreter")
    make_environment(root / ".venv", record=False, packages=("mcp",), base=base)
    git(root, "reset", "--hard", target)
    (root / "pyproject.toml").write_text(PROJECT)
    (root / "requirements.txt").write_text("mcp==1.28.1\n")
    git(root, "add", "pyproject.toml", "requirements.txt"); git(root, "commit", "-qm", "manifests")
    git(root, "push", "-q", "origin", "main")
    controller.config["python"] = environments.python_path(root / ".venv")
    write_json(controller.directory / "service.json", controller.config)

    builder, real = FakeBuilder(), self_update._run_command

    def run(args, cwd, timeout, *, env=None):
        # The base interpreter's version probe and venv, and pip of a new environment.
        if args[0] == str(base) or list(args[1:3]) == ["-m", "pip"]:
            assert controller.running  # the environment is built while the service serves
            return builder(args, cwd=cwd, timeout=timeout, env=env)
        return real(args, cwd, timeout, env=env)
    monkeypatch.setattr(self_update, "_run_command", run)
    return SimpleNamespace(controller=controller, root=root, base=git(root, "rev-parse", "HEAD"),
                           builder=builder, staged_with=controller.staged_with)


def publish_requirements(root, base, content):
    """Publish a requirements.txt change on top of *base* and leave the installation at *base*."""
    (root / "requirements.txt").write_text(content)
    git(root, "commit", "-qam", "requirements")
    changed = git(root, "rev-parse", "HEAD")
    git(root, "push", "-q", "origin", "main")
    git(root, "reset", "--hard", base)
    return changed


def test_a_dependency_update_builds_while_serving_and_switches_venv_in_maintenance(venv_installation, monkeypatch):
    from src import environments
    env = venv_installation
    controller, root = env.controller, env.root
    target = publish_requirements(root, env.base, "mcp==2\n")
    seen = []
    start = controller._start

    def probation(probation=None):
        # The restarted service finds .venv on the new environment, and the journal names the old one.
        journal = read_json(controller.directory / "transaction.json")
        seen.append((environments.state(str(root)), journal["environment"]))
        start(probation)
    monkeypatch.setattr(controller, "_start", probation)

    result = offline_update(controller)
    assert result["state"] == "UPDATED" and git(root, "rev-parse", "HEAD") == target
    (state, planned), = seen
    assert state == {"kind": "link", "name": planned["to"]} and planned["previous"]["kind"] == "directory"
    assert env.staged_with == [environments.python_path(root / ".venvs" / planned["to"])]
    assert controller.stops == 1 and not (controller.directory / "transaction.json").exists()
    assert (root / ".venvs" / planned["previous"]["legacy"] / "pyvenv.cfg").exists()


def test_a_failed_probation_restores_the_environment_with_the_code_and_stores(venv_installation):
    from src import environments
    env = venv_installation
    controller, root = env.controller, env.root
    publish_requirements(root, env.base, "mcp==2\n")
    controller.fail_ready = 1
    result = offline_update(controller)
    assert result["state"] == "rolled_back" and git(root, "rev-parse", "HEAD") == env.base
    assert environments.state(str(root)) == {"kind": "directory"} and (root / ".venv/pyvenv.cfg").exists()
    assert (root / "data/skills_store.npz").read_text() == "old-index"
    assert not (controller.directory / "transaction.json").exists()


def test_recover_restores_the_environment_of_an_interrupted_update(venv_installation, monkeypatch):
    from src.daemon import update
    from src import environments
    env = venv_installation
    controller, root = env.controller, env.root
    publish_requirements(root, env.base, "mcp==2\n")
    probation, rollback = update.probation, update.rollback

    def failed(*args, **kwargs):
        raise RuntimeError("candidate warmup failed")

    def dies(*args):
        raise KeyboardInterrupt  # the controller dies before its own rollback
    monkeypatch.setattr(update, "probation", failed)
    monkeypatch.setattr(update, "rollback", dies)
    with pytest.raises(KeyboardInterrupt):
        offline_update(controller)
    assert environments.state(str(root))["kind"] == "link"
    assert read_json(controller.directory / "transaction.json")["environment"]["previous"]["kind"] == "directory"
    monkeypatch.setattr(update, "probation", probation)
    monkeypatch.setattr(update, "rollback", rollback)
    assert recover(controller)["state"] == "rolled_back"
    assert environments.state(str(root)) == {"kind": "directory"} and git(root, "rev-parse", "HEAD") == env.base


def test_a_failed_environment_build_leaves_the_service_serving(venv_installation):
    env = venv_installation
    controller, root = env.controller, env.root
    publish_requirements(root, env.base, "mcp==2\n")
    env.builder.fail = "pip"
    result = offline_update(controller)
    assert result["state"] == "PREPARE_ENVIRONMENT_FAILED" and "ResolutionImpossible" in result["reason"]
    assert controller.running and controller.stops == 0 and git(root, "rev-parse", "HEAD") == env.base
    assert list((root / ".venvs").iterdir()) == []


def test_the_service_reindex_runs_the_interpreter_it_is_given(installation, monkeypatch, tmp_path):
    controller, root, _, _ = installation
    commands = []

    def run(args, cwd, timeout, *, env=None):
        commands.append((args, cwd))
        return subprocess.CompletedProcess(args, 0, "", "")
    monkeypatch.setattr(self_update, "_run_command", run)
    assert real_prepare_reindex(controller, str(tmp_path / "staged"), python="/x/python")
    assert real_prepare_reindex(controller, str(tmp_path / "staged"))
    assert commands == [(["/x/python", "-m", "src.reindex"], str(tmp_path / "staged")),
                        ([controller.config["python"], "-m", "src.reindex"], str(tmp_path / "staged"))]


def test_a_failed_environment_switch_restores_code_stores_and_venv(venv_installation, monkeypatch):
    from src import environments
    env = venv_installation
    controller, root = env.controller, env.root
    publish_requirements(root, env.base, "mcp==2\n")

    def interrupted(installation, planned):
        os.rename(os.path.join(installation, ".venv"),
                  os.path.join(installation, ".venvs", planned["previous"]["legacy"]))
        raise OSError("no space for the link")
    monkeypatch.setattr(environments, "switch", interrupted)
    result = offline_update(controller)
    assert result["state"] == "ACTIVATE_ENVIRONMENT_FAILED" and git(root, "rev-parse", "HEAD") == env.base
    assert environments.state(str(root)) == {"kind": "directory"}
    assert (root / "data/skills_store.npz").read_text() == "old-index"
    assert not (controller.directory / "transaction.json").exists() and controller.running


def test_a_comment_only_requirements_change_updates_without_an_environment(venv_installation):
    """#364: a comment in requirements.txt changes no package; the update applies on the setup .venv."""
    from src import environments
    env = venv_installation
    controller, root = env.controller, env.root
    target = publish_requirements(root, env.base, "# the MCP server\nmcp==1.28.1   # pinned\n\n")
    result = offline_update(controller)
    assert result["state"] == "UPDATED" and git(root, "rev-parse", "HEAD") == target
    assert env.builder.builds == [] and env.staged_with == [None]
    assert environments.state(str(root)) == {"kind": "directory"}


def test_malformed_toml_is_refused_with_its_reason(venv_installation):
    env = venv_installation
    controller, root = env.controller, env.root
    (root / "pyproject.toml").write_text("not valid TOML !\n")
    git(root, "commit", "-qam", "broken manifest")
    git(root, "push", "-q", "origin", "main")
    git(root, "reset", "--hard", env.base)
    with pytest.raises(RuntimeError, match="cannot verify environment compatibility"):
        offline_update(controller)
    assert controller.running and controller.stops == 0 and env.builder.calls == []


def test_a_bridge_package_change_is_refused_before_build_or_stop(venv_installation):
    env = venv_installation
    controller, root = env.controller, env.root
    (root / "bridge").mkdir()
    (root / "bridge/package.json").write_text("{}\n")
    git(root, "add", "bridge/package.json"); git(root, "commit", "-qm", "bridge")
    git(root, "push", "-q", "origin", "main")
    git(root, "reset", "--hard", env.base)
    with pytest.raises(RuntimeError, match="bridge"):
        offline_update(controller)
    assert controller.running and controller.stops == 0 and env.builder.calls == []
