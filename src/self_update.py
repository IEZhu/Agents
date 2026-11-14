"""Background self-update for the Agents-Core MCP server.

Two-phase, "prepare in the background, activate by move on the next start"
(the default; ``AGENTS_AUTO_UPDATE_STAGING=1``):

  * **Phase B — prepare** (:func:`prepare_update`, daemon thread via
    :func:`start_background_update`, non-blocking): take a non-blocking
    cross-process lock, throttle redundant network checks, ``git fetch``, and — if
    the install is fast-forwardable and no update is already pending — build the
    new version's vector stores in an **isolated git worktree** under
    ``AUTO_UPDATE_STAGING_DIR`` and write a marker. A target that changes the
    dependencies first gets its own Python environment under ``.venvs``
    (`src.environments`, #181), which the stores are built with. The live
    install is never touched.
  * **Phase A — activate** (:func:`activate_prepared_update`, via
    :func:`run_activation_safely` inside ``startup.py``'s exclusive session lease,
    before the engine modules eagerly load the stores): if a valid prepared marker
    exists, fast-forward the live tree to the prepared sha (local, no network),
    atomically move the pre-built stores into ``data/`` and point ``.venv`` at the
    prepared environment, if any. The expensive work already happened in Phase B,
    so this is just a merge + a few ``os.replace``.

Set ``AGENTS_AUTO_UPDATE_STAGING=0`` to use the synchronous in-place path at an
idle startup under the same exclusive lease
(:func:`check_and_apply_update`): fast-forward + reindex on the live tree, rolled
back via ``git reset --hard`` if the reindex fails.

The new code takes effect at the next start with no active readers. Every server
holds a shared installation lease for its lifetime, protecting lazy prompt reads
as well as imports. A start contending with activation waits before engine imports.

Safety invariants (both paths):
    * acts **only** when the checked-out branch is the target branch
      (``AUTO_UPDATE_BRANCH``) — a no-op on feature branches / local dev —
      or, given a :class:`src.update_channel.Policy`, the previous setting while
      the ``.env`` setting names the target: the update then switches the checkout
      to the target branch (#381); a branch exported over ``.env`` never switches,
      nor does a target branch another worktree has checked out,
    * only when the working tree is clean, and only **fast-forward** (never
      merge / rebase); a switch to a branch that lacks non-merge commits of HEAD
      (a downgrade) needs the owner's recorded confirmation,
    * staging never mutates the live install; a failed prepare writes no marker,
      the legacy path rolls back a failed reindex, and a failed activation move
      rolls back the just-merged tree,
    * an interrupted live mutation leaves a recovery journal that blocks later
      startup; after recovery, torn-pair detection and content hashes repair any
      incomplete stores on load,
    * ordinary update failures preserve the current version; a failed rollback
      or re-exec stops startup rather than allowing mixed versions to serve.
"""

import json
import logging
import os
import re
import selectors
import shutil
import signal
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from typing import Optional

from src import environments, executables
from src.daemon.update_dependencies import NONE as DEPENDENCIES_UNCHANGED, classify as classify_dependencies
from src.startup import UPDATE_JOURNAL
from src.update_channel import Policy, drops_commits, in_use_reason, plausible_branch, policy_for, worktree_on

from src.engine.config import (
    INSTALL_ROOT,
    INSTALL_DATA_DIR,
    EMBEDDING_MODEL,
    AUTO_UPDATE_ENABLED,
    AUTO_UPDATE_REMOTE,
    AUTO_UPDATE_BRANCH,
    AUTO_UPDATE_GIT_TIMEOUT,
    AUTO_UPDATE_MIN_INTERVAL,
    AUTO_UPDATE_REINDEX_TIMEOUT,
    AUTO_UPDATE_ENVIRONMENT_TIMEOUT,
    AUTO_UPDATE_STAGING,
    AUTO_UPDATE_STAGING_DIR,
)

logger = logging.getLogger(__name__)

# Runtime artifacts (all under data/, which is gitignored — so they never make
# the working tree look dirty). Module-level so tests can redirect them.
LOCK_FILE = os.path.join(INSTALL_DATA_DIR, ".update.lock")
CHECK_STAMP = os.path.join(INSTALL_DATA_DIR, ".last_update_check")
STATE_FILE = os.path.join(INSTALL_DATA_DIR, ".last_update.json")
# Two-phase staged update (Phase A activates / Phase B prepares). The marker is
# the single signal that a prepared update awaits activation; STAGING_ROOT is the
# parent dir for per-sha worktrees. Module-level so tests can redirect them.
PREPARED_MARKER = os.path.join(INSTALL_DATA_DIR, ".prepared_update.json")
STAGING_ROOT = AUTO_UPDATE_STAGING_DIR
# Marker schema version — bumped if the on-disk format changes; mismatches are
# treated as invalid and discarded by the Phase A validator. A marker that also
# switches the Python environment uses the next version, which an updater that
# cannot switch environments discards instead of activating code without them.
PREPARED_MARKER_SCHEMA = 1
ENVIRONMENT_MARKER_SCHEMA = 2

# Vector stores that a prepared update stages and activates, paired with their
# content-hash sidecar file. The router cache is intentionally NOT here — it is a
# per-deployment runtime query cache, not derived from the .mdc sources, and it
# self-invalidates on model change.
_STAGED_STORES = (
    ("skills_store", ".skills_hash"),
    ("implants_store", ".implants_hash"),
)

# The default switch policy of the updaters' entry points: the update keys of the
# installation's .env under this process's environment, which a stdio server loaded
# from that .env (#381); a branch exported over .env switches nothing. The shared
# service's controller passes the policy it read (`src.daemon.update`); None allows
# no branch switch.
FROM_ENVIRONMENT = "environment"


def _policy(policy, repo_root) -> Optional[Policy]:
    return policy_for(repo_root) if policy == FROM_ENVIRONMENT else policy


def _git_in(repo_root, git_timeout):
    """``update_channel``'s git callable for *repo_root*."""
    return lambda *args: _run_git(list(args), repo_root, git_timeout)


# A full 40-hex commit sha — the shape of both a marker's target_sha and the
# basename of every staging worktree prepare_update creates.
_FULL_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")


class UpdateStatus:
    """Outcome of one :func:`check_and_apply_update` run (string constants)."""

    NO_GIT = "NO_GIT"
    SKIPPED_BAD_CONFIG = "SKIPPED_BAD_CONFIG"
    SKIPPED_WRONG_BRANCH = "SKIPPED_WRONG_BRANCH"
    SKIPPED_DIRTY = "SKIPPED_DIRTY"
    FETCH_FAILED = "FETCH_FAILED"
    UP_TO_DATE = "UP_TO_DATE"
    SKIPPED_DIVERGED = "SKIPPED_DIVERGED"
    SKIPPED_AHEAD = "SKIPPED_AHEAD"
    # A branch switch to code that does not contain HEAD, without the owner's confirmation.
    SKIPPED_DOWNGRADE = "SKIPPED_DOWNGRADE"
    # A branch switch whose target branch is checked out in another worktree.
    SKIPPED_BRANCH_IN_USE = "SKIPPED_BRANCH_IN_USE"
    # The target changes dependencies this path cannot deliver (`src.environments`).
    SKIPPED_DEPENDENCIES = "SKIPPED_DEPENDENCIES"
    MERGE_FAILED = "MERGE_FAILED"
    REINDEX_FAILED = "REINDEX_FAILED"
    ROLLBACK_FAILED = "ROLLBACK_FAILED"
    UPDATED = "UPDATED"


# Statuses reached *before* any network access. We don't write the throttle
# stamp for these, so a wrong-branch / dirty skip never delays a later
# legitimate check (the branch guard is local and instant).
_PRE_NETWORK_STATUSES = frozenset(
    {
        UpdateStatus.NO_GIT,
        UpdateStatus.SKIPPED_BAD_CONFIG,
        UpdateStatus.SKIPPED_WRONG_BRANCH,
        UpdateStatus.SKIPPED_DIRTY,
        UpdateStatus.SKIPPED_BRANCH_IN_USE,
    }
)


class PreparedStatus:
    """Outcome of one :func:`prepare_update` run (Phase B).

    Reuses :class:`UpdateStatus` string values where the meaning is identical (so
    ``_PRE_NETWORK_STATUSES`` membership and existing throttle logic keep working)
    and adds the staging-specific outcomes.
    """

    NO_GIT = UpdateStatus.NO_GIT
    SKIPPED_BAD_CONFIG = UpdateStatus.SKIPPED_BAD_CONFIG
    SKIPPED_WRONG_BRANCH = UpdateStatus.SKIPPED_WRONG_BRANCH
    SKIPPED_DIRTY = UpdateStatus.SKIPPED_DIRTY
    FETCH_FAILED = UpdateStatus.FETCH_FAILED
    UP_TO_DATE = UpdateStatus.UP_TO_DATE
    SKIPPED_DIVERGED = UpdateStatus.SKIPPED_DIVERGED
    SKIPPED_AHEAD = UpdateStatus.SKIPPED_AHEAD
    SKIPPED_DOWNGRADE = UpdateStatus.SKIPPED_DOWNGRADE
    SKIPPED_BRANCH_IN_USE = UpdateStatus.SKIPPED_BRANCH_IN_USE
    SKIPPED_DEPENDENCIES = UpdateStatus.SKIPPED_DEPENDENCIES
    PREPARED = "PREPARED"
    PREPARE_ENVIRONMENT_FAILED = "PREPARE_ENVIRONMENT_FAILED"
    PREPARE_WORKTREE_FAILED = "PREPARE_WORKTREE_FAILED"
    PREPARE_REINDEX_FAILED = "PREPARE_REINDEX_FAILED"
    PREPARE_STAGE_INCONSISTENT = "PREPARE_STAGE_INCONSISTENT"


class ActivationStatus:
    """Outcome of one :func:`activate_prepared_update` run (Phase A, startup)."""

    NO_MARKER = "NO_MARKER"
    ACTIVATED = "ACTIVATED"
    INVALID_MARKER = "INVALID_MARKER"
    INVALID_MODEL_MISMATCH = "INVALID_MODEL_MISMATCH"
    INVALID_SHA_MISSING = "INVALID_SHA_MISSING"
    INVALID_NOT_FF = "INVALID_NOT_FF"
    INVALID_DIRTY = "INVALID_DIRTY"
    INVALID_WRONG_BRANCH = "INVALID_WRONG_BRANCH"
    INVALID_DOWNGRADE = "INVALID_DOWNGRADE"
    INVALID_BRANCH_IN_USE = "INVALID_BRANCH_IN_USE"
    INVALID_STAGING_MISSING = "INVALID_STAGING_MISSING"
    INVALID_STAGING_INCONSISTENT = "INVALID_STAGING_INCONSISTENT"
    INVALID_CROSS_DEVICE = "INVALID_CROSS_DEVICE"
    INVALID_ENVIRONMENT = "INVALID_ENVIRONMENT"
    ACTIVATE_MERGE_FAILED = "ACTIVATE_MERGE_FAILED"
    ACTIVATE_MOVE_FAILED = "ACTIVATE_MOVE_FAILED"
    ACTIVATE_ENVIRONMENT_FAILED = "ACTIVATE_ENVIRONMENT_FAILED"
    ACTIVATE_ROLLBACK_FAILED = "ACTIVATE_ROLLBACK_FAILED"


# --- Cross-process lock (non-blocking) ---------------------------------------
# fcntl.flock on POSIX, LockFileEx on Windows (src/file_lock.py). Mirrors the
# helper in src/memory/history.py but never blocks, so a held lock means
# "another server is already updating" -> skip immediately.
_lock_state = threading.local()


def _subprocess_lock_fds():
    """Leases a child must retain if its updater parent exits unexpectedly.

    None on Windows: a child cannot hold its parent's lock there (src/windows_job.py).
    """
    return _held_lock_fds() if os.name == "posix" else ()


def _held_lock_fds():
    """Leases registered on the current thread; a Windows re-exec releases them."""
    return getattr(_lock_state, "fds", ())


@contextmanager
def _inherit_lock(fd):
    """Register a held lease for subprocesses launched on the current thread."""
    previous = _held_lock_fds()
    _lock_state.fds = previous if fd is None else (*previous, fd)
    try:
        yield
    finally:
        _lock_state.fds = previous


try:  # pragma: no cover - platform-specific
    import fcntl as _fcntl

    def _try_lock(fh) -> bool:
        """Acquire an exclusive non-blocking lock; return False if already held."""
        try:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fd) -> None:
        """Closing releases the lock; see _process_lock."""

except ImportError:  # pragma: no cover - Windows
    from src.file_lock import _windows_lock, _windows_unlock as _unlock

    def _try_lock(fh) -> bool:
        """Acquire an exclusive non-blocking lock; return False if already held,
        and on a file system without byte-range locks."""
        try:
            return _windows_lock(fh.fileno(), blocking=False, shared=False)
        except OSError:
            return False


@contextmanager
def _process_lock(path: str):
    """Yield True if the exclusive lock was acquired, False otherwise."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    fh = open(path, "w")
    acquired = _try_lock(fh)
    try:
        with _inherit_lock(fh.fileno() if acquired else None):
            yield acquired
    finally:
        # Closing releases only our reference. LOCK_UN would also revoke the
        # lease of any worker still holding the inherited file description.
        # A Windows lock belongs to this process alone: release it explicitly.
        if acquired:
            _unlock(fh.fileno())
        fh.close()


# --- git / reindex helpers ---------------------------------------------------

def _run_command(args, cwd: str, timeout: int, *, env=None) -> subprocess.CompletedProcess:
    """Wait for a command and all descendants retaining its inherited leases.

    A timeout kills the command's process group, including hooks/grandchildren.
    An inherited pipe tracks completion independently of captured stdout: even a
    detached descendant must close its inherited descriptors before rollback or
    lease release can proceed. Such a descendant can extend the timeout, but we
    must not mutate or expose files while it still owns a writer lease.
    """
    if os.name == "nt":
        return _run_command_in_job(args, cwd, timeout, env=env)
    if os.name != "posix":
        return subprocess.run(args, cwd=cwd, capture_output=True, text=True,
                              stdin=subprocess.DEVNULL, timeout=timeout, env=env, check=False)
    completed_read, completed_write = os.pipe()
    deadline = time.monotonic() + timeout
    try:
        with subprocess.Popen(
            args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, env=env, start_new_session=True,
            pass_fds=(*_subprocess_lock_fds(), completed_write),
        ) as process:
            os.close(completed_write)
            completed_write = None
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except BaseException:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                finally:
                    process.wait()
                raise
            finally:
                # The leader may be gone while a descendant still holds the
                # same flock description. Never unlock/downgrade it early.
                with selectors.DefaultSelector() as completion:
                    completion.register(completed_read, selectors.EVENT_READ)
                    if not completion.select(max(0, deadline - time.monotonic())):
                        logger.warning(
                            "Auto-update: waiting for descendants of PID %s (%s) to release inherited leases.",
                            process.pid, args[0],
                        )
                while os.read(completed_read, 4096):
                    pass
            return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)
    finally:
        if completed_write is not None:
            os.close(completed_write)
        os.close(completed_read)


def _run_command_in_job(args, cwd: str, timeout: int, *, env=None) -> subprocess.CompletedProcess:
    """Windows: run the command and its descendants in a job that ends with this process.

    A LockFileEx lease belongs to the process that took it: a child cannot keep it
    after this updater exits unexpectedly, so the job ends the child then instead
    (src/windows_job.py). A timeout ends the whole job, as killpg does on POSIX.
    Descendants that outlive the command get the rest of the timeout, then end
    with the job: none may write once the caller releases its leases.
    """
    from src.windows_job import Job
    deadline = time.monotonic() + timeout
    with Job() as job:
        # Git writes paths in UTF-8 whatever the ANSI code page; a stray byte must not fail the update.
        process = job.start(args, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, encoding="utf-8", errors="replace", env=env,
                            creationflags=subprocess.CREATE_NO_WINDOW)
        with process:
            try:
                stdout, stderr = process.communicate(timeout=timeout)
            except BaseException:
                job.terminate()
                process.wait()
                raise
        if not job.wait_empty(max(0, deadline - time.monotonic())):
            logger.warning("Auto-update: ending descendants of PID %s (%s) that outlived it.", process.pid, args[0])
            if not job.terminate():
                raise RuntimeError(f"Descendants of {args[0]} did not end; restart required")
        return subprocess.CompletedProcess(args, process.returncode, stdout, stderr)


def _is_safe_arg(value: str) -> bool:
    """True if *value* is a non-empty token git won't mistake for an option.

    Guards against argument injection when a remote or branch name (sourced from
    AGENTS_AUTO_UPDATE_REMOTE / _BRANCH) begins with ``-`` and git would parse it
    as a flag rather than a positional argument.
    """
    return bool(value) and not value.startswith("-")


def _run_git(args, cwd: str, timeout: int) -> subprocess.CompletedProcess:
    """Run ``git <args>`` capturing output. Does not raise on non-zero exit.

    May raise ``FileNotFoundError`` (git missing) or ``subprocess.TimeoutExpired``;
    callers handle those where the distinction matters.
    """
    return _run_command(
        [executables.path("git"), *args],
        cwd=cwd,
        timeout=timeout,
        # Without the user's git configuration (#279). A credential prompt would wait for nobody;
        # Git Credential Manager would open a window.
        env={**executables.git_environment(cwd), "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "false"},
    )


def _run_reindex(repo_root: str, timeout: int) -> bool:
    """Rebuild the vector stores in a subprocess running the freshly-pulled code.

    Spawned with ``cwd=repo_root`` so ``-m src.reindex`` resolves, and with the
    inherited environment so a per-process ``LD_LIBRARY_PATH`` (NixOS) survives.
    Returns True on success.
    """
    try:
        result = _run_command(
            [sys.executable, "-m", "src.reindex"],
            cwd=repo_root,
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


def _manifest_git(repo_root: str, timeout: int):
    """``update_dependencies``' git callable: ``check`` raises CalledProcessError, as ``subprocess.run`` does."""
    def git(*args, check=True):
        result = _run_git(list(args), repo_root, timeout)
        if check and result.returncode:
            raise subprocess.CalledProcessError(result.returncode, ["git", *args], result.stdout, result.stderr)
        return result
    return git


# --- State + throttle persistence --------------------------------------------

def _write_state(status: str, old_sha: str, new_sha: str, branch: Optional[str] = None) -> None:
    """Atomically persist the latest update outcome to ``STATE_FILE``; *branch* names a switch's target."""
    payload = {
        "status": status,
        "old_sha": old_sha,
        "new_sha": new_sha,
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if branch:
        payload["branch"] = branch
    try:
        os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
        tmp = STATE_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False)
        os.replace(tmp, STATE_FILE)
    except OSError as e:
        logger.debug("Auto-update: could not write state file: %s", e)


def _begin_update(repo_root: str, old_sha: str, target_sha: str, details=None) -> None:
    """Persist recovery evidence before touching the live tree or store files.

    *details* adds a branch switch's ``old_branch``, ``branch`` and ``branch_tip``
    (see :func:`_rollback_activation`), so a manual recovery knows the branch to return to.
    """
    data_dir = os.path.join(repo_root, "data")
    os.makedirs(data_dir, exist_ok=True)
    journal = os.path.join(data_dir, UPDATE_JOURNAL)
    # Exclusive creation never overwrites evidence of an earlier failed update.
    # A partial/unreadable journal also blocks startup, so writing in place is safe.
    with open(journal, "x", encoding="utf-8") as stream:
        json.dump({"old_sha": old_sha, "target_sha": target_sha, **(details or {})}, stream)
        stream.flush()
        os.fsync(stream.fileno())
    if os.name != "posix":
        return  # Windows cannot open a directory to flush it; NTFS journals the new entry
    directory = os.open(data_dir, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def _finish_update(repo_root: str) -> None:
    """Clear the guard only after activation or verified rollback succeeds."""
    _unlink_if_present(os.path.join(repo_root, "data", UPDATE_JOURNAL))


def log_last_update() -> None:
    """Log the last recorded update outcome (startup observability)."""
    try:
        if not os.path.exists(STATE_FILE):
            return
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            state = json.load(f)
        logger.info(
            "Auto-update: last applied update %s (%s -> %s) at %s",
            state.get("status"),
            (state.get("old_sha") or "")[:9],
            (state.get("new_sha") or "")[:9],
            state.get("ts"),
        )
    except (OSError, ValueError) as e:
        logger.debug("Auto-update: could not read state file: %s", e)


def _recently_checked(interval: int) -> bool:
    """True if the throttle stamp was written within the last *interval* seconds."""
    if interval <= 0:
        return False
    try:
        return (time.time() - os.path.getmtime(CHECK_STAMP)) < interval
    except OSError:
        return False


def _touch_check_stamp() -> None:
    """Record 'now' as the time of the last network check (for throttling)."""
    try:
        os.makedirs(os.path.dirname(CHECK_STAMP) or ".", exist_ok=True)
        with open(CHECK_STAMP, "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%S%z"))
    except OSError as e:
        logger.debug("Auto-update: could not write check stamp: %s", e)


# --- Prepared-update marker (Phase A/B) --------------------------------------

def _write_prepared_marker(target_sha, base_sha, branch, embedding_model, staging_dir, stores, switch=None,
                           environment=None) -> None:
    """Atomically write the prepared-update marker.

    Written **last** by ``prepare_update`` — its presence is the sole signal that
    a staged update is ready for activation. tempfile + ``os.replace`` so a crash
    mid-write can never leave a truncated marker. Raises ``OSError`` on failure so
    the caller can clean up the staging worktree (a build with no marker is moot).
    """
    payload = {
        "schema": PREPARED_MARKER_SCHEMA,
        "target_sha": target_sha,
        "base_sha": base_sha,
        "branch": branch,
        "embedding_model": embedding_model,
        "staging_dir": staging_dir,
        "stores": list(stores),
        "built_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if switch:
        # A branch switch: activation checks out *branch* from ``switch_from``.
        payload.update(switch_from=switch["from"], remote=switch["remote"], downgrade=bool(switch["downgrade"]))
    if environment:
        # The target's Python environment under .venvs: activation points .venv at it.
        payload.update(schema=ENVIRONMENT_MARKER_SCHEMA, environment={
            key: environment[key] for key in ("id", "path", "requirements_sha256")})
    os.makedirs(os.path.dirname(PREPARED_MARKER) or ".", exist_ok=True)
    tmp = PREPARED_MARKER + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, PREPARED_MARKER)


def _read_prepared_marker():
    """Return the marker dict, or ``None`` if absent / unreadable / not a dict.

    A truncated or garbage file (e.g. an aborted writer on a system without
    atomic replace) reads as ``None`` and is thus treated as "no prepared update";
    schema/field validation is the Phase A validator's job, not this reader's.
    """
    try:
        with open(PREPARED_MARKER, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


# --- Core state machine ------------------------------------------------------

def _resolve_ff_target(repo_root: str, remote: str, branch: str, git_timeout: int):
    """:func:`_resolve_target` without branch switches: ``(status, old_sha, target_sha)``."""
    return _resolve_target(repo_root, remote, branch, git_timeout)[:3]


def _branch_tip(repo_root: str, branch: str, target_sha: str, git_timeout: int):
    """The local *branch*'s commit ("" when there is none), or None when it has commits
    *target_sha* lacks: switching to the target would drop them."""
    tip = _run_git(["rev-parse", "--verify", "--quiet", "refs/heads/" + branch], repo_root, git_timeout)
    found = tip.stdout.strip() if tip.returncode == 0 else ""
    if not found:
        return ""
    contained = _run_git(["merge-base", "--is-ancestor", found, target_sha], repo_root, git_timeout)
    return found if contained.returncode == 0 else None


def _resolve_target(repo_root: str, remote: str, branch: str, git_timeout: int, policy: Optional[Policy] = None):
    """Validate + fetch + measure how far *repo_root* is behind ``<remote>/<branch>``.

    Returns ``(status, old_sha, target_sha, switch)``. With a *policy*, a checkout on
    the previous setting while *branch* is the setting (``Policy.may_switch``) resolves to a branch
    switch: ``switch`` is ``{"from", "remote", "downgrade"}`` and the target is the
    fetched tip whether or not it descends from HEAD; one that lacks non-merge commits
    of HEAD (a downgrade, ``update_channel.drops_commits``) needs
    ``Policy.downgrade_confirmed``. Otherwise ``switch`` is None and the rules
    below apply.

    Shared, mutation-free prefix of the legacy in-place path
    (:func:`check_and_apply_update`) and Phase B (:func:`prepare_update`). Handles
    its own git errors; ``(status, old_sha, target_sha)`` are:

    * Terminal outcome (nothing to do, or a failure): ``status`` is the
      :class:`UpdateStatus` to surface and ``target_sha`` is ``None``.
    * Fast-forwardable: ``status is None``, ``old_sha`` is the current HEAD and
      ``target_sha`` is the resolved ``FETCH_HEAD`` commit to fast-forward to.

    Runs entirely before any tree mutation, so the caller's rollback logic only
    needs to cover the merge that follows. A timeout before the fetch returns
    ``NO_GIT`` (pre-network, so it never writes the throttle stamp); after the
    fetch it returns ``FETCH_FAILED``.
    """
    # Step 0 — reject config values git could misread as options (argument
    # injection via AGENTS_AUTO_UPDATE_REMOTE / _BRANCH starting with '-').
    if not (_is_safe_arg(remote) and _is_safe_arg(branch)):
        logger.warning(
            "Auto-update: refusing unsafe remote/branch (%r / %r); skipping.",
            remote, branch,
        )
        return UpdateStatus.SKIPPED_BAD_CONFIG, "", None, None

    # Step 1 — git available and repo_root is a work tree.
    try:
        rev = _run_git(["rev-parse", "--is-inside-work-tree"], repo_root, git_timeout)
    except FileNotFoundError:
        logger.info("Auto-update: git not found; skipping.")
        return UpdateStatus.NO_GIT, "", None, None
    except subprocess.TimeoutExpired:
        logger.warning("Auto-update: git timed out; skipping.")
        return UpdateStatus.NO_GIT, "", None, None
    if rev.returncode != 0 or rev.stdout.strip() != "true":
        logger.info("Auto-update: %s is not a git work tree; skipping.", repo_root)
        return UpdateStatus.NO_GIT, "", None, None

    network_reached = False
    old_sha = ""
    try:
        # Step 2 — branch guard: only act on the target branch.
        cur = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root, git_timeout)
        current_branch = cur.stdout.strip() if cur.returncode == 0 else ""
        switch = None
        if current_branch != branch:
            if policy is None or not policy.may_switch(current_branch, branch):
                logger.info(
                    "Auto-update: on '%s', target is '%s'; skipping (%s).",
                    current_branch or "?", branch,
                    policy.reason(current_branch, branch) if policy is not None else "local development",
                )
                return UpdateStatus.SKIPPED_WRONG_BRANCH, "", None, None
            # A switch writes the name into the repository's configuration: the same
            # check as `channel set` (src/update_channel.py).
            checked = _run_git(["check-ref-format", "--branch", branch], repo_root, git_timeout)
            if not plausible_branch(branch) or checked.returncode != 0 or checked.stdout.strip() != branch:
                logger.warning("Auto-update: refusing invalid branch name %r; skipping.", branch)
                return UpdateStatus.SKIPPED_BAD_CONFIG, "", None, None
            # `checkout -B` fails for a branch another worktree has checked out.
            elsewhere = worktree_on(_git_in(repo_root, git_timeout), branch)
            if elsewhere:
                logger.info("Auto-update: %s; skipping.", in_use_reason(branch, elsewhere))
                return UpdateStatus.SKIPPED_BRANCH_IN_USE, "", None, None
            switch = {"from": current_branch, "remote": remote, "downgrade": False}

        # Step 3 — clean working tree (tracked files only; an incoming file that
        # collides with an untracked one makes the ff-merge fail loudly later).
        status = _run_git(["status", "--porcelain", "--untracked-files=no"], repo_root, git_timeout)
        if status.returncode != 0:
            logger.warning("Auto-update: git status failed; skipping. %s", status.stderr.strip())
            return UpdateStatus.SKIPPED_DIRTY, "", None, None
        if status.stdout.strip():
            logger.info("Auto-update: working tree has uncommitted changes; skipping.")
            return UpdateStatus.SKIPPED_DIRTY, "", None, None

        head = _run_git(["rev-parse", "HEAD"], repo_root, git_timeout)
        old_sha = head.stdout.strip() if head.returncode == 0 else ""
        if not old_sha:
            # Without a baseline commit we cannot roll back, so refuse to mutate.
            logger.warning("Auto-update: could not resolve current HEAD; skipping.")
            return UpdateStatus.NO_GIT, "", None, None

        # Step 4 — fetch the target branch.
        try:
            network_reached = True
            fetch = _run_git(["fetch", remote, branch], repo_root, git_timeout)
        except subprocess.TimeoutExpired:
            logger.warning("Auto-update: git fetch timed out after %ds; serving current code.", git_timeout)
            return UpdateStatus.FETCH_FAILED, old_sha, None, None
        if fetch.returncode != 0:
            logger.warning("Auto-update: git fetch failed; serving current code. %s", fetch.stderr.strip())
            return UpdateStatus.FETCH_FAILED, old_sha, None, None

        remote_ref = f"{remote}/{branch}"
        # Read FETCH_HEAD once, right after this fetch, and use that commit from here
        # on: fetch points it at the just-fetched branch tip (exactly what `git pull`
        # would merge, without remote-tracking-ref refspec nuances), and a later
        # writer cannot change what this run compares and builds.
        tgt = _run_git(["rev-parse", "--verify", "--quiet", "FETCH_HEAD^{commit}"], repo_root, git_timeout)
        target_sha = tgt.stdout.strip() if tgt.returncode == 0 else ""
        if not _FULL_SHA_RE.match(target_sha):
            logger.warning("Auto-update: could not resolve FETCH_HEAD; skipping.")
            return UpdateStatus.FETCH_FAILED, old_sha, None, None
        if switch is not None:
            return _resolve_switch(repo_root, branch, remote_ref, old_sha, target_sha, switch, policy, git_timeout)

        # Step 5 — how far behind / ahead are we?
        counts = _run_git(
            ["rev-list", "--left-right", "--count", f"{target_sha}...HEAD"],
            repo_root, git_timeout,
        )
        if counts.returncode != 0:
            logger.warning("Auto-update: could not compare with %s; skipping. %s", remote_ref, counts.stderr.strip())
            return UpdateStatus.FETCH_FAILED, old_sha, None, None
        try:
            behind_str, ahead_str = counts.stdout.split()
            behind, ahead = int(behind_str), int(ahead_str)
        except ValueError:
            logger.warning("Auto-update: unexpected rev-list output %r; skipping.", counts.stdout)
            return UpdateStatus.FETCH_FAILED, old_sha, None, None
        if ahead > 0:
            # Local has commits the remote lacks: diverged (when also behind) or
            # purely ahead. Either way, never fast-forward over local work.
            outcome = UpdateStatus.SKIPPED_DIVERGED if behind > 0 else UpdateStatus.SKIPPED_AHEAD
            logger.info(
                "Auto-update: local branch has unpushed commits vs %s (ahead %d, behind %d); skipping.",
                remote_ref, ahead, behind,
            )
            return outcome, old_sha, None, None
        if behind == 0:
            logger.info("Auto-update: already up to date with %s.", remote_ref)
            return UpdateStatus.UP_TO_DATE, old_sha, None, None

        # Fast-forwardable — the concrete target commit (both callers want the
        # explicit sha: the legacy path merges it, Phase B stages + records it).
        return None, old_sha, target_sha, None
    except (subprocess.TimeoutExpired, FileNotFoundError):
        # Pre-mutation, so no rollback is needed here. A post-fetch failure is
        # network-reached (FETCH_FAILED, which throttles); a pre-fetch local-op
        # failure stays pre-network (NO_GIT) so it never suppresses a later check.
        logger.warning("Auto-update: a git operation failed; serving current code.")
        return (UpdateStatus.FETCH_FAILED if network_reached else UpdateStatus.NO_GIT), old_sha, None, None


def _resolve_switch(repo_root, branch, remote_ref, old_sha, target_sha, switch, policy, git_timeout):
    """The fetched tip *target_sha* of *branch* as a switch target (see :func:`_resolve_target`)."""
    dropped = drops_commits(_git_in(repo_root, git_timeout), old_sha, target_sha)
    if dropped is None:
        logger.warning("Auto-update: could not compare HEAD with %s; skipping.", remote_ref)
        return UpdateStatus.FETCH_FAILED, old_sha, None, None
    switch["downgrade"] = dropped
    if switch["downgrade"] and not policy.downgrade_confirmed(branch, old_sha):
        logger.warning(
            "Auto-update: %s lacks commits of HEAD %s on '%s'; switching would go back to older code. "
            "Confirm with `channel set %s --allow-downgrade`, or keep '%s' with `channel set %s`; skipping.",
            remote_ref, old_sha[:9], switch["from"], branch, switch["from"], switch["from"],
        )
        return UpdateStatus.SKIPPED_DOWNGRADE, old_sha, None, None
    switch["branch_tip"] = _branch_tip(repo_root, branch, target_sha, git_timeout)
    if switch["branch_tip"] is None:
        logger.warning("Auto-update: local branch '%s' has commits %s lacks; skipping the switch.", branch, remote_ref)
        return UpdateStatus.SKIPPED_DIVERGED, old_sha, None, None
    switch["branch_config"] = _branch_config(repo_root, branch, git_timeout)
    logger.info("Auto-update: switching from '%s' to %s.", switch["from"], remote_ref)
    return None, old_sha, target_sha, switch


def _move_to_target(repo_root: str, target_sha: str, branch: str, switch, git_timeout: int):
    """The commit point: fast-forward, or for a *switch* check out *branch* at the target.

    The switched-to local branch is named like the remote branch and tracks exactly that
    remote branch, never another one. Returns the failing (or last) git result.
    """
    if not switch:
        return _run_git(["merge", "--ff-only", target_sha], repo_root, git_timeout)
    result = _run_git(["checkout", "--quiet", "--no-track", "-B", branch, target_sha], repo_root, git_timeout)
    for key, value in (("remote", switch["remote"]), ("merge", "refs/heads/" + branch)):
        if result.returncode != 0:
            break
        result = _run_git(["config", f"branch.{branch}.{key}", value], repo_root, git_timeout)
    return result


def _branch_config(repo_root: str, branch: str, git_timeout: int) -> dict:
    """The local *branch*'s upstream settings, which a switch overwrites and a rollback restores ("" = unset)."""
    found = {}
    for key in ("remote", "merge"):
        value = _run_git(["config", "--get", f"branch.{branch}.{key}"], repo_root, git_timeout)
        found[key] = value.stdout.strip() if value.returncode == 0 else ""
    return found


def _switch_rollback(branch: str, switch) -> dict:
    """What :func:`_rollback_activation` needs to undo a switch to *branch*; empty for a fast-forward."""
    if not switch:
        return {}
    undo = {"old_branch": switch["from"], "branch": branch, "branch_tip": switch.get("branch_tip") or ""}
    if switch.get("branch_config") is not None:
        undo["branch_config"] = switch["branch_config"]
    return undo


def check_and_apply_update(
    repo_root: str = INSTALL_ROOT,
    remote: str = AUTO_UPDATE_REMOTE,
    branch: str = AUTO_UPDATE_BRANCH,
    *,
    git_timeout: int = AUTO_UPDATE_GIT_TIMEOUT,
    reindex_timeout: int = AUTO_UPDATE_REINDEX_TIMEOUT,
    reindex_fn=None,
    validate_target=None,
    policy=FROM_ENVIRONMENT,
) -> str:
    """Legacy in-place path: fast-forward *repo_root* and rebuild stores in place.

    Used when ``AGENTS_AUTO_UPDATE_STAGING=0``. Pure of threading/locking so it can
    be unit-tested directly. ``reindex_fn`` (``repo_root -> bool``) is injected in
    tests; defaults to spawning ``python -m src.reindex``. A *policy* allows a
    branch switch (:func:`_resolve_target`). Returns an :class:`UpdateStatus` value.
    A target that changes dependencies is skipped: this path rebuilds in place with
    the running environment and has no second one to build or roll back to.
    """
    reindex = reindex_fn or (lambda rr: _run_reindex(rr, reindex_timeout))

    status, old_sha, target_sha, switch = _resolve_target(repo_root, remote, branch, git_timeout,
                                                          _policy(policy, repo_root))
    if target_sha is None:
        return status  # terminal: skip / up-to-date / pre-merge failure

    if validate_target is not None:
        validate_target(old_sha, target_sha)

    try:
        change = classify_dependencies(_manifest_git(repo_root, git_timeout), old_sha, target_sha)
    except (subprocess.SubprocessError, OSError):
        logger.warning("Auto-update: could not compare dependency manifests; serving current code.")
        return UpdateStatus.FETCH_FAILED
    if change.kind != DEPENDENCIES_UNCHANGED:
        logger.warning(
            "Auto-update: %s. The in-place updater (AGENTS_AUTO_UPDATE_STAGING=0) does not install "
            "dependencies: use the staged updater or update in explicit maintenance; skipping.", change.reason,
        )
        return UpdateStatus.SKIPPED_DEPENDENCIES

    undo = _switch_rollback(branch, switch)
    if undo:
        _begin_update(repo_root, old_sha, target_sha, undo)
    else:
        _begin_update(repo_root, old_sha, target_sha)
    try:
        # Step 6 — fast-forward only (or the branch switch).
        merge = _move_to_target(repo_root, target_sha, branch, switch, git_timeout)
        if merge.returncode != 0:
            logger.warning("Auto-update: fast-forward merge failed; skipping. %s", merge.stderr.strip())
            return (UpdateStatus.MERGE_FAILED if _rollback_activation(repo_root, old_sha, git_timeout, **undo)
                    else UpdateStatus.ROLLBACK_FAILED)
        new = _run_git(["rev-parse", "HEAD"], repo_root, git_timeout)
        new_sha = new.stdout.strip() if new.returncode == 0 else ""
        logger.info("Auto-update: fast-forwarded %s -> %s on %s.", old_sha[:9], new_sha[:9], branch)

        # Step 7 — rebuild the vector stores with the new code; roll back on failure.
        # reindex_fn may raise (injected fns, unexpected errors); treat any raise
        # as a failure so the rollback below always runs.
        try:
            reindex_ok = bool(reindex(repo_root))
        except Exception:
            logger.error("Auto-update: reindex raised; treating as failure.", exc_info=True)
            reindex_ok = False
        if not reindex_ok:
            logger.error("Auto-update: reindex failed; rolling back to %s.", old_sha[:9])
            stores_invalidated = True
            try:
                _invalidate_store_hashes(os.path.join(repo_root, "data"))
            except OSError:
                stores_invalidated = False
                logger.error("Auto-update: cannot invalidate partial reindex results.", exc_info=True)
            tree_restored = _rollback_activation(repo_root, old_sha, git_timeout,
                                                clear_journal=stores_invalidated, **undo)
            if not (tree_restored and stores_invalidated):
                return UpdateStatus.ROLLBACK_FAILED
            _write_state(UpdateStatus.REINDEX_FAILED, old_sha, new_sha)
            return UpdateStatus.REINDEX_FAILED

        # Step 8 — record; new code applies on the next start.
        logger.info(
            "Auto-update: prepared update %s -> %s; takes effect on next restart.",
            old_sha[:9], new_sha[:9],
        )
        _write_state(UpdateStatus.UPDATED, old_sha, new_sha, branch if switch else None)
        _finish_update(repo_root)
        return UpdateStatus.UPDATED
    except (subprocess.TimeoutExpired, FileNotFoundError):
        # A git op should not time out (or git vanish) under the budget. The merge
        # has already put us past the network, so this is always FETCH_FAILED. If
        # the fast-forward already mutated the tree, roll back so we never strand
        # the install on new code without rebuilt indexes.
        # Even the merge command itself can time out after advancing HEAD.
        if old_sha:
            logger.error("Auto-update: exception after fast-forward; rolling back to %s.", old_sha[:9])
            if not _rollback_activation(repo_root, old_sha, git_timeout, **undo):
                return UpdateStatus.ROLLBACK_FAILED
        logger.warning("Auto-update: a git operation failed; serving current code.")
        return UpdateStatus.FETCH_FAILED


# --- Phase B: prepare a staged update (git worktree) -------------------------

def _is_unredirected_path(path: str) -> bool:
    """Reject symlinks (and Windows junctions) at *path* or in its parent components.

    Windows paths differ in case without naming another file.
    """
    absolute = os.path.abspath(path)
    return not os.path.islink(absolute) and _same_path(os.path.realpath(absolute), absolute)


def _same_path(first: str, second: str) -> bool:
    return os.path.normcase(first) == os.path.normcase(second)


def _staging_worktrees(repo_root: str, staging_parent: str, git_timeout: int):
    """Return the registered git worktrees that are OUR staging checkouts.

    Parses ``git worktree list --porcelain``. Used to reap **any** staging
    worktree (of any sha) — ``git worktree prune`` alone won't remove one whose
    directory still exists, so a crashed prepare would otherwise leak it forever.

    Callers ``rmtree`` every returned path, and the porcelain list always
    includes the **main worktree** (the live install), so this filter is
    defense-in-depth against a misconfigured ``AGENTS_AUTO_UPDATE_STAGING_DIR``
    (the repo root, or one of its ancestors): a path qualifies only if it is
    a direct child of canonical *staging_parent*, is not *repo_root* itself, and
    its basename is a full sha — the exact shape ``prepare_update`` creates.
    Reject symlinked paths instead of following them to a cleanup target. The
    live install and unrelated user worktrees are never returned.
    """
    if not _is_unredirected_path(staging_parent):
        return []
    try:
        r = _run_git(["worktree", "list", "--porcelain"], repo_root, git_timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if r.returncode != 0:
        return []
    parent = os.path.realpath(staging_parent)
    root = os.path.realpath(repo_root)
    out = []
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
            ap = os.path.abspath(path)
            if (
                _same_path(os.path.dirname(ap), parent)
                and not _same_path(ap, root)
                and _FULL_SHA_RE.match(os.path.basename(ap))
                and _is_unredirected_path(ap)
            ):
                # Keep the registered path: never substitute a symlink target
                # for the worktree that git actually listed.
                out.append(ap)
    return out


def _prune_staging_worktrees(repo_root: str, staging_parent: str, git_timeout: int) -> None:
    """Remove every staging worktree under *staging_parent* + prune admin records.

    Best-effort: ``git worktree remove --force`` each, ``rmtree`` any leftover
    dir, then ``git worktree prune``. Bounds orphan accumulation regardless of how
    a previous prepare/activation died.
    """
    if not _is_unredirected_path(staging_parent):
        logger.warning("Auto-update: refusing cleanup through redirected staging parent %s", staging_parent)
        return
    for path in _staging_worktrees(repo_root, staging_parent, git_timeout):
        try:
            _run_git(["worktree", "remove", "--force", path], repo_root, git_timeout)
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        if os.path.isdir(path):
            shutil.rmtree(path, ignore_errors=True)
    try:
        _run_git(["worktree", "prune"], repo_root, git_timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    # Remove the now-empty parent: its mere existence makes the startup fast
    # path in run_activation_safely take the lock and fork git on every start.
    try:
        os.rmdir(staging_parent)
    except OSError:
        pass  # non-empty (live staging present) or already gone — keep it


def _add_worktree(staging_parent: str, target_sha: str, repo_root: str, git_timeout: int):
    """Create a detached worktree for *target_sha* at ``<staging_parent>/<sha>``.

    Reaps any pre-existing staging worktrees first (orphan recovery), then adds a
    fresh one. Returns the staging dir path, or ``None`` on failure. The worktree
    shares the repo's object store, so the checkout is cheap.
    """
    if not _is_unredirected_path(staging_parent):
        logger.warning("Auto-update: refusing redirected staging parent %s", staging_parent)
        return None
    _prune_staging_worktrees(repo_root, staging_parent, git_timeout)
    try:
        os.makedirs(staging_parent, exist_ok=True)
    except OSError as e:
        logger.warning("Auto-update: could not create staging parent %s: %s", staging_parent, e)
        return None
    staging_dir = os.path.join(staging_parent, target_sha)
    if os.path.lexists(staging_dir):
        # Pruning only removes validated registered worktrees. Anything left
        # belongs to the user or could not be removed safely.
        logger.warning("Auto-update: staging destination already exists: %s", staging_dir)
        return None
    try:
        r = _run_git(["worktree", "add", "--detach", staging_dir, target_sha], repo_root, git_timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("Auto-update: 'git worktree add' error: %s", e)
        return None
    if r.returncode != 0:
        logger.warning("Auto-update: 'git worktree add' failed: %s", r.stderr.strip())
        return None
    return staging_dir


def _run_reindex_at(staging_dir: str, timeout: int, python: Optional[str] = None) -> bool:
    """Build the vector stores INSIDE *staging_dir* by running the worktree's code.

    ``python -m src.reindex`` with ``cwd=staging_dir`` makes the worktree its own
    import root (``-m`` puts cwd on ``sys.path``), so ``config.INSTALL_DATA_DIR``
    resolves to ``<staging_dir>/data`` and the stores never touch the live install
    (verified). Uses *python*, the target's environment when the target changes
    dependencies, else the **live** interpreter (``sys.executable``): the worktree
    has no ``.venv``, and site-packages come from the interpreter prefix regardless
    of cwd. ``EMBEDDING_MODEL`` is pinned explicitly so the staged vectors match the
    live process even though the worktree has no ``.env``.
    """
    env = dict(os.environ)
    env["EMBEDDING_MODEL"] = EMBEDDING_MODEL
    try:
        result = _run_command(
            [python or sys.executable, "-m", "src.reindex"],
            cwd=staging_dir,
            timeout=timeout,
            env=env,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("Auto-update: prepare reindex subprocess error: %s", e)
        return False
    if result.returncode != 0:
        logger.warning(
            "Auto-update: prepare reindex failed (rc=%d): %s",
            result.returncode, (result.stderr or "")[-2000:],
        )
        return False
    return True


def _validate_staged_stores(staging_dir: str, stores) -> bool:
    """Verify the freshly-built stores under ``<staging_dir>/data`` are coherent.

    Numpy-free (json + stat only): for each store, the hash sidecar must exist and
    the ``.npz``/``.json`` pair must be both-present (json parseable, has
    ``save_version``) or both-absent (the valid-empty case where ``save()`` removed
    the files and wrote only the hash). A torn pair fails. The deeper torn-pair
    detection at load time is handled by ``NumpyVectorStore._load``.
    """
    data_dir = os.path.join(staging_dir, "data")
    if not _is_unredirected_path(data_dir):
        logger.warning("Auto-update: staged data path is redirected.")
        return False
    for name, hash_file in _STAGED_STORES:
        if name not in stores:
            continue
        hash_path = os.path.join(data_dir, hash_file)
        npz = os.path.join(data_dir, f"{name}.npz")
        meta = os.path.join(data_dir, f"{name}.json")
        if not all(_is_unredirected_path(path) for path in (hash_path, npz, meta)):
            logger.warning("Auto-update: staged store %s contains a redirected artifact.", name)
            return False
        if not os.path.exists(hash_path):
            logger.warning("Auto-update: staged store %s is missing its hash file.", name)
            return False
        npz_exists, meta_exists = os.path.exists(npz), os.path.exists(meta)
        if not npz_exists and not meta_exists:
            continue  # valid-empty store
        if npz_exists != meta_exists:
            logger.warning("Auto-update: staged store %s has a torn npz/json pair.", name)
            return False
        try:
            with open(meta, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError):
            logger.warning("Auto-update: staged store %s json is unreadable.", name)
            return False
        if not isinstance(payload, dict) or not isinstance(payload.get("save_version"), str):
            logger.warning("Auto-update: staged store %s json missing save_version.", name)
            return False
    return True


def prepare_update(
    repo_root: str = INSTALL_ROOT,
    remote: str = AUTO_UPDATE_REMOTE,
    branch: str = AUTO_UPDATE_BRANCH,
    *,
    git_timeout: int = AUTO_UPDATE_GIT_TIMEOUT,
    reindex_timeout: int = AUTO_UPDATE_REINDEX_TIMEOUT,
    reindex_fn=None,
    staging_parent: Optional[str] = None,
    embedding_model: Optional[str] = None,
    validate_target=None,
    policy=FROM_ENVIRONMENT,
    live_python: Optional[str] = None,
    environment_timeout: int = AUTO_UPDATE_ENVIRONMENT_TIMEOUT,
    environment_runner=None,
    reasons: Optional[list] = None,
) -> str:
    """Phase B: prepare a staged update WITHOUT mutating the live install.

    Fetches the target, and if *repo_root* is fast-forwardable, builds the new
    version's indexes in an isolated git worktree and writes the prepared marker
    (last) so the next start activates it. Never touches the live tree or stores.
    Pure of threading/locking. ``reindex_fn`` (``staging_dir -> bool``) is injected
    in tests and by the daemon; defaults to spawning ``python -m src.reindex`` in
    the worktree. ``embedding_model`` is the model the marker records (default:
    this process's). ``validate_target(old_sha, target_sha)`` runs before anything
    is built and may raise to refuse the target. A *policy* allows a branch switch
    (:func:`_resolve_target`); the marker then records it for activation. Returns a
    :class:`PreparedStatus` value.

    A target that changes dependencies first gets its environment under ``.venvs``
    (`src.environments`): built with *environment_runner* (default
    :func:`_run_command`) within *environment_timeout*, or reused. ``reindex_fn`` then
    gets ``python=`` that environment's interpreter, and the marker records the
    environment for activation to switch ``.venv`` to it. *live_python* is the
    interpreter serving the installation (default: this process's). A dependency
    change that cannot be delivered returns ``SKIPPED_DEPENDENCIES``, a failed build
    ``PREPARE_ENVIRONMENT_FAILED``; *reasons*, when given, receives the explanation.
    """
    staging_parent = staging_parent or STAGING_ROOT
    reindex = reindex_fn or (lambda sd, python=None: _run_reindex_at(sd, reindex_timeout, python))

    status, old_sha, target_sha, switch = _resolve_target(repo_root, remote, branch, git_timeout,
                                                          _policy(policy, repo_root))
    if target_sha is None:
        return status  # terminal: skip / up-to-date / pre-merge failure

    if validate_target is not None:
        validate_target(old_sha, target_sha)

    environment = None
    try:
        needed = environments.needed(repo_root, _manifest_git(repo_root, git_timeout), old_sha, target_sha,
                                     live_python or sys.executable, run=environment_runner or _run_command)
        if needed is not None:
            logger.info("Auto-update: %s; preparing the target's Python environment.", needed.reason)
            environment = environments.build(repo_root, needed, run=environment_runner or _run_command,
                                             timeout=environment_timeout)
    except environments.Refused as refusal:
        logger.warning("Auto-update: %s; skipping.", refusal)
        if reasons is not None:
            reasons.append(str(refusal))
        return PreparedStatus.SKIPPED_DEPENDENCIES
    except environments.BuildFailed as failure:
        logger.error("Auto-update: building the target's Python environment failed: %s", failure)
        if reasons is not None:
            reasons.append(f"building the target's Python environment failed: {failure}")
        return PreparedStatus.PREPARE_ENVIRONMENT_FAILED
    except (subprocess.SubprocessError, OSError):
        logger.warning("Auto-update: could not compare dependency manifests; serving current code.", exc_info=True)
        return PreparedStatus.FETCH_FAILED

    stores = [name for name, _ in _STAGED_STORES]

    staging_dir = _add_worktree(staging_parent, target_sha, repo_root, git_timeout)
    if staging_dir is None:
        return PreparedStatus.PREPARE_WORKTREE_FAILED

    try:
        try:
            built = bool(reindex(staging_dir, python=environment["python"]) if environment
                         else reindex(staging_dir))
        except Exception:
            logger.error("Auto-update: prepare reindex raised; treating as failure.", exc_info=True)
            built = False
        if not built:
            logger.error("Auto-update: prepare reindex failed; discarding staging worktree.")
            _prune_staging_worktrees(repo_root, staging_parent, git_timeout)
            return PreparedStatus.PREPARE_REINDEX_FAILED

        if not _validate_staged_stores(staging_dir, stores):
            logger.error("Auto-update: staged stores inconsistent; discarding staging worktree.")
            _prune_staging_worktrees(repo_root, staging_parent, git_timeout)
            return PreparedStatus.PREPARE_STAGE_INCONSISTENT

        # Marker is written LAST — its presence is the sole "ready to activate"
        # signal, so an interrupted prepare never leaves a marker without stores.
        _write_prepared_marker(
            target_sha=target_sha,
            base_sha=old_sha,
            branch=branch,
            embedding_model=embedding_model or EMBEDDING_MODEL,
            staging_dir=staging_dir,
            stores=stores,
            switch=switch,
            environment=environment,
        )
    except OSError as e:
        logger.error("Auto-update: prepare failed to finalize (%s); discarding staging.", e)
        _prune_staging_worktrees(repo_root, staging_parent, git_timeout)
        return PreparedStatus.PREPARE_STAGE_INCONSISTENT

    logger.info(
        "Auto-update: prepared update %s -> %s; will activate on next start.",
        old_sha[:9], target_sha[:9],
    )
    return PreparedStatus.PREPARED


# --- Phase A: activate a prepared update (startup) ---------------------------

def _same_filesystem(path_a: str, path_b: str) -> bool:
    """True if both paths live on the same device (``st_dev``); False on any stat error."""
    try:
        return os.stat(path_a).st_dev == os.stat(path_b).st_dev
    except OSError:
        return False


def _unlink_if_present(path: str) -> None:
    """Remove a consistency barrier; only an already absent file is harmless."""
    try:
        os.remove(path)
    except FileNotFoundError:
        pass


def _discard_staging(repo_root: str, git_timeout: int) -> None:
    """Drop a prepared update: remove the marker FIRST, then reap staging worktrees.

    Marker-first so a crash mid-cleanup can never re-activate a half-removed
    staging set on the next start. Failure to remove the marker must propagate
    before any worktree cleanup; only an already-missing marker is harmless.
    """
    _unlink_if_present(PREPARED_MARKER)
    try:
        _prune_staging_worktrees(repo_root, STAGING_ROOT, git_timeout)
    except Exception:
        # Cleanup is best-effort, including after merge/move have committed.
        # It must never bypass the caller's re-exec or rollback-failure handling.
        logger.warning("Auto-update: staging cleanup failed; will retry later.", exc_info=True)


def _validate_prepared(marker, repo_root, branch, embedding_model, git_timeout, policy=None):
    """Run the activation gates. Returns ``(invalid_status_or_None, already_at_target)``.

    A marker with ``switch_from`` was built for a branch switch: it activates from that
    branch at exactly the base it was built from, and a downgrade only while *policy*
    still holds the confirmation for that HEAD.

    ``invalid_status_or_None`` is an :class:`ActivationStatus` ``INVALID_*`` when a
    gate fails (the caller discards the staging and returns it); ``None`` means OK
    to activate. ``already_at_target`` is True when HEAD is already the prepared
    sha (the crash-between-merge-and-move case) — the caller then skips the merge
    and only completes the file move.
    """
    # Gate 1 — well-formed marker. `stores` must be a non-empty list of KNOWN
    # store names (prepare_update always records the full _STAGED_STORES set):
    # a truthy non-list would raise mid-validation/move and strand the marker
    # on disk forever (Phase B skips preparing while a marker exists), while
    # unknown names would validate and move NOTHING yet still ff-merge —
    # activating new code without its pre-built stores.
    known_stores = {name for name, _ in _STAGED_STORES}
    target_sha = marker.get("target_sha")
    base_sha = marker.get("base_sha")
    stores = marker.get("stores")
    environment = marker.get("environment")
    if (
        marker.get("schema") not in (PREPARED_MARKER_SCHEMA, ENVIRONMENT_MARKER_SCHEMA)
        or (marker.get("schema") == ENVIRONMENT_MARKER_SCHEMA) != (environment is not None)
        or (environment is not None and (not isinstance(environment, dict)
                                         or not environments.valid_id(environment.get("id"))))
        or not isinstance(target_sha, str)
        or not _FULL_SHA_RE.match(target_sha)
        or not isinstance(base_sha, str)
        or not _FULL_SHA_RE.match(base_sha)
        or not isinstance(stores, list)
        or not stores
        or not all(isinstance(s, str) and s in known_stores for s in stores)
        or set(stores) != known_stores
    ):
        logger.warning("Auto-update: prepared marker is malformed; discarding.")
        return ActivationStatus.INVALID_MARKER, False

    # Gate 1b — the marker was prepared for the branch we would activate on.
    if marker.get("branch") != branch:
        logger.info(
            "Auto-update: marker prepared for branch %r, expected %r; discarding.",
            marker.get("branch"), branch,
        )
        return ActivationStatus.INVALID_WRONG_BRANCH, False

    # Gate 2 — embedding model matches the current process.
    if marker.get("embedding_model") != embedding_model:
        logger.info(
            "Auto-update: prepared marker model %r != current %r; discarding.",
            marker.get("embedding_model"), embedding_model,
        )
        return ActivationStatus.INVALID_MODEL_MISMATCH, False

    try:
        # Gate 3 — branch guard.
        cur = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root, git_timeout)
        current_branch = cur.stdout.strip() if cur.returncode == 0 else ""
        switching = current_branch != branch
        if switching and (not marker.get("switch_from") or current_branch != marker.get("switch_from")
                          or policy is None or not policy.may_switch(current_branch, branch)):
            logger.info(
                "Auto-update: on '%s', prepared for '%s'; discarding.",
                current_branch or "?", branch,
            )
            return ActivationStatus.INVALID_WRONG_BRANCH, False

        # Gate 4 — clean working tree.
        status = _run_git(["status", "--porcelain", "--untracked-files=no"], repo_root, git_timeout)
        if status.returncode != 0 or status.stdout.strip():
            logger.info("Auto-update: working tree not clean; discarding prepared update.")
            return ActivationStatus.INVALID_DIRTY, False

        # Gate 5 — target is a real commit object.
        exists = _run_git(["rev-parse", "--verify", "--quiet", target_sha + "^{commit}"], repo_root, git_timeout)
        if exists.returncode != 0:
            logger.warning("Auto-update: prepared target %s is not a known commit; discarding.", target_sha[:9])
            return ActivationStatus.INVALID_SHA_MISSING, False

        # Gate 6 — fast-forward descendant of HEAD (or already there).
        head = _run_git(["rev-parse", "HEAD"], repo_root, git_timeout)
        head_sha = head.stdout.strip() if head.returncode == 0 else ""
        if switching:
            # Gate 6s — a switch activates from the commit it was built from.
            if head_sha != base_sha:
                logger.info("Auto-update: HEAD moved since the switch was prepared; discarding.")
                return ActivationStatus.INVALID_NOT_FF, False
            dropped = drops_commits(_git_in(repo_root, git_timeout), head_sha, target_sha)
            if dropped is not False and not (marker.get("downgrade") is True
                                             and policy.downgrade_confirmed(branch, head_sha)):
                logger.info("Auto-update: prepared switch to %s goes back to older code without a "
                            "confirmation; discarding.", target_sha[:9])
                return ActivationStatus.INVALID_DOWNGRADE, False
            if _branch_tip(repo_root, branch, target_sha, git_timeout) is None:
                logger.info("Auto-update: local branch '%s' has commits the target lacks; discarding.", branch)
                return ActivationStatus.INVALID_NOT_FF, False
            elsewhere = worktree_on(_git_in(repo_root, git_timeout), branch)
            if elsewhere:
                logger.info("Auto-update: %s; discarding.", in_use_reason(branch, elsewhere))
                return ActivationStatus.INVALID_BRANCH_IN_USE, False
            already_at_target = False
        elif head_sha == target_sha:
            already_at_target = True
        else:
            anc = _run_git(["merge-base", "--is-ancestor", "HEAD", target_sha], repo_root, git_timeout)
            if anc.returncode != 0:
                logger.info("Auto-update: prepared target %s is not a fast-forward of HEAD; discarding.", target_sha[:9])
                return ActivationStatus.INVALID_NOT_FF, False
            already_at_target = False
    except (subprocess.TimeoutExpired, FileNotFoundError):
        logger.warning("Auto-update: git op failed during activation validation; discarding.")
        return ActivationStatus.INVALID_SHA_MISSING, False

    # Gate 7 — staging present & consistent. Derive the path from STAGING_ROOT
    # rather than trusting the marker's recorded absolute path (robust to a moved
    # install); a missing/torn staging set discards and lets Phase B re-prepare.
    staging_dir = os.path.join(STAGING_ROOT, target_sha)
    if not _is_unredirected_path(STAGING_ROOT) or not _is_unredirected_path(staging_dir):
        logger.warning("Auto-update: prepared staging path is redirected; discarding.")
        return ActivationStatus.INVALID_STAGING_INCONSISTENT, False
    if not os.path.isdir(staging_dir):
        logger.warning("Auto-update: staging dir for %s is missing; discarding.", target_sha[:9])
        return ActivationStatus.INVALID_STAGING_MISSING, False
    if not _validate_staged_stores(staging_dir, marker.get("stores", [])):
        return ActivationStatus.INVALID_STAGING_INCONSISTENT, False

    # Gate 8 — staging and live data must share a filesystem: the activation
    # move relies on atomic os.replace, which fails with EXDEV across mounts —
    # and by then the ff-merge (the commit point) would already have run.
    live_data = os.path.join(repo_root, "data")
    try:
        os.makedirs(live_data, exist_ok=True)
    except OSError:
        pass  # the stat below fails -> discard
    if not _same_filesystem(os.path.join(staging_dir, "data"), live_data):
        logger.warning(
            "Auto-update: staging dir %s is not on the same filesystem as %s; discarding.",
            staging_dir, live_data,
        )
        return ActivationStatus.INVALID_CROSS_DEVICE, False

    # Gate 9 — the target's Python environment is complete, and .venv can be pointed at it.
    if environment is not None and (reason := environments.activation_refusal(repo_root, environment)):
        logger.warning("Auto-update: %s; discarding.", reason)
        return ActivationStatus.INVALID_ENVIRONMENT, False

    return None, already_at_target


class StoreInvalidationError(OSError):
    """A partially published store batch could not be marked for rebuilding."""


def _invalidate_store_hashes(data_dir: str, stores=_STAGED_STORES) -> None:
    """Invalidate all selected stores, reporting any failure after trying each."""
    error = None
    for _, hash_file in stores:
        try:
            _unlink_if_present(os.path.join(data_dir, hash_file))
        except OSError as exc:
            error = exc
    if error is not None:
        raise error


def _relocate_store_metadata(metadata_path: str, source_dir: str) -> None:
    """Point persisted source paths at the live checkout without rebuilding vectors.

    Derive paths from the indexers' flat filenames, not their old absolute prefix:
    the installation itself may have moved since preparation. Keep save_version
    unchanged so the existing NPZ/JSON pair remains coherent.
    """
    with open(metadata_path, "r", encoding="utf-8") as f:
        try:
            payload = json.load(f)
        except ValueError as exc:
            raise OSError("Cannot relocate unreadable store metadata") from exc
    records = payload.get("metadatas") if isinstance(payload, dict) else None
    if not isinstance(records, list):
        raise OSError("Cannot relocate malformed store metadata")
    for record in records:
        filename = record.get("filename") if isinstance(record, dict) else None
        if (not isinstance(filename, str) or filename in {"", ".", ".."}
                or os.path.basename(filename) != filename):
            raise OSError("Cannot relocate invalid source filename")
        record["path"] = os.path.join(os.path.abspath(source_dir), filename)
    # This is still a staged file. A failed write takes the normal activation
    # rollback path; no new hash is published until the entire batch succeeds.
    with open(metadata_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)


def _activate_staged_stores(staging_dir: str, repo_root: str, stores) -> None:
    """Move the staged store set into the live ``<repo_root>/data`` atomically per file.

    Invalidate every hash, move every store pair, then publish every new hash.
    Any failure after moving starts invalidates the entire batch again, including
    hashes already published. If that cleanup fails, the recovery journal must
    remain in place; even a code-only update may change the index format.
    """
    src_data = os.path.join(staging_dir, "data")
    dst_data = os.path.join(repo_root, "data")
    os.makedirs(dst_data, exist_ok=True)
    selected = [(name, hash_file) for name, hash_file in _STAGED_STORES if name in stores]
    # Failure here is harmless to old stores: no data file has moved yet.
    _invalidate_store_hashes(dst_data, selected)
    try:
        for name, _ in selected:
            src_npz = os.path.join(src_data, f"{name}.npz")
            src_json = os.path.join(src_data, f"{name}.json")
            dst_npz = os.path.join(dst_data, f"{name}.npz")
            dst_json = os.path.join(dst_data, f"{name}.json")
            if os.path.exists(src_npz) and os.path.exists(src_json):
                _relocate_store_metadata(src_json, os.path.join(repo_root, name.removesuffix("_store")))
                os.replace(src_npz, dst_npz)
                os.replace(src_json, dst_json)
            else:
                _unlink_if_present(dst_npz)
                _unlink_if_present(dst_json)
        for _, hash_file in selected:
            os.replace(os.path.join(src_data, hash_file), os.path.join(dst_data, hash_file))
    except OSError:
        try:
            _invalidate_store_hashes(dst_data, selected)
        except OSError as exc:
            raise StoreInvalidationError("Cannot invalidate the partial store batch") from exc
        raise


def activate_prepared_update(
    repo_root: str = INSTALL_ROOT,
    branch: str = AUTO_UPDATE_BRANCH,
    *,
    git_timeout: int = AUTO_UPDATE_GIT_TIMEOUT,
    embedding_model: str = EMBEDDING_MODEL,
    policy=FROM_ENVIRONMENT,
    environment_plan=None,
) -> str:
    """Phase A: activate a prepared update via a fast local ff-merge + file move.

    Runs at startup BEFORE the engine modules load the stores. The common case is
    no marker (a single stat). When a valid marker exists: ff-merge the live tree
    to the prepared sha (no network) and move the pre-built stores in. Any gate
    failure discards the staging and keeps serving the old code; a move failure
    additionally rolls the just-merged tree back to the pre-merge commit (best
    effort), so a failed activation never leaves new code without its stores.
    A marker built for a branch switch checks out *branch* at the target instead of
    merging (:func:`_move_to_target`); a failure returns to the previous branch.
    *policy* rechecks a downgrade's confirmation (:func:`_validate_prepared`).
    A marker with a Python environment points ``.venv`` at it after the store move
    (`src.environments.switch`); the recovery journal records what it replaces, and
    every rollback restores it. *environment_plan* is that record when the caller
    journaled it already (the shared service). After success, environments other
    than the new and the previous one are removed.
    Pure of threading/locking. Returns an :class:`ActivationStatus` value.
    """
    marker = _read_prepared_marker()
    if marker is None:
        # Distinguish "no file" (the common fast path) from "file present but
        # unreadable": the latter would otherwise satisfy run_activation_safely's
        # existence gate forever, costing a lock + git forks on every start
        # until someone removes the file by hand.
        if os.path.exists(PREPARED_MARKER):
            logger.warning("Auto-update: prepared marker exists but is unreadable; discarding.")
            _discard_staging(repo_root, git_timeout)
            return ActivationStatus.INVALID_MARKER
        return ActivationStatus.NO_MARKER

    invalid, already_at_target = _validate_prepared(
        marker, repo_root, branch, embedding_model, git_timeout, _policy(policy, repo_root)
    )
    if invalid is not None:
        if _stranded_environment(repo_root, marker, git_timeout) and (
                held := _hold_stranded(repo_root, marker, f"activation refused it ({invalid})", git_timeout)):
            return held
        _discard_staging(repo_root, git_timeout)
        return invalid

    target_sha = marker["target_sha"]
    old_sha = marker.get("base_sha", "")
    staging_dir = os.path.join(STAGING_ROOT, target_sha)
    stores = marker.get("stores", [])
    plan = None
    if marker.get("environment"):
        wanted = marker["environment"]["id"]
        try:
            plan = environment_plan or environments.plan(repo_root, wanted)
        except environments.Refused as refusal:
            logger.warning("Auto-update: %s; discarding.", refusal)
            plan = None
        if not plan or plan.get("to") != wanted:
            if already_at_target and (held := _hold_stranded(repo_root, marker, "its switch cannot be planned",
                                                             git_timeout)):
                return held
            _discard_staging(repo_root, git_timeout)
            return ActivationStatus.INVALID_ENVIRONMENT
    # Journaled with the branch switch: every rollback below restores the environment too.
    env_undo = {"environment": plan} if plan else {}

    # Commit point: the ff-merge. Skipped when already at the target (a prior run
    # merged then died before the move) — we only need to finish the move.
    merged_now = False
    pre_merge_sha = ""
    undo = dict(env_undo)
    switched = {}
    if not already_at_target:
        try:
            head = _run_git(["rev-parse", "HEAD"], repo_root, git_timeout)
            pre_merge_sha = head.stdout.strip() if head.returncode == 0 else ""
            if not pre_merge_sha:
                # Without a rollback point we refuse to merge at all.
                logger.error("Auto-update: cannot resolve pre-merge HEAD; discarding.")
                _discard_staging(repo_root, git_timeout)
                return ActivationStatus.ACTIVATE_MERGE_FAILED
            switch = _prepared_switch(marker, repo_root, branch, target_sha, git_timeout)
            switched = _switch_rollback(branch, switch)
            undo = {**switched, **env_undo}
            if undo:
                _begin_update(repo_root, pre_merge_sha, target_sha, undo)
            else:
                _begin_update(repo_root, pre_merge_sha, target_sha)
            merge = _move_to_target(repo_root, target_sha, branch, switch, git_timeout)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.error("Auto-update: activation merge error: %s; discarding.", e)
            # A timeout can occur in post-merge, after HEAD has advanced.
            restored = not pre_merge_sha or _rollback_activation(repo_root, pre_merge_sha, git_timeout, **undo)
            if restored:
                _discard_staging(repo_root, git_timeout)
            return (ActivationStatus.ACTIVATE_MERGE_FAILED if restored
                    else ActivationStatus.ACTIVATE_ROLLBACK_FAILED)
        if merge.returncode != 0:
            logger.error("Auto-update: activation ff-merge failed; discarding. %s", merge.stderr.strip())
            restored = _rollback_activation(repo_root, pre_merge_sha, git_timeout, **undo)
            if restored:
                _discard_staging(repo_root, git_timeout)
            return (ActivationStatus.ACTIVATE_MERGE_FAILED if restored
                    else ActivationStatus.ACTIVATE_ROLLBACK_FAILED)
        merged_now = True
    else:
        # Resumed: an earlier run merged. The marker's base is still the revision to go back to.
        resumed_from = old_sha if isinstance(old_sha, str) and _FULL_SHA_RE.match(old_sha) else target_sha
        if env_undo:
            _begin_update(repo_root, resumed_from, target_sha, env_undo)
        else:
            _begin_update(repo_root, resumed_from, target_sha)

    try:
        _activate_staged_stores(staging_dir, repo_root, stores)
    except OSError as e:
        logger.error("Auto-update: activation store move failed (%s).", e)
        stores_invalidated = not isinstance(e, StoreInvalidationError)
        if not merged_now:
            # The first invalidation can fail before any move. With target code
            # already installed, even untouched old stores must be invalidated
            # before startup is safe; a plain OSError is not proof of that.
            try:
                _invalidate_store_hashes(os.path.join(repo_root, "data"))
            except OSError:
                stores_invalidated = False
                logger.error("Auto-update: cannot invalidate stores while resuming activation.", exc_info=True)
            else:
                stores_invalidated = True
        restored = stores_invalidated
        if merged_now:
            # This run performed the merge: restore the pre-merge tree so the
            # process keeps serving the old code (no mixed-version runtime and
            # no new code without its stores). In the already_at_target resume
            # case the merge was a prior run's fait accompli — completing or
            # discarding is all that can be done there.
            tree_restored = _rollback_activation(repo_root, pre_merge_sha, git_timeout,
                                                clear_journal=stores_invalidated, **undo)
            restored = tree_restored and stores_invalidated
        elif stores_invalidated:
            # No code mutation in this run; missing hashes repair partial moves. The
            # code is the target's already: it still gets its environment (Gate 9
            # found it complete), or startup stops.
            if plan:
                try:
                    environments.switch(repo_root, plan)
                except (OSError, ValueError) as switch_error:
                    _restore_environment(repo_root, plan)
                    if held := _hold_stranded(repo_root, marker, f"switching to it failed ({switch_error})",
                                              git_timeout, plan):
                        return held
            _finish_update(repo_root)
        if restored:
            _discard_staging(repo_root, git_timeout)
        return (ActivationStatus.ACTIVATE_MOVE_FAILED if restored
                else ActivationStatus.ACTIVATE_ROLLBACK_FAILED)

    if plan:
        try:
            environments.switch(repo_root, plan)
        except (OSError, ValueError) as e:
            logger.error("Auto-update: switching the Python environment failed (%s).", e)
            return _environment_switch_failed(repo_root, git_timeout, merged_now, pre_merge_sha, undo, marker)

    _discard_staging(repo_root, git_timeout)
    _write_state(ActivationStatus.ACTIVATED, old_sha, target_sha, branch if switched else None)
    _finish_update(repo_root)
    if plan:
        _prune_environments(repo_root, plan)
    logger.info("Auto-update: activated prepared update %s -> %s.", (old_sha or "")[:9], target_sha[:9])
    return ActivationStatus.ACTIVATED


def _environment_switch_failed(repo_root, git_timeout, merged_now, pre_merge_sha, undo, marker) -> str:
    """Undo an activation whose stores moved in but whose environment switch failed.

    The new stores are invalidated, so the old code rebuilds them; the environment
    and, when this run merged, the tree are restored. A resumed activation (merged by
    an earlier run) cannot return to the old code, and the new code must not run
    on an environment that lacks its packages: it keeps the journal, which stops
    startup (:func:`_hold_stranded`).
    """
    try:
        _invalidate_store_hashes(os.path.join(repo_root, "data"))
        invalidated = True
    except OSError:
        invalidated = False
        logger.error("Auto-update: cannot invalidate the moved stores.", exc_info=True)
    if not merged_now:
        _restore_environment(repo_root, undo.get("environment"))
        if held := _hold_stranded(repo_root, marker, "switching to it failed", git_timeout, undo["environment"]):
            return held
        if invalidated:
            _finish_update(repo_root)
            _discard_staging(repo_root, git_timeout)
            return ActivationStatus.ACTIVATE_ENVIRONMENT_FAILED
        return ActivationStatus.ACTIVATE_ROLLBACK_FAILED
    restored = _rollback_activation(repo_root, pre_merge_sha, git_timeout, clear_journal=invalidated, **undo)
    if restored and invalidated:
        _discard_staging(repo_root, git_timeout)
        return ActivationStatus.ACTIVATE_ENVIRONMENT_FAILED
    return ActivationStatus.ACTIVATE_ROLLBACK_FAILED


def _stranded_environment(repo_root, marker, git_timeout):
    """The environment *marker* names when the checkout runs its target already but ``.venv`` points
    elsewhere, else None: then the update must not end on the old environment."""
    environment = marker.get("environment")
    if not isinstance(environment, dict) or not environments.valid_id(environment.get("id")):
        return None
    try:
        head = _run_git(["rev-parse", "HEAD"], repo_root, git_timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return None
    if head.returncode or head.stdout.strip() != marker.get("target_sha"):
        return None
    if environments.state(repo_root) == {"kind": "link", "name": environment["id"]}:
        return None
    return environment["id"]


def _unmet_requirements(repo_root, marker, git_timeout):
    """The requirements of the target's environment that ``.venv`` does not satisfy; None when unknown.

    The prepared environment's requirements when it still exists, else the target's
    ``requirements.txt`` and runtime dependencies; each distribution must be installed
    in a release version its specifiers accept (`src.environments.unmet`).
    """
    try:
        with open(os.path.join(environments.path(repo_root, marker["environment"]["id"]),
                               environments.REQUIREMENTS), encoding="utf-8") as stream:
            text = stream.read()
    except OSError:
        from src.daemon.update_dependencies import inputs
        try:
            spec = inputs(_manifest_git(repo_root, git_timeout), marker.get("base_sha"), marker["target_sha"])
        except (ValueError, TypeError, subprocess.SubprocessError, OSError):
            return None
        text = "\n".join([spec.requirements, *spec.dependencies])
    try:
        return environments.unmet(text, os.path.realpath(environments.link_path(repo_root)))
    except OSError:
        return None


def _hold_stranded(repo_root, marker, reason, git_timeout, plan=None) -> Optional[str]:
    """Stop startup while the checkout runs code whose environment is not active and ``.venv`` does not
    satisfy all of its requirements; None when it does (installed by hand), with a warning.

    The recovery journal stays (or is written, with the marker's base as ``old_sha``)
    with the environment switch, so no start serves the target's code without its
    packages; the prepared update stays for a later activation. ``ACTIVATE_ROLLBACK_FAILED``.
    """
    name, target_sha = marker["environment"]["id"], marker["target_sha"]
    base_sha = marker.get("base_sha") if isinstance(marker.get("base_sha"), str) else ""
    missing = _unmet_requirements(repo_root, marker, git_timeout)
    if missing == []:
        logger.warning("Auto-update: the checkout runs %s without its Python environment %s/%s (%s), but .venv "
                       "satisfies every requirement it names (names and release versions; markers are not "
                       "evaluated); continuing on .venv.", target_sha[:9], environments.ENVIRONMENTS, name, reason)
        return None
    journal = os.path.join(repo_root, "data", UPDATE_JOURNAL)
    logger.error(
        "Auto-update: the checkout runs the update %s, which needs the Python environment %s/%s (.venv lacks %s), "
        "but %s. Startup stops until recovery: point .venv at %s/%s and rebuild the stores with its interpreter "
        "(python -m src.reindex), or restore the previous revision %s and the environment .venv pointed at; "
        "then remove %s.", target_sha[:9], environments.ENVIRONMENTS, name,
        ", ".join(missing[:5]) if missing else "packages it may need", reason, environments.ENVIRONMENTS, name,
        base_sha[:9] or "(unknown)", journal)
    try:
        _begin_update(repo_root, base_sha or target_sha, target_sha,
                      {"environment": plan or {"to": name, "previous": environments.state(repo_root)}})
    except FileExistsError:
        pass  # the journal of this activation stays
    return ActivationStatus.ACTIVATE_ROLLBACK_FAILED


def _restore_environment(repo_root, plan) -> bool:
    """Point ``.venv`` back at what *plan* replaced; True when that holds (or nothing was planned)."""
    if not plan:
        return True
    try:
        environments.restore(repo_root, plan)
    except (OSError, ValueError):
        logger.error("Auto-update: could not restore the previous Python environment.", exc_info=True)
        return False
    return True


def _prune_environments(repo_root, plan) -> None:
    """Best effort: keep the new and the previous environment, remove older ones."""
    previous = plan.get("previous") or {}
    try:
        removed = environments.prune(repo_root, keep=(plan.get("to"), previous.get("name"), previous.get("legacy")))
    except OSError:
        logger.warning("Auto-update: could not remove older Python environments; will retry later.", exc_info=True)
        return
    if removed:
        logger.info("Auto-update: removed older Python environments %s.", ", ".join(removed))


def _prepared_switch(marker, repo_root, branch, target_sha, git_timeout):
    """The switch a validated *marker* asks for, or None when the checkout is on *branch* already."""
    if not marker.get("switch_from"):
        return None
    cur = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root, git_timeout)
    if cur.returncode != 0 or cur.stdout.strip() == branch:
        return None
    return {"from": marker["switch_from"], "remote": marker.get("remote") or AUTO_UPDATE_REMOTE,
            "downgrade": bool(marker.get("downgrade")),
            "branch_tip": _branch_tip(repo_root, branch, target_sha, git_timeout) or "",
            "branch_config": _branch_config(repo_root, branch, git_timeout)}


def _rollback_activation(repo_root: str, old_sha: str, git_timeout: int, *, clear_journal=True,
                         old_branch: Optional[str] = None, branch: Optional[str] = None,
                         branch_tip: str = "", branch_config: Optional[dict] = None,
                         environment: Optional[dict] = None) -> bool:
    """Restore the pre-activation tree even if merge timed out after changing HEAD.

    After a branch switch (*old_branch* given) check *old_branch* out again first, then
    put the switched-to *branch* back where it was (*branch_tip*), with the upstream
    settings it had (*branch_config*, ``""`` for unset), or remove it when the switch
    created it. An *environment* switch (`src.environments.plan`) is undone as well,
    whether or not it happened.
    """
    environment_restored = _restore_environment(repo_root, environment)
    try:
        if old_branch:
            back = _run_git(["checkout", "--quiet", "--force", old_branch], repo_root, git_timeout)
            if back.returncode != 0:
                logger.error("Auto-update: could not check out '%s' again: %s", old_branch, back.stderr.strip())
                return False
        result = _run_git(["reset", "--hard", old_sha], repo_root, git_timeout)
        if result.returncode == 0 and old_branch and branch and branch != old_branch:
            if branch_tip:
                _run_git(["branch", "--force", branch, branch_tip], repo_root, git_timeout)
                for key, value in (branch_config or {}).items():
                    if key not in ("remote", "merge"):
                        continue
                    setting = f"branch.{branch}.{key}"
                    _run_git(["config", setting, value] if value else ["config", "--unset", setting],
                             repo_root, git_timeout)
            else:
                _run_git(["branch", "--delete", "--force", branch], repo_root, git_timeout)
    except (subprocess.TimeoutExpired, OSError):
        logger.error("Auto-update: activation rollback raised.", exc_info=True)
        return False
    if result.returncode != 0:
        logger.error("Auto-update: activation rollback failed: %s", result.stderr.strip())
        return False
    if not environment_restored:
        return False
    try:
        if clear_journal:
            _finish_update(repo_root)
    except OSError:
        logger.error("Auto-update: restored tree but could not clear recovery journal.", exc_info=True)
        return False
    return True


# --- Background orchestration -------------------------------------------------

def run_activation_safely(session_fd=None) -> None:
    """Retain the exclusive installation lease in activation's child processes."""
    with _inherit_lock(session_fd):
        _activate_at_startup()


def _activate_at_startup() -> None:
    """Phase A entry, called with startup's exclusive installation lease held.

    Called by ``startup.py`` under an exclusive installation lease (before the
    engine modules eagerly load the stores). Cheap on the common no-update path:
    honors the master switch, then a single lock-free ``stat`` — it only takes the
    lock and touches git when a prepared update (or a leftover staging dir) exists.
    """
    try:
        if not AUTO_UPDATE_ENABLED:
            return
        if not AUTO_UPDATE_STAGING:
            # Legacy in-place work also needs a quiet installation. Run it here,
            # never in a background thread holding a lifetime reader lease.
            status = _run_update_safely()
            if status == UpdateStatus.ROLLBACK_FAILED:
                raise SystemExit("Auto-update could not restore the installation; restart required.")
            if status == UpdateStatus.UPDATED:
                _reexec_updated_server()
            return
        # Lock-free fast path: nothing staged -> nothing to do (one stat each).
        if not os.path.exists(PREPARED_MARKER) and not os.path.exists(STAGING_ROOT):
            return
        with _process_lock(LOCK_FILE) as acquired:
            if not acquired:
                logger.debug("Auto-update: another process holds the lock; skipping activation.")
                return
            status = activate_prepared_update()
            if status == ActivationStatus.NO_MARKER:
                # We only took the lock because STAGING_ROOT had leftovers (a crashed
                # prepare with no marker) -> reap the orphan worktrees.
                _discard_staging(INSTALL_ROOT, AUTO_UPDATE_GIT_TIMEOUT)
            elif status == ActivationStatus.ACTIVATE_ROLLBACK_FAILED:
                raise SystemExit("Auto-update could not restore the installation; restart required.")
            elif status == ActivationStatus.ACTIVATED or environments.replaced(INSTALL_ROOT):
                # The live tree now holds the new version, but this process was
                # compiled from the old one (server.py / config / self_update are
                # already imported). Re-exec so the activation start serves the
                # new code end-to-end; stdio fds survive exec, and the marker is
                # gone, so the re-exec'd process takes the NO_MARKER fast path.
                # A resumed activation whose stores failed still switched .venv
                # under this process: it re-execs too.
                logger.info("Auto-update: re-exec into the updated code.")
                _reexec_updated_server()
            logger.debug("Auto-update: activation finished with status %s", status)
    except Exception as exc:
        # Expected failures return explicit statuses. An unexpected exception
        # gives us no guarantee that the live tree was left untouched.
        logger.error("Auto-update: startup activation crashed.", exc_info=True)
        raise SystemExit("Auto-update startup failed; restart required.") from exc


def _reexec_executable(repo_root: str = INSTALL_ROOT) -> str:
    """The interpreter a re-exec runs: ``.venv``'s when this process started on an environment of
    the installation (activation may have pointed ``.venv`` at another), else this process's."""
    candidate = environments.python_path(environments.link_path(repo_root))
    return candidate if environments.ours(repo_root) and os.path.exists(candidate) else sys.executable


def _reexec_updated_server() -> None:
    executable = _reexec_executable()
    try:
        _exec_server(executable, [executable, *sys.argv])
    except OSError as exc:
        # Falling through would mix already-imported old modules with new files.
        raise SystemExit("Auto-update re-exec failed; restart required.") from exc


def _exec_server(executable: str, argv: list) -> None:
    """Replace this process with *argv*: return never, raise OSError on failure."""
    if os.name == "nt":
        _respawn_server(argv)
    os.execv(executable, argv)


def _respawn_server(argv: list) -> None:
    """Windows has no exec: serve *argv* from a child, then exit with its code.

    ``os.execv`` there starts a new process and ends this one, which the MCP client
    started and waits on: the client would see its server exit. Instead, as exec
    does, release this process's leases (the child waits for them otherwise), then
    let *argv* take this process's place (`windows_job.replace_process`).
    """
    from src.startup import release_leases
    from src.windows_job import replace_process
    for fd in _held_lock_fds():  # the activation's updater lock and session lease
        _unlock(fd)
    release_leases()
    replace_process(argv)


def _run_update_safely() -> Optional[str]:
    """Lock + throttle + run the background update, swallowing every error.

    Dispatches to Phase B (:func:`prepare_update`) when staging is on, else the
    synchronous in-place :func:`check_and_apply_update` (the caller must hold the
    exclusive installation lease for that mode). When staging is on and a
    prepared update is already pending activation, it does NOT prepare again.
    """
    try:
        with _process_lock(LOCK_FILE) as acquired:
            if not acquired:
                logger.debug("Auto-update: another process holds the update lock; skipping.")
                return
            if AUTO_UPDATE_STAGING and _read_prepared_marker() is not None:
                logger.debug("Auto-update: a prepared update is pending activation; skipping prepare.")
                return
            if _recently_checked(AUTO_UPDATE_MIN_INTERVAL):
                logger.debug("Auto-update: checked within the throttle window; skipping.")
                return
            status = prepare_update() if AUTO_UPDATE_STAGING else check_and_apply_update()
            # Only throttle once we've actually hit the network, so a
            # wrong-branch / dirty skip never delays a later real check.
            if status not in _PRE_NETWORK_STATUSES:
                _touch_check_stamp()
            logger.debug("Auto-update: finished with status %s", status)
            return status
    except Exception:
        if not AUTO_UPDATE_STAGING:
            raise  # synchronous in-place caller must stop on unknown tree state
        logger.warning("Auto-update: background update crashed (ignored).", exc_info=True)


def start_background_update():
    """Spawn the daemon update thread, unless disabled. Returns it (or None).

    Non-blocking: returns immediately so ``mcp.run()`` can start serving.
    """
    if not AUTO_UPDATE_ENABLED:
        logger.debug("Auto-update: disabled (AGENTS_AUTO_UPDATE=0).")
        return None
    from src.startup import LEASES
    if not LEASES or not AUTO_UPDATE_STAGING:
        # Platforms without installation leases never auto-update. The legacy mode
        # runs synchronously at startup under the exclusive installation lease.
        return None
    thread = threading.Thread(target=_run_update_safely, name="auto-update", daemon=True)
    thread.start()
    return thread
