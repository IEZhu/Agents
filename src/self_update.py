"""Background self-update for the Agents-Core MCP server.

Two-phase, "prepare in the background, activate by move on the next start"
(the default; ``AGENTS_AUTO_UPDATE_STAGING=1``):

  * **Phase B — prepare** (:func:`prepare_update`, daemon thread via
    :func:`start_background_update`, non-blocking): take a non-blocking
    cross-process lock, throttle redundant network checks, ``git fetch``, and — if
    the install is fast-forwardable and no update is already pending — build the
    new version's vector stores in an **isolated git worktree** under
    ``AUTO_UPDATE_STAGING_DIR`` and write a marker. The live install is never
    touched.
  * **Phase A — activate** (:func:`activate_prepared_update`, via
    :func:`run_activation_safely` at the very top of ``server.py``'s ``__main__``,
    before the engine modules eagerly load the stores): if a valid prepared marker
    exists, fast-forward the live tree to the prepared sha (local, no network) and
    atomically move the pre-built stores into ``data/``. The expensive embedding
    already happened in Phase B, so this is just a merge + a few ``os.replace``.

Set ``AGENTS_AUTO_UPDATE_STAGING=0`` to fall back to the legacy in-place path
(:func:`check_and_apply_update`): fast-forward + reindex on the live tree, rolled
back via ``git reset --hard`` if the reindex fails.

Either way the new code takes effect on the **next** start (for per-session stdio
servers, the next spawn) — the running process keeps its already-imported code.

Safety invariants (both paths):
    * acts **only** when the checked-out branch is the target branch
      (``AUTO_UPDATE_BRANCH``) — a no-op on feature branches / local dev,
    * only when the working tree is clean, and only **fast-forward** (never
      merge / rebase / auto-checkout),
    * staging never mutates the live install; a failed prepare writes no marker,
      the legacy path rolls back a failed reindex, and a failed activation move
      rolls back the just-merged tree,
    * crash windows self-heal: the store's torn-pair detection plus the
      content-hash re-embed mean a half-applied move is repaired on load,
    * every failure is logged and swallowed — the server is never crashed or
      blocked by the updater.
"""

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from typing import Optional

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
# treated as invalid and discarded by the Phase A validator.
PREPARED_MARKER_SCHEMA = 1

# Vector stores that a prepared update stages and activates, paired with their
# content-hash sidecar file. The router cache is intentionally NOT here — it is a
# per-deployment runtime query cache, not derived from the .mdc sources, and it
# self-invalidates on model change.
_STAGED_STORES = (
    ("skills_store", ".skills_hash"),
    ("implants_store", ".implants_hash"),
)

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
    MERGE_FAILED = "MERGE_FAILED"
    REINDEX_FAILED = "REINDEX_FAILED"
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
    PREPARED = "PREPARED"
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
    INVALID_STAGING_MISSING = "INVALID_STAGING_MISSING"
    INVALID_STAGING_INCONSISTENT = "INVALID_STAGING_INCONSISTENT"
    INVALID_CROSS_DEVICE = "INVALID_CROSS_DEVICE"
    ACTIVATE_MERGE_FAILED = "ACTIVATE_MERGE_FAILED"
    ACTIVATE_MOVE_FAILED = "ACTIVATE_MOVE_FAILED"


# --- Cross-process lock (non-blocking) ---------------------------------------
# fcntl.flock on POSIX; no-op on Windows where stdio MCP doesn't run concurrent
# sessions. Mirrors the helper in src/memory/history.py but uses LOCK_NB so a
# held lock means "another server is already updating" -> skip immediately.
try:  # pragma: no cover - platform-specific
    import fcntl as _fcntl

    def _try_lock(fh) -> bool:
        """Acquire an exclusive non-blocking lock; return False if already held."""
        try:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_EX | _fcntl.LOCK_NB)
            return True
        except OSError:
            return False

    def _unlock(fh) -> None:
        """Release the lock held on *fh* (best effort)."""
        try:
            _fcntl.flock(fh.fileno(), _fcntl.LOCK_UN)
        except OSError:
            pass

except ImportError:  # pragma: no cover - Windows
    def _try_lock(fh) -> bool:
        """No-op lock on platforms without fcntl; always reports success."""
        return True

    def _unlock(fh) -> None:
        """No-op unlock on platforms without fcntl."""
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
    """Atomically persist the latest update outcome to ``STATE_FILE``."""
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

def _write_prepared_marker(target_sha, base_sha, branch, embedding_model, staging_dir, stores) -> None:
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
    """Validate + fetch + measure how far *repo_root* is behind ``<remote>/<branch>``.

    Shared, mutation-free prefix of the legacy in-place path
    (:func:`check_and_apply_update`) and Phase B (:func:`prepare_update`). Handles
    its own git errors and returns a tuple ``(status, old_sha, target_sha)``:

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
        return UpdateStatus.SKIPPED_BAD_CONFIG, "", None

    # Step 1 — git available and repo_root is a work tree.
    try:
        rev = _run_git(["rev-parse", "--is-inside-work-tree"], repo_root, git_timeout)
    except FileNotFoundError:
        logger.info("Auto-update: git not found; skipping.")
        return UpdateStatus.NO_GIT, "", None
    except subprocess.TimeoutExpired:
        logger.warning("Auto-update: git timed out; skipping.")
        return UpdateStatus.NO_GIT, "", None
    if rev.returncode != 0 or rev.stdout.strip() != "true":
        logger.info("Auto-update: %s is not a git work tree; skipping.", repo_root)
        return UpdateStatus.NO_GIT, "", None

    network_reached = False
    old_sha = ""
    try:
        # Step 2 — branch guard: only act on the target branch.
        cur = _run_git(["rev-parse", "--abbrev-ref", "HEAD"], repo_root, git_timeout)
        current_branch = cur.stdout.strip() if cur.returncode == 0 else ""
        if current_branch != branch:
            logger.info(
                "Auto-update: on '%s', target is '%s'; skipping (local development).",
                current_branch or "?", branch,
            )
            return UpdateStatus.SKIPPED_WRONG_BRANCH, "", None

        # Step 3 — clean working tree (tracked files only; an incoming file that
        # collides with an untracked one makes the ff-merge fail loudly later).
        status = _run_git(["status", "--porcelain", "--untracked-files=no"], repo_root, git_timeout)
        if status.returncode != 0:
            logger.warning("Auto-update: git status failed; skipping. %s", status.stderr.strip())
            return UpdateStatus.SKIPPED_DIRTY, "", None
        if status.stdout.strip():
            logger.info("Auto-update: working tree has uncommitted changes; skipping.")
            return UpdateStatus.SKIPPED_DIRTY, "", None

        head = _run_git(["rev-parse", "HEAD"], repo_root, git_timeout)
        old_sha = head.stdout.strip() if head.returncode == 0 else ""
        if not old_sha:
            # Without a baseline commit we cannot roll back, so refuse to mutate.
            logger.warning("Auto-update: could not resolve current HEAD; skipping.")
            return UpdateStatus.NO_GIT, "", None

        # Step 4 — fetch the target branch.
        try:
            network_reached = True
            fetch = _run_git(["fetch", remote, branch], repo_root, git_timeout)
        except subprocess.TimeoutExpired:
            logger.warning("Auto-update: git fetch timed out after %ds; serving current code.", git_timeout)
            return UpdateStatus.FETCH_FAILED, old_sha, None
        if fetch.returncode != 0:
            logger.warning("Auto-update: git fetch failed; serving current code. %s", fetch.stderr.strip())
            return UpdateStatus.FETCH_FAILED, old_sha, None

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
            return UpdateStatus.FETCH_FAILED, old_sha, None
        try:
            behind_str, ahead_str = counts.stdout.split()
            behind, ahead = int(behind_str), int(ahead_str)
        except ValueError:
            logger.warning("Auto-update: unexpected rev-list output %r; skipping.", counts.stdout)
            return UpdateStatus.FETCH_FAILED, old_sha, None
        if ahead > 0:
            # Local has commits the remote lacks: diverged (when also behind) or
            # purely ahead. Either way, never fast-forward over local work.
            outcome = UpdateStatus.SKIPPED_DIVERGED if behind > 0 else UpdateStatus.SKIPPED_AHEAD
            logger.info(
                "Auto-update: local branch has unpushed commits vs %s (ahead %d, behind %d); skipping.",
                remote_ref, ahead, behind,
            )
            return outcome, old_sha, None
        if behind == 0:
            logger.info("Auto-update: already up to date with %s.", remote_ref)
            return UpdateStatus.UP_TO_DATE, old_sha, None

        # Fast-forwardable — resolve the concrete target commit (both callers want
        # the explicit sha: the legacy path merges it, Phase B stages + records it).
        tgt = _run_git(["rev-parse", "FETCH_HEAD"], repo_root, git_timeout)
        target_sha = tgt.stdout.strip() if tgt.returncode == 0 else ""
        if not target_sha:
            logger.warning("Auto-update: could not resolve FETCH_HEAD; skipping.")
            return UpdateStatus.FETCH_FAILED, old_sha, None
        return None, old_sha, target_sha
    except (subprocess.TimeoutExpired, FileNotFoundError):
        # Pre-mutation, so no rollback is needed here. A post-fetch failure is
        # network-reached (FETCH_FAILED, which throttles); a pre-fetch local-op
        # failure stays pre-network (NO_GIT) so it never suppresses a later check.
        logger.warning("Auto-update: a git operation failed; serving current code.")
        return (UpdateStatus.FETCH_FAILED if network_reached else UpdateStatus.NO_GIT), old_sha, None


def check_and_apply_update(
    repo_root: str = INSTALL_ROOT,
    remote: str = AUTO_UPDATE_REMOTE,
    branch: str = AUTO_UPDATE_BRANCH,
    *,
    git_timeout: int = AUTO_UPDATE_GIT_TIMEOUT,
    reindex_timeout: int = AUTO_UPDATE_REINDEX_TIMEOUT,
    reindex_fn=None,
) -> str:
    """Legacy in-place path: fast-forward *repo_root* and rebuild stores in place.

    Used when ``AGENTS_AUTO_UPDATE_STAGING=0``. Pure of threading/locking so it can
    be unit-tested directly. ``reindex_fn`` (``repo_root -> bool``) is injected in
    tests; defaults to spawning ``python -m src.reindex``. Returns an
    :class:`UpdateStatus` value.
    """
    reindex = reindex_fn or (lambda rr: _run_reindex(rr, reindex_timeout))

    status, old_sha, target_sha = _resolve_ff_target(repo_root, remote, branch, git_timeout)
    if target_sha is None:
        return status  # terminal: skip / up-to-date / pre-merge failure

    merged = False  # tracks whether the ff-merge already mutated the tree
    try:
        # Step 6 — fast-forward only.
        merge = _run_git(["merge", "--ff-only", target_sha], repo_root, git_timeout)
        if merge.returncode != 0:
            logger.warning("Auto-update: fast-forward merge failed; skipping. %s", merge.stderr.strip())
            return UpdateStatus.MERGE_FAILED
        merged = True
        new = _run_git(["rev-parse", "HEAD"], repo_root, git_timeout)
        new_sha = new.stdout.strip() if new.returncode == 0 else ""
        logger.info("Auto-update: fast-forwarded %s -> %s on %s.", old_sha[:9], new_sha[:9], branch)

        _warn_if_deps_changed(repo_root, old_sha, new_sha, git_timeout)

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
    except (subprocess.TimeoutExpired, FileNotFoundError):
        # A git op should not time out (or git vanish) under the budget. The merge
        # has already put us past the network, so this is always FETCH_FAILED. If
        # the fast-forward already mutated the tree, roll back so we never strand
        # the install on new code without rebuilt indexes.
        if merged and old_sha:
            logger.error("Auto-update: exception after fast-forward; rolling back to %s.", old_sha[:9])
            try:
                rollback = _run_git(["reset", "--hard", old_sha], repo_root, git_timeout)
                if rollback.returncode != 0:
                    logger.error("Auto-update: rollback after exception failed. %s", rollback.stderr.strip())
            except Exception:
                logger.error("Auto-update: rollback after exception raised.", exc_info=True)
        logger.warning("Auto-update: a git operation failed; serving current code.")
        return UpdateStatus.FETCH_FAILED


# --- Phase B: prepare a staged update (git worktree) -------------------------

def _staging_worktrees(repo_root: str, staging_parent: str, git_timeout: int):
    """Return the registered git worktrees that are OUR staging checkouts.

    Parses ``git worktree list --porcelain``. Used to reap **any** staging
    worktree (of any sha) — ``git worktree prune`` alone won't remove one whose
    directory still exists, so a crashed prepare would otherwise leak it forever.

    Callers ``rmtree`` every returned path, and the porcelain list always
    includes the **main worktree** (the live install), so this filter is
    defense-in-depth against a misconfigured ``AGENTS_AUTO_UPDATE_STAGING_DIR``
    (the repo root, or one of its ancestors): a path qualifies only if it is
    strictly UNDER *staging_parent*, is not *repo_root* itself, and its basename
    is a full sha — the exact shape ``prepare_update`` creates. The live install
    and unrelated user worktrees are never returned.
    """
    try:
        r = _run_git(["worktree", "list", "--porcelain"], repo_root, git_timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    if r.returncode != 0:
        return []
    parent = os.path.abspath(staging_parent)
    root = os.path.abspath(repo_root)
    out = []
    for line in r.stdout.splitlines():
        if line.startswith("worktree "):
            path = line[len("worktree "):].strip()
            ap = os.path.abspath(path)
            if (
                ap.startswith(parent + os.sep)
                and ap != root
                and _FULL_SHA_RE.match(os.path.basename(ap))
            ):
                # Return the validated absolute path, not git's raw string, so
                # the callers' remove/rmtree act on exactly what was validated.
                out.append(ap)
    return out


def _prune_staging_worktrees(repo_root: str, staging_parent: str, git_timeout: int) -> None:
    """Remove every staging worktree under *staging_parent* + prune admin records.

    Best-effort: ``git worktree remove --force`` each, ``rmtree`` any leftover
    dir, then ``git worktree prune``. Bounds orphan accumulation regardless of how
    a previous prepare/activation died.
    """
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
    _prune_staging_worktrees(repo_root, staging_parent, git_timeout)
    try:
        os.makedirs(staging_parent, exist_ok=True)
    except OSError as e:
        logger.warning("Auto-update: could not create staging parent %s: %s", staging_parent, e)
        return None
    staging_dir = os.path.join(staging_parent, target_sha)
    if os.path.exists(staging_dir):
        shutil.rmtree(staging_dir, ignore_errors=True)
    try:
        r = _run_git(["worktree", "add", "--detach", staging_dir, target_sha], repo_root, git_timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        logger.warning("Auto-update: 'git worktree add' error: %s", e)
        return None
    if r.returncode != 0:
        logger.warning("Auto-update: 'git worktree add' failed: %s", r.stderr.strip())
        return None
    return staging_dir


def _run_reindex_at(staging_dir: str, timeout: int) -> bool:
    """Build the vector stores INSIDE *staging_dir* by running the worktree's code.

    ``python -m src.reindex`` with ``cwd=staging_dir`` makes the worktree its own
    import root (``-m`` puts cwd on ``sys.path``), so ``config.INSTALL_DATA_DIR``
    resolves to ``<staging_dir>/data`` and the stores never touch the live install
    (verified). Uses the **live** interpreter (``sys.executable``) because the
    worktree has no ``.venv``; site-packages come from the interpreter prefix
    regardless of cwd. ``EMBEDDING_MODEL`` is pinned explicitly so the staged
    vectors match the live process even though the worktree has no ``.env``.
    """
    env = dict(os.environ)
    env["EMBEDDING_MODEL"] = EMBEDDING_MODEL
    try:
        result = subprocess.run(
            [sys.executable, "-m", "src.reindex"],
            cwd=staging_dir,
            capture_output=True,
            text=True,
            check=False,
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
    for name, hash_file in _STAGED_STORES:
        if name not in stores:
            continue
        if not os.path.exists(os.path.join(data_dir, hash_file)):
            logger.warning("Auto-update: staged store %s is missing its hash file.", name)
            return False
        npz = os.path.join(data_dir, f"{name}.npz")
        meta = os.path.join(data_dir, f"{name}.json")
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
        if "save_version" not in payload:
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
) -> str:
    """Phase B: prepare a staged update WITHOUT mutating the live install.

    Fetches the target, and if *repo_root* is fast-forwardable, builds the new
    version's indexes in an isolated git worktree and writes the prepared marker
    (last) so the next start activates it. Never touches the live tree or stores.
    Pure of threading/locking. ``reindex_fn`` (``staging_dir -> bool``) is injected
    in tests; defaults to spawning ``python -m src.reindex`` in the worktree.
    Returns a :class:`PreparedStatus` value.
    """
    staging_parent = staging_parent or STAGING_ROOT
    reindex = reindex_fn or (lambda sd: _run_reindex_at(sd, reindex_timeout))

    status, old_sha, target_sha = _resolve_ff_target(repo_root, remote, branch, git_timeout)
    if target_sha is None:
        return status  # terminal: skip / up-to-date / pre-merge failure

    _warn_if_deps_changed(repo_root, old_sha, target_sha, git_timeout)

    stores = [name for name, _ in _STAGED_STORES]

    staging_dir = _add_worktree(staging_parent, target_sha, repo_root, git_timeout)
    if staging_dir is None:
        return PreparedStatus.PREPARE_WORKTREE_FAILED

    try:
        try:
            built = bool(reindex(staging_dir))
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
            embedding_model=EMBEDDING_MODEL,
            staging_dir=staging_dir,
            stores=stores,
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

def _silent_unlink(path: str) -> None:
    """Remove *path* if present; ignore a missing file or any OS error."""
    try:
        os.remove(path)
    except OSError:
        pass


def _same_filesystem(path_a: str, path_b: str) -> bool:
    """True if both paths live on the same device (``st_dev``); False on any stat error."""
    try:
        return os.stat(path_a).st_dev == os.stat(path_b).st_dev
    except OSError:
        return False


def _discard_staging(repo_root: str, git_timeout: int) -> None:
    """Drop a prepared update: remove the marker FIRST, then reap staging worktrees.

    Marker-first so a crash mid-cleanup can never re-activate a half-removed
    staging set on the next start.
    """
    _silent_unlink(PREPARED_MARKER)
    _prune_staging_worktrees(repo_root, STAGING_ROOT, git_timeout)


def _validate_prepared(marker, repo_root, branch, embedding_model, git_timeout):
    """Run the activation gates. Returns ``(invalid_status_or_None, already_at_target)``.

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
    stores = marker.get("stores")
    if (
        marker.get("schema") != PREPARED_MARKER_SCHEMA
        or not isinstance(target_sha, str)
        or not _FULL_SHA_RE.match(target_sha)
        or not isinstance(stores, list)
        or not stores
        or not all(isinstance(s, str) and s in known_stores for s in stores)
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
        if current_branch != branch:
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
        if head_sha == target_sha:
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

    return None, already_at_target


def _activate_staged_stores(staging_dir: str, repo_root: str, stores) -> None:
    """Move the staged store set into the live ``<repo_root>/data`` atomically per file.

    Ordering per store is ``unlink(live .hash) -> replace(.npz) -> replace(.json)
    -> put(.hash)``. Deleting the live hash first means any crash mid-move leaves
    *no* hash (forcing a clean re-embed on load) rather than a stale hash masking a
    half-moved store. Each ``os.replace`` is atomic (same filesystem). Raises
    ``OSError`` on a failed move so the caller can fail to ACTIVATE_MOVE_FAILED.
    """
    src_data = os.path.join(staging_dir, "data")
    dst_data = os.path.join(repo_root, "data")
    os.makedirs(dst_data, exist_ok=True)
    for name, hash_file in _STAGED_STORES:
        if name not in stores:
            continue
        src_npz = os.path.join(src_data, f"{name}.npz")
        src_json = os.path.join(src_data, f"{name}.json")
        src_hash = os.path.join(src_data, hash_file)
        dst_npz = os.path.join(dst_data, f"{name}.npz")
        dst_json = os.path.join(dst_data, f"{name}.json")
        dst_hash = os.path.join(dst_data, hash_file)

        _silent_unlink(dst_hash)
        if os.path.exists(src_npz) and os.path.exists(src_json):
            os.replace(src_npz, dst_npz)
            os.replace(src_json, dst_json)
        else:
            # Staged store is valid-empty (save() removed both files): clear the
            # live store too so the moved hash matches an empty store.
            _silent_unlink(dst_npz)
            _silent_unlink(dst_json)
        os.replace(src_hash, dst_hash)


def activate_prepared_update(
    repo_root: str = INSTALL_ROOT,
    branch: str = AUTO_UPDATE_BRANCH,
    *,
    git_timeout: int = AUTO_UPDATE_GIT_TIMEOUT,
    embedding_model: str = EMBEDDING_MODEL,
) -> str:
    """Phase A: activate a prepared update via a fast local ff-merge + file move.

    Runs at startup BEFORE the engine modules load the stores. The common case is
    no marker (a single stat). When a valid marker exists: ff-merge the live tree
    to the prepared sha (no network) and move the pre-built stores in. Any gate
    failure discards the staging and keeps serving the old code; a move failure
    additionally rolls the just-merged tree back to the pre-merge commit (best
    effort), so a failed activation never leaves new code without its stores.
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
        marker, repo_root, branch, embedding_model, git_timeout
    )
    if invalid is not None:
        _discard_staging(repo_root, git_timeout)
        return invalid

    target_sha = marker["target_sha"]
    old_sha = marker.get("base_sha", "")
    staging_dir = os.path.join(STAGING_ROOT, target_sha)
    stores = marker.get("stores", [])

    # Commit point: the ff-merge. Skipped when already at the target (a prior run
    # merged then died before the move) — we only need to finish the move.
    merged_now = False
    pre_merge_sha = ""
    if not already_at_target:
        try:
            head = _run_git(["rev-parse", "HEAD"], repo_root, git_timeout)
            pre_merge_sha = head.stdout.strip() if head.returncode == 0 else ""
            if not pre_merge_sha:
                # Without a rollback point we refuse to merge at all.
                logger.error("Auto-update: cannot resolve pre-merge HEAD; discarding.")
                _discard_staging(repo_root, git_timeout)
                return ActivationStatus.ACTIVATE_MERGE_FAILED
            merge = _run_git(["merge", "--ff-only", target_sha], repo_root, git_timeout)
        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
            logger.error("Auto-update: activation merge error: %s; discarding.", e)
            _discard_staging(repo_root, git_timeout)
            return ActivationStatus.ACTIVATE_MERGE_FAILED
        if merge.returncode != 0:
            logger.error("Auto-update: activation ff-merge failed; discarding. %s", merge.stderr.strip())
            _discard_staging(repo_root, git_timeout)
            return ActivationStatus.ACTIVATE_MERGE_FAILED
        merged_now = True

    try:
        _activate_staged_stores(staging_dir, repo_root, stores)
    except OSError as e:
        logger.error(
            "Auto-update: activation move failed (%s); discarding. Stores re-embed on load.", e,
        )
        if merged_now:
            # This run performed the merge: restore the pre-merge tree so the
            # process keeps serving the old code (no mixed-version runtime and
            # no new code without its stores). In the already_at_target resume
            # case the merge was a prior run's fait accompli — completing or
            # discarding is all that can be done there.
            try:
                rollback = _run_git(["reset", "--hard", pre_merge_sha], repo_root, git_timeout)
                if rollback.returncode != 0:
                    logger.error(
                        "Auto-update: rollback after failed move FAILED — tree is on new code. %s",
                        rollback.stderr.strip(),
                    )
            except (subprocess.TimeoutExpired, FileNotFoundError):
                logger.error("Auto-update: rollback after failed move raised.", exc_info=True)
        _discard_staging(repo_root, git_timeout)
        return ActivationStatus.ACTIVATE_MOVE_FAILED

    _discard_staging(repo_root, git_timeout)
    _write_state(ActivationStatus.ACTIVATED, old_sha, target_sha)
    logger.info("Auto-update: activated prepared update %s -> %s.", (old_sha or "")[:9], target_sha[:9])
    return ActivationStatus.ACTIVATED


# --- Background orchestration -------------------------------------------------

def run_activation_safely() -> None:
    """Phase A entry: activate a prepared update at startup, swallowing every error.

    Called as the very first thing in ``server.py``'s ``__main__`` (before the
    engine modules eagerly load the stores). Cheap on the common no-update path:
    honors the master switch, then a single lock-free ``stat`` — it only takes the
    lock and touches git when a prepared update (or a leftover staging dir) exists.
    """
    try:
        if not (AUTO_UPDATE_ENABLED and AUTO_UPDATE_STAGING):
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
                _prune_staging_worktrees(INSTALL_ROOT, STAGING_ROOT, AUTO_UPDATE_GIT_TIMEOUT)
            elif status == ActivationStatus.ACTIVATED:
                # The live tree now holds the new version, but this process was
                # compiled from the old one (server.py / config / self_update are
                # already imported). Re-exec so the activation start serves the
                # new code end-to-end; stdio fds survive exec, and the marker is
                # gone, so the re-exec'd process takes the NO_MARKER fast path.
                logger.info("Auto-update: re-exec into the updated code.")
                try:
                    os.execv(sys.executable, [sys.executable, *sys.argv])
                except OSError:
                    logger.warning("Auto-update: re-exec failed; new code applies on the next start.")
            logger.debug("Auto-update: activation finished with status %s", status)
    except Exception:
        logger.warning("Auto-update: startup activation crashed (ignored).", exc_info=True)


def _run_update_safely() -> None:
    """Lock + throttle + run the background update, swallowing every error.

    Dispatches to Phase B (:func:`prepare_update`) when staging is on, else the
    legacy in-place :func:`check_and_apply_update`. When staging is on and a
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
