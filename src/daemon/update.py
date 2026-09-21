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
    git = controller.config["git"]
    current = subprocess.run([git, "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True).stdout.strip()
    if current not in (journal["old_sha"], journal.get("target_sha")):
        raise RuntimeError("Installation HEAD changed outside this transaction; manual recovery required")
    # The file updater starts from a verified clean tree and retains the leases.
    from src import self_update
    if not self_update._rollback_activation(str(root), journal["old_sha"], 30, clear_journal=False):
        raise RuntimeError("Code rollback failed; maintenance retained")
    backup = Path(journal["backup"])
    for name in INDEX_FILES:
        target = root / "data" / name
        if (backup / name).exists(): atomic_private(target, (backup / name).read_bytes())
        else: target.unlink(missing_ok=True)
    # Router/history derivatives detect revision/content on next load.
    (root / "data/.update_in_progress.json").unlink(missing_ok=True)
    phase(controller, journal, "restored")


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


def offline_update(controller):
    root = Path(controller.config["installation"])
    with file_lock(controller.directory / "control.lock", blocking=False):
        if (controller.directory / "transaction.json").exists():
            raise RuntimeError("An unfinished transaction requires recover")
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
                os.environ["EMBEDDING_MODEL"] = controller.config["model"]
                os.environ["FASTEMBED_CACHE_DIR"] = controller.config["model_cache"]
                os.environ["AGENTS_MODEL_ARTIFACT"] = controller.config["model_artifact"]
                os.environ["AGENTS_MODEL_PATH"] = controller.config["model_path"]
                os.environ["PATH"] = controller.config["path"]
                from src import self_update
                def validate(old, target):
                    nonlocal mutated
                    changed = subprocess.run([controller.config["git"], "diff", "--name-only", old, target, "--", *DEPENDENCIES],
                                             cwd=root, capture_output=True, text=True, check=True)
                    if changed.stdout.strip():
                        raise RuntimeError("Dependency manifests changed; update the environment in explicit maintenance")
                    backup = private_dir(controller.directory / "rollback" / old)
                    for name in INDEX_FILES:
                        source = root / "data" / name
                        if source.exists(): atomic_private(backup / name, source.read_bytes())
                        else: (backup / name).unlink(missing_ok=True)
                    journal.update(old_sha=old, target_sha=target, backup=str(backup))
                    phase(controller, journal, "applying")
                    mutated = True
                with self_update._inherit_lock(session_fd), self_update._inherit_lock(updater_fd):
                    result = self_update.check_and_apply_update(repo_root=str(root), validate_target=validate)
                if result == self_update.UpdateStatus.ROLLBACK_FAILED:
                    raise RuntimeError("File updater rollback failed")
                phase(controller, journal, "files_complete")
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
        if journal.get("old_sha"):
            return rollback(controller, journal)
        if journal.get("was_running"):
            controller._stop()
            probation(controller, journal)
        cleanup(controller)
        return {"state": "recovered_before_mutation"}
