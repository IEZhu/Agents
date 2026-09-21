"""Coordinate installation readers before importing any application modules.

Keep this bootstrap stdlib-only: another process may be replacing the application
while we wait for its exclusive lease. Server sessions retain a shared lease until
exit; only a startup with no existing readers can activate an update.
"""

import logging
import importlib
import os
import runpy
import sys
from contextlib import contextmanager

UPDATE_JOURNAL = ".update_in_progress.json"

try:
    import fcntl
except ImportError:  # Windows: serve normally, but do not auto-update.
    fcntl = None


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
    if fcntl is None:
        assert_installation_safe(repo_root)
        logging.getLogger(__name__).warning(
            "Auto-update disabled: shared installation locks are unavailable."
        )
        yield
        return

    data_dir = os.path.join(repo_root, "data")
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, ".sessions.lock"), "a+b") as lease:
        # Python descriptors are non-inheritable: exec releases the lease.
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            # Existing readers permit an immediate start. An activating writer
            # must finish before we load config, prompts, stores or engine code.
            fcntl.flock(lease, fcntl.LOCK_SH)
        else:
            assert_installation_safe(repo_root)
            activate(lease.fileno())
            fcntl.flock(lease, fcntl.LOCK_SH)
        try:
            # The writer we waited for may have crashed or failed rollback.
            assert_installation_safe(repo_root)
            yield
        finally:
            fcntl.flock(lease, fcntl.LOCK_UN)


def _activate(repo_root, session_fd):
    # These imports must stay inside the exclusive lease.
    from dotenv import load_dotenv
    load_dotenv(os.path.join(repo_root, ".env"))
    from src.self_update import run_activation_safely
    run_activation_safely(session_fd)


@contextmanager
def stdio_derived_state(repo_root):
    """Reuse persistent indexes without sharing mutable stores between processes.

    Each running stdio server leases one slot for its lifetime. The first free
    slot is reused after exit (including a crash), while concurrent processes
    get distinct stores. History stores additionally namespace by workspace.
    Call only inside the installation reader lease.
    """
    if fcntl is None:
        import tempfile
        with tempfile.TemporaryDirectory(prefix="agents-stdio-") as derived:
            yield derived
        return

    base = os.path.join(repo_root, "data", "stdio")
    os.makedirs(base, mode=0o700, exist_ok=True)
    slot = 0
    while True:
        derived = os.path.join(base, str(slot))
        os.makedirs(derived, mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(derived, ".lease"), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            slot += 1
            continue
        except BaseException:
            os.close(fd)
            raise
        try:
            yield derived
        finally:
            os.close(fd)
        return


def run_server(server_path):
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(server_path)))
    # Bootstrap stays stdlib-only even while another process changes src/.
    import json
    marker_path = os.path.join(repo_root, "data/.shared-service.json")
    def check_service():
        try:
            with open(marker_path) as stream: marker = json.load(stream)
        except FileNotFoundError:
            return
        directory = marker["directory"]
        if any(os.path.lexists(os.path.join(directory, name)) for name in
               ("maintenance.json", "transaction.json")):
            raise SystemExit("Shared service is in maintenance; use the controller to recover")
        os.environ["AGENTS_AUTO_UPDATE"] = "0"
    check_service()
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
