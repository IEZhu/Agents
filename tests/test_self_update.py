"""Tests for the background self-updater (src/self_update.py).

Fast by design: real temporary git repos (git is quick) but the reindex step is
injected as a recorder, so no embedding model is ever loaded. Not marked slow.
"""

import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import self_update
from src.self_update import UpdateStatus, check_and_apply_update

# These tests shell out to the real `git` CLI; skip cleanly where it's absent
# (minimal CI sandboxes, some Windows runners) instead of erroring the suite.
pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git CLI not available")


# --- git fixture helpers -----------------------------------------------------

def _git(cwd, *args, check=True):
    r = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed in {cwd}: {r.stderr}")
    return r


def _init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q")
    # Pin the initial branch to 'main' portably (no reliance on init.defaultBranch).
    _git(path, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(path, "config", "user.email", "test@example.com")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "commit.gpgsign", "false")
    return path


def _commit(path: Path, filename: str, content: str, msg: str):
    (path / filename).write_text(content)
    _git(path, "add", "-A")
    _git(path, "commit", "-q", "-m", msg)


def _head(path) -> str:
    return _git(path, "rev-parse", "HEAD").stdout.strip()


@pytest.fixture
def repos(tmp_path):
    """An 'upstream' repo on main with one commit, plus a 'local' clone of it."""
    upstream = _init_repo(tmp_path / "upstream")
    _commit(upstream, "file.txt", "v1\n", "init")
    local = tmp_path / "local"
    _git(tmp_path, "clone", "-q", str(upstream), str(local))
    _git(local, "config", "user.email", "test@example.com")
    _git(local, "config", "user.name", "Test")
    _git(local, "config", "commit.gpgsign", "false")
    return SimpleNamespace(upstream=upstream, local=local)


@pytest.fixture
def recorder():
    calls = []

    def fn(repo_root):
        calls.append(repo_root)
        return True

    fn.calls = calls
    return fn


# --- check_and_apply_update: the state machine -------------------------------

def test_up_to_date_no_reindex(repos, recorder):
    status = check_and_apply_update(str(repos.local), "origin", "main", reindex_fn=recorder)
    assert status == UpdateStatus.UP_TO_DATE
    assert recorder.calls == []


def test_fast_forward_applies_and_reindexes(repos, recorder):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    old = _head(repos.local)

    status = check_and_apply_update(str(repos.local), "origin", "main", reindex_fn=recorder)

    assert status == UpdateStatus.UPDATED
    new = _head(repos.local)
    assert new == _head(repos.upstream)  # fast-forwarded to upstream tip
    assert new != old
    assert recorder.calls == [str(repos.local)]  # reindex ran against the repo


def test_reindex_failure_rolls_back(repos):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    old = _head(repos.local)

    status = check_and_apply_update(
        str(repos.local), "origin", "main", reindex_fn=lambda rr: False
    )

    assert status == UpdateStatus.REINDEX_FAILED
    assert _head(repos.local) == old  # rolled back to the pre-merge commit


def test_reindex_failure_rollback_removes_added_file(repos):
    # The pulled update ADDS a new path. git reset --hard must remove it and
    # leave a clean tree (covers the added-file rollback case, not just edits).
    (repos.upstream / "newdir").mkdir()
    _commit(repos.upstream, "newdir/added.txt", "added\n", "add new file")
    old = _head(repos.local)

    status = check_and_apply_update(str(repos.local), "origin", "main", reindex_fn=lambda rr: False)

    assert status == UpdateStatus.REINDEX_FAILED
    assert _head(repos.local) == old
    assert not (repos.local / "newdir" / "added.txt").exists()
    assert _git(repos.local, "status", "--porcelain").stdout.strip() == ""


def test_reindex_exception_triggers_rollback(repos):
    # A reindex that *raises* (not just returns False) must still roll back.
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    old = _head(repos.local)

    def boom(repo_root):
        raise RuntimeError("reindex crashed")

    status = check_and_apply_update(str(repos.local), "origin", "main", reindex_fn=boom)

    assert status == UpdateStatus.REINDEX_FAILED
    assert _head(repos.local) == old


def test_bad_remote_or_branch_is_skipped(repos, recorder):
    # A remote/branch starting with '-' would be argument injection; refuse early.
    status = check_and_apply_update(str(repos.local), "-x", "main", reindex_fn=recorder)
    assert status == UpdateStatus.SKIPPED_BAD_CONFIG
    assert recorder.calls == []


def test_skip_on_wrong_branch(repos, recorder):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    _git(repos.local, "checkout", "-q", "-b", "feature")

    status = check_and_apply_update(str(repos.local), "origin", "main", reindex_fn=recorder)

    assert status == UpdateStatus.SKIPPED_WRONG_BRANCH
    assert recorder.calls == []


def test_skip_on_dirty_tree(repos, recorder):
    (repos.local / "file.txt").write_text("uncommitted change\n")

    status = check_and_apply_update(str(repos.local), "origin", "main", reindex_fn=recorder)

    assert status == UpdateStatus.SKIPPED_DIRTY
    assert recorder.calls == []


def test_skip_on_diverged(repos, recorder):
    _commit(repos.upstream, "file.txt", "remote-change\n", "remote")
    _commit(repos.local, "other.txt", "local-change\n", "local")

    status = check_and_apply_update(str(repos.local), "origin", "main", reindex_fn=recorder)

    assert status == UpdateStatus.SKIPPED_DIVERGED
    assert recorder.calls == []
    # local commit is preserved
    assert _head(repos.local) != _head(repos.upstream)


def test_skip_when_ahead_only(repos, recorder):
    # Local has a commit the remote lacks and the remote has nothing new
    # (ahead>0, behind==0): must skip, not report UP_TO_DATE, and not reindex.
    _commit(repos.local, "local-only.txt", "local\n", "local only")
    ahead_head = _head(repos.local)

    status = check_and_apply_update(str(repos.local), "origin", "main", reindex_fn=recorder)

    assert status == UpdateStatus.SKIPPED_AHEAD
    assert recorder.calls == []
    assert _head(repos.local) == ahead_head  # untouched


def test_fetch_failure_is_fail_open(repos, recorder):
    # Clean tree, correct branch, but a bogus remote -> fetch fails, no raise.
    status = check_and_apply_update(
        str(repos.local), "no-such-remote", "main", reindex_fn=recorder
    )
    assert status == UpdateStatus.FETCH_FAILED
    assert recorder.calls == []


def test_not_a_git_tree(tmp_path, recorder):
    plain = tmp_path / "plain"
    plain.mkdir()
    status = check_and_apply_update(str(plain), "origin", "main", reindex_fn=recorder)
    assert status == UpdateStatus.NO_GIT
    assert recorder.calls == []


# --- start_background_update / _run_update_safely: orchestration -------------

def test_disabled_spawns_no_thread(monkeypatch):
    monkeypatch.setattr(self_update, "AUTO_UPDATE_ENABLED", False)
    assert self_update.start_background_update() is None


def test_background_thread_runs_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(self_update, "AUTO_UPDATE_ENABLED", True)
    monkeypatch.setattr(self_update, "LOCK_FILE", str(tmp_path / ".update.lock"))
    monkeypatch.setattr(self_update, "CHECK_STAMP", str(tmp_path / ".check"))
    monkeypatch.setattr(self_update, "AUTO_UPDATE_MIN_INTERVAL", 0)
    calls = []
    monkeypatch.setattr(self_update, "check_and_apply_update",
                        lambda: calls.append(1) or UpdateStatus.UP_TO_DATE)

    thread = self_update.start_background_update()
    assert thread is not None
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert calls == [1]


def test_throttle_skips_check(tmp_path, monkeypatch):
    monkeypatch.setattr(self_update, "LOCK_FILE", str(tmp_path / ".update.lock"))
    monkeypatch.setattr(self_update, "CHECK_STAMP", str(tmp_path / ".check"))
    monkeypatch.setattr(self_update, "AUTO_UPDATE_MIN_INTERVAL", 9999)
    self_update._touch_check_stamp()  # fresh stamp -> within the window
    calls = []
    monkeypatch.setattr(self_update, "check_and_apply_update",
                        lambda: calls.append(1) or UpdateStatus.UP_TO_DATE)

    self_update._run_update_safely()
    assert calls == []  # throttled before any check


def test_lock_contention_skips_check(tmp_path, monkeypatch):
    fcntl = pytest.importorskip("fcntl")
    lock = str(tmp_path / ".update.lock")
    monkeypatch.setattr(self_update, "LOCK_FILE", lock)
    monkeypatch.setattr(self_update, "CHECK_STAMP", str(tmp_path / ".check"))
    monkeypatch.setattr(self_update, "AUTO_UPDATE_MIN_INTERVAL", 0)
    calls = []
    monkeypatch.setattr(self_update, "check_and_apply_update",
                        lambda: calls.append(1) or UpdateStatus.UP_TO_DATE)

    holder = open(lock, "w")
    fcntl.flock(holder.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        self_update._run_update_safely()
    finally:
        fcntl.flock(holder.fileno(), fcntl.LOCK_UN)
        holder.close()

    assert calls == []  # another holder -> skipped


def test_wrong_branch_does_not_write_throttle_stamp(repos, tmp_path, monkeypatch):
    # A pre-network skip must not stamp the throttle, so a later on-branch check
    # is never delayed.
    monkeypatch.setattr(self_update, "LOCK_FILE", str(tmp_path / ".update.lock"))
    stamp = tmp_path / ".check"
    monkeypatch.setattr(self_update, "CHECK_STAMP", str(stamp))
    monkeypatch.setattr(self_update, "AUTO_UPDATE_MIN_INTERVAL", 0)
    monkeypatch.setattr(self_update, "check_and_apply_update",
                        lambda: UpdateStatus.SKIPPED_WRONG_BRANCH)

    self_update._run_update_safely()
    assert not stamp.exists()


def test_network_reached_failure_writes_throttle_stamp(tmp_path, monkeypatch):
    # A post-network outcome (e.g. FETCH_FAILED) must write the throttle stamp.
    monkeypatch.setattr(self_update, "LOCK_FILE", str(tmp_path / ".update.lock"))
    stamp = tmp_path / ".check"
    monkeypatch.setattr(self_update, "CHECK_STAMP", str(stamp))
    monkeypatch.setattr(self_update, "AUTO_UPDATE_MIN_INTERVAL", 0)
    monkeypatch.setattr(self_update, "check_and_apply_update",
                        lambda: UpdateStatus.FETCH_FAILED)

    self_update._run_update_safely()
    assert stamp.exists()


# --- timeout / network-reached boundary --------------------------------------

def test_timeout_before_fetch_is_pre_network(repos, recorder, monkeypatch):
    # A timeout on a pre-fetch local op (status) is not a network failure: it
    # returns NO_GIT (pre-network), so the throttle stamp is not written later.
    real = self_update._run_git

    def fake(args, cwd, timeout):
        if args and args[0] == "status":
            raise subprocess.TimeoutExpired(cmd="git status", timeout=timeout)
        return real(args, cwd, timeout)

    monkeypatch.setattr(self_update, "_run_git", fake)
    status = check_and_apply_update(str(repos.local), "origin", "main", reindex_fn=recorder)
    assert status == UpdateStatus.NO_GIT
    assert recorder.calls == []


def test_timeout_after_fetch_is_network_failure(repos, recorder, monkeypatch):
    # A timeout after a successful fetch (here on rev-list) is a network-reached
    # failure, so it returns FETCH_FAILED.
    real = self_update._run_git

    def fake(args, cwd, timeout):
        if args and args[0] == "rev-list":
            raise subprocess.TimeoutExpired(cmd="git rev-list", timeout=timeout)
        return real(args, cwd, timeout)

    monkeypatch.setattr(self_update, "_run_git", fake)
    status = check_and_apply_update(str(repos.local), "origin", "main", reindex_fn=recorder)
    assert status == UpdateStatus.FETCH_FAILED
    assert recorder.calls == []


def test_exception_after_merge_rolls_back(repos, monkeypatch):
    # If a git op raises AFTER the fast-forward, the tree must be rolled back.
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    old = _head(repos.local)
    real = self_update._run_git
    head_calls = {"n": 0}

    def fake(args, cwd, timeout):
        # Blow up only on the post-merge `rev-parse HEAD` (the 2nd bare one;
        # the 1st is the pre-merge old_sha lookup).
        if args[:2] == ["rev-parse", "HEAD"]:
            head_calls["n"] += 1
            if head_calls["n"] >= 2:
                raise subprocess.TimeoutExpired(cmd="git rev-parse HEAD", timeout=timeout)
        return real(args, cwd, timeout)

    monkeypatch.setattr(self_update, "_run_git", fake)
    status = check_and_apply_update(str(repos.local), "origin", "main", reindex_fn=lambda rr: True)

    assert status == UpdateStatus.FETCH_FAILED  # network was reached
    assert _head(repos.local) == old  # rolled back despite the post-merge failure


# --- state file round-trip ---------------------------------------------------

def test_state_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(self_update, "STATE_FILE", str(tmp_path / ".last_update.json"))
    self_update._write_state(UpdateStatus.UPDATED, "a" * 40, "b" * 40)
    assert os.path.exists(self_update.STATE_FILE)
    # Must not raise on a present, well-formed file.
    self_update.log_last_update()


def test_log_last_update_missing_file_is_silent(tmp_path, monkeypatch):
    monkeypatch.setattr(self_update, "STATE_FILE", str(tmp_path / "does-not-exist.json"))
    self_update.log_last_update()  # no exception
