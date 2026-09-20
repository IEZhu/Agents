"""Coherent token rotation across managed private client files."""
from pathlib import Path
import secrets

from src.file_lock import file_lock
from .clients import ClientMigration
from .state import atomic_private, read_json, write_json


def rotate_token(controller):
    with file_lock(controller.directory / "control.lock", blocking=False):
        if (controller.directory / "transaction.json").exists():
            raise RuntimeError("Recover the pending service transaction first")
        migration = ClientMigration(controller.directory)
        old = migration.token
        new = secrets.token_urlsafe(48)
        paths = set(controller.directory.glob("bridges/*.json"))
        # Only files our own migration journals prove were managed are touched.
        for journal in (controller.directory / "backups").glob("*/changes.json"):
            paths.update(Path(entry["path"]) for entry in read_json(journal, []))
        changes = []
        for path in paths:
            if path.exists():
                original = migration.read_config(path, as_json=False)
                if old in original: changes.append((path, original.replace(old, new), True))
        changes.append((controller.directory / "token", new + "\n", True))
        journal = {"operation": "token_rotate", "was_running": controller.status().get("state") == "ready"}
        from .update import phase, probation, cleanup
        write_json(controller.directory / "maintenance.json", {"operation": "token_rotate"})
        phase(controller, journal, "draining")
        try:
            controller._stop()
            def prepared(backup):
                journal["backup"] = str(backup)
                phase(controller, journal, "rotating")
            migration.apply(changes, on_prepared=prepared)
            if journal["was_running"]: probation(controller, journal)
            cleanup(controller)
        except BaseException:
            restore_rotation(controller, journal)
            raise
        return {"state": "rotated", "files": len(changes)}


def restore_rotation(controller, journal):
    from .update import probation, cleanup
    controller._stop()
    if journal.get("backup"):
        ClientMigration(controller.directory).restore(journal["backup"], check=False)
    if journal.get("was_running"):
        probation(controller, journal)
    cleanup(controller)
    return {"state": "token_rotation_rolled_back"}
