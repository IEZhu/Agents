"""Coordinate installation readers before importing any application modules.

Keep this bootstrap stdlib-only: another process may be replacing the application
while we wait for its exclusive lease. Server sessions retain a shared lease until
exit; only a startup with no existing readers can activate an update.

POSIX leases are ``flock`` locks; Windows leases are ``LockFileEx`` locks on the first
byte of the same files (``src.file_lock`` locks them the same way). A Windows file
system without byte-range locks serves without leases and never auto-updates.

An installation with a shared service runs no second engine: its stdio servers hand
their session to the service's bridge (``_serve_through_service``, #266).
"""

import errno
import logging
import importlib
import os
import runpy
import sys
from contextlib import contextmanager

UPDATE_JOURNAL = ".update_in_progress.json"
# Set to 1, a stdio server of a shared service installation serves standalone (for debugging).
STANDALONE = "AGENTS_STDIO_STANDALONE"

try:
    import fcntl
except ImportError:  # Windows
    fcntl = None

if fcntl is None and os.name == "nt":
    import ctypes
    from ctypes import wintypes
    import msvcrt

    class _Overlapped(ctypes.Structure):
        """OVERLAPPED with only the fields LockFileEx reads: the lock starts at offset 0."""
        _fields_ = [("Internal", ctypes.c_void_p), ("InternalHigh", ctypes.c_void_p),
                    ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
                    ("hEvent", wintypes.HANDLE)]

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _kernel32.LockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                     wintypes.DWORD, ctypes.POINTER(_Overlapped)]
    _kernel32.UnlockFileEx.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                                       ctypes.POINTER(_Overlapped)]

    def _lock(fd, *, exclusive, blocking):
        """Lock the lease byte; BlockingIOError when held, False without byte-range locks."""
        flags = (0x2 if exclusive else 0) | (0 if blocking else 0x1)  # LOCKFILE_EXCLUSIVE_LOCK, _FAIL_IMMEDIATELY
        if _kernel32.LockFileEx(msvcrt.get_osfhandle(fd), flags, 0, 1, 0, ctypes.byref(_Overlapped())):
            return True
        code = ctypes.get_last_error()
        if code == 33:  # ERROR_LOCK_VIOLATION
            raise BlockingIOError(errno.EWOULDBLOCK, "The lease is held by another process")
        if code in (1, 50):  # ERROR_INVALID_FUNCTION, ERROR_NOT_SUPPORTED
            return False
        raise ctypes.WinError(code)

    def _downgrade(fd):
        # A shared lock over our own exclusive one, then one unlock: Windows removes the
        # exclusive lock first, so no other process can take the lease in between.
        _lock(fd, exclusive=False, blocking=False)
        if not _kernel32.UnlockFileEx(msvcrt.get_osfhandle(fd), 0, 1, 0, ctypes.byref(_Overlapped())):
            # Still exclusive: every other start would wait for this whole session.
            raise ctypes.WinError(ctypes.get_last_error())

    def _unlock(fd):
        # Each call removes one lock of this handle: the exclusive one first, then the shared one.
        while _kernel32.UnlockFileEx(msvcrt.get_osfhandle(fd), 0, 1, 0, ctypes.byref(_Overlapped())):
            pass

elif fcntl is not None:
    def _lock(fd, *, exclusive, blocking):
        fcntl.flock(fd, (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | (0 if blocking else fcntl.LOCK_NB))
        return True

    def _downgrade(fd):
        fcntl.flock(fd, fcntl.LOCK_SH)

    def _unlock(fd):
        fcntl.flock(fd, fcntl.LOCK_UN)

# Whether this platform has installation leases; without them a server never auto-updates.
LEASES = fcntl is not None or os.name == "nt"
# Descriptors of the leases this process holds (Windows re-exec releases them, see release_leases).
_HELD = []


def assert_installation_safe(repo_root):
    """Reject an interrupted mutation before any application code is imported."""
    journal = os.path.join(repo_root, "data", UPDATE_JOURNAL)
    try:
        os.lstat(journal)
    except FileNotFoundError:
        return
    raise SystemExit(
        f"Unfinished auto-update: {journal}. Restore the installation and rebuild "
        "its stores, then remove this journal only after successful recovery."
    )


@contextmanager
def server_session(repo_root, activate):
    """Activate when idle, then protect the live tree for the whole session.

    The separate updater lock can be busy preparing a worktree without blocking
    readers here. Lock failures propagate: importing during an unknown writer's
    critical section would be unsafe.
    """
    if not LEASES:
        yield from _without_leases(repo_root)
        return

    data_dir = os.path.join(repo_root, "data")
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, ".sessions.lock"), "a+b") as lease:
        # Python descriptors are non-inheritable: exec releases the lease.
        fd = lease.fileno()
        _HELD.append(fd)
        try:
            if not _acquire_session(repo_root, fd, activate):
                yield from _without_leases(repo_root)
                return
            # The writer we waited for may have crashed or failed rollback.
            assert_installation_safe(repo_root)
            yield
        finally:
            _HELD.remove(fd)
            _unlock(fd)


def _acquire_session(repo_root, fd, activate):
    """Hold the session lease, shared; False on a file system without byte-range locks.

    Only a start that gets the lease exclusively, with no other reader, activates.
    """
    try:
        if not _lock(fd, exclusive=True, blocking=False):
            return False
    except BlockingIOError:
        # Existing readers permit an immediate start. An activating writer
        # must finish before we load config, prompts, stores or engine code.
        return _lock(fd, exclusive=False, blocking=True)
    assert_installation_safe(repo_root)
    activate(fd)
    _downgrade(fd)
    return True


def _without_leases(repo_root):
    assert_installation_safe(repo_root)
    logging.getLogger(__name__).warning(
        "Auto-update disabled: shared installation locks are unavailable."
    )
    _migrate_model(repo_root)
    yield


def release_leases():
    """Release every lease of this process, which must not read the installation afterwards.

    Windows has no exec: a re-exec starts the updated server as a child
    (``self_update._reexec_updated_server``), which would wait for the leases this
    process still holds. On POSIX, exec closes their descriptors.
    """
    for fd in _HELD:
        _unlock(fd)


def _activate(repo_root, session_fd):
    # These imports must stay inside the exclusive lease.
    from dotenv import load_dotenv
    load_dotenv(os.path.join(repo_root, ".env"))
    from src.self_update import run_activation_safely
    run_activation_safely(session_fd)
    # After activation: a prepared update was built for the model it recorded.
    _migrate_model(repo_root)


def _migrate_model(repo_root):
    """Move .env to the current default embedding model once (src/model_migration.py).

    Runs under the exclusive installation lease, so no running server shares
    stores with a different model; without leases at every start. A failure
    keeps the configured model.
    """
    try:
        from src.model_migration import migrate_env_file
        switched = migrate_env_file(os.path.join(repo_root, ".env"))
    except Exception:
        logging.getLogger(__name__).warning("Embedding model migration failed; keeping the configured model",
                                            exc_info=True)
        return
    if switched and "src.engine.config" in sys.modules:
        # Activation imported the engine config with the previous model; the
        # updater would also stage future updates for it. Start over with the new one.
        from src.self_update import _reexec_updated_server
        _reexec_updated_server()


@contextmanager
def stdio_derived_state(repo_root):
    """Reuse persistent indexes without sharing mutable stores between processes.

    Each running stdio server leases one slot for its lifetime. The first free
    slot is reused after exit (including a crash), while concurrent processes
    get distinct stores. History stores additionally namespace by workspace.
    Call only inside the installation reader lease. Without leases each server
    gets a temporary directory.
    """
    if LEASES:
        base = os.path.join(repo_root, "data", "stdio")
        os.makedirs(base, mode=0o700, exist_ok=True)
        slot = 0
        while True:
            derived = os.path.join(base, str(slot))
            os.makedirs(derived, mode=0o700, exist_ok=True)
            fd = _open_slot_lease(os.path.join(derived, ".lease"))
            try:
                locked = _lock(fd, exclusive=True, blocking=False)
            except BlockingIOError:
                os.close(fd)
                slot += 1
                continue
            except BaseException:
                os.close(fd)
                raise
            if not locked:  # a file system without byte-range locks
                os.close(fd)
                break
            _HELD.append(fd)
            try:
                yield derived
            finally:
                _HELD.remove(fd)
                if os.name == "nt":
                    _unlock(fd)  # closing would release it too, but not at a defined time
                os.close(fd)
            return

    import tempfile
    with tempfile.TemporaryDirectory(prefix="agents-stdio-") as derived:
        yield derived


def _open_slot_lease(path):
    if os.name == "nt" and os.path.islink(path):  # Windows has no O_NOFOLLOW
        raise OSError(errno.ELOOP, "A lease file must not be a symlink", path)
    return os.open(path, os.O_CREAT | os.O_RDWR | getattr(os, "O_NOFOLLOW", 0), 0o600)


def _serve_through_service(repo_root, directory):
    """Let the stdio bridge serve this session through the shared service (#266).

    A second engine next to the service would load the embedding model again and miss the
    service's updates. The bridge that migrated clients run (`bridge/stdio.mjs`) takes this
    process's place instead, with the configuration the service keeps for stdio servers
    (`bridges/stdio-auto.json`, written at install and at every service start). Without
    Node or that configuration, returns to serve standalone and says why.
    """
    import json
    try:
        with open(os.path.join(directory, "service.json")) as stream:
            node = json.load(stream).get("node")
    except (OSError, ValueError):
        node = None
    config = os.path.join(directory, "bridges", "stdio-auto.json")
    missing = [what for what, path in (("Node", node), ("bridge configuration", config))
               if not path or not os.path.isfile(path)]
    if missing:
        sys.stderr.write(f"Agents-Core: the shared service is installed, but there is no "
                         f"{' and no '.join(missing)}; serving standalone.\n")
        return
    argv = [node, os.path.join(repo_root, "bridge", "stdio.mjs"), config]
    try:
        if os.name != "nt":
            os.execv(node, argv)
        # No installation lease is held: a service installation changes only in
        # maintenance, which check_service refused.
        from src.windows_job import replace_process
        replace_process(argv)
    except OSError as error:
        sys.stderr.write(f"Agents-Core: the bridge to the shared service did not start ({error}); "
                         "serving standalone.\n")


def run_server(server_path):
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(server_path)))
    # Bootstrap stays stdlib-only even while another process changes src/.
    import json
    marker_path = os.path.join(repo_root, "data/.shared-service.json")
    def check_service():
        try:
            with open(marker_path) as stream: marker = json.load(stream)
        except FileNotFoundError:
            return None
        directory = marker["directory"]
        if any(os.path.lexists(os.path.join(directory, name)) for name in
               ("maintenance.json", "transaction.json")):
            raise SystemExit("Shared service is in maintenance; use the controller to recover")
        os.environ["AGENTS_AUTO_UPDATE"] = "0"
        return directory
    directory = check_service()
    if directory is not None and os.environ.get(STANDALONE) != "1":
        _serve_through_service(repo_root, directory)  # returns only to serve standalone
    with server_session(repo_root, lambda fd: _activate(repo_root, fd)), stdio_derived_state(repo_root) as derived:
        check_service()
        os.environ["AGENTS_DERIVED_DIR"] = derived
        os.environ["AGENTS_ROUTER_DATA_DIR"] = os.path.join(derived, "router")
        # A contender imported this bootstrap before waiting for the writer.
        # Refresh its module too: updated application code may use new symbols.
        # Use a fresh namespace: reload() would retain names removed by the update.
        del sys.modules[__name__]
        importlib.import_module(__name__)
        # The outer server.py was compiled before we acquired the lease. Read it
        # again now so a contending startup cannot execute the previous version.
        runpy.run_path(server_path, run_name="__main__",
                       init_globals={"_agents_bootstrapped": True})
