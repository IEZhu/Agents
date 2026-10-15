"""Owner-invoked offline cleanup, deliberately not reachable from MCP.

Run ``python -m src.cooperation.admin --help``. This never repairs files, grants
replacement ownership or recovers an independent root's lost capability online.
"""
import argparse
import json
import os
from pathlib import Path
import time
import uuid

from .models import PROTOCOL, EventEnvelope
from .store import AdministrationLock, CooperationError, Store, bodies, canonical


_SYNCHRONIZE = 0x00100000
_ERROR_INVALID_PARAMETER = 87
_WAIT_OBJECT_0 = 0
_MAX_WINDOWS_PID = 0xFFFFFFFF


def _windows_execution_stopped(pid):
    """Query a process handle without sending a console event or terminating it."""
    import ctypes
    from ctypes import wintypes

    if pid > _MAX_WINDOWS_PID:  # Do not let DWORD conversion select another PID.
        return False
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.OpenProcess(_SYNCHRONIZE, False, pid)
    if not handle:
        # A valid, nonzero PID that no longer exists returns this error. Access
        # denied and every other failure leave the execution quarantined.
        return ctypes.get_last_error() == _ERROR_INVALID_PARAMETER
    try:
        stopped = kernel32.WaitForSingleObject(handle, 0) == _WAIT_OBJECT_0
    finally:
        closed = kernel32.CloseHandle(handle)
    return stopped and bool(closed)


def execution_stopped(pid):
    """Unknown, inaccessible, live or reused PIDs remain quarantined."""
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    if os.name == "nt":
        try:
            return _windows_execution_stopped(pid)
        except (OSError, AttributeError):
            return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return True
    except (PermissionError, OSError):
        return False
    return False


def cleanup(directory, *, repository_id, participant_id, expected_generation,
            claims, execution_pid, checkpoint_id, inspection, reason,
            owner_authorized=False, stopped_probe=execution_stopped, clock=time.time):
    """Explicit owner attestation plus OS stop check, exclusive lock and exact revisions.

    ``stopped_probe`` is a test/owner adapter, not an input to the MCP service or CLI.
    The OS cannot establish that an arbitrary historical PID is the intended
    execution; the owner's explicit attestation and inspected checkpoint supply
    that part of the trusted-account boundary.
    """
    directory = Path(directory)
    if not owner_authorized or not inspection.strip() or not reason.strip():
        raise CooperationError("unauthorized_actor", "Explicit installation-owner authorization and inspection are required")
    if directory.is_symlink() or not directory.is_dir():
        raise CooperationError("store_unavailable")
    if os.name == "nt":
        from src.daemon import acl
        if not acl.owned_by_user_or_admins(directory) or not acl.is_private(directory, inherited_below=True):
            raise CooperationError("unauthorized_actor")
    elif directory.stat().st_uid != os.getuid() or directory.stat().st_mode & 0o077:
        raise CooperationError("unauthorized_actor", "Private state must belong to the invoking OS owner")
    if not stopped_probe(execution_pid):
        raise CooperationError("scope_denied", "Named execution is still running or its state is unknown")
    lock = AdministrationLock(directory)
    lock.acquire()
    try:
        store = Store(directory)
        if not store.path.is_file():
            raise CooperationError("store_unavailable")
        with store.transaction() as db:
            row = db.execute("SELECT body FROM participants WHERE repository_id=? AND id=?", (repository_id, participant_id)).fetchone()
            if not row:
                raise CooperationError("unauthorized_actor")
            participant = json.loads(row[0])
            if participant["generation"] != expected_generation or participant["status"] == "cancelled":
                raise CooperationError("stale_generation")
            held = {claim["claim_id"]: claim for claim in bodies(db, "claims", repository_id) if claim["participant_id"] == participant_id}
            if len(claims) != len(held) or {ref["claim_id"] for ref in claims} != set(held):
                raise CooperationError("stale_revision", "Name every held claim exactly once")
            for ref in claims:
                if set(ref) != {"claim_id", "expected_revision"} or isinstance(ref["expected_revision"], bool) or held[ref["claim_id"]]["revision"] != ref["expected_revision"]:
                    raise CooperationError("stale_revision")
            recorded = participant.get("checkpoint")
            if recorded and recorded["checkpoint_id"] != checkpoint_id:
                raise CooperationError("stale_revision", "Inspect the latest checkpoint")
            if not stopped_probe(execution_pid):
                raise CooperationError("scope_denied", "Execution state changed")
            now = clock()
            revision = db.execute("SELECT revision FROM repositories WHERE id=?", (repository_id,)).fetchone()[0] + 1
            participant.update(status="cancelled", generation=participant["generation"] + 1,
                               running=False, terminal_at=now, reason=reason)
            db.execute("UPDATE participants SET verifier='',body=? WHERE id=?", (canonical(participant), participant_id))
            for identifier in held:
                db.execute("DELETE FROM claims WHERE id=?", (identifier,))
            for agreement in bodies(db, "agreements", repository_id):
                if participant_id in agreement["required_participants"] and agreement["state"] not in ("completed", "cancelled", "rejected", "expired"):
                    agreement.update(state="cancelled", reason="owner_offline_cleanup", terminal_at=now)
                    db.execute("UPDATE agreements SET body=? WHERE repository_id=? AND id=?", (canonical(agreement), repository_id, agreement["agreement_id"]))
            event = {"protocol": PROTOCOL, "event_id": str(uuid.uuid4()), "operation_id": str(uuid.uuid4()),
                     "repository_id": repository_id, "revision": revision, "actor_id": "installation_owner",
                     "subject_id": participant_id, "task_id": participant["task_id"], "attempt_id": participant["attempt_id"],
                     "generation": participant["generation"], "kind": "owner_cleanup", "agreement_id": None,
                     "proposal_revision": None, "reply_to": None, "created_at": now,
                     "payload": {"reason": reason, "inspection": inspection, "execution_pid": execution_pid,
                                 "checkpoint_id": checkpoint_id, "released_claims": sorted(held)}}
            try:
                EventEnvelope.model_validate(event)
                if len(canonical(event).encode()) > 32768:
                    raise ValueError()
            except ValueError as error:
                raise CooperationError("invalid_request", "Owner cleanup audit exceeds protocol bounds") from error
            db.execute("INSERT INTO events(repository_id,body,created_at) VALUES (?,?,?)", (repository_id, canonical(event), now))
            db.execute("UPDATE repositories SET revision=? WHERE id=?", (revision, repository_id))
            return {"status": "ok", "revision": revision, "released": sorted(held), "participant_id": participant_id}
    finally:
        lock.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--directory", required=True, help="Installed daemon's private cooperation directory")
    parser.add_argument("--repository-id", required=True)
    parser.add_argument("--participant-id", required=True)
    parser.add_argument("--expected-generation", required=True, type=int)
    parser.add_argument("--claims-json", required=True, help="JSON array of exact claim_id/expected_revision pairs")
    parser.add_argument("--execution-pid", required=True, type=int)
    parser.add_argument("--checkpoint-id")
    parser.add_argument("--inspection", required=True, help="Observed dirty/checkpoint state; does not read files automatically")
    parser.add_argument("--reason", required=True)
    parser.add_argument("--owner-authorized", action="store_true", help="Attest explicit human installation-owner authorization for this exact cleanup")
    args = vars(parser.parse_args())
    args["claims"] = json.loads(args.pop("claims_json"))
    try:
        result = cleanup(**args)
    except CooperationError as error:
        parser.exit(1, error.code + ": " + error.message + "\n")
    print(canonical(result))


if __name__ == "__main__":
    main()
