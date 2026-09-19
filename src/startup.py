"""Coordinate installation readers before importing any application modules.

Keep this bootstrap stdlib-only: another process may be replacing the application
while we wait for its exclusive lease. Server sessions retain a shared lease until
exit; only a startup with no existing readers can activate an update.
"""

import logging
import os
import runpy
from contextlib import contextmanager

try:
    import fcntl
except ImportError:  # Windows: serve normally, but do not auto-update.
    fcntl = None


@contextmanager
def server_session(repo_root, activate):
    """Activate when idle, then protect the live tree for the whole session.

    The separate updater lock can be busy preparing a worktree without blocking
    readers here. Lock failures propagate: importing during an unknown writer's
    critical section would be unsafe.
    """
    if fcntl is None:
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
            activate()
            fcntl.flock(lease, fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lease, fcntl.LOCK_UN)


def _activate(repo_root):
    # These imports must stay inside the exclusive lease.
    from dotenv import load_dotenv
    load_dotenv(os.path.join(repo_root, ".env"))
    from src.self_update import run_activation_safely
    run_activation_safely()


def run_server(server_path):
    repo_root = os.path.dirname(os.path.dirname(os.path.abspath(server_path)))
    with server_session(repo_root, lambda: _activate(repo_root)):
        # The outer server.py was compiled before we acquired the lease. Read it
        # again now so a contending startup cannot execute the previous version.
        runpy.run_path(server_path, run_name="__main__",
                       init_globals={"_agents_bootstrapped": True})
