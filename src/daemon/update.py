"""Offline controller transaction surrounding the existing ff-only updater."""
from contextlib import ExitStack, suppress
from pathlib import Path
import hashlib
import json
import os
import secrets
import shutil
import subprocess

from src import environments, executables
from src.file_lock import file_lock
from .state import read_json, write_json, private_dir, atomic_private


INDEX_FILES = ("skills_store.npz", "skills_store.json", ".skills_hash",
               "implants_store.npz", "implants_store.json", ".implants_hash")
# A console child of the windowless controller (Task Scheduler's pythonw) would open a window.
NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def phase(controller, journal, name):
    journal["phase"] = name
    write_json(controller.directory / "transaction.json", journal)


def probation(controller, journal, *, expected_commit=None, allow_legacy=False):
    nonce = secrets.token_urlsafe(32)
    write_json(controller.directory / "probation.json", {"nonce": nonce})
    phase(controller, journal, "probation")
    controller._start(probation=nonce)
    ready = controller.wait_ready()
    if expected_commit:
        actual = ready.get("agents_core_commit")
        if actual != expected_commit:
            # Only rollback may recover a version predating commit health. A
            # candidate without identity must never count as an applied update.
            if allow_legacy and "agents_core_commit" not in ready:
                ready = {**ready, "commit_verified": False,
                         "commit_verification": "legacy health has no agents_core_commit"}
            else:
                raise RuntimeError(f"Ready service reports source commit {actual or 'unknown'}, expected "
                                   f"{expected_commit}; inspect the service launcher/runtime_root")
        else:
            ready = {**ready, "commit_verified": True}
    # Rewrite future admission without restarting the verified PID. launchd
    # retains the original argv until bootout, a running task its command line;
    # the nonce is accepted only while maintenance exists. Normal post-commit
    # restarts ignore this nonce.
    controller.write_plist()
    return ready


def restored_probation(controller, journal, *, expected_commit):
    """Verify a terminal restoration and stop it on failure, retaining recovery state."""
    try:
        return probation(controller, journal, expected_commit=expected_commit, allow_legacy=True)
    except BaseException as failure:
        # Keep the primary verification error even if stopping also fails.
        try:
            controller._stop()
        except BaseException as stop_failure:
            failure.add_note("Stopping the restored service also failed (" + type(stop_failure).__name__ +
                             "); recovery barriers retained")
        raise


def cleanup(controller):
    for name in ("transaction.json", "maintenance.json", "probation.json"):
        (controller.directory / name).unlink(missing_ok=True)


def restore_files(controller, journal):
    """Called only while stopped, holding exclusive installation + updater leases."""
    root = Path(controller.config["installation"])
    if journal.get("old_sha"):
        current = subprocess.run([executables.path("git"), "rev-parse", "HEAD"], cwd=root, check=True,
                                 capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                 env=executables.git_environment(root), creationflags=NO_WINDOW).stdout.strip()
        if current not in (journal["old_sha"], journal.get("target_sha")):
            raise RuntimeError("Installation HEAD changed outside this transaction; manual recovery required")
        # The file updater starts from a verified clean tree and retains the leases.
        from src import self_update
        # A branch switch (#381) returns to the branch the installation was on, and
        # .venv to the Python environment it pointed at (#181).
        switched = {key: journal[key] for key in ("old_branch", "branch", "branch_tip", "branch_config",
                                                  "environment") if journal.get(key)}
        if not self_update._rollback_activation(str(root), journal["old_sha"], 30, clear_journal=False, **switched):
            raise RuntimeError("Code rollback failed; maintenance retained")
    if journal.get("service_backup"):
        # The model switch rewrote service.json; the restored stores match the old model.
        atomic_private(controller.directory / "service.json", Path(journal["service_backup"]).read_bytes())
        controller.config = read_json(controller.directory / "service.json")
    backup = Path(journal["backup"]) if journal.get("backup") else None
    for name in INDEX_FILES if backup else ():
        target = root / "data" / name
        if (backup / name).exists(): atomic_private(target, (backup / name).read_bytes())
        else: target.unlink(missing_ok=True)
    # A build that an interrupted activation partly moved out must not be activated again.
    if journal.get("old_sha"):
        from src import self_update
        self_update._discard_staging(str(root), 30)
    # Router/history derivatives detect revision/content on next load.
    (root / "data/.update_in_progress.json").unlink(missing_ok=True)
    phase(controller, journal, "restored")


def backup_indexes(controller, backup):
    root = Path(controller.config["installation"])
    for name in INDEX_FILES:
        source = root / "data" / name
        if source.exists(): atomic_private(backup / name, source.read_bytes())
        else: (backup / name).unlink(missing_ok=True)


def model_env(config):
    """Process configuration that selects the service's embedding model."""
    return {"EMBEDDING_MODEL": config["model"], "FASTEMBED_CACHE_DIR": config["model_cache"],
            "AGENTS_MODEL_ARTIFACT": config["model_artifact"], "AGENTS_MODEL_PATH": config["model_path"]}


def switch_model(controller, journal, switched):
    """Move the stopped service to *switched*'s model and rebuild its stores for it.

    Holds the exclusive installation and updater leases; `rollback` restores the
    previous service.json and stores from the journal.
    """
    if not journal.get("backup"):
        backup = private_dir(controller.directory / "rollback" / "model-switch")
        backup_indexes(controller, backup)
        journal["backup"] = str(backup)
    backup = Path(journal["backup"])
    atomic_private(backup / "service.json", (controller.directory / "service.json").read_bytes())
    journal.update(service_backup=str(backup / "service.json"), model_from=controller.config["model"],
                   model_to=switched["model"])
    phase(controller, journal, "model_switch")
    write_json(controller.directory / "service.json", switched)
    controller.config = switched
    reindex(controller)


def reindex(controller):
    """Rebuild the installation's stores with the service's model in a child that inherits the leases."""
    from src import self_update
    from src.engine.config import AUTO_UPDATE_REINDEX_TIMEOUT
    config = controller.config
    result = self_update._run_command([config["python"], "-m", "src.reindex"], cwd=config["installation"],
                                      timeout=AUTO_UPDATE_REINDEX_TIMEOUT, env={**os.environ, **model_env(config)})
    if result.returncode:
        raise RuntimeError("Reindex for " + config["model"] + " failed: " + (result.stderr or "")[-500:])


# Settings that select or shape the embeddings (src/engine/fingerprint.py,
# embedding_prompts.py, embedder.py). A build made under other values is stale:
# the restarted service would re-embed its stores during warmup.
EMBEDDING_INPUTS = ("EMBEDDING_", "AGENTS_MODEL_", "FASTEMBED_")
# Records the settings a finished build used, in the controller's private state:
# the staging worktree is a checkout of the target commit, which could plant a
# symlink there.
BUILD_RECORD = "prepared-build.json"
STALE_BUILD = "INVALID_EMBEDDING_INPUTS"


def reindex_env(config):
    """The update reindex's environment: the installation's .env under this process's.

    The worktree has no .env, so the installation's is passed under the process
    environment, as the service loads it (for example EMBEDDING_PROMPTS).
    """
    from dotenv import dotenv_values
    installed = {key: value for key, value in dotenv_values(Path(config["installation"]) / ".env").items()
                 if value is not None}
    return {**installed, **executables.service_environment(), **model_env(config), "AGENTS_AUTO_UPDATE": "0"}


def embedding_inputs(config):
    """A digest of the reindex settings that shape the embeddings; other settings stay out."""
    shaping = {key: value for key, value in reindex_env(config).items() if key.startswith(EMBEDDING_INPUTS)}
    return hashlib.sha256(json.dumps(shaping, sort_keys=True).encode()).hexdigest()


def built_with_current_inputs(controller, target):
    """Whether the prepared build of *target* recorded the embedding settings in effect now."""
    try:
        record = read_json(controller.directory / BUILD_RECORD, {})
    except ValueError:
        return False  # a torn record only costs a rebuild
    return bool(target) and isinstance(record, dict) and record.get("target") == target and \
        record.get("embedding_inputs") == embedding_inputs(controller.config)


def prepare_reindex(controller, staging_dir, python=None):
    """Build the target's stores inside *staging_dir* with the service's interpreter and model.

    Runs while the service still serves. The worktree as working directory makes its
    own code and ``data/`` the import root, as in ``self_update._run_reindex_at``.
    The live stores are copied in first: their content hashes let the reindex skip
    a store whose sources did not change, as the in-place reindex does. *python* is
    the target's environment when the target changes dependencies (#181).
    """
    from src import self_update
    from src.engine.config import AUTO_UPDATE_REINDEX_TIMEOUT
    config = controller.config
    live, staged = Path(config["installation"]) / "data", Path(staging_dir) / "data"
    staged.mkdir(parents=True, exist_ok=True)
    if not self_update._is_unredirected_path(str(staged)):
        raise RuntimeError("Staged data path is redirected: " + str(staged))
    for name in INDEX_FILES:
        if (live / name).is_file() and not (live / name).is_symlink():
            shutil.copyfile(live / name, staged / name)
    result = self_update._run_command([python or config["python"], "-m", "src.reindex"], cwd=staging_dir,
                                      timeout=AUTO_UPDATE_REINDEX_TIMEOUT, env=reindex_env(config))
    if result.returncode:
        raise RuntimeError("Prepared reindex for " + config["model"] + " failed: " + (result.stderr or "")[-500:])
    return True


def rollback(controller, journal):
    controller._stop()
    root = Path(controller.config["installation"])
    with file_lock(root / "data/.sessions.lock", blocking=False) as session_fd, file_lock(root / "data/.update.lock", blocking=False) as updater_fd:
        from src import self_update
        with self_update._inherit_lock(session_fd), self_update._inherit_lock(updater_fd):
            restore_files(controller, journal)
    ready = restored_probation(controller, journal, expected_commit=journal.get("old_sha") or journal.get("runtime_sha"))
    cleanup(controller)
    return {"state": "rolled_back", "health": ready}


class TargetMoved(RuntimeError):
    """The branch moved after auto-update checked it; nothing was applied."""


def channel_settings(controller):
    """The update keys for this command: exported variables over the installation's ``.env`` (#381).

    Read with ``dotenv_values``; nothing else of ``.env`` enters the controller's environment (#279).
    """
    from src import update_channel
    return update_channel.read(controller.config["installation"])


def runtime_source_refusal(config):
    """A Git content update cannot replace an explicitly pinned runtime source."""
    source = config.get("runtime_root")
    if source is None:
        return None
    if isinstance(source, str) and source.strip() and Path(source).is_absolute() and \
            Path(source).resolve() == Path(config["installation"]).resolve():
        return None
    return (f"Service runtime_root {source!r} differs from installation {config['installation']!r}; "
            "Git updates cannot switch this pinned source. Complete an explicit owner-controlled runtime "
            "migration and verify the launcher before updating; the override has been retained")


def prepare(controller, expected_target=None, channel=None, reasons=None):
    """Build the update while the service serves: Phase B of the staged updater.

    The target and its stores land in a worktree under ``data/.prepared``; the live
    tree stays untouched. The build holds the updater lease, so a stdio server's
    background update cannot replace the staging while it builds. A valid build that
    an earlier, deferred run left for the same target is reused. Every build passes
    the activation gates before the service stops, so a build that activation would
    refuse fails here instead. Returns a ``PreparedStatus`` value, or the
    ``ActivationStatus`` of a refused build. *channel* (default: read now) names the
    branch, which may differ from the checked-out one: then the build is a branch switch.

    A target that changes dependencies gets its Python environment under ``.venvs``
    first (`src.environments`), and its stores are built with that interpreter. A
    change that cannot be delivered (`src.daemon.update_dependencies`) raises before
    anything is built; *reasons* receives why an environment build failed.
    """
    from src import self_update
    from src.engine.config import AUTO_UPDATE_GIT_TIMEOUT
    config = controller.config
    root = Path(config["installation"])
    channel = channel or channel_settings(controller)

    def refusal(marker):
        return self_update._validate_prepared(marker, str(root), channel.branch, config["model"],
                                              AUTO_UPDATE_GIT_TIMEOUT, channel.policy)[0]

    def check(old, target):
        # Auto-update checked one commit before preparing; build only that one.
        if expected_target and target != expected_target:
            raise TargetMoved(f"branch moved from checked {expected_target[:12]} to {target[:12]}")
        def git(*args, check=True):
            return subprocess.run([executables.path("git"), *args], cwd=root, capture_output=True, text=True,
                                  check=check, stdin=subprocess.DEVNULL, env=executables.git_environment(root),
                                  creationflags=NO_WINDOW)
        try:
            environments.needed(str(root), git, old, target, config["python"], run=self_update._run_command)
        except environments.Refused as refusal:
            raise RuntimeError(str(refusal)) from None

    with file_lock(root / "data/.update.lock", blocking=False) as updater_fd, self_update._inherit_lock(updater_fd):
        inputs = embedding_inputs(config)
        marker = self_update._read_prepared_marker()
        if expected_target and marker and marker.get("target_sha") == expected_target and refusal(marker) is None \
                and built_with_current_inputs(controller, expected_target):
            return self_update.PreparedStatus.PREPARED
        status = self_update.prepare_update(repo_root=str(root), remote=channel.remote, branch=channel.branch,
                                            embedding_model=config["model"], validate_target=check,
                                            reindex_fn=lambda staging_dir, **kw: prepare_reindex(
                                                controller, staging_dir, **kw),
                                            policy=channel.policy, live_python=config["python"],
                                            reasons=reasons)
        if status != self_update.PreparedStatus.PREPARED:
            return status
        marker = self_update._read_prepared_marker() or {}
        # Activation would discard a refused build after the stop, on every run; a
        # build whose embedding settings changed while it ran would be re-embedded
        # by the restarted service.
        refused = refusal(marker) or (STALE_BUILD if embedding_inputs(config) != inputs else None)
        if refused is not None:
            self_update._discard_staging(str(root), AUTO_UPDATE_GIT_TIMEOUT)
            return refused
        write_json(controller.directory / BUILD_RECORD, {"target": marker.get("target_sha"), "embedding_inputs": inputs})
        return status


def activate(controller, journal, channel=None):
    """Phase A on the stopped service: fast-forward (or switch branches) and move the prepared stores in.

    A failed merge or move rolls the tree back inside activation; the stores then
    come back from the journal's backup, so the old code restarts on its own indexes
    instead of rebuilding them. A build with a Python environment points ``.venv`` at
    it last; the journal's ``environment`` names what it replaced, which every
    rollback restores. Returns ``UPDATED`` for an activated update.
    """
    from src import self_update
    status = self_update.ActivationStatus
    channel = channel or channel_settings(controller)
    planned = {"environment_plan": journal["environment"]} if journal.get("environment") else {}
    result = self_update.activate_prepared_update(controller.config["installation"], channel.branch,
                                                  embedding_model=controller.config["model"],
                                                  policy=channel.policy, **planned)
    if result == status.ACTIVATE_ROLLBACK_FAILED:
        raise RuntimeError("File updater rollback failed")
    if result in (status.ACTIVATE_MERGE_FAILED, status.ACTIVATE_MOVE_FAILED, status.ACTIVATE_ENVIRONMENT_FAILED):
        restore_files(controller, journal)
    return self_update.UpdateStatus.UPDATED if result == status.ACTIVATED else result


# Builds that failed. Like a refused build (``INVALID_…``) and a rolled-back
# update, they leave a pending model switch for the next run.
PREPARE_FAILED = ("PREPARE_ENVIRONMENT_FAILED", "SKIPPED_DEPENDENCIES", "PREPARE_WORKTREE_FAILED",
                  "PREPARE_REINDEX_FAILED", "PREPARE_STAGE_INCONSISTENT")
# service.json settings a build used; a change during the build makes it stale.
BUILD_CONFIG = ("installation", "runtime_root", "python", "git", "path", "model", "model_cache", "model_artifact", "model_path")


def load_service(controller):
    """Under the control lock: refuse an unfinished transaction or a removed service."""
    if (controller.directory / "transaction.json").exists():
        raise RuntimeError("An unfinished transaction requires recover")
    controller.config = read_json(controller.directory / "service.json")
    if not controller.config:
        raise RuntimeError("Service is not installed")
    executables.configure(controller.config)


def switch_journal(controller, channel):
    """The rollback facts of a prepared branch switch (#381) for the journal; empty for a fast-forward.

    Taken on the stopped service right before activation: the branch to return to, and
    where the switched-to local branch pointed (empty when the switch creates it) with its
    upstream settings, which the switch overwrites.
    """
    from src import self_update
    marker = self_update._read_prepared_marker() or {}
    root = Path(controller.config["installation"])

    def git(*args):
        return subprocess.run([executables.path("git"), *args], cwd=root, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, env=executables.git_environment(root),
                              creationflags=NO_WINDOW)
    current = git("symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    if not marker.get("switch_from") or current == channel.branch:
        return {}
    tip = git("rev-parse", "--verify", "--quiet", "refs/heads/" + channel.branch)
    config = {key: git("config", "--get", f"branch.{channel.branch}.{key}").stdout.strip()
              for key in ("remote", "merge")}
    return {"old_branch": current, "branch": channel.branch,
            "branch_tip": tip.stdout.strip() if tip.returncode == 0 else "", "branch_config": config}


def environment_journal(controller):
    """The Python environment switch of a prepared update (#181) for the journal; empty without one.

    Taken on the stopped service right before activation: the environment ``.venv``
    will point at and what it points at now (or the name a ``.venv`` directory gets
    under ``.venvs``), so that a rollback or ``recover`` restores it.
    """
    from src import self_update
    environment = (self_update._read_prepared_marker() or {}).get("environment")
    if not isinstance(environment, dict) or not environment.get("id"):
        return {}
    return {"environment": environments.plan(controller.config["installation"], environment["id"])}


def _channel_used(channel):
    """The channel values a build depends on; a change during the build defers the activation."""
    return (channel.branch, channel.remote, channel.previous, channel.allow_downgrade, channel.setting)


def offline_update(controller, expected_target=None, precheck=None):
    """:func:`_offline_update`, with a note when an exported branch overrides ``.env`` (#381)."""
    notes = []
    result = _offline_update(controller, expected_target, precheck, notes)
    if notes and isinstance(result, dict):
        result.setdefault("note", notes[0])
    return result


def _offline_update(controller, expected_target, precheck, notes):
    """Update the installation; once per model generation also switch its embedding model.

    The update is built while the service still serves (`prepare`); the service
    stops only to activate it, so clients see a restart instead of minutes without
    the server. The build runs without the control lock, so `stop`, `restart` and
    `auto-update disable` work during a long build; afterwards the transaction takes
    the lock again and rechecks the service. The default model downloads under the
    control lock while the service still serves (`switched_model_config`); a failed
    download changes nothing.

    The branch comes from the installation's update channel (`channel_settings`,
    #381). When it differs from the checked-out channel branch, the same transaction
    switches branches: the build is the target branch's tip, activation checks that
    branch out, and a failed probation returns to the previous branch and stores. A
    downgrade needs the confirmation `channel set <branch> --allow-downgrade` recorded.
    """
    root = Path(controller.config["installation"])
    with file_lock(controller.directory / "control.lock", blocking=False):
        # Under the lock, so `uninstall` or another controller command cannot change
        # service.json while the model downloads; the service keeps serving meanwhile.
        load_service(controller)
        if reason := runtime_source_refusal(controller.config):
            raise RuntimeError(reason)
        # Before the controller takes the service's environment: an exported value
        # overrides .env for this one command (#381).
        channel = channel_settings(controller)
        if channel.overridden:
            notes.append(channel.override_note())
        from src.model_migration import service_switch_pending
        switched = None
        if service_switch_pending(controller.config):
            from .control import switched_model_config
            switched = switched_model_config(controller.config)
        # A scheduled update rechecks its preconditions under the lock that `disable`
        # and `stop` also take, so either one that returned before this point wins.
        if precheck is not None and (refusal := precheck()):
            return refusal
    # Stdlib config + updater only. Reindex is the sole model process this
    # controller starts, and it inherits the leases it runs under.
    os.environ["AGENTS_AUTO_UPDATE"] = "0"
    os.environ.update(model_env(controller.config))
    executables.enter_service_environment()
    from src import self_update
    reasons = []
    prepared = prepare(controller, expected_target, channel, reasons)
    if prepared in PREPARE_FAILED or prepared.startswith("INVALID_"):
        # Leave the switch pending: the next update tries both again.
        switched = None
    if prepared != self_update.PreparedStatus.PREPARED and not switched:
        # Nothing to activate, or the build failed: the service never stopped.
        return {"state": prepared, **({"reason": reasons[-1]} if reasons else {})}
    built = controller.config
    with file_lock(controller.directory / "control.lock", blocking=False):
        load_service(controller)
        if reason := runtime_source_refusal(controller.config):
            raise RuntimeError(reason)
        if any(controller.config.get(key) != built.get(key) for key in BUILD_CONFIG):
            # The next run rebuilds for the new configuration.
            return {"state": "deferred", "reason": "service configuration changed during the build"}
        if prepared == self_update.PreparedStatus.PREPARED and \
                _channel_used(channel_settings(controller)) != _channel_used(channel):
            # `channel set` or the page saved while the build ran: activating the build
            # would apply the setting it replaced. The next run builds for the new one;
            # until then the refused build is discarded, as `prepare` discards one, since
            # a channel that is up to date never runs this transaction again.
            # A held lock means a stdio server is updating; its own gates re-validate the build.
            from src.engine.config import AUTO_UPDATE_GIT_TIMEOUT
            with suppress(BlockingIOError), file_lock(root / "data/.update.lock", blocking=False):
                self_update._discard_staging(str(root), AUTO_UPDATE_GIT_TIMEOUT)
            return {"state": "deferred", "reason": "update channel changed during the build"}
        # Building takes minutes: work may have arrived, or `stop` or `disable` ran;
        # a deferred run keeps the build, and the next one activates it.
        if precheck is not None and (refusal := precheck()):
            return refusal
        prior = controller.status()
        journal = {"phase": "draining", "was_running": prior.get("state") in ("ready", "starting", "draining"),
                   "autostart": controller.config["autostart"]}
        journal["runtime_sha"] = subprocess.run(
            [executables.path("git"), "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True,
            stdin=subprocess.DEVNULL, env=executables.git_environment(root), creationflags=NO_WINDOW).stdout.strip()
        write_json(controller.directory / "maintenance.json", {"operation": "update"})
        phase(controller, journal, "draining")
        mutated = False
        rollback_attempted = False
        try:
            controller._stop()
            with ExitStack() as locks:
                session_fd = locks.enter_context(file_lock(root / "data/.sessions.lock", blocking=False))
                updater_fd = locks.enter_context(file_lock(root / "data/.update.lock", blocking=False))
                result = prepared
                if prepared == self_update.PreparedStatus.PREPARED:
                    target = (self_update._read_prepared_marker() or {}).get("target_sha")
                    if not target or (expected_target and target != expected_target):
                        # Only a process that ignores the shared service could replace the build.
                        raise TargetMoved("the prepared update changed after it was built")
                    if not built_with_current_inputs(controller, target):
                        # .env changed after the build: the restarted service would re-embed
                        # the stores. The next run builds again.
                        raise TargetMoved("the embedding settings changed after the update was built")
                    old = subprocess.run([executables.path("git"), "rev-parse", "HEAD"], cwd=root, check=True,
                                         capture_output=True, text=True, stdin=subprocess.DEVNULL,
                                         env=executables.git_environment(root),
                                         creationflags=NO_WINDOW).stdout.strip()
                    backup = private_dir(controller.directory / "rollback" / old)
                    backup_indexes(controller, backup)
                    journal.update(old_sha=old, target_sha=target, backup=str(backup),
                                   **switch_journal(controller, channel), **environment_journal(controller))
                    phase(controller, journal, "applying")
                    mutated = True
                    with self_update._inherit_lock(session_fd), self_update._inherit_lock(updater_fd):
                        result = activate(controller, journal, channel)
                    if result != self_update.UpdateStatus.UPDATED:
                        # Leave the switch pending: the code and stores are the
                        # previous ones, and the next update tries both again.
                        switched = None
                phase(controller, journal, "files_complete")
                if switched:
                    mutated = True
                    with self_update._inherit_lock(session_fd), self_update._inherit_lock(updater_fd):
                        switch_model(controller, journal, switched)
            if not mutated:
                # Skip/up-to-date still verifies the existing runtime before
                # restoring admission after maintenance.
                ready = probation(controller, journal, expected_commit=journal["runtime_sha"],
                                  allow_legacy=True) if journal["was_running"] else None
                cleanup(controller)
                return {"state": result, "health": ready}
            try:
                ready = probation(controller, journal, expected_commit=(
                    journal.get("target_sha") if result == self_update.UpdateStatus.UPDATED else journal["runtime_sha"]),
                    allow_legacy=result != self_update.UpdateStatus.UPDATED)
            except BaseException:
                rollback_attempted = True
                return rollback(controller, journal)
            phase(controller, journal, "committed")
            cleanup(controller)
            done = {"state": result, "health": ready}
            if journal.get("old_branch") and result == self_update.UpdateStatus.UPDATED:
                # Only a switch that activated; a refused activation restored the old branch.
                done.update(branch=journal["branch"], branch_from=journal["old_branch"])
            if switched:
                done.update(model=switched["model"], model_from=journal["model_from"])
            return done
        except BaseException:
            if mutated:
                # Retain journal on recovery failure; no normal start can pass.
                if not rollback_attempted:
                    rollback(controller, journal)
            else:
                if journal["was_running"]:
                    restored_probation(controller, journal, expected_commit=journal.get("runtime_sha"))
                cleanup(controller)
            raise


def recover(controller):
    with file_lock(controller.directory / "control.lock", blocking=False):
        journal = read_json(controller.directory / "transaction.json")
        if not journal: return {"state": "no_transaction"}
        if journal.get("operation") == "token_rotate":
            from .rotation import restore_rotation
            return restore_rotation(controller, journal)
        if journal.get("old_sha") or journal.get("service_backup"):
            return rollback(controller, journal)
        if journal.get("was_running"):
            controller._stop()
            restored_probation(controller, journal, expected_commit=journal.get("runtime_sha"))
        cleanup(controller)
        return {"state": "recovered_before_mutation"}
