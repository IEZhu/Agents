"""Offline controller transaction surrounding the existing ff-only updater."""
from contextlib import ExitStack
from pathlib import Path
import os
import secrets
import shutil
import subprocess

from src.file_lock import file_lock
from .state import read_json, write_json, private_dir, atomic_private


INDEX_FILES = ("skills_store.npz", "skills_store.json", ".skills_hash",
               "implants_store.npz", "implants_store.json", ".implants_hash")
DEPENDENCIES = ("pyproject.toml", "requirements.txt", "uv.lock", "bridge/package.json", "bridge/package-lock.json")


def phase(controller, journal, name):
    journal["phase"] = name
    write_json(controller.directory / "transaction.json", journal)


def probation(controller, journal):
    nonce = secrets.token_urlsafe(32)
    write_json(controller.directory / "probation.json", {"nonce": nonce})
    phase(controller, journal, "probation")
    controller._start(probation=nonce)
    ready = controller.wait_ready()
    # Rewrite future launchd admission without restarting the verified PID.
    # launchd retains the original argv until bootout; the nonce is accepted only
    # while maintenance exists. Normal post-commit restarts ignore this nonce.
    controller.write_plist()
    return ready


def cleanup(controller):
    for name in ("transaction.json", "maintenance.json", "probation.json"):
        (controller.directory / name).unlink(missing_ok=True)


def restore_files(controller, journal):
    """Called only while stopped, holding exclusive installation + updater leases."""
    root = Path(controller.config["installation"])
    if journal.get("old_sha"):
        git = controller.config["git"]
        current = subprocess.run([git, "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
        if current not in (journal["old_sha"], journal.get("target_sha")):
            raise RuntimeError("Installation HEAD changed outside this transaction; manual recovery required")
        # The file updater starts from a verified clean tree and retains the leases.
        from src import self_update
        if not self_update._rollback_activation(str(root), journal["old_sha"], 30, clear_journal=False):
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


def rollback(controller, journal):
    controller._stop()
    root = Path(controller.config["installation"])
    with file_lock(root / "data/.sessions.lock", blocking=False) as session_fd, file_lock(root / "data/.update.lock", blocking=False) as updater_fd:
        from src import self_update
        with self_update._inherit_lock(session_fd), self_update._inherit_lock(updater_fd):
            restore_files(controller, journal)
    ready = probation(controller, journal)
    cleanup(controller)
    return {"state": "rolled_back", "health": ready}


# File-updater outcomes that rolled the checkout back to its previous revision.
ROLLED_BACK = ("MERGE_FAILED", "REINDEX_FAILED")


class TargetMoved(RuntimeError):
    """The branch moved after auto-update checked it; nothing was applied."""


def offline_update(controller, expected_target=None, precheck=None):
    """Update the installation; once per model generation also switch its embedding model.

    The default model downloads under the control lock while the service still
    serves (`switched_model_config`); a failed download changes nothing.
    """
    root = Path(controller.config["installation"])
    with file_lock(controller.directory / "control.lock", blocking=False):
        if (controller.directory / "transaction.json").exists():
            raise RuntimeError("An unfinished transaction requires recover")
        # Under the lock, so `uninstall` or another controller command cannot change
        # service.json while the model downloads; the service keeps serving meanwhile.
        controller.config = read_json(controller.directory / "service.json")
        if not controller.config:
            raise RuntimeError("Service is not installed")
        from src.model_migration import service_switch_pending
        switched = None
        if service_switch_pending(controller.config):
            from .control import switched_model_config
            switched = switched_model_config(controller.config)
        # A scheduled update rechecks its preconditions under the lock that `disable`
        # and `stop` also take, so either one that returned before this point wins.
        if precheck is not None and (refusal := precheck()):
            return refusal
        prior = controller.status()
        journal = {"phase": "draining", "was_running": prior.get("state") in ("ready", "starting", "draining"),
                   "autostart": controller.config["autostart"]}
        write_json(controller.directory / "maintenance.json", {"operation": "update"})
        phase(controller, journal, "draining")
        mutated = False
        rollback_attempted = False
        try:
            controller._stop()
            with ExitStack() as locks:
                session_fd = locks.enter_context(file_lock(root / "data/.sessions.lock", blocking=False))
                updater_fd = locks.enter_context(file_lock(root / "data/.update.lock", blocking=False))
                # Stdlib config + updater only. Reindex is the sole model process
                # and inherits both leases until it and its descendants exit.
                os.environ["AGENTS_AUTO_UPDATE"] = "0"
                os.environ.update(model_env(controller.config))
                os.environ["PATH"] = controller.config["path"]
                from src import self_update
                def validate(old, target):
                    nonlocal mutated
                    # Auto-update checked one commit before draining; apply only that one.
                    if expected_target and target != expected_target:
                        raise TargetMoved(f"branch moved from checked {expected_target[:12]} to {target[:12]}")
                    changed = subprocess.run([controller.config["git"], "diff", "--name-only", old, target, "--", *DEPENDENCIES],
                                             cwd=root, capture_output=True, text=True, check=True)
                    if changed.stdout.strip():
                        raise RuntimeError("Dependency manifests changed; update the environment in explicit maintenance")
                    backup = private_dir(controller.directory / "rollback" / old)
                    backup_indexes(controller, backup)
                    journal.update(old_sha=old, target_sha=target, backup=str(backup))
                    phase(controller, journal, "applying")
                    mutated = True
                with self_update._inherit_lock(session_fd), self_update._inherit_lock(updater_fd):
                    result = self_update.check_and_apply_update(repo_root=str(root), validate_target=validate)
                if result == self_update.UpdateStatus.ROLLBACK_FAILED:
                    raise RuntimeError("File updater rollback failed")
                phase(controller, journal, "files_complete")
                if switched and result in ROLLED_BACK:
                    # Leave the switch pending: a failed file update restored the
                    # previous code and stores, and the next update tries both again.
                    switched = None
                if switched:
                    mutated = True
                    with self_update._inherit_lock(session_fd), self_update._inherit_lock(updater_fd):
                        switch_model(controller, journal, switched)
            if not mutated:
                # Skip/up-to-date still verifies the existing runtime before
                # restoring admission after maintenance.
                ready = probation(controller, journal) if journal["was_running"] else None
                cleanup(controller)
                return {"state": result, "health": ready}
            try:
                ready = probation(controller, journal)
            except BaseException:
                rollback_attempted = True
                return rollback(controller, journal)
            phase(controller, journal, "committed")
            cleanup(controller)
            if switched:
                return {"state": result, "health": ready, "model": switched["model"],
                        "model_from": journal["model_from"]}
            return {"state": result, "health": ready}
        except BaseException:
            if mutated:
                # Retain journal on recovery failure; no normal start can pass.
                if not rollback_attempted:
                    rollback(controller, journal)
            else:
                if journal["was_running"]:
                    probation(controller, journal)
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
            probation(controller, journal)
        cleanup(controller)
        return {"state": "recovered_before_mutation"}
