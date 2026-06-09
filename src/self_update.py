"""Background self-update for the Agents-Core MCP server.

At startup the server spawns a daemon thread (``start_background_update``) that
keeps the install current **without blocking startup**:

    1. take a non-blocking cross-process lock (only one of N concurrent stdio
       servers updates),
    2. throttle redundant network checks,
    3. fast-forward the install's git repo to the configured branch, and
    4. rebuild the vector stores in a subprocess running the freshly-pulled code.

The pull updates files on disk; the *running* process keeps its already-imported
code and in-memory vectors, so the new code takes effect on the **next** start —
which, for per-session stdio servers, is the next spawn. The expensive reindex
runs now (in the background) so that next start is fast.

Safety invariants:
    * acts **only** when the checked-out branch is the target branch
      (``AUTO_UPDATE_BRANCH``) — a no-op on feature branches / local dev,
    * only when the working tree is clean, and only **fast-forward** (never
      merge / rebase / auto-checkout),
    * a failed reindex (e.g. broken new code) is **rolled back** via
      ``git reset --hard`` to the pre-merge commit,
    * every failure is logged and swallowed — the server is never crashed or
      blocked by the updater.
"""

import json
import logging
import os
import subprocess
import sys
import threading
import time
from contextlib import contextmanager

from src.engine.config import (
    INSTALL_ROOT,
    INSTALL_DATA_DIR,
    AUTO_UPDATE_ENABLED,
    AUTO_UPDATE_REMOTE,
    AUTO_UPDATE_BRANCH,
    AUTO_UPDATE_GIT_TIMEOUT,
    AUTO_UPDATE_MIN_INTERVAL,
    AUTO_UPDATE_REINDEX_TIMEOUT,
)

logger = logging.getLogger(__name__)

# Runtime artifacts (all under data/, which is gitignored — so they never make
# the working tree look dirty). Module-level so tests can redirect them.
LOCK_FILE = os.path.join(INSTALL_DATA_DIR, ".update.lock")
CHECK_STAMP = os.path.join(INSTALL_DATA_DIR, ".last_update_check")
STATE_FILE = os.path.join(INSTALL_DATA_DIR, ".last_update.json")


class UpdateStatus:
    """Outcome of one :func:`check_and_apply_update` run (string constants)."""

    NO_GIT = "NO_GIT"
    SKIPPED_WRONG_BRANCH = "SKIPPED_WRONG_BRANCH"
    SKIPPED_DIRTY = "SKIPPED_DIRTY"
    FETCH_FAILED = "FETCH_FAILED"
    UP_TO_DATE = "UP_TO_DATE"
    SKIPPED_DIVERGED = "SKIPPED_DIVERGED"
    MERGE_FAILED = "MERGE_FAILED"
    REINDEX_FAILED = "REINDEX_FAILED"
    UPDATED = "UPDATED"


# Statuses reached *before* any network access. We don't write the throttle
# stamp for these, so a wrong-branch / dirty skip never delays a later
# legitimate check (the branch guard is local and instant).
_PRE_NETWORK_STATUSES = frozenset(
    {UpdateStatus.NO_GIT, UpdateStatus.SKIPPED_WRONG_BRANCH, UpdateStatus.SKIPPED_DIRTY}
)


# --- Cross-process lock (non-blocking) ---------------------------------------
# fcntl.flock on POSIX; no-op on Windows where stdio MCP doesn't run concurrent
# sessions. Mirrors the helper in src/memory/history.py but uses LOCK_NB so a
# held lock means "another server is already updating" -> skip immediately.
try:  # pragma: no cover - platform-specific
    import fcntl as _fcntl

    def _try_lock(fh) -> bool:
        try:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fh) -> None:
        try:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
        except OSError:
            pass

except ImportError:  # pragma: no cover - Windows
    def _try_lock(fh) -> bool:
        return True

    def _unlock(fh) -> None:
        return None


@contextmanager
def _process_lock(path: str):
    """Yield True if the exclusive lock was acquired, False otherwise."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fh = open(path, "w")
    acquired = _try_lock(fh)
    try:
        yield acquired
    finally:
        if acquired:
            _unlock(fh)
        fh.close()


# --- git / reindex helpers ---------------------------------------------------

def _run_git(args, cwd: str, timeout: int) -> subprocess.CompletedProcess:
    """Run ``git <args>`` capturing output. Does not raise on non-zero exit.

    May raise ``FileNotFoundError`` (git missing) or ``subprocess.TimeoutExpired``;
    callers handle those where the distinction matters.
    """
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        timeout=timeout,
    )


def _run_reindex(repo_root: str, timeout: int) -> bool:
    """Rebuild the vector stores in a subprocess running the freshly-pulled code.

    Spawned with ``cwd=repo_root`` so ``-m src.reindex`` resolves, and with the
    inherited environment so a per-process ``LD_LIBRARY_PATH`` (NixOS) survives.
    Returns True on success.
    """
    try:
        result = subprocess.run(
            [sys.executable, "-m", "src.reindex"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=timeout,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("Auto-update: reindex subprocess error: %s", e)
        return False
    if result.returncode != 0:
        logger.warning(
            "Auto-update: reindex subprocess failed (rc=%d): %s",
            result.returncode, (result.stderr or "")[-2000:],
        )
        return False
    return True


def _warn_if_deps_changed(repo_root: str, old_sha: str, new_sha: str, timeout: int) -> None:
    """Log a warning if the pulled range touched dependency manifests.

    We deliberately do NOT run ``pip install`` automatically; this just makes
    the need visible to the operator.
    """
    if not old_sha or not new_sha:
        return
    try:
        diff = _run_git(["diff", "--name-only", f"{old_sha}..{new_sha}"], repo_root, timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return
    if diff.returncode != 0:
        return
    changed = set(diff.stdout.split())
    hit = {"requirements.txt", "pyproject.toml"} & changed
    if hit:
        logger.warning(
            "Auto-update: dependency files changed (%s) but pip install is NOT run "
            "automatically — update the environment manually if needed.",
            ", ".join(sorted(hit)),
        )


# --- State + throttle persistence --------------------------------------------

def _write_state(status: str, old_sha: str, new_sha: str) -> None:
    payload = {
        "status": status,
        "old_sha": old_sha,
        "new_sha": new_sha,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        logger.debug("Auto-update: could not write state file: %s", e)


def log_last_update() -> None:
    """Log the last recorded update outcome (startup observability)."""
    try:
        if not os.path.exists(STATE_FILE):
            return
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        logger.info(
            "Auto-update: last run %s (%s -> %s) at %s",
            state.get("status"),
            (state.get("old_sha") or "")[:9],
            (state.get("new_sha") or "")[:9],
            state.get("ts"),
        )
    except (OSError, ValueError) as e:
        logger.debug("Auto-update: could not read state file: %s", e)


def _recently_checked(interval: int) -> bool:
    if interval <= 0:
        return False
    try:
        return (time.time() - os.path.getmtime(CHECK_STAMP)) < interval
    except OSError:
        return False


def _touch_check_stamp() -> None:
    try:
        os.makedirs(os.path.dirname(CHECK_STAMP) or ".", exist_ok=True)
        with open(CHECK_STAMP, "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    except OSError as e:
        logger.debug("Auto-update: could not write check stamp: %s", e)


# --- Core state machine ------------------------------------------------------

def check_and_apply_update(
    repo_root: str = INSTALL_ROOT,
    remote: str = AUTO_UPDATE_REMOTE,
    branch: str = AUTO_UPDATE_BRANCH,
    *,
    git_timeout: int = AUTO_UPDATE_GIT_TIMEOUT,
    reindex_timeout: int = AUTO_UPDATE_REINDEX_TIMEOUT,
    reindex_fn=None,
) -> str:
    """Fast-forward *repo_root* to ``<remote>/<branch>`` and rebuild stores.

    Pure of threading/locking so it can be unit-tested directly. ``reindex_fn``
    (``repo_root -> bool``) is injected in tests; defaults to spawning
    ``python -m src.reindex``. Returns an :class:`UpdateStatus` value.
    """
    reindex = reindex_fn or (lambda rr: _run_reindex(rr, reindex_timeout))

    # Step 1 — git available and repo_root is a work tree.
    try:
        rev = _run_git(["rev-parse", "--is-inside-work-tree"], repo_root, git_timeout)
    except FileNotFoundError:
        logger.info("Auto-update: git not found; skipping.")
        return UpdateStatus.NO_GIT
    except subprocess.TimeoutExpired:
        logger.warning("Auto-update: git timed out; skipping.")
        return UpdateStatus.NO_GIT
    if rev.returncode != 0 or rev.stdout.strip() != "true":
        logger.info("Auto-update: %s is not a git work tree; skipping.", repo_root)
        return UpdateStatus.NO_GIT

    try:
        # Step 2 — branch guard: only act on the target branch.
        cur = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root, git_timeout)
        current_branch = cur.stdout.strip() if cur.returncode == 0 else ""
        if current_branch != branch:
            logger.info(
                "Auto-update: on '%s', target is '%s'; skipping (local development).",
                current_branch or "?", branch,
            )
            return UpdateStatus.SKIPPED_WRONG_BRANCH

        # Step 3 — clean working tree (tracked files only; an incoming file that
        # collides with an untracked one makes the ff-merge fail loudly at step 6).
        status = _run_git(["status", "--porcelain", "--untracked-files=no"], repo_root, git_timeout)
        if status.returncode != 0:
            logger.warning("Auto-update: git status failed; skipping. %s", status.stderr.strip())
            return UpdateStatus.SKIPPED_DIRTY
        if status.stdout.strip():
            logger.info("Auto-update: working tree has uncommitted changes; skipping.")
            return UpdateStatus.SKIPPED_DIRTY

        head = _run_git(["rev-parse", "HEAD"], repo_root, git_timeout)
        old_sha = head.stdout.strip() if head.returncode == 0 else ""

        # Step 4 — fetch the target branch.
        try:
            fetch = _run_git(["fetch", remote, branch], repo_root, git_timeout)
        except subprocess.TimeoutExpired:
            logger.warning("Auto-update: git fetch timed out after %ds; serving current code.", git_timeout)
            return UpdateStatus.FETCH_FAILED
        if fetch.returncode != 0:
            logger.warning("Auto-update: git fetch failed; serving current code. %s", fetch.stderr.strip())
            return UpdateStatus.FETCH_FAILED

        remote_ref = f"{remote}/{branch}"

        # Compare against FETCH_HEAD — fetch always points it at the just-fetched
        # branch tip, so this avoids depending on remote-tracking-ref refspec
        # nuances (and it's exactly what `git pull` would merge).

        # Step 5 — how far behind / ahead are we?
        counts = _run_git(
            ["rev-list", "--left-right", "--count", "FETCH_HEAD...HEAD"],
            repo_root, git_timeout,
        )
        if counts.returncode != 0:
            logger.warning("Auto-update: could not compare with %s; skipping. %s", remote_ref, counts.stderr.strip())
            return UpdateStatus.FETCH_FAILED
        try:
            behind_str, ahead_str = counts.stdout.split()
            behind, ahead = int(behind_str), int(ahead_str)
        except ValueError:
            logger.warning("Auto-update: unexpected rev-list output %r; skipping.", counts.stdout)
            return UpdateStatus.FETCH_FAILED
        if behind == 0:
            logger.info("Auto-update: already up to date with %s.", remote_ref)
            return UpdateStatus.UP_TO_DATE
        if ahead > 0:
            logger.info(
                "Auto-update: local branch diverged from %s (ahead %d, behind %d); skipping.",
                remote_ref, ahead, behind,
            )
            return UpdateStatus.SKIPPED_DIVERGED

        # Step 6 — fast-forward only.
        merge = _run_git(["merge", "--ff-only", "FETCH_HEAD"], repo_root, git_timeout)
        if merge.returncode != 0:
            logger.warning("Auto-update: fast-forward merge failed; skipping. %s", merge.stderr.strip())
            return UpdateStatus.MERGE_FAILED
        new = _run_git(["rev-parse", "HEAD"], repo_root, git_timeout)
        new_sha = new.stdout.strip() if new.returncode == 0 else ""
        logger.info("Auto-update: fast-forwarded %s -> %s on %s.", old_sha[:9], new_sha[:9], branch)

        _warn_if_deps_changed(repo_root, old_sha, new_sha, git_timeout)

        # Step 7 — rebuild the vector stores with the new code; roll back on failure.
        if not reindex(repo_root):
            logger.error("Auto-update: reindex failed; rolling back to %s.", old_sha[:9])
            rollback = _run_git(["reset", "--hard", old_sha], repo_root, git_timeout)
            if rollback.returncode != 0:
                logger.error(
                    "Auto-update: ROLLBACK FAILED — repo is on new code without rebuilt indexes. %s",
                    rollback.stderr.strip(),
                )
            _write_state(UpdateStatus.REINDEX_FAILED, old_sha, new_sha)
            return UpdateStatus.REINDEX_FAILED

        # Step 8 — record; new code applies on the next start.
        logger.info(
            "Auto-update: prepared update %s -> %s; takes effect on next restart.",
            old_sha[:9], new_sha[:9],
        )
        _write_state(UpdateStatus.UPDATED, old_sha, new_sha)
        return UpdateStatus.UPDATED
    except subprocess.TimeoutExpired:
        # A local git op should not time out under the budget; if one does the
        # system is in a bad state — fail open rather than crash the thread.
        logger.warning("Auto-update: a git operation timed out; serving current code.")
        return UpdateStatus.FETCH_FAILED


# --- Background orchestration -------------------------------------------------

def _run_update_safely() -> None:
    """Lock + throttle + run the update, swallowing every error."""
    try:
        with _process_lock(LOCK_FILE) as acquired:
            if not acquired:
                logger.debug("Auto-update: another process holds the update lock; skipping.")
                return
            if _recently_checked(AUTO_UPDATE_MIN_INTERVAL):
                logger.debug("Auto-update: checked within the throttle window; skipping.")
                return
            status = check_and_apply_update()
            # Only throttle once we've actually hit the network, so a
            # wrong-branch / dirty skip never delays a later real check.
            if status not in _PRE_NETWORK_STATUSES:
                _touch_check_stamp()
            logger.debug("Auto-update: finished with status %s", status)
    except Exception:
        logger.warning("Auto-update: background update crashed (ignored).", exc_info=True)


def start_background_update():
    """Spawn the daemon update thread, unless disabled. Returns it (or None).

    Non-blocking: returns immediately so ``mcp.run()`` can start serving.
    """
    if not AUTO_UPDATE_ENABLED:
        logger.debug("Auto-update: disabled (AGENTS_AUTO_UPDATE=0).")
        return None
    thread = threading.Thread(target=_run_update_safely, name="auto-update", daemon=True)
    thread.start()
    return thread
