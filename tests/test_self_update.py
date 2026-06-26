"""Tests for the background self-updater (src/self_update.py).

Fast by design: real temporary git repos (git is quick) but the reindex step is
injected as a recorder, so no embedding model is ever loaded. Not marked slow.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from src import self_update
from src.self_update import UpdateStatus, check_and_apply_update
from src.self_update import PreparedStatus, ActivationStatus

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


# --- staged-store helpers (Phase A/B) ----------------------------------------

# (store_name, hash_file) — mirrors self_update._STAGED_STORES.
_FAKE_STORES = [("skills_store", ".skills_hash"), ("implants_store", ".implants_hash")]


def _write_fake_store_set(data_dir: Path):
    """Write plausible (but tiny, numpy-free) store files into *data_dir*.

    Mirrors what a real reindex produces: a `<name>.npz`, a `<name>.json` with a
    `save_version`, and the `.<name>_hash` marker — enough to exercise the move
    and validation paths without loading the embedding model.
    """
    data_dir.mkdir(parents=True, exist_ok=True)
    for i, (name, hash_file) in enumerate(_FAKE_STORES):
        (data_dir / f"{name}.npz").write_bytes(b"fake-npz-" + name.encode())
        (data_dir / f"{name}.json").write_text(
            json.dumps({"save_version": f"ver-{i}", "ids": [], "documents": [], "metadatas": []})
        )
        (data_dir / hash_file).write_text(f"hash-{i}")


@pytest.fixture
def staging(tmp_path):
    """A staging-parent dir + a fake reindex builder that writes store files into
    <staging>/<sha>/data/ (no embedding model loaded)."""
    parent = tmp_path / "staging"

    def builder(staging_dir):
        _write_fake_store_set(Path(staging_dir) / "data")
        return True

    return SimpleNamespace(parent=str(parent), builder=builder)


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
    monkeypatch.setattr(self_update, "AUTO_UPDATE_STAGING", False)  # exercise the legacy path
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
    monkeypatch.setattr(self_update, "AUTO_UPDATE_STAGING", False)
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
    monkeypatch.setattr(self_update, "AUTO_UPDATE_STAGING", False)
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
    monkeypatch.setattr(self_update, "AUTO_UPDATE_STAGING", False)
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
    monkeypatch.setattr(self_update, "AUTO_UPDATE_STAGING", False)
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


# --- prepared-update marker I/O (Phase A/B) ----------------------------------

def test_prepared_marker_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(self_update, "PREPARED_MARKER", str(tmp_path / ".prepared_update.json"))
    self_update._write_prepared_marker(
        target_sha="a" * 40,
        base_sha="b" * 40,
        branch="main",
        embedding_model="some-model",
        staging_dir=str(tmp_path / ".prepared" / ("a" * 40)),
        stores=["skills_store", "implants_store"],
    )
    m = self_update._read_prepared_marker()
    assert m is not None
    assert m["schema"] == 1
    assert m["target_sha"] == "a" * 40
    assert m["base_sha"] == "b" * 40
    assert m["branch"] == "main"
    assert m["embedding_model"] == "some-model"
    assert m["stores"] == ["skills_store", "implants_store"]
    assert "built_at" in m


def test_read_prepared_marker_missing_is_none(tmp_path, monkeypatch):
    monkeypatch.setattr(self_update, "PREPARED_MARKER", str(tmp_path / "nope.json"))
    assert self_update._read_prepared_marker() is None


def test_read_prepared_marker_garbage_is_none(tmp_path, monkeypatch):
    p = tmp_path / ".prepared_update.json"
    p.write_text("{not valid json")
    monkeypatch.setattr(self_update, "PREPARED_MARKER", str(p))
    assert self_update._read_prepared_marker() is None


# --- Phase B: prepare_update -------------------------------------------------

def test_prepare_happy_path_writes_marker_and_leaves_live_untouched(repos, staging, tmp_path, monkeypatch):
    monkeypatch.setattr(self_update, "PREPARED_MARKER", str(tmp_path / ".prepared_update.json"))
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    old = _head(repos.local)

    status = self_update.prepare_update(
        str(repos.local), "origin", "main",
        reindex_fn=staging.builder, staging_parent=staging.parent,
    )

    assert status == PreparedStatus.PREPARED
    assert _head(repos.local) == old  # live tree NOT mutated by prepare
    m = self_update._read_prepared_marker()
    assert m is not None
    assert m["target_sha"] == _head(repos.upstream)
    assert m["base_sha"] == old
    assert m["embedding_model"] == self_update.EMBEDDING_MODEL
    assert m["stores"] == ["skills_store", "implants_store"]
    # staged stores live in the worktree, not the live install
    wt_data = Path(staging.parent) / m["target_sha"] / "data"
    assert (wt_data / "skills_store.npz").exists()
    assert (wt_data / ".skills_hash").exists()


def test_prepare_up_to_date_no_worktree(repos, staging, tmp_path, monkeypatch):
    monkeypatch.setattr(self_update, "PREPARED_MARKER", str(tmp_path / ".prepared_update.json"))
    status = self_update.prepare_update(
        str(repos.local), "origin", "main",
        reindex_fn=staging.builder, staging_parent=staging.parent,
    )
    assert status == PreparedStatus.UP_TO_DATE
    assert self_update._read_prepared_marker() is None
    assert not Path(staging.parent).exists() or not any(Path(staging.parent).iterdir())


def test_prepare_diverged_skips(repos, staging, tmp_path, monkeypatch):
    monkeypatch.setattr(self_update, "PREPARED_MARKER", str(tmp_path / ".prepared_update.json"))
    _commit(repos.upstream, "file.txt", "remote\n", "remote")
    _commit(repos.local, "other.txt", "local\n", "local")

    status = self_update.prepare_update(
        str(repos.local), "origin", "main",
        reindex_fn=staging.builder, staging_parent=staging.parent,
    )
    assert status == PreparedStatus.SKIPPED_DIVERGED
    assert self_update._read_prepared_marker() is None


def test_prepare_reindex_failure_aborts_clean(repos, tmp_path, monkeypatch):
    monkeypatch.setattr(self_update, "PREPARED_MARKER", str(tmp_path / ".prepared_update.json"))
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    old = _head(repos.local)
    parent = str(tmp_path / "staging")

    status = self_update.prepare_update(
        str(repos.local), "origin", "main",
        reindex_fn=lambda sd: False, staging_parent=parent,
    )

    assert status == PreparedStatus.PREPARE_REINDEX_FAILED
    assert self_update._read_prepared_marker() is None
    assert _head(repos.local) == old
    # no lingering worktree registered under the staging parent
    wt_list = _git(repos.local, "worktree", "list", "--porcelain").stdout
    assert os.path.abspath(parent) not in wt_list


def test_prepare_reindex_raises_aborts_clean(repos, tmp_path, monkeypatch):
    monkeypatch.setattr(self_update, "PREPARED_MARKER", str(tmp_path / ".prepared_update.json"))
    _commit(repos.upstream, "file.txt", "v2\n", "update")

    def boom(staging_dir):
        raise RuntimeError("reindex crashed")

    status = self_update.prepare_update(
        str(repos.local), "origin", "main",
        reindex_fn=boom, staging_parent=str(tmp_path / "staging"),
    )
    assert status == PreparedStatus.PREPARE_REINDEX_FAILED
    assert self_update._read_prepared_marker() is None


def test_prepare_stage_inconsistent_when_no_stores_written(repos, tmp_path, monkeypatch):
    # Builder returns True but writes no store files -> validation fails -> no marker.
    monkeypatch.setattr(self_update, "PREPARED_MARKER", str(tmp_path / ".prepared_update.json"))
    _commit(repos.upstream, "file.txt", "v2\n", "update")

    status = self_update.prepare_update(
        str(repos.local), "origin", "main",
        reindex_fn=lambda sd: True, staging_parent=str(tmp_path / "staging"),
    )
    assert status == PreparedStatus.PREPARE_STAGE_INCONSISTENT
    assert self_update._read_prepared_marker() is None


def test_prepare_worktree_add_failure(repos, staging, tmp_path, monkeypatch):
    monkeypatch.setattr(self_update, "PREPARED_MARKER", str(tmp_path / ".prepared_update.json"))
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    real = self_update._run_git

    def fake(args, cwd, timeout):
        if args[:2] == ["worktree", "add"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="boom")
        return real(args, cwd, timeout)

    monkeypatch.setattr(self_update, "_run_git", fake)
    status = self_update.prepare_update(
        str(repos.local), "origin", "main",
        reindex_fn=staging.builder, staging_parent=staging.parent,
    )
    assert status == PreparedStatus.PREPARE_WORKTREE_FAILED
    assert self_update._read_prepared_marker() is None


# --- Phase A: activate_prepared_update ---------------------------------------

@pytest.fixture
def phase_a_env(tmp_path, monkeypatch, staging):
    """Redirect marker / staging-root / state paths to temp; STAGING_ROOT is the
    staging parent so activation derives staging dirs there."""
    monkeypatch.setattr(self_update, "PREPARED_MARKER", str(tmp_path / ".prepared_update.json"))
    monkeypatch.setattr(self_update, "STAGING_ROOT", staging.parent)
    monkeypatch.setattr(self_update, "STATE_FILE", str(tmp_path / ".last_update.json"))
    return staging


def _prepare(repos, env):
    return self_update.prepare_update(
        str(repos.local), "origin", "main",
        reindex_fn=env.builder, staging_parent=env.parent,
    )


def test_activate_happy_path(repos, phase_a_env):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    old = _head(repos.local)
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    target = _head(repos.upstream)

    status = self_update.activate_prepared_update(
        str(repos.local), "main", embedding_model=self_update.EMBEDDING_MODEL
    )

    assert status == ActivationStatus.ACTIVATED
    assert _head(repos.local) == target  # ff-merged to the prepared target
    assert _head(repos.local) != old
    live = repos.local / "data"
    assert (live / "skills_store.npz").exists()
    assert (live / "skills_store.json").exists()
    assert (live / ".skills_hash").exists()
    assert (live / "implants_store.npz").exists()
    # marker + staging cleaned up
    assert self_update._read_prepared_marker() is None
    assert not (Path(phase_a_env.parent) / target).exists()


def test_activate_no_marker_is_noop(repos, phase_a_env):
    old = _head(repos.local)
    status = self_update.activate_prepared_update(
        str(repos.local), "main", embedding_model=self_update.EMBEDDING_MODEL
    )
    assert status == ActivationStatus.NO_MARKER
    assert _head(repos.local) == old


def test_activate_model_mismatch_discards(repos, phase_a_env):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    old = _head(repos.local)
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED

    status = self_update.activate_prepared_update(
        str(repos.local), "main", embedding_model="a-different-model"
    )
    assert status == ActivationStatus.INVALID_MODEL_MISMATCH
    assert _head(repos.local) == old  # not merged
    assert self_update._read_prepared_marker() is None  # discarded


def test_activate_sha_missing_discards(repos, phase_a_env):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    old = _head(repos.local)
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    # Corrupt the marker's target to a non-existent commit.
    m = self_update._read_prepared_marker()
    m["target_sha"] = "f" * 40
    Path(self_update.PREPARED_MARKER).write_text(json.dumps(m))

    status = self_update.activate_prepared_update(
        str(repos.local), "main", embedding_model=self_update.EMBEDDING_MODEL
    )
    assert status == ActivationStatus.INVALID_SHA_MISSING
    assert _head(repos.local) == old
    assert self_update._read_prepared_marker() is None


def test_activate_not_ff_discards(repos, phase_a_env):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    # Advance local on a divergent line so target is no longer an ancestor path.
    _commit(repos.local, "local-only.txt", "x\n", "diverge")
    diverged = _head(repos.local)

    status = self_update.activate_prepared_update(
        str(repos.local), "main", embedding_model=self_update.EMBEDDING_MODEL
    )
    assert status == ActivationStatus.INVALID_NOT_FF
    assert _head(repos.local) == diverged
    assert self_update._read_prepared_marker() is None


def test_activate_dirty_tree_skips(repos, phase_a_env):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    old = _head(repos.local)
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    (repos.local / "file.txt").write_text("dirty\n")  # uncommitted change

    status = self_update.activate_prepared_update(
        str(repos.local), "main", embedding_model=self_update.EMBEDDING_MODEL
    )
    assert status == ActivationStatus.INVALID_DIRTY
    assert _head(repos.local) == old
    assert self_update._read_prepared_marker() is None


def test_activate_staging_missing_discards(repos, phase_a_env):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    old = _head(repos.local)
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    target = _head(repos.upstream)
    # Remove the staged worktree files out from under the marker.
    shutil.rmtree(Path(phase_a_env.parent) / target, ignore_errors=True)

    status = self_update.activate_prepared_update(
        str(repos.local), "main", embedding_model=self_update.EMBEDDING_MODEL
    )
    assert status == ActivationStatus.INVALID_STAGING_MISSING
    assert _head(repos.local) == old
    assert self_update._read_prepared_marker() is None


def test_activate_already_at_target_completes_move(repos, phase_a_env):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    target = _head(repos.upstream)
    # Simulate "crashed after merge, before move": HEAD already == target.
    _git(repos.local, "merge", "--ff-only", target)
    assert _head(repos.local) == target

    status = self_update.activate_prepared_update(
        str(repos.local), "main", embedding_model=self_update.EMBEDDING_MODEL
    )
    assert status == ActivationStatus.ACTIVATED
    assert _head(repos.local) == target  # unchanged (no second merge)
    assert (repos.local / "data" / "skills_store.npz").exists()  # move completed
    assert self_update._read_prepared_marker() is None


def test_activate_merge_fails_keeps_old(repos, phase_a_env, monkeypatch):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    old = _head(repos.local)
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    real = self_update._run_git

    def fake(args, cwd, timeout):
        if args[:2] == ["merge", "--ff-only"]:
            return SimpleNamespace(returncode=1, stdout="", stderr="merge boom")
        return real(args, cwd, timeout)

    monkeypatch.setattr(self_update, "_run_git", fake)
    status = self_update.activate_prepared_update(
        str(repos.local), "main", embedding_model=self_update.EMBEDDING_MODEL
    )
    assert status == ActivationStatus.ACTIVATE_MERGE_FAILED
    assert _head(repos.local) == old  # not moved
    assert not (repos.local / "data" / "skills_store.npz").exists()  # stores untouched
    assert self_update._read_prepared_marker() is None


def test_activate_move_failure_unlinks_hash_first(repos, phase_a_env, monkeypatch):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    live = repos.local / "data"
    live.mkdir(exist_ok=True)
    (live / ".skills_hash").write_text("OLD-HASH")  # must be unlinked before the npz move

    real_replace = os.replace

    def boom(src, dst, *a, **k):
        if str(dst).endswith(".npz"):
            raise OSError("disk full")
        return real_replace(src, dst, *a, **k)

    monkeypatch.setattr(self_update.os, "replace", boom)
    status = self_update.activate_prepared_update(
        str(repos.local), "main", embedding_model=self_update.EMBEDDING_MODEL
    )
    assert status == ActivationStatus.ACTIVATE_MOVE_FAILED
    # delete-hash-first ordering: the live hash is gone, so the retriever will be
    # forced to re-embed rather than trust a stale hash over a half-moved store.
    assert not (live / ".skills_hash").exists()
    assert self_update._read_prepared_marker() is None


# --- orchestration: staged dispatch + startup activation ---------------------

def test_run_activation_safely_noop_when_master_switch_off(monkeypatch):
    monkeypatch.setattr(self_update, "AUTO_UPDATE_ENABLED", False)
    monkeypatch.setattr(self_update, "AUTO_UPDATE_STAGING", True)
    calls = []
    monkeypatch.setattr(self_update, "activate_prepared_update", lambda *a, **k: calls.append(1) or "X")
    self_update.run_activation_safely()
    assert calls == []


def test_run_activation_safely_lock_free_when_nothing_staged(tmp_path, monkeypatch):
    monkeypatch.setattr(self_update, "AUTO_UPDATE_ENABLED", True)
    monkeypatch.setattr(self_update, "AUTO_UPDATE_STAGING", True)
    monkeypatch.setattr(self_update, "PREPARED_MARKER", str(tmp_path / "nope.json"))
    monkeypatch.setattr(self_update, "STAGING_ROOT", str(tmp_path / "no-staging"))
    git_calls = []
    monkeypatch.setattr(self_update, "_run_git",
                        lambda *a, **k: git_calls.append(a) or SimpleNamespace(returncode=0, stdout="", stderr=""))
    act_calls = []
    monkeypatch.setattr(self_update, "activate_prepared_update", lambda *a, **k: act_calls.append(1) or "X")
    self_update.run_activation_safely()
    assert git_calls == []  # never forked git
    assert act_calls == []  # never even took the lock / called activate


def test_run_activation_safely_activates_when_marker_present(tmp_path, monkeypatch):
    marker = tmp_path / ".prepared_update.json"
    marker.write_text("{}")
    monkeypatch.setattr(self_update, "AUTO_UPDATE_ENABLED", True)
    monkeypatch.setattr(self_update, "AUTO_UPDATE_STAGING", True)
    monkeypatch.setattr(self_update, "PREPARED_MARKER", str(marker))
    monkeypatch.setattr(self_update, "STAGING_ROOT", str(tmp_path / "staging"))
    monkeypatch.setattr(self_update, "LOCK_FILE", str(tmp_path / ".update.lock"))
    calls = []
    monkeypatch.setattr(self_update, "activate_prepared_update",
                        lambda *a, **k: calls.append(1) or ActivationStatus.ACTIVATED)
    self_update.run_activation_safely()
    assert calls == [1]


def test_run_update_safely_skips_prepare_when_marker_pending(tmp_path, monkeypatch):
    marker = tmp_path / ".prepared_update.json"
    marker.write_text("{}")
    monkeypatch.setattr(self_update, "PREPARED_MARKER", str(marker))
    monkeypatch.setattr(self_update, "LOCK_FILE", str(tmp_path / ".update.lock"))
    monkeypatch.setattr(self_update, "CHECK_STAMP", str(tmp_path / ".check"))
    monkeypatch.setattr(self_update, "AUTO_UPDATE_STAGING", True)
    monkeypatch.setattr(self_update, "AUTO_UPDATE_MIN_INTERVAL", 0)
    prep = []
    monkeypatch.setattr(self_update, "prepare_update", lambda *a, **k: prep.append(1) or PreparedStatus.PREPARED)
    self_update._run_update_safely()
    assert prep == []  # a prepared update is pending -> do not prepare again


def test_run_update_safely_staging_dispatches_to_prepare(tmp_path, monkeypatch):
    monkeypatch.setattr(self_update, "PREPARED_MARKER", str(tmp_path / "nope.json"))
    monkeypatch.setattr(self_update, "LOCK_FILE", str(tmp_path / ".update.lock"))
    monkeypatch.setattr(self_update, "CHECK_STAMP", str(tmp_path / ".check"))
    monkeypatch.setattr(self_update, "AUTO_UPDATE_STAGING", True)
    monkeypatch.setattr(self_update, "AUTO_UPDATE_MIN_INTERVAL", 0)
    calls = {"prepare": 0, "legacy": 0}
    monkeypatch.setattr(self_update, "prepare_update",
                        lambda *a, **k: calls.__setitem__("prepare", calls["prepare"] + 1) or PreparedStatus.UP_TO_DATE)
    monkeypatch.setattr(self_update, "check_and_apply_update",
                        lambda *a, **k: calls.__setitem__("legacy", calls["legacy"] + 1) or UpdateStatus.UP_TO_DATE)
    self_update._run_update_safely()
    assert calls == {"prepare": 1, "legacy": 0}


def test_run_update_safely_legacy_dispatches_to_check_and_apply(tmp_path, monkeypatch):
    monkeypatch.setattr(self_update, "PREPARED_MARKER", str(tmp_path / "nope.json"))
    monkeypatch.setattr(self_update, "LOCK_FILE", str(tmp_path / ".update.lock"))
    monkeypatch.setattr(self_update, "CHECK_STAMP", str(tmp_path / ".check"))
    monkeypatch.setattr(self_update, "AUTO_UPDATE_STAGING", False)
    monkeypatch.setattr(self_update, "AUTO_UPDATE_MIN_INTERVAL", 0)
    calls = {"prepare": 0, "legacy": 0}
    monkeypatch.setattr(self_update, "prepare_update",
                        lambda *a, **k: calls.__setitem__("prepare", calls["prepare"] + 1) or PreparedStatus.UP_TO_DATE)
    monkeypatch.setattr(self_update, "check_and_apply_update",
                        lambda *a, **k: calls.__setitem__("legacy", calls["legacy"] + 1) or UpdateStatus.UP_TO_DATE)
    self_update._run_update_safely()
    assert calls == {"prepare": 0, "legacy": 1}


def test_server_activates_before_engine_imports():
    # Phase A must run before server.py's `from src.engine...` imports, which
    # eagerly load the vector stores into memory at module scope. Guard the
    # ordering invariant statically (numpy-free) so a future reorder is caught.
    # Match real code lines, not comments/docstrings that mention the strings.
    server_py = Path(__file__).resolve().parents[1] / "src" / "server.py"
    lines = server_py.read_text().splitlines()
    act_line = next(
        (i for i, l in enumerate(lines)
         if "run_activation_safely()" in l and not l.lstrip().startswith("#")),
        None,
    )
    eng_line = next((i for i, l in enumerate(lines) if l.startswith("from src.engine")), None)
    assert act_line is not None, "server.py must call run_activation_safely()"
    assert eng_line is not None, "server.py must import from src.engine"
    assert act_line < eng_line, "run_activation_safely() must precede the engine imports"
