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
def repos(tmp_path, monkeypatch):
    """An 'upstream' repo on main with one commit, plus a 'local' clone of it."""
    # Update bookkeeping goes to tmp_path, never the live data/ (issue #68).
    for name, filename in [("STATE_FILE", ".last_update.json"), ("CHECK_STAMP", ".last_update_check")]:
        monkeypatch.setattr(self_update, name, str(tmp_path / filename))
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
    from src.startup import LEASES
    if not LEASES:
        pytest.skip("no installation leases on this platform")
    monkeypatch.setattr(self_update, "AUTO_UPDATE_ENABLED", True)
    monkeypatch.setattr(self_update, "AUTO_UPDATE_STAGING", True)
    monkeypatch.setattr(self_update, "PREPARED_MARKER", str(tmp_path / "missing.json"))
    monkeypatch.setattr(self_update, "LOCK_FILE", str(tmp_path / ".update.lock"))
    monkeypatch.setattr(self_update, "CHECK_STAMP", str(tmp_path / ".check"))
    monkeypatch.setattr(self_update, "AUTO_UPDATE_MIN_INTERVAL", 0)
    calls = []
    monkeypatch.setattr(self_update, "prepare_update",
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
    lock = str(tmp_path / ".update.lock")
    monkeypatch.setattr(self_update, "AUTO_UPDATE_STAGING", False)
    monkeypatch.setattr(self_update, "LOCK_FILE", lock)
    monkeypatch.setattr(self_update, "CHECK_STAMP", str(tmp_path / ".check"))
    monkeypatch.setattr(self_update, "AUTO_UPDATE_MIN_INTERVAL", 0)
    calls = []
    monkeypatch.setattr(self_update, "check_and_apply_update",
                        lambda: calls.append(1) or UpdateStatus.UP_TO_DATE)

    with self_update._process_lock(lock) as held:  # another open of the file excludes this one
        assert held
        self_update._run_update_safely()

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


def test_prepare_records_the_given_model_and_checks_the_target_first(repos, staging, tmp_path, monkeypatch):
    monkeypatch.setattr(self_update, "PREPARED_MARKER", str(tmp_path / ".prepared_update.json"))
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    old, checked = _head(repos.local), []

    status = self_update.prepare_update(
        str(repos.local), "origin", "main",
        reindex_fn=staging.builder, staging_parent=staging.parent,
        embedding_model="service-model", validate_target=lambda *shas: checked.append(shas),
    )

    assert status == PreparedStatus.PREPARED
    assert checked == [(old, _head(repos.upstream))]
    assert self_update._read_prepared_marker()["embedding_model"] == "service-model"


def test_prepare_target_refusal_builds_nothing(repos, tmp_path, monkeypatch):
    monkeypatch.setattr(self_update, "PREPARED_MARKER", str(tmp_path / ".prepared_update.json"))
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    parent, built = tmp_path / "staging", []

    def refuse(old, target):
        raise RuntimeError("refused")

    with pytest.raises(RuntimeError, match="refused"):
        self_update.prepare_update(
            str(repos.local), "origin", "main",
            reindex_fn=built.append, staging_parent=str(parent), validate_target=refuse,
        )
    assert built == [] and not parent.exists()
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


@pytest.mark.parametrize("existing_kind", ["directory", "empty_directory", "file", "dangling_symlink"])
def test_prepare_preserves_unregistered_destination(repos, phase_a_env, existing_kind):
    if existing_kind == "dangling_symlink" and os.name == "nt":
        pytest.skip("POSIX symlink staging path")
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    original = _head(repos.local)
    destination = repos.local / _head(repos.upstream)
    if existing_kind in {"directory", "empty_directory"}:
        destination.mkdir()
        if existing_kind == "directory":
            (destination / "keep.txt").write_text("user data\n")
    elif existing_kind == "file":
        destination.write_text("user data\n")
    else:
        destination.symlink_to(repos.local / "missing", target_is_directory=True)
    builds = []

    def builder(path):
        builds.append(path)
        return phase_a_env.builder(path)

    status = self_update.prepare_update(
        str(repos.local), "origin", "main",
        reindex_fn=builder, staging_parent=str(repos.local),
    )

    assert status == PreparedStatus.PREPARE_WORKTREE_FAILED
    assert builds == []
    assert self_update._read_prepared_marker() is None
    assert _head(repos.local) == original
    if existing_kind == "directory":
        assert (destination / "keep.txt").read_text() == "user data\n"
    elif existing_kind == "empty_directory":
        assert list(destination.iterdir()) == []
    elif existing_kind == "file":
        assert destination.read_text() == "user data\n"
    else:
        assert destination.is_symlink()
        assert destination.readlink() == repos.local / "missing"


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
    assert not (live / self_update.UPDATE_JOURNAL).exists()


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


def test_activate_non_list_stores_discards(repos, phase_a_env):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    old = _head(repos.local)
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    # Corrupt the marker: `stores` is truthy but not a list. Gate 1 must reject
    # it — raising mid-validation/move would strand the marker on disk and make
    # Phase B skip preparing forever.
    m = self_update._read_prepared_marker()
    m["stores"] = 5
    Path(self_update.PREPARED_MARKER).write_text(json.dumps(m))

    status = self_update.activate_prepared_update(
        str(repos.local), "main", embedding_model=self_update.EMBEDDING_MODEL
    )
    assert status == ActivationStatus.INVALID_MARKER
    assert _head(repos.local) == old
    assert self_update._read_prepared_marker() is None


def test_activate_unknown_stores_discards(repos, phase_a_env):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    old = _head(repos.local)
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    # Unknown store names would validate and move NOTHING while still
    # ff-merging — new code would activate without its pre-built stores.
    m = self_update._read_prepared_marker()
    m["stores"] = ["unknown_store"]
    Path(self_update.PREPARED_MARKER).write_text(json.dumps(m))

    status = self_update.activate_prepared_update(
        str(repos.local), "main", embedding_model=self_update.EMBEDDING_MODEL
    )
    assert status == ActivationStatus.INVALID_MARKER
    assert _head(repos.local) == old  # merge never ran
    assert self_update._read_prepared_marker() is None


def test_activate_marker_branch_mismatch_discards(repos, phase_a_env):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    old = _head(repos.local)
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    # Marker recorded for a different branch than the one we'd activate on.
    m = self_update._read_prepared_marker()
    m["branch"] = "some-other-branch"
    Path(self_update.PREPARED_MARKER).write_text(json.dumps(m))

    status = self_update.activate_prepared_update(
        str(repos.local), "main", embedding_model=self_update.EMBEDDING_MODEL
    )
    assert status == ActivationStatus.INVALID_WRONG_BRANCH
    assert _head(repos.local) == old
    assert self_update._read_prepared_marker() is None


def test_activate_unreadable_marker_discards(repos, phase_a_env):
    # A marker FILE that exists but cannot be parsed must be discarded (not
    # treated as NO_MARKER): its mere existence gates run_activation_safely's
    # fast path, so leaving it would cost a lock + git forks on every start.
    old = _head(repos.local)
    Path(self_update.PREPARED_MARKER).write_text("{not-json", encoding="utf-8")

    status = self_update.activate_prepared_update(
        str(repos.local), "main", embedding_model=self_update.EMBEDDING_MODEL
    )
    assert status == ActivationStatus.INVALID_MARKER
    assert not os.path.exists(self_update.PREPARED_MARKER)  # discarded
    assert _head(repos.local) == old


def test_activate_cross_device_staging_discards(repos, phase_a_env, monkeypatch):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    old = _head(repos.local)
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    # Staging on a different mount: os.replace would fail with EXDEV after the
    # ff-merge commit point, so the gate must discard BEFORE merging.
    monkeypatch.setattr(self_update, "_same_filesystem", lambda a, b: False)

    status = self_update.activate_prepared_update(
        str(repos.local), "main", embedding_model=self_update.EMBEDDING_MODEL
    )
    assert status == ActivationStatus.INVALID_CROSS_DEVICE
    assert _head(repos.local) == old  # merge never ran
    assert self_update._read_prepared_marker() is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX staging symlinks")
@pytest.mark.parametrize("redirect_root", [False, True])
def test_activation_rejects_redirected_staging_without_touching_target(repos, phase_a_env, tmp_path, redirect_root):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    original = _head(repos.local)
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    target_sha = _head(repos.upstream)
    parent = Path(phase_a_env.parent)
    staged = parent / target_sha
    protected_parent = tmp_path / "protected"
    protected_parent.mkdir()
    protected = protected_parent / target_sha
    # Keep the target registered: cleanup must not accept it merely because the
    # redirected STAGING_ROOT resolves to its otherwise valid canonical parent.
    _git(repos.local, "worktree", "move", str(staged), str(protected))
    if redirect_root:
        parent.rmdir()
        parent.symlink_to(protected_parent, target_is_directory=True)
    else:
        staged.symlink_to(protected, target_is_directory=True)
    saved = {p.name: p.read_bytes() for p in (protected / "data").iterdir()}

    status = self_update.activate_prepared_update(str(repos.local), "main")

    assert status == ActivationStatus.INVALID_STAGING_INCONSISTENT
    assert _head(repos.local) == original
    assert {p.name: p.read_bytes() for p in (protected / "data").iterdir()} == saved
    assert (protected / "file.txt").read_text() == "v2\n"
    assert not (repos.local / "data/skills_store.npz").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX staging symlinks")
def test_prepare_rejects_redirected_staging_root(repos, phase_a_env, tmp_path):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    parent = Path(phase_a_env.parent)
    protected = tmp_path / "protected"
    protected.mkdir()
    (protected / "keep.txt").write_text("user data\n")
    parent.symlink_to(protected, target_is_directory=True)

    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARE_WORKTREE_FAILED
    assert list(protected.iterdir()) == [protected / "keep.txt"]
    assert (protected / "keep.txt").read_text() == "user data\n"
    assert self_update._read_prepared_marker() is None


@pytest.mark.skipif(os.name == "nt", reason="POSIX staging symlinks")
@pytest.mark.parametrize("redirect", ["data", "skills_store.npz"])
def test_activation_rejects_redirected_store_contents(repos, phase_a_env, tmp_path, redirect):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    original = _head(repos.local)
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    staged = Path(phase_a_env.parent) / _head(repos.upstream)
    path = staged / "data" if redirect == "data" else staged / "data" / redirect
    protected = tmp_path / "protected-content"
    path.rename(protected)
    saved = ({p.name: p.read_bytes() for p in protected.iterdir()}
             if protected.is_dir() else protected.read_bytes())
    path.symlink_to(protected, target_is_directory=protected.is_dir())

    status = self_update.activate_prepared_update(str(repos.local), "main")

    assert status == ActivationStatus.INVALID_STAGING_INCONSISTENT
    assert _head(repos.local) == original
    current = ({p.name: p.read_bytes() for p in protected.iterdir()}
               if protected.is_dir() else protected.read_bytes())
    assert current == saved
    assert not (repos.local / "data/skills_store.npz").exists()


def test_staging_worktrees_returns_validated_absolute_paths(repos, phase_a_env):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    target = _head(repos.upstream)

    paths = self_update._staging_worktrees(str(repos.local), phase_a_env.parent, 30)
    assert len(paths) == 1
    # The returned value is the validated absolute path, not git's raw string.
    assert paths[0] == os.path.abspath(paths[0])
    assert os.path.basename(paths[0]) == target


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
    old = _head(repos.local)
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
    assert _head(repos.local) == old  # merge rolled back — old code keeps serving


def test_activate_move_failure_rolls_back_merge(repos, phase_a_env, monkeypatch):
    # A failed store move after a successful ff-merge must restore the pre-merge
    # tree: otherwise the process serves old imported code over a new on-disk
    # tree, and the next start runs new code without its pre-built stores.
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    old = _head(repos.local)
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED

    monkeypatch.setattr(
        self_update, "_activate_staged_stores",
        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")),
    )
    status = self_update.activate_prepared_update(
        str(repos.local), "main", embedding_model=self_update.EMBEDDING_MODEL
    )
    assert status == ActivationStatus.ACTIVATE_MOVE_FAILED
    assert _head(repos.local) == old
    assert (repos.local / "file.txt").read_text(encoding="utf-8") == "v1\n"  # worktree content restored


def test_prune_never_touches_live_install_when_staging_dir_is_repo_root(repos):
    # Misconfiguration: AGENTS_AUTO_UPDATE_STAGING_DIR set to the repo root.
    # `git worktree list` reports the main worktree (the live install); it must
    # never be returned for rmtree.
    assert self_update._staging_worktrees(str(repos.local), str(repos.local), 30) == []
    self_update._prune_staging_worktrees(str(repos.local), str(repos.local), 30)
    assert (repos.local / ".git").exists()
    assert (repos.local / "file.txt").exists()


def test_prune_never_touches_live_install_when_staging_dir_is_repo_ancestor(repos, tmp_path):
    # Misconfiguration: staging dir set to an ancestor of the repo. The main
    # worktree lies "under the parent" but is not a <sha>-named staging
    # checkout, so it must be filtered out.
    assert self_update._staging_worktrees(str(repos.local), str(tmp_path), 30) == []
    self_update._prune_staging_worktrees(str(repos.local), str(tmp_path), 30)
    assert (repos.local / ".git").exists()
    assert (repos.local / "file.txt").exists()
    assert (repos.upstream / "file.txt").exists()  # sibling repo untouched too


def test_prune_removes_empty_staging_parent(repos, tmp_path):
    # An empty leftover parent would defeat the lock-free startup fast path
    # (run_activation_safely stats STAGING_ROOT) on every subsequent start.
    parent = tmp_path / "staging-parent"
    parent.mkdir()
    self_update._prune_staging_worktrees(str(repos.local), str(parent), 30)
    assert not parent.exists()


def test_prune_keeps_non_empty_staging_parent(repos, tmp_path):
    # Foreign content under the parent must never be deleted by the reaper.
    parent = tmp_path / "staging-parent"
    parent.mkdir()
    (parent / "stray.txt").write_text("x")
    self_update._prune_staging_worktrees(str(repos.local), str(parent), 30)
    assert parent.exists()
    assert (parent / "stray.txt").read_text() == "x"


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
    execs = []
    monkeypatch.setattr(self_update, "_exec_server", lambda exe, argv: execs.append((exe, argv)))
    calls = []
    monkeypatch.setattr(self_update, "activate_prepared_update",
                        lambda *a, **k: calls.append(1) or ActivationStatus.ACTIVATED)
    self_update.run_activation_safely()
    assert calls == [1]


def test_run_activation_safely_reexecs_after_activation(tmp_path, monkeypatch):
    # A successful activation must re-exec the process so the activation start
    # serves the new code end-to-end (this module was compiled pre-merge).
    marker = tmp_path / ".prepared_update.json"
    marker.write_text("{}")
    monkeypatch.setattr(self_update, "AUTO_UPDATE_ENABLED", True)
    monkeypatch.setattr(self_update, "AUTO_UPDATE_STAGING", True)
    monkeypatch.setattr(self_update, "PREPARED_MARKER", str(marker))
    monkeypatch.setattr(self_update, "STAGING_ROOT", str(tmp_path / "staging"))
    monkeypatch.setattr(self_update, "LOCK_FILE", str(tmp_path / ".update.lock"))
    monkeypatch.setattr(self_update, "activate_prepared_update",
                        lambda *a, **k: ActivationStatus.ACTIVATED)
    execs = []
    monkeypatch.setattr(self_update, "_exec_server", lambda exe, argv: execs.append((exe, argv)))
    self_update.run_activation_safely()
    assert len(execs) == 1
    exe, argv = execs[0]
    assert exe == self_update.sys.executable
    assert argv[0] == self_update.sys.executable


def test_run_activation_safely_no_reexec_when_discarded(tmp_path, monkeypatch):
    # Non-ACTIVATED outcomes (discards, failures) must NOT re-exec.
    marker = tmp_path / ".prepared_update.json"
    marker.write_text("{}")
    monkeypatch.setattr(self_update, "AUTO_UPDATE_ENABLED", True)
    monkeypatch.setattr(self_update, "AUTO_UPDATE_STAGING", True)
    monkeypatch.setattr(self_update, "PREPARED_MARKER", str(marker))
    monkeypatch.setattr(self_update, "STAGING_ROOT", str(tmp_path / "staging"))
    monkeypatch.setattr(self_update, "LOCK_FILE", str(tmp_path / ".update.lock"))
    monkeypatch.setattr(self_update, "activate_prepared_update",
                        lambda *a, **k: ActivationStatus.INVALID_MARKER)
    execs = []
    monkeypatch.setattr(self_update, "_exec_server", lambda exe, argv: execs.append((exe, argv)))
    self_update.run_activation_safely()
    assert execs == []


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
    lines = server_py.read_text(encoding="utf-8").splitlines()
    act_line = next(
        (i for i, line in enumerate(lines)
         if "run_server(__file__)" in line and not line.lstrip().startswith("#")),
        None,
    )
    eng_line = next((i for i, line in enumerate(lines) if line.startswith("from src.engine")), None)
    assert act_line is not None, "server.py must enter the leased bootstrap"
    assert eng_line is not None, "server.py must import from src.engine"
    assert act_line < eng_line, "leased bootstrap must precede the engine imports"


@pytest.mark.parametrize("metadata", [5, None, [], "save_version", {"save_version": 5}])
def test_malformed_store_metadata_is_discarded(repos, phase_a_env, metadata):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    original = _head(repos.local)
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    marker = self_update._read_prepared_marker()
    meta = Path(phase_a_env.parent) / marker["target_sha"] / "data/skills_store.json"
    meta.write_text(json.dumps(metadata))
    assert self_update.activate_prepared_update(str(repos.local), "main") == ActivationStatus.INVALID_STAGING_INCONSISTENT
    assert self_update._read_prepared_marker() is None
    assert _head(repos.local) == original
    # A bad marker must not strand the background updater forever.
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED


@pytest.mark.parametrize("field,value", [
    ("base_sha", 5), ("base_sha", None), ("base_sha", ""),
    ("stores", ["skills_store"]),
])
def test_incomplete_marker_is_rejected_before_merge(repos, phase_a_env, field, value):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    original = _head(repos.local)
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    marker = self_update._read_prepared_marker()
    marker[field] = value
    Path(self_update.PREPARED_MARKER).write_text(json.dumps(marker))
    assert self_update.activate_prepared_update(str(repos.local), "main") == ActivationStatus.INVALID_MARKER
    assert _head(repos.local) == original
    assert self_update._read_prepared_marker() is None


def test_live_reader_keeps_prepared_update_pending(repos, phase_a_env):
    from src.startup import LEASES, server_session
    if not LEASES:
        pytest.skip("no installation leases on this platform")
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    original = _head(repos.local)
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    code = '''
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from src.startup import server_session
root = Path(sys.argv[2])
with server_session(root, lambda fd: None):
    print((root / "file.txt").read_text().strip(), flush=True)
    sys.stdin.readline()
    print((root / "file.txt").read_text().strip(), flush=True)
'''
    process = subprocess.Popen(
        [self_update.sys.executable, "-c", code, str(Path(__file__).resolve().parents[1]), str(repos.local)],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )
    def activate(fd):
        return self_update.activate_prepared_update(str(repos.local), "main")
    try:
        assert process.stdout.readline().strip() == "v1"
        with server_session(repos.local, activate):
            assert _head(repos.local) == original
            assert self_update._read_prepared_marker() is not None
        output, errors = process.communicate(input="next request\n", timeout=5)
        assert process.returncode == 0, errors
        assert output.strip() == "v1"
        with server_session(repos.local, activate):
            assert _head(repos.local) == _head(repos.upstream)
            assert self_update._read_prepared_marker() is None
    finally:
        if process.poll() is None:
            process.terminate()
        process.communicate(timeout=5)


@pytest.mark.parametrize("old_location", [False, True])
def test_moved_prebuilt_stores_load_without_embedding(repos, phase_a_env, monkeypatch, old_location):
    import numpy as np
    from src.engine import skills, implants

    specs = (
        (skills, skills.SkillRetriever, "skills", "SKILLS_DIR", ".skills_hash"),
        (implants, implants.ImplantRetriever, "implants", "IMPLANTS_DIR", ".implants_hash"),
    )
    for _, _, folder, _, _ in specs:
        (repos.upstream / folder).mkdir()
        _commit(repos.upstream, f"{folder}/demo.mdc", "---\ndescription: Demo\n---\nBody\n", folder)

    def configure(patch, root, embed):
        for module, cls, folder, setting, sidecar in specs:
            patch.setattr(module, setting, str(root / folder))
            patch.setattr(module, "DATA_DIR", str(root / "data"))
            patch.setattr(cls, "HASH_FILE", str(root / "data" / sidecar))
            patch.setattr(module, "embed_texts", embed)

    saved_stores = {}

    def builder(root):
        with monkeypatch.context() as patch:
            configure(patch, Path(root), lambda docs: np.ones((len(docs), 3), dtype=np.float32))
            for _, cls, folder, _, _ in specs:
                assert cls().store.count() == 1
                data = Path(root) / "data"
                metadata_path = data / f"{folder}_store.json"
                payload = json.loads(metadata_path.read_text())
                if old_location:
                    payload["metadatas"][0]["path"] = f"/old/installation/{folder}/demo.mdc"
                    metadata_path.write_text(json.dumps(payload))
                saved_stores[folder] = ((data / f"{folder}_store.npz").read_bytes(), payload["save_version"])
        return True

    assert self_update.prepare_update(str(repos.local), "origin", "main", reindex_fn=builder,
                                     staging_parent=phase_a_env.parent) == PreparedStatus.PREPARED
    assert self_update.activate_prepared_update(str(repos.local), "main") == ActivationStatus.ACTIVATED
    configure(monkeypatch, repos.local, lambda docs: pytest.fail("activation re-embedded prepared stores"))
    for module, cls, folder, _, _ in specs:
        retriever = cls()
        assert retriever.store.count() == 1
        metadata = retriever.store.get(ids=["demo.mdc"]).metadatas[0]
        assert metadata["path"] == str(repos.local / folder / "demo.mdc")
        assert Path(metadata["path"]).is_file()
        data = repos.local / "data"
        assert (data / f"{folder}_store.npz").read_bytes() == saved_stores[folder][0]
        assert json.loads((data / f"{folder}_store.json").read_text())["save_version"] == saved_stores[folder][1]
        # Relocation is ignored, but actual content changes still invalidate.
        (repos.local / folder / "demo.mdc").write_text("changed\n")
        assert cls.__new__(cls)._needs_reindex()[0]


def test_metadata_relocation_write_failure_rolls_back(repos, phase_a_env, monkeypatch):
    import builtins

    _commit(repos.upstream, "file.txt", "v2\n", "update")
    original = _head(repos.local)
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    metadata = Path(phase_a_env.parent) / _head(repos.upstream) / "data/skills_store.json"
    live = repos.local / "data"
    _write_fake_store_set(live)
    old_store = (live / "skills_store.npz").read_bytes()
    real_open = builtins.open

    def denied(path, mode="r", *args, **kwargs):
        if str(path) == str(metadata) and mode == "w":
            raise PermissionError("cannot rewrite staged metadata")
        return real_open(path, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", denied)
    assert self_update.activate_prepared_update(str(repos.local), "main") == ActivationStatus.ACTIVATE_MOVE_FAILED
    assert _head(repos.local) == original
    assert (live / "skills_store.npz").read_bytes() == old_store
    assert not (live / ".skills_hash").exists()
    assert not (live / ".implants_hash").exists()
    assert not (live / self_update.UPDATE_JOURNAL).exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX git hook")
@pytest.mark.parametrize("staged", [True, False])
def test_merge_timeout_after_head_advanced_rolls_back(repos, phase_a_env, staged):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    original = _head(repos.local)
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    hook = repos.local / ".git/hooks/post-merge"
    hook.write_text("#!/bin/sh\nsleep 2\n")
    hook.chmod(0o755)
    if staged:
        status = self_update.activate_prepared_update(str(repos.local), "main", git_timeout=1)
        assert status == ActivationStatus.ACTIVATE_MERGE_FAILED
        assert self_update._read_prepared_marker() is None
    else:
        status = check_and_apply_update(str(repos.local), "origin", "main", git_timeout=1)
        assert status == UpdateStatus.FETCH_FAILED
    assert _head(repos.local) == original
    assert (repos.local / "file.txt").read_text() == "v1\n"
    assert not (repos.local / "data" / self_update.UPDATE_JOURNAL).exists()


def test_activation_rollback_failure_is_distinct(repos, phase_a_env, monkeypatch):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    real_git = self_update._run_git

    def failing_git(args, cwd, timeout):
        if args[0] == "merge":
            real_git(args, cwd, timeout)
            raise subprocess.TimeoutExpired("git merge", timeout)
        if args[0] == "reset":
            return SimpleNamespace(returncode=1, stdout="", stderr="reset failed")
        return real_git(args, cwd, timeout)

    monkeypatch.setattr(self_update, "_run_git", failing_git)
    assert self_update.activate_prepared_update(str(repos.local), "main") == ActivationStatus.ACTIVATE_ROLLBACK_FAILED


@pytest.mark.parametrize("failure", ["rollback", "exec"])
def test_unsafe_activation_failure_does_not_fall_through(tmp_path, monkeypatch, failure):
    marker = tmp_path / "marker.json"
    marker.write_text("{}")
    monkeypatch.setattr(self_update, "AUTO_UPDATE_ENABLED", True)
    monkeypatch.setattr(self_update, "AUTO_UPDATE_STAGING", True)
    monkeypatch.setattr(self_update, "PREPARED_MARKER", str(marker))
    monkeypatch.setattr(self_update, "LOCK_FILE", str(tmp_path / "update.lock"))
    status = ActivationStatus.ACTIVATE_ROLLBACK_FAILED if failure == "rollback" else ActivationStatus.ACTIVATED
    monkeypatch.setattr(self_update, "activate_prepared_update", lambda: status)

    def fail_exec(*args):
        raise OSError("exec failed")

    monkeypatch.setattr(self_update, "_exec_server", fail_exec)
    with pytest.raises(SystemExit, match="restart required"):
        self_update.run_activation_safely()


@pytest.mark.parametrize("status", [UpdateStatus.UPDATED, UpdateStatus.ROLLBACK_FAILED])
def test_legacy_updates_only_at_quiet_startup(monkeypatch, status):
    monkeypatch.setattr(self_update, "AUTO_UPDATE_ENABLED", True)
    monkeypatch.setattr(self_update, "AUTO_UPDATE_STAGING", False)
    calls = []
    monkeypatch.setattr(self_update, "_run_update_safely", lambda: calls.append("update") or status)
    monkeypatch.setattr(self_update, "_reexec_updated_server", lambda: calls.append("exec"))
    assert self_update.start_background_update() is None
    if status == UpdateStatus.ROLLBACK_FAILED:
        with pytest.raises(SystemExit, match="restart required"):
            self_update.run_activation_safely()
        assert calls == ["update"]
    else:
        self_update.run_activation_safely()
        assert calls == ["update", "exec"]


def _configure_startup_activation(monkeypatch, repos):
    monkeypatch.setattr(self_update, "AUTO_UPDATE_ENABLED", True)
    monkeypatch.setattr(self_update, "AUTO_UPDATE_STAGING", True)
    monkeypatch.setattr(self_update, "LOCK_FILE", str(repos.local / "data/.update.lock"))
    activate = self_update.activate_prepared_update
    monkeypatch.setattr(self_update, "activate_prepared_update", lambda: activate(str(repos.local), "main"))


def test_post_commit_cleanup_launch_failure_still_reexecs(repos, phase_a_env, monkeypatch):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    _configure_startup_activation(monkeypatch, repos)
    real_git = self_update._run_git

    def permission_denied_during_cleanup(args, cwd, timeout):
        if args[0] == "worktree":
            raise PermissionError("cannot launch cleanup git")
        return real_git(args, cwd, timeout)

    monkeypatch.setattr(self_update, "_run_git", permission_denied_during_cleanup)
    execs = []
    monkeypatch.setattr(self_update, "_reexec_updated_server", lambda: execs.append(True))
    self_update.run_activation_safely()
    assert _head(repos.local) == _head(repos.upstream)
    assert self_update._read_prepared_marker() is None
    assert json.loads(Path(self_update.STATE_FILE).read_text())["status"] == ActivationStatus.ACTIVATED
    assert execs == [True]


@pytest.mark.parametrize("invalid_marker", [False, True])
def test_marker_removal_failure_stops_startup_and_preserves_staging(repos, phase_a_env, monkeypatch, invalid_marker):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    original = _head(repos.local)
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    target = _head(repos.upstream)
    if invalid_marker:
        marker = self_update._read_prepared_marker()
        marker["branch"] = "another-branch"
        Path(self_update.PREPARED_MARKER).write_text(json.dumps(marker))
    _configure_startup_activation(monkeypatch, repos)
    remove = self_update.os.remove

    def denied(path, *args, **kwargs):
        if str(path) == self_update.PREPARED_MARKER:
            raise PermissionError("cannot remove prepared marker")
        return remove(path, *args, **kwargs)

    monkeypatch.setattr(self_update.os, "remove", denied)
    execs = []
    monkeypatch.setattr(self_update, "_reexec_updated_server", lambda: execs.append(True))
    with pytest.raises(SystemExit, match="startup failed") as failure:
        self_update.run_activation_safely()

    assert isinstance(failure.value.__cause__, PermissionError)
    assert self_update._read_prepared_marker() is not None
    assert (Path(phase_a_env.parent) / target).is_dir()
    assert execs == []
    assert _head(repos.local) == (original if invalid_marker else target)
    if not invalid_marker:
        assert (repos.local / "data" / self_update.UPDATE_JOURNAL).exists()


@pytest.mark.parametrize("staged", [True, False])
def test_rollback_launch_failure_stops_startup(repos, phase_a_env, monkeypatch, staged):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    _configure_startup_activation(monkeypatch, repos)
    monkeypatch.setattr(self_update, "AUTO_UPDATE_STAGING", staged)
    monkeypatch.setattr(self_update, "CHECK_STAMP", str(repos.local / "data/.check"))
    monkeypatch.setattr(self_update, "AUTO_UPDATE_MIN_INTERVAL", 0)
    monkeypatch.setattr(self_update, "check_and_apply_update", lambda: check_and_apply_update(str(repos.local), "origin", "main"))
    real_git = self_update._run_git

    def permission_denied_during_rollback(args, cwd, timeout):
        if args[0] == "merge":
            real_git(args, cwd, timeout)
            raise subprocess.TimeoutExpired("git merge", timeout)
        if args[0] == "reset":
            raise PermissionError("cannot launch rollback git")
        return real_git(args, cwd, timeout)

    monkeypatch.setattr(self_update, "_run_git", permission_denied_during_rollback)
    with pytest.raises(SystemExit, match="could not restore"):
        self_update.run_activation_safely()
    journal = repos.local / "data" / self_update.UPDATE_JOURNAL
    assert journal.exists()
    if staged:
        assert self_update._read_prepared_marker() is not None  # retain recovery artifacts
    code = '''
import sys
sys.path.insert(0, sys.argv[1])
from src.startup import server_session
with server_session(sys.argv[2], lambda fd: print("ACTIVATION_REACHED")):
    print("IMPORTS_REACHED")
'''
    restarted = subprocess.run(
        [self_update.sys.executable, "-c", code, str(Path(__file__).resolve().parents[1]), str(repos.local)],
        env=dict(os.environ, AGENTS_AUTO_UPDATE="0"), capture_output=True, text=True, timeout=5,
    )
    assert restarted.returncode != 0
    assert "Unfinished auto-update" in restarted.stderr
    assert not restarted.stdout  # blocked before imports, even with updates disabled
    assert journal.exists()


@pytest.mark.parametrize("staged", [True, False])
def test_unexpected_startup_update_exception_cannot_serve_mixed_tree(tmp_path, monkeypatch, staged):
    monkeypatch.setattr(self_update, "AUTO_UPDATE_ENABLED", True)
    monkeypatch.setattr(self_update, "AUTO_UPDATE_STAGING", staged)
    marker = tmp_path / "marker.json"
    marker.write_text("{}")
    monkeypatch.setattr(self_update, "PREPARED_MARKER", str(marker))
    monkeypatch.setattr(self_update, "LOCK_FILE", str(tmp_path / "update.lock"))
    monkeypatch.setattr(self_update, "AUTO_UPDATE_MIN_INTERVAL", 0)

    def unexpected_failure():
        raise RuntimeError("unexpected failure after mutation")

    monkeypatch.setattr(self_update, "activate_prepared_update" if staged else "check_and_apply_update", unexpected_failure)
    with pytest.raises(SystemExit, match="startup failed"):
        self_update.run_activation_safely()


def test_hash_invalidation_failure_aborts_before_store_moves(repos, phase_a_env, monkeypatch):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    original = _head(repos.local)
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    live = repos.local / "data"
    _write_fake_store_set(live)
    (live / "skills_store.npz").write_bytes(b"old store")
    old_metadata = (live / "skills_store.json").read_bytes()
    real_remove = self_update.os.remove

    def denied(path, *args, **kwargs):
        if str(path) == str(live / ".skills_hash"):
            raise PermissionError("cannot invalidate hash")
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(self_update.os, "remove", denied)
    assert self_update.activate_prepared_update(str(repos.local), "main") == ActivationStatus.ACTIVATE_MOVE_FAILED
    assert _head(repos.local) == original
    assert (live / "skills_store.npz").read_bytes() == b"old store"
    assert (live / "skills_store.json").read_bytes() == old_metadata
    assert (live / ".skills_hash").read_text() == "hash-0"


@pytest.mark.parametrize("transient", [False, True])
def test_resume_verifies_hash_invalidation_before_clearing_journal(repos, phase_a_env, monkeypatch, transient):
    _commit(repos.upstream, "file.txt", "new indexing code\n", "code-only update")
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    target = _head(repos.upstream)
    _git(repos.local, "merge", "--ff-only", target)
    live = repos.local / "data"
    _write_fake_store_set(live)
    (live / "skills_store.npz").write_bytes(b"old index format")
    remove = self_update.os.remove
    attempts = []

    def denied(path, *args, **kwargs):
        if str(path) == str(live / ".skills_hash"):
            attempts.append(path)
            if not transient or len(attempts) == 1:
                raise PermissionError("cannot invalidate old index")
        return remove(path, *args, **kwargs)

    monkeypatch.setattr(self_update.os, "remove", denied)
    status = self_update.activate_prepared_update(str(repos.local), "main")

    assert _head(repos.local) == target
    assert (live / "skills_store.npz").read_bytes() == b"old index format"
    assert not (live / ".implants_hash").exists()
    journal = live / self_update.UPDATE_JOURNAL
    if transient:
        assert status == ActivationStatus.ACTIVATE_MOVE_FAILED
        assert not (live / ".skills_hash").exists()
        assert not journal.exists()
    else:
        assert status == ActivationStatus.ACTIVATE_ROLLBACK_FAILED
        assert journal.exists()
        assert (live / ".skills_hash").exists()
        assert self_update._read_prepared_marker() is not None
        assert (Path(phase_a_env.parent) / target).is_dir()
        from src.startup import assert_installation_safe
        with pytest.raises(SystemExit, match="Unfinished auto-update"):
            assert_installation_safe(repos.local)


@pytest.mark.parametrize("blocked_file", ["skills_store.npz", "skills_store.json"])
def test_empty_store_deletion_failure_does_not_publish_hash(repos, phase_a_env, monkeypatch, blocked_file):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    original = _head(repos.local)
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    staged = Path(phase_a_env.parent) / self_update._read_prepared_marker()["target_sha"] / "data"
    (staged / "skills_store.npz").unlink()
    (staged / "skills_store.json").unlink()
    (staged / ".skills_hash").write_text("empty-source-hash")
    live = repos.local / "data"
    _write_fake_store_set(live)
    real_remove = self_update.os.remove

    def denied(path, *args, **kwargs):
        if str(path) == str(live / blocked_file):
            raise PermissionError("cannot clear stale store")
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(self_update.os, "remove", denied)
    assert self_update.activate_prepared_update(str(repos.local), "main") == ActivationStatus.ACTIVATE_MOVE_FAILED
    assert _head(repos.local) == original
    assert not (live / ".skills_hash").exists()  # next load must rebuild


@pytest.mark.parametrize("staged", [True, False])
def test_recovery_journal_must_be_written_before_mutation(repos, phase_a_env, monkeypatch, staged):
    _commit(repos.upstream, "file.txt", "v2\n", "update")
    original = _head(repos.local)
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED

    def denied(*args):
        raise PermissionError("cannot persist recovery evidence")

    monkeypatch.setattr(self_update, "_begin_update", denied)
    with pytest.raises(PermissionError):
        if staged:
            self_update.activate_prepared_update(str(repos.local), "main")
        else:
            check_and_apply_update(str(repos.local), "origin", "main")
    assert _head(repos.local) == original
    assert (repos.local / "file.txt").read_text() == "v1\n"


@pytest.mark.parametrize("blocked_file,deny_cleanup", [
    ("implants_store.npz", False), (".implants_hash", False), (".implants_hash", True),
])
def test_partial_batch_failure_invalidates_all_stores_or_keeps_journal(
    repos, phase_a_env, monkeypatch, blocked_file, deny_cleanup,
):
    # A code-only update can produce a new store while retaining the old source
    # digest. The earlier store must not remain trusted after a later one fails.
    _commit(repos.upstream, "file.txt", "new indexing code\n", "code-only update")
    original = _head(repos.local)
    assert _prepare(repos, phase_a_env) == PreparedStatus.PREPARED
    live = repos.local / "data"
    _write_fake_store_set(live)  # same hashes as the staged sources
    (live / "skills_store.npz").write_bytes(b"old indexing output")
    replace, remove = self_update.os.replace, self_update.os.remove
    published = []

    def fail_later_store(src, dst):
        if str(dst) == str(live / blocked_file):
            raise OSError("later store failed")
        result = replace(src, dst)
        if str(dst) == str(live / ".skills_hash"):
            published.append(True)
        return result

    def fail_invalidation(path, *args, **kwargs):
        if deny_cleanup and published and str(path) == str(live / ".skills_hash"):
            raise PermissionError("cannot invalidate published hash")
        return remove(path, *args, **kwargs)

    monkeypatch.setattr(self_update.os, "replace", fail_later_store)
    monkeypatch.setattr(self_update.os, "remove", fail_invalidation)
    status = self_update.activate_prepared_update(str(repos.local), "main")
    assert _head(repos.local) == original
    journal = live / self_update.UPDATE_JOURNAL
    if deny_cleanup:
        assert status == ActivationStatus.ACTIVATE_ROLLBACK_FAILED
        assert journal.exists()
        from src.startup import assert_installation_safe
        with pytest.raises(SystemExit, match="Unfinished auto-update"):
            assert_installation_safe(repos.local)
    else:
        assert status == ActivationStatus.ACTIVATE_MOVE_FAILED
        assert not journal.exists()
        assert not (live / ".skills_hash").exists()
        assert not (live / ".implants_hash").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink install alias")
def test_staging_cleanup_excludes_live_repo_through_symlink(tmp_path):
    live = _init_repo(tmp_path / ("a" * 40))
    _commit(live, "file.txt", "live installation\n", "init")
    alias = tmp_path / "install-alias"
    alias.symlink_to(live, target_is_directory=True)
    assert self_update._staging_worktrees(str(alias), str(tmp_path), 30) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink staging paths")
@pytest.mark.parametrize("symlink_parent", [False, True])
def test_staging_cleanup_does_not_follow_replaced_worktree_paths(repos, tmp_path, symlink_parent):
    parent = tmp_path / "staging"
    registered = parent / ("a" * 40)
    _git(repos.local, "worktree", "add", "--detach", str(registered), "HEAD")
    moved_path = parent if symlink_parent else registered
    protected = tmp_path / "protected" if symlink_parent else parent / ("b" * 40)
    moved_path.rename(protected)
    moved_path.symlink_to(protected, target_is_directory=True)
    target = protected / registered.name if symlink_parent else protected
    sentinel = target / "keep.txt"
    sentinel.write_text("unrelated contents must survive cleanup\n")

    self_update._prune_staging_worktrees(str(repos.local), str(parent), 30)

    assert sentinel.read_text() == "unrelated contents must survive cleanup\n"
    assert moved_path.is_symlink()
    assert (repos.local / "file.txt").exists()


def test_staging_cleanup_preserves_nested_user_worktree(repos, tmp_path):
    parent = tmp_path / "staging"
    nested = parent / "user" / ("a" * 40)
    _git(repos.local, "worktree", "add", "--detach", str(nested), "HEAD")

    self_update._prune_staging_worktrees(str(repos.local), str(parent), 30)

    assert (nested / "file.txt").read_text() == "v1\n"
    assert _head(nested) == _head(repos.local)


@pytest.mark.parametrize("deny_cleanup", [False, True])
def test_legacy_reindex_failure_invalidates_new_store_hashes(repos, monkeypatch, deny_cleanup):
    _commit(repos.upstream, "file.txt", "new indexing code\n", "code-only update")
    original = _head(repos.local)
    live = repos.local / "data"
    remove = self_update.os.remove

    def failed_reindex(root):
        _write_fake_store_set(live)  # partial reindex may have published hashes
        return False

    def fail_invalidation(path, *args, **kwargs):
        if deny_cleanup and str(path) == str(live / ".skills_hash"):
            raise PermissionError("cannot invalidate new index")
        return remove(path, *args, **kwargs)

    monkeypatch.setattr(self_update.os, "remove", fail_invalidation)
    status = check_and_apply_update(str(repos.local), "origin", "main", reindex_fn=failed_reindex)
    assert _head(repos.local) == original
    if deny_cleanup:
        assert status == UpdateStatus.ROLLBACK_FAILED
        assert (live / self_update.UPDATE_JOURNAL).exists()
    else:
        assert status == UpdateStatus.REINDEX_FAILED
        assert not (live / ".skills_hash").exists()
        assert not (live / ".implants_hash").exists()
        assert not (live / self_update.UPDATE_JOURNAL).exists()


# --- Update channel: branch switches of the stdio updater (#381) -------------

def _publish_dev(repos, content="dev\n"):
    """Upstream gets `dev`, one commit ahead of its main; upstream stays on main."""
    _git(repos.upstream, "checkout", "-q", "-b", "dev")
    _commit(repos.upstream, "dev.txt", content, "on dev")
    tip = _head(repos.upstream)
    _git(repos.upstream, "checkout", "-q", "main")
    return tip


def _branch(path):
    return _git(path, "symbolic-ref", "--short", "HEAD").stdout.strip()


def _setting(repos, branch, previous=None):
    """The installation's .env as `channel set` leaves it: the setting and the previous one."""
    lines = [f"AGENTS_AUTO_UPDATE_BRANCH={branch}"] + ([f"AGENTS_AUTO_UPDATE_PREVIOUS_BRANCH={previous}"]
                                                       if previous else [])
    (repos.local / ".env").write_text("\n".join(lines) + "\n")


def _release(repos):
    """Upstream main gets a release: a merge commit of dev, which dev never gets."""
    _git(repos.upstream, "merge", "-q", "--no-ff", "-m", "Release", "dev")
    return _head(repos.upstream)


@pytest.fixture
def channel_env(monkeypatch):
    """The stdio server's environment holds no update keys of the developer's shell."""
    from src import update_channel
    for key in update_channel.KEYS:
        monkeypatch.delenv(key, raising=False)
    return monkeypatch


def test_staged_switch_from_main_to_dev_activates_on_the_next_start(repos, phase_a_env, channel_env):
    tip = _publish_dev(repos)
    _setting(repos, "dev", "main")
    old = _head(repos.local)
    # The default policy is this process's environment, as a stdio server loaded it from .env.
    status = self_update.prepare_update(str(repos.local), "origin", "dev",
                                        reindex_fn=phase_a_env.builder, staging_parent=phase_a_env.parent)
    assert status == PreparedStatus.PREPARED
    marker = self_update._read_prepared_marker()
    assert (marker["branch"], marker["switch_from"], marker["downgrade"]) == ("dev", "main", False)
    assert _branch(repos.local) == "main" and _head(repos.local) == old  # prepare never touches the live tree

    status = self_update.activate_prepared_update(str(repos.local), "dev", embedding_model=self_update.EMBEDDING_MODEL)
    assert status == ActivationStatus.ACTIVATED
    assert _branch(repos.local) == "dev" and _head(repos.local) == tip
    assert _git(repos.local, "config", "branch.dev.remote").stdout.strip() == "origin"
    assert _git(repos.local, "config", "branch.dev.merge").stdout.strip() == "refs/heads/dev"
    assert json.loads(Path(self_update.STATE_FILE).read_text())["branch"] == "dev"
    assert not (repos.local / "data" / ".update_in_progress.json").exists()


def test_no_policy_never_switches(repos, phase_a_env, channel_env):
    _publish_dev(repos)
    _setting(repos, "dev", "main")
    status = self_update.prepare_update(str(repos.local), "origin", "dev", reindex_fn=phase_a_env.builder,
                                        staging_parent=phase_a_env.parent, policy=None)
    assert status == PreparedStatus.SKIPPED_WRONG_BRANCH


def test_stdio_updater_leaves_a_third_branch_alone(repos, recorder, channel_env):
    _publish_dev(repos)
    _setting(repos, "dev", "main")
    _git(repos.local, "checkout", "-q", "-b", "feature")
    assert check_and_apply_update(str(repos.local), "origin", "dev", reindex_fn=recorder) == \
        UpdateStatus.SKIPPED_WRONG_BRANCH
    assert recorder.calls == [] and _branch(repos.local) == "feature"


def test_stdio_downgrade_needs_the_recorded_confirmation(repos, phase_a_env, channel_env):
    tip = _publish_dev(repos)
    _git(repos.local, "fetch", "-q", "origin", "dev")
    _git(repos.local, "checkout", "-q", "-B", "dev", tip)
    _setting(repos, "main", "dev")
    status = self_update.prepare_update(str(repos.local), "origin", "main",
                                        reindex_fn=phase_a_env.builder, staging_parent=phase_a_env.parent)
    assert status == PreparedStatus.SKIPPED_DOWNGRADE and self_update._read_prepared_marker() is None

    channel_env.setenv("AGENTS_AUTO_UPDATE_ALLOW_DOWNGRADE", f"main@{tip}")
    status = self_update.prepare_update(str(repos.local), "origin", "main",
                                        reindex_fn=phase_a_env.builder, staging_parent=phase_a_env.parent)
    assert status == PreparedStatus.PREPARED and self_update._read_prepared_marker()["downgrade"] is True
    # The confirmation was withdrawn before the next start: the build is discarded.
    channel_env.delenv("AGENTS_AUTO_UPDATE_ALLOW_DOWNGRADE")
    status = self_update.activate_prepared_update(str(repos.local), "main", embedding_model=self_update.EMBEDDING_MODEL)
    assert status == ActivationStatus.INVALID_DOWNGRADE and _branch(repos.local) == "dev"

    channel_env.setenv("AGENTS_AUTO_UPDATE_ALLOW_DOWNGRADE", f"main@{tip}")
    assert self_update.prepare_update(str(repos.local), "origin", "main", reindex_fn=phase_a_env.builder,
                                      staging_parent=phase_a_env.parent) == PreparedStatus.PREPARED
    status = self_update.activate_prepared_update(str(repos.local), "main", embedding_model=self_update.EMBEDDING_MODEL)
    assert status == ActivationStatus.ACTIVATED
    assert _branch(repos.local) == "main" and _head(repos.local) == _head(repos.upstream)
    assert _git(repos.local, "rev-parse", "dev").stdout.strip() == tip


def test_failed_store_move_after_a_switch_returns_to_the_previous_branch(repos, phase_a_env, channel_env, monkeypatch):
    _publish_dev(repos)
    old = _head(repos.local)
    _git(repos.local, "branch", "dev", old)  # an older local dev the switch moves and the rollback restores
    _setting(repos, "dev", "main")
    assert self_update.prepare_update(str(repos.local), "origin", "dev", reindex_fn=phase_a_env.builder,
                                      staging_parent=phase_a_env.parent) == PreparedStatus.PREPARED

    def failing_move(staging_dir, repo_root, stores):
        raise OSError("disk full")
    monkeypatch.setattr(self_update, "_activate_staged_stores", failing_move)
    status = self_update.activate_prepared_update(str(repos.local), "dev", embedding_model=self_update.EMBEDDING_MODEL)
    assert status == ActivationStatus.ACTIVATE_MOVE_FAILED
    assert _branch(repos.local) == "main" and _head(repos.local) == old
    assert _git(repos.local, "rev-parse", "dev").stdout.strip() == old
    assert not (repos.local / "data" / ".update_in_progress.json").exists()


def test_local_dev_with_commits_the_remote_lacks_is_not_switched_to(repos, recorder, channel_env):
    _publish_dev(repos)
    _git(repos.local, "checkout", "-q", "-b", "dev")
    _commit(repos.local, "mine.txt", "local\n", "local work on dev")
    mine = _head(repos.local)
    _git(repos.local, "checkout", "-q", "main")
    _setting(repos, "dev", "main")
    assert check_and_apply_update(str(repos.local), "origin", "dev", reindex_fn=recorder) == \
        UpdateStatus.SKIPPED_DIVERGED
    assert _git(repos.local, "rev-parse", "dev").stdout.strip() == mine and _branch(repos.local) == "main"


def test_legacy_switch_rolls_back_to_main_when_the_reindex_fails(repos, channel_env):
    _publish_dev(repos)
    _setting(repos, "dev", "main")
    old = _head(repos.local)
    status = check_and_apply_update(str(repos.local), "origin", "dev", reindex_fn=lambda root: False)
    assert status == UpdateStatus.REINDEX_FAILED
    assert _branch(repos.local) == "main" and _head(repos.local) == old
    assert _git(repos.local, "rev-parse", "--verify", "--quiet", "refs/heads/dev", check=False).returncode == 1
    status = check_and_apply_update(str(repos.local), "origin", "dev", reindex_fn=lambda root: True)
    assert status == UpdateStatus.UPDATED and _branch(repos.local) == "dev"


def test_invalid_branch_names_are_refused_before_a_switch(repos, recorder, channel_env):
    for name in ("dev..x", "it's"):
        _setting(repos, "dev", "main")
        (repos.local / ".env").write_text(f'AGENTS_AUTO_UPDATE_BRANCH="{name}"\nAGENTS_AUTO_UPDATE_PREVIOUS_BRANCH=main\n')
        assert check_and_apply_update(str(repos.local), "origin", name, reindex_fn=recorder) == \
            UpdateStatus.SKIPPED_BAD_CONFIG
    assert recorder.calls == []


# --- Review round 1 (#381) ---------------------------------------------------

def _activate(repos, branch="dev"):
    return self_update.activate_prepared_update(str(repos.local), branch, embedding_model=self_update.EMBEDDING_MODEL)


def _prepare_switch(repos, env):
    return self_update.prepare_update(str(repos.local), "origin", "dev", reindex_fn=env.builder,
                                      staging_parent=env.parent)


def test_an_exported_branch_never_switches_a_dev_installation(repos, recorder, channel_env):
    """txn-1: on dev with .env dev / previous main, an exported main (which contains HEAD) switches nothing."""
    tip = _publish_dev(repos)
    _release(repos)
    _git(repos.local, "fetch", "-q", "origin", "dev")
    _git(repos.local, "checkout", "-q", "-B", "dev", tip)
    _setting(repos, "dev", "main")
    channel_env.setenv("AGENTS_AUTO_UPDATE_BRANCH", "main")
    assert check_and_apply_update(str(repos.local), "origin", "main", reindex_fn=recorder) == \
        UpdateStatus.SKIPPED_WRONG_BRANCH
    assert _branch(repos.local) == "dev" and _head(repos.local) == tip and recorder.calls == []


def test_main_after_a_release_switches_to_dev_without_a_confirmation(repos, phase_a_env, channel_env):
    """txn-2: the release merge commit on main is no downgrade."""
    tip = _publish_dev(repos)
    merge = _release(repos)
    _git(repos.local, "pull", "-q", "--ff-only", "origin", "main")
    assert _head(repos.local) == merge
    _setting(repos, "dev", "main")
    assert _prepare_switch(repos, phase_a_env) == PreparedStatus.PREPARED
    assert self_update._read_prepared_marker()["downgrade"] is False
    assert _activate(repos) == ActivationStatus.ACTIVATED
    assert _branch(repos.local) == "dev" and _head(repos.local) == tip


@pytest.mark.parametrize("rewrite", ["another fetch", "the local HEAD"])
def test_the_updater_reads_fetch_head_once(repos, recorder, channel_env, monkeypatch, rewrite):
    """sec-1: FETCH_HEAD rewritten after the updater's first read changes neither its target nor
    the ahead/behind count: pointed at the local HEAD, a second read would count 0/0, up to date."""
    tip = _publish_dev(repos)
    old = _head(repos.local)
    _commit(repos.upstream, "file.txt", "v2\n", "main moves")
    main_tip = _head(repos.upstream)
    real = self_update._run_git
    fetch_head = repos.local / ".git" / "FETCH_HEAD"

    def racing(args, cwd, timeout):
        result = real(args, cwd, timeout)
        if any("FETCH_HEAD" in arg for arg in args) and not racing.done:
            racing.done = True  # another writer lands right after the first read
            if rewrite == "another fetch":
                _git(repos.local, "fetch", "-q", "origin", "dev")
                assert fetch_head.read_text().startswith(tip)
            else:
                fetch_head.write_text(f"{old}\t\tbranch 'main' of {repos.upstream}\n")
                assert _git(repos.local, "rev-parse", "FETCH_HEAD").stdout.strip() == old
        return result
    racing.done = False
    monkeypatch.setattr(self_update, "_run_git", racing)
    assert check_and_apply_update(str(repos.local), "origin", "main", reindex_fn=recorder) == UpdateStatus.UPDATED
    assert _head(repos.local) == main_tip


def test_a_target_branch_in_another_worktree_is_skipped_before_the_build(repos, recorder, phase_a_env,
                                                                         channel_env, tmp_path):
    """txn-4: `checkout -B dev` would fail at activation; the switch waits instead."""
    tip = _publish_dev(repos)
    _setting(repos, "dev", "main")
    _git(repos.local, "fetch", "-q", "origin", "dev")
    _git(repos.local, "worktree", "add", "-q", "-b", "dev", str(tmp_path / "wt"), tip)
    assert check_and_apply_update(str(repos.local), "origin", "dev", reindex_fn=recorder) == \
        UpdateStatus.SKIPPED_BRANCH_IN_USE
    assert _prepare_switch(repos, phase_a_env) == PreparedStatus.SKIPPED_BRANCH_IN_USE
    assert recorder.calls == [] and self_update._read_prepared_marker() is None
    _git(repos.local, "worktree", "remove", "--force", str(tmp_path / "wt"))
    _git(repos.local, "branch", "-D", "dev")
    # Checked out elsewhere after the build: activation discards it and stays on main.
    assert _prepare_switch(repos, phase_a_env) == PreparedStatus.PREPARED
    _git(repos.local, "worktree", "add", "-q", "-b", "dev", str(tmp_path / "wt2"), tip)
    assert _activate(repos) == ActivationStatus.INVALID_BRANCH_IN_USE
    assert _branch(repos.local) == "main" and self_update._read_prepared_marker() is None


def test_a_rollback_restores_the_target_branchs_upstream(repos, channel_env):
    """txn-5: the switch rewrites branch.dev.remote/merge; a rollback puts the old values back."""
    _publish_dev(repos)
    old = _head(repos.local)
    _git(repos.local, "branch", "dev", old)
    _git(repos.local, "config", "branch.dev.remote", "upstream-x")
    _git(repos.local, "config", "branch.dev.merge", "refs/heads/foo")
    _setting(repos, "dev", "main")
    assert check_and_apply_update(str(repos.local), "origin", "dev", reindex_fn=lambda root: False) == \
        UpdateStatus.REINDEX_FAILED
    assert _branch(repos.local) == "main" and _git(repos.local, "rev-parse", "dev").stdout.strip() == old
    assert _git(repos.local, "config", "branch.dev.remote").stdout.strip() == "upstream-x"
    assert _git(repos.local, "config", "branch.dev.merge").stdout.strip() == "refs/heads/foo"
    # A branch without upstream settings gets none back.
    _git(repos.local, "config", "--unset", "branch.dev.remote")
    _git(repos.local, "config", "--unset", "branch.dev.merge")
    assert check_and_apply_update(str(repos.local), "origin", "dev", reindex_fn=lambda root: False) == \
        UpdateStatus.REINDEX_FAILED
    assert _git(repos.local, "config", "--get", "branch.dev.remote", check=False).returncode == 1


def test_a_switch_is_discarded_when_head_moved_after_the_build(repos, phase_a_env, channel_env):
    """txn-6 (a) / tests-4: gate 6s, HEAD must still be the base the switch was built from."""
    _publish_dev(repos)
    _setting(repos, "dev", "main")
    assert _prepare_switch(repos, phase_a_env) == PreparedStatus.PREPARED
    (repos.local / "mine.txt").write_text("local\n")
    _git(repos.local, "add", "mine.txt")
    _git(repos.local, "commit", "-q", "-m", "local work on main")
    moved = _head(repos.local)
    assert _activate(repos) == ActivationStatus.INVALID_NOT_FF
    assert _branch(repos.local) == "main" and _head(repos.local) == moved
    assert self_update._read_prepared_marker() is None and not any(Path(phase_a_env.parent).glob("*"))


def test_a_switch_never_resets_a_local_target_branch_that_gained_commits(repos, phase_a_env, channel_env):
    """txn-6 (b) / tests-4: `checkout -B` would drop commits local dev got after the build."""
    tip = _publish_dev(repos)
    _setting(repos, "dev", "main")
    assert _prepare_switch(repos, phase_a_env) == PreparedStatus.PREPARED
    _git(repos.local, "fetch", "-q", "origin", "dev")
    tree = _git(repos.local, "rev-parse", f"{tip}^{{tree}}").stdout.strip()
    mine = _git(repos.local, "commit-tree", tree, "-p", tip, "-m", "local on dev").stdout.strip()
    _git(repos.local, "branch", "-f", "dev", mine)
    assert _activate(repos) == ActivationStatus.INVALID_NOT_FF
    assert _branch(repos.local) == "main" and _git(repos.local, "rev-parse", "dev").stdout.strip() == mine


def test_a_switch_built_from_main_does_not_activate_from_another_branch(repos, phase_a_env, channel_env):
    """tests-2: gate 3 checks the branch the switch was built from, even when the policy would allow it."""
    _publish_dev(repos)
    _setting(repos, "dev", "main")
    assert _prepare_switch(repos, phase_a_env) == PreparedStatus.PREPARED
    _git(repos.local, "checkout", "-q", "-b", "experiment")  # same commit as the build's base
    _setting(repos, "dev", "experiment")
    assert _activate(repos) == ActivationStatus.INVALID_WRONG_BRANCH
    assert _branch(repos.local) == "experiment"


# --- Review round 2 (#381) ---------------------------------------------------

@pytest.mark.parametrize("previous", [None, "feature"])
def test_a_prepared_switch_rechecks_the_policy_at_activation(repos, phase_a_env, channel_env, previous):
    """fixes-2: gate 3 asks the policy again: .env edited between prepare and activation (no previous
    setting, or another one) leaves the checkout on main, although the build came from main."""
    _publish_dev(repos)
    _setting(repos, "dev", "main")
    old = _head(repos.local)
    assert _prepare_switch(repos, phase_a_env) == PreparedStatus.PREPARED
    _setting(repos, "dev", previous)
    assert _activate(repos) == ActivationStatus.INVALID_WRONG_BRANCH
    assert _branch(repos.local) == "main" and _head(repos.local) == old
    assert self_update._read_prepared_marker() is None


# --- Dependencies: the target's Python environment (#181) ----------------------

@pytest.fixture
def venv_repos(repos, phase_a_env, tmp_path):
    """An installation with the setup's ``.venv`` and both manifests; fake venv/pip builds."""
    from tests.test_environments import PROJECT, FakeBuilder, make_environment
    base = tmp_path / "base/bin/python3"
    base.parent.mkdir(parents=True)
    base.write_text("base interpreter")
    make_environment(repos.local / ".venv", record=False, packages=("mcp",), base=base)
    (repos.upstream / "pyproject.toml").write_text(PROJECT)
    _commit(repos.upstream, "requirements.txt", "mcp==1.28.1\n", "manifests")
    _git(repos.local, "fetch", "-q", "origin")
    _git(repos.local, "merge", "-q", "--ff-only", "origin/main")
    repos.builder, repos.staged_with = FakeBuilder(), []

    def reindex(staging_dir, python=None):
        repos.staged_with.append(python)
        return phase_a_env.builder(staging_dir)
    repos.reindex = reindex
    return repos


def _env_prepare(repos, env, **kwargs):
    from src import environments
    return self_update.prepare_update(
        str(repos.local), "origin", "main", reindex_fn=repos.reindex, staging_parent=env.parent,
        live_python=environments.python_path(repos.local / ".venv"), environment_runner=repos.builder, **kwargs)


def _venv_state(repos):
    from src import environments
    return environments.state(str(repos.local))


def test_prepare_builds_the_target_environment_and_stages_with_it(venv_repos, phase_a_env):
    from src import environments
    _commit(venv_repos.upstream, "requirements.txt", "mcp==2\n", "new requirements")
    old = _head(venv_repos.local)
    assert _env_prepare(venv_repos, phase_a_env) == PreparedStatus.PREPARED
    marker = self_update._read_prepared_marker()
    environment = marker["environment"]
    assert marker["schema"] == self_update.ENVIRONMENT_MARKER_SCHEMA
    assert environment["path"] == str(venv_repos.local / ".venvs" / environment["id"])
    assert environments.complete(environment["path"], environment["requirements_sha256"])
    # The staged reindex ran on the new environment; the live tree and .venv are untouched.
    assert venv_repos.staged_with == [environments.python_path(environment["path"])]
    assert _head(venv_repos.local) == old and _venv_state(venv_repos) == {"kind": "directory"}
    assert "mcp==2" in (Path(environment["path"]) / environments.REQUIREMENTS).read_text()


@pytest.mark.parametrize("failure", ["venv", "pip"])
def test_a_failed_environment_build_changes_nothing(venv_repos, phase_a_env, failure):
    from tests.test_environments import FakeBuilder
    _commit(venv_repos.upstream, "requirements.txt", "mcp==2\n", "new requirements")
    venv_repos.builder = FakeBuilder(fail=failure)
    reasons = []
    assert _env_prepare(venv_repos, phase_a_env, reasons=reasons) == PreparedStatus.PREPARE_ENVIRONMENT_FAILED
    assert "environment failed" in reasons[0]
    assert self_update._read_prepared_marker() is None and venv_repos.staged_with == []
    assert list((venv_repos.local / ".venvs").iterdir()) == []
    assert not Path(phase_a_env.parent).exists() or not any(Path(phase_a_env.parent).iterdir())


@pytest.mark.parametrize("path, content, reason", [
    ("pyproject.toml", None, "does not satisfy requires-python"),
    ("bridge/package.json", "{}\n", "bridge"),
    ("pyproject.toml", "not valid TOML !\n", "cannot verify"),
])
def test_a_dependency_change_that_cannot_be_delivered_is_skipped_before_a_build(
        venv_repos, phase_a_env, path, content, reason):
    from tests.test_environments import PROJECT
    if content is None:
        content = PROJECT.replace('requires-python = ">=3.11"', 'requires-python = ">=3.99"')
    (venv_repos.upstream / "bridge").mkdir(exist_ok=True)
    _commit(venv_repos.upstream, path, content, "dependency change")
    reasons = []
    assert _env_prepare(venv_repos, phase_a_env, reasons=reasons) == PreparedStatus.SKIPPED_DEPENDENCIES
    assert reason in reasons[0] and venv_repos.builder.builds == [] and venv_repos.staged_with == []
    assert self_update._read_prepared_marker() is None


def test_a_lock_file_only_change_prepares_without_an_environment(venv_repos, phase_a_env):
    _commit(venv_repos.upstream, "uv.lock", "version = 1\n", "lock only")
    assert _env_prepare(venv_repos, phase_a_env) == PreparedStatus.PREPARED
    marker = self_update._read_prepared_marker()
    assert "environment" not in marker and marker["schema"] == self_update.PREPARED_MARKER_SCHEMA
    assert venv_repos.builder.builds == [] and venv_repos.staged_with == [None]


def _activate_recording_journal(repos, monkeypatch):
    journals = []
    begin = self_update._begin_update

    def recorded(repo_root, old_sha, target_sha, details=None):
        journals.append(details)
        return begin(repo_root, old_sha, target_sha, details)
    monkeypatch.setattr(self_update, "_begin_update", recorded)
    return self_update.activate_prepared_update(str(repos.local), "main"), journals


def test_activation_switches_venv_after_the_merge_and_the_store_move(venv_repos, phase_a_env, monkeypatch):
    from src import environments
    _commit(venv_repos.upstream, "requirements.txt", "mcp==2\n", "new requirements")
    assert _env_prepare(venv_repos, phase_a_env) == PreparedStatus.PREPARED
    new = self_update._read_prepared_marker()["environment"]["id"]
    status, journals = _activate_recording_journal(venv_repos, monkeypatch)
    assert status == ActivationStatus.ACTIVATED and _head(venv_repos.local) == _head(venv_repos.upstream)
    # The journal named the switch before anything changed: a rollback renames the directory back.
    planned = journals[0]["environment"]
    assert planned["to"] == new and planned["previous"]["kind"] == "directory"
    assert _venv_state(venv_repos) == {"kind": "link", "name": new}
    assert (venv_repos.local / ".venvs" / planned["previous"]["legacy"] / "pyvenv.cfg").exists()
    assert not (venv_repos.local / "data" / self_update.UPDATE_JOURNAL).exists()
    # A process started on the setup's directory re-execs through .venv, into the new environment.
    monkeypatch.setattr(environments, "RUNNING", str(venv_repos.local / ".venv"))
    assert environments.replaced(str(venv_repos.local))
    assert self_update._reexec_executable(str(venv_repos.local)) == environments.python_path(venv_repos.local / ".venv")


def test_the_next_dependency_update_replaces_the_link_and_keeps_only_the_previous_one(
        venv_repos, phase_a_env, monkeypatch):
    _commit(venv_repos.upstream, "requirements.txt", "mcp==2\n", "first")
    assert _env_prepare(venv_repos, phase_a_env) == PreparedStatus.PREPARED
    assert self_update.activate_prepared_update(str(venv_repos.local), "main") == ActivationStatus.ACTIVATED
    first = _venv_state(venv_repos)["name"]
    _commit(venv_repos.upstream, "requirements.txt", "mcp==3\n", "second")
    assert _env_prepare(venv_repos, phase_a_env) == PreparedStatus.PREPARED
    status, journals = _activate_recording_journal(venv_repos, monkeypatch)
    second = _venv_state(venv_repos)["name"]
    assert status == ActivationStatus.ACTIVATED and second != first
    assert journals[0]["environment"]["previous"] == {"kind": "link", "name": first}
    # The setup's directory, two switches old, is removed; the previous one stays for a rollback.
    assert sorted(path.name for path in (venv_repos.local / ".venvs").iterdir()) == sorted([first, second])


@pytest.mark.parametrize("failure", ["merge", "move"])
def test_a_failed_merge_or_store_move_keeps_the_previous_environment(venv_repos, phase_a_env, monkeypatch, failure):
    _commit(venv_repos.upstream, "requirements.txt", "mcp==2\n", "new requirements")
    old = _head(venv_repos.local)
    assert _env_prepare(venv_repos, phase_a_env) == PreparedStatus.PREPARED
    if failure == "merge":
        monkeypatch.setattr(self_update, "_move_to_target",
                            lambda *a: SimpleNamespace(returncode=1, stdout="", stderr="conflict"))
    else:
        monkeypatch.setattr(self_update, "_activate_staged_stores",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    status = self_update.activate_prepared_update(str(venv_repos.local), "main")
    assert status == (ActivationStatus.ACTIVATE_MERGE_FAILED if failure == "merge"
                      else ActivationStatus.ACTIVATE_MOVE_FAILED)
    assert _head(venv_repos.local) == old and _venv_state(venv_repos) == {"kind": "directory"}
    assert not (venv_repos.local / "data" / self_update.UPDATE_JOURNAL).exists()


def test_a_failed_environment_switch_restores_the_tree_the_stores_and_the_environment(
        venv_repos, phase_a_env, monkeypatch):
    from src import environments
    _commit(venv_repos.upstream, "requirements.txt", "mcp==2\n", "new requirements")
    old = _head(venv_repos.local)
    assert _env_prepare(venv_repos, phase_a_env) == PreparedStatus.PREPARED

    def interrupted(root, planned):
        # The directory is renamed, then making the link fails.
        os.rename(os.path.join(root, ".venv"), os.path.join(root, ".venvs", planned["previous"]["legacy"]))
        raise OSError("no space for the link")
    monkeypatch.setattr(environments, "switch", interrupted)
    status = self_update.activate_prepared_update(str(venv_repos.local), "main")
    assert status == ActivationStatus.ACTIVATE_ENVIRONMENT_FAILED
    assert _head(venv_repos.local) == old and _venv_state(venv_repos) == {"kind": "directory"}
    # The moved stores were made for the new code: the old code rebuilds them on load.
    assert not (venv_repos.local / "data/.skills_hash").exists()
    assert not (venv_repos.local / "data" / self_update.UPDATE_JOURNAL).exists()
    assert self_update._read_prepared_marker() is None


def test_a_resumed_activation_whose_environment_switch_fails_stops_startup(venv_repos, phase_a_env, monkeypatch):
    from src import environments
    _commit(venv_repos.upstream, "requirements.txt", "mcp==2\n", "new requirements")
    assert _env_prepare(venv_repos, phase_a_env) == PreparedStatus.PREPARED
    _git(venv_repos.local, "merge", "-q", "--ff-only", _head(venv_repos.upstream))  # an earlier run merged
    monkeypatch.setattr(environments, "switch", lambda *a: (_ for _ in ()).throw(OSError("denied")))
    status = self_update.activate_prepared_update(str(venv_repos.local), "main")
    # New code must not start on the old environment: the journal stays and blocks startup.
    assert status == ActivationStatus.ACTIVATE_ROLLBACK_FAILED
    assert (venv_repos.local / "data" / self_update.UPDATE_JOURNAL).exists()
    assert _venv_state(venv_repos) == {"kind": "directory"}


def test_a_prepared_environment_that_disappeared_is_discarded(venv_repos, phase_a_env):
    _commit(venv_repos.upstream, "requirements.txt", "mcp==2\n", "new requirements")
    old = _head(venv_repos.local)
    assert _env_prepare(venv_repos, phase_a_env) == PreparedStatus.PREPARED
    shutil.rmtree(self_update._read_prepared_marker()["environment"]["path"])
    assert self_update.activate_prepared_update(str(venv_repos.local), "main") == ActivationStatus.INVALID_ENVIRONMENT
    assert _head(venv_repos.local) == old and self_update._read_prepared_marker() is None


@pytest.mark.parametrize("schema, environment", [
    (1, {"id": "py313-" + "a" * 12, "path": "x", "requirements_sha256": "d"}),
    (2, None),
    (2, {"id": "../outside", "path": "x", "requirements_sha256": "d"}),
])
def test_a_marker_whose_schema_and_environment_disagree_is_rejected(venv_repos, phase_a_env, schema, environment):
    _commit(venv_repos.upstream, "requirements.txt", "mcp==2\n", "new requirements")
    assert _env_prepare(venv_repos, phase_a_env) == PreparedStatus.PREPARED
    marker = self_update._read_prepared_marker()
    marker["schema"] = schema
    if environment is None:
        marker.pop("environment")
    else:
        marker["environment"] = environment
    Path(self_update.PREPARED_MARKER).write_text(json.dumps(marker))
    assert self_update.activate_prepared_update(str(venv_repos.local), "main") == ActivationStatus.INVALID_MARKER


def test_the_in_place_updater_refuses_a_dependency_change(venv_repos, recorder):
    _commit(venv_repos.upstream, "requirements.txt", "mcp==2\n", "new requirements")
    old = _head(venv_repos.local)
    status = check_and_apply_update(str(venv_repos.local), "origin", "main", reindex_fn=recorder)
    assert status == UpdateStatus.SKIPPED_DEPENDENCIES
    assert _head(venv_repos.local) == old and recorder.calls == []
    assert not (venv_repos.local / "data" / self_update.UPDATE_JOURNAL).exists()
    _commit(venv_repos.upstream, "uv.lock", "version = 1\n", "lock only")  # still blocked by requirements
    assert check_and_apply_update(str(venv_repos.local), "origin", "main",
                                  reindex_fn=recorder) == UpdateStatus.SKIPPED_DEPENDENCIES


def test_the_in_place_updater_accepts_a_lock_file_only_change(venv_repos, recorder):
    _commit(venv_repos.upstream, "uv.lock", "version = 1\n", "lock only")
    assert check_and_apply_update(str(venv_repos.local), "origin", "main", reindex_fn=recorder) == UpdateStatus.UPDATED


def test_a_reexec_outside_the_installation_keeps_this_interpreter(tmp_path):
    assert self_update._reexec_executable(str(tmp_path)) == self_update.sys.executable


def test_the_default_staged_reindex_runs_the_new_environment(venv_repos, phase_a_env, monkeypatch):
    """Without an injected reindex, `python -m src.reindex` runs with the target environment's interpreter."""
    from src import environments
    _commit(venv_repos.upstream, "requirements.txt", "mcp==2\n", "new requirements")
    real, reindexed = self_update._run_command, []

    def run(args, cwd, timeout, *, env=None):
        if list(args[1:]) == ["-m", "src.reindex"]:
            reindexed.append(args[0])
            phase_a_env.builder(cwd)
            return subprocess.CompletedProcess(args, 0, "", "")
        return real(args, cwd, timeout, env=env)
    monkeypatch.setattr(self_update, "_run_command", run)
    status = self_update.prepare_update(
        str(venv_repos.local), "origin", "main", staging_parent=phase_a_env.parent,
        live_python=environments.python_path(venv_repos.local / ".venv"), environment_runner=venv_repos.builder)
    assert status == PreparedStatus.PREPARED
    assert reindexed == [environments.python_path(self_update._read_prepared_marker()["environment"]["path"])]


def test_a_reexec_uses_the_interpreter_chosen_for_it(monkeypatch):
    monkeypatch.setattr(self_update, "_reexec_executable", lambda: "/sentinel/python")
    execs = []
    monkeypatch.setattr(self_update, "_exec_server", lambda exe, argv: execs.append((exe, argv)))
    self_update._reexec_updated_server()
    assert execs == [("/sentinel/python", ["/sentinel/python", *self_update.sys.argv])]


def _merge_target(repos):
    """An earlier run fast-forwarded and died: the checkout runs the target already."""
    _git(repos.local, "merge", "-q", "--ff-only", _head(repos.upstream))


def test_a_resumed_activation_whose_store_move_fails_still_switches(venv_repos, phase_a_env, monkeypatch):
    _commit(venv_repos.upstream, "requirements.txt", "mcp==2\n", "new requirements")
    assert _env_prepare(venv_repos, phase_a_env) == PreparedStatus.PREPARED
    wanted = self_update._read_prepared_marker()["environment"]["id"]
    _merge_target(venv_repos)
    monkeypatch.setattr(self_update, "_activate_staged_stores",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    status = self_update.activate_prepared_update(str(venv_repos.local), "main")
    # The code is the target's: it keeps its environment, and the stores rebuild on load.
    assert status == ActivationStatus.ACTIVATE_MOVE_FAILED
    assert _venv_state(venv_repos) == {"kind": "link", "name": wanted}
    assert not (venv_repos.local / "data" / self_update.UPDATE_JOURNAL).exists()


def test_a_resumed_activation_refused_by_a_gate_keeps_the_journal(venv_repos, phase_a_env, caplog):
    from src.startup import assert_installation_safe
    _commit(venv_repos.upstream, "requirements.txt", "mcp==2\n", "new requirements")
    assert _env_prepare(venv_repos, phase_a_env) == PreparedStatus.PREPARED
    marker = self_update._read_prepared_marker()
    _merge_target(venv_repos)
    shutil.rmtree(marker["environment"]["path"])  # Gate 9: INVALID_ENVIRONMENT
    status = self_update.activate_prepared_update(str(venv_repos.local), "main")
    # Target code must not start on the old environment: startup stops, naming both recoveries.
    assert status == ActivationStatus.ACTIVATE_ROLLBACK_FAILED
    assert "INVALID_ENVIRONMENT" in caplog.text and marker["environment"]["id"] in caplog.text
    assert self_update._read_prepared_marker() == marker and _venv_state(venv_repos) == {"kind": "directory"}
    # The journal names the revision to go back to, and so does the log, with what .venv lacks.
    journal = json.loads((venv_repos.local / "data" / self_update.UPDATE_JOURNAL).read_text())
    assert journal["old_sha"] == marker["base_sha"] and marker["base_sha"][:9] in caplog.text
    assert "lacks mcp 1.0 (needs ==2), python-dotenv (not installed)" in caplog.text
    with pytest.raises(SystemExit, match=f"switches .venv to .venvs/{marker['environment']['id']}"):
        assert_installation_safe(str(venv_repos.local))


def _install(repos, *names):
    """Packages installed by hand into the setup's .venv."""
    from tests.test_environments import make_environment
    make_environment(repos.local / ".venv", record=False, packages=names,
                     base=Path((repos.local / ".venv/pyvenv.cfg").read_text().split("executable = ")[1].strip()))


def test_a_resumed_activation_on_a_venv_with_every_package_continues(venv_repos, phase_a_env, caplog):
    """The owner pulled and installed the dependencies by hand: nothing to hold, the build is discarded."""
    _commit(venv_repos.upstream, "requirements.txt", "mcp==2\n", "new requirements")
    assert _env_prepare(venv_repos, phase_a_env) == PreparedStatus.PREPARED
    marker = self_update._read_prepared_marker()
    _merge_target(venv_repos)
    _install(venv_repos, ("mcp", "2"), "python-dotenv", "httpx")
    shutil.rmtree(marker["environment"]["path"])  # Gate 9: INVALID_ENVIRONMENT
    status = self_update.activate_prepared_update(str(venv_repos.local), "main")
    assert status == ActivationStatus.INVALID_ENVIRONMENT and "satisfies every requirement it names" in caplog.text
    assert self_update._read_prepared_marker() is None
    assert not (venv_repos.local / "data" / self_update.UPDATE_JOURNAL).exists()


def test_a_resumed_activation_whose_switch_cannot_be_planned_holds(venv_repos, phase_a_env, monkeypatch, caplog):
    from src import environments
    _commit(venv_repos.upstream, "requirements.txt", "mcp==2\n", "new requirements")
    assert _env_prepare(venv_repos, phase_a_env) == PreparedStatus.PREPARED
    _merge_target(venv_repos)
    monkeypatch.setattr(environments, "plan", lambda *a, **k: (_ for _ in ()).throw(environments.Refused("no")))
    status = self_update.activate_prepared_update(str(venv_repos.local), "main")
    assert status == ActivationStatus.ACTIVATE_ROLLBACK_FAILED and "its switch cannot be planned" in caplog.text
    assert (venv_repos.local / "data" / self_update.UPDATE_JOURNAL).exists()


def test_a_resumed_activation_whose_move_and_switch_fail_holds_on_the_restored_venv(
        venv_repos, phase_a_env, monkeypatch):
    from src import environments
    _commit(venv_repos.upstream, "requirements.txt", "mcp==2\n", "new requirements")
    assert _env_prepare(venv_repos, phase_a_env) == PreparedStatus.PREPARED
    _merge_target(venv_repos)
    monkeypatch.setattr(self_update, "_activate_staged_stores",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))

    def interrupted(root, planned):
        os.rename(os.path.join(root, ".venv"), os.path.join(root, ".venvs", planned["previous"]["legacy"]))
        raise OSError("no space for the link")
    monkeypatch.setattr(environments, "switch", interrupted)
    status = self_update.activate_prepared_update(str(venv_repos.local), "main")
    assert status == ActivationStatus.ACTIVATE_ROLLBACK_FAILED
    journal = json.loads((venv_repos.local / "data" / self_update.UPDATE_JOURNAL).read_text())
    # A resumed activation journals the revision to go back to, not the target it already runs.
    assert journal["old_sha"] == self_update._read_prepared_marker()["base_sha"] != journal["target_sha"]
    assert _venv_state(venv_repos) == {"kind": "directory"} and (venv_repos.local / ".venv/pyvenv.cfg").exists()


@pytest.mark.parametrize("installed, held", [("1.28.1", True), ("1.31.0", False)])
def test_a_version_bump_holds_until_venv_has_the_new_version(venv_repos, phase_a_env, installed, held, caplog):
    """mcp==1.28.1 -> mcp==1.31.0: .venv with the old version must not serve the target."""
    _commit(venv_repos.upstream, "requirements.txt", "mcp==1.31.0\n", "bump mcp")
    assert _env_prepare(venv_repos, phase_a_env) == PreparedStatus.PREPARED
    marker = self_update._read_prepared_marker()
    _merge_target(venv_repos)
    _install(venv_repos, ("mcp", installed), "python-dotenv")
    shutil.rmtree(marker["environment"]["path"])  # Gate 9: INVALID_ENVIRONMENT
    status = self_update.activate_prepared_update(str(venv_repos.local), "main")
    if held:
        assert status == ActivationStatus.ACTIVATE_ROLLBACK_FAILED and "mcp 1.28.1 (needs ==1.31.0)" in caplog.text
    else:
        assert status == ActivationStatus.INVALID_ENVIRONMENT
    assert (venv_repos.local / "data" / self_update.UPDATE_JOURNAL).exists() == held


def test_a_malformed_base_in_a_held_marker_keeps_the_recovery_message(venv_repos, phase_a_env, caplog):
    _commit(venv_repos.upstream, "requirements.txt", "mcp==2\n", "new requirements")
    assert _env_prepare(venv_repos, phase_a_env) == PreparedStatus.PREPARED
    marker = self_update._read_prepared_marker()
    marker["base_sha"] = 12345  # Gate 1: INVALID_MARKER
    Path(self_update.PREPARED_MARKER).write_text(json.dumps(marker))
    _merge_target(venv_repos)
    status = self_update.activate_prepared_update(str(venv_repos.local), "main")
    assert status == ActivationStatus.ACTIVATE_ROLLBACK_FAILED
    assert "restore the previous revision (unknown)" in caplog.text and "INVALID_MARKER" in caplog.text
    journal = json.loads((venv_repos.local / "data" / self_update.UPDATE_JOURNAL).read_text())
    assert journal["old_sha"] == journal["target_sha"] == marker["target_sha"]


def _every_package(repos):
    _install(repos, ("mcp", "2"), "python-dotenv", "httpx")


def test_an_unplannable_switch_on_a_venv_with_every_package_is_discarded(venv_repos, phase_a_env, monkeypatch):
    from src import environments
    _commit(venv_repos.upstream, "requirements.txt", "mcp==2\n", "new requirements")
    assert _env_prepare(venv_repos, phase_a_env) == PreparedStatus.PREPARED
    _merge_target(venv_repos)
    _every_package(venv_repos)
    monkeypatch.setattr(environments, "plan", lambda *a, **k: (_ for _ in ()).throw(environments.Refused("no")))
    status = self_update.activate_prepared_update(str(venv_repos.local), "main")
    assert status == ActivationStatus.INVALID_ENVIRONMENT and self_update._read_prepared_marker() is None
    assert not (venv_repos.local / "data" / self_update.UPDATE_JOURNAL).exists()


def test_a_resumed_move_and_switch_failure_on_a_venv_with_every_package_finishes(venv_repos, phase_a_env, monkeypatch):
    from src import environments
    _commit(venv_repos.upstream, "requirements.txt", "mcp==2\n", "new requirements")
    assert _env_prepare(venv_repos, phase_a_env) == PreparedStatus.PREPARED
    _merge_target(venv_repos)
    _every_package(venv_repos)
    monkeypatch.setattr(self_update, "_activate_staged_stores",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("disk full")))
    monkeypatch.setattr(environments, "switch", lambda *a: (_ for _ in ()).throw(OSError("no space")))
    status = self_update.activate_prepared_update(str(venv_repos.local), "main")
    assert status == ActivationStatus.ACTIVATE_MOVE_FAILED and _venv_state(venv_repos) == {"kind": "directory"}
    assert not (venv_repos.local / "data" / self_update.UPDATE_JOURNAL).exists()


def test_a_resumed_switch_failure_on_a_venv_with_every_package_finishes(venv_repos, phase_a_env, monkeypatch):
    from src import environments
    _commit(venv_repos.upstream, "requirements.txt", "mcp==2\n", "new requirements")
    assert _env_prepare(venv_repos, phase_a_env) == PreparedStatus.PREPARED
    _merge_target(venv_repos)
    _every_package(venv_repos)
    monkeypatch.setattr(environments, "switch", lambda *a: (_ for _ in ()).throw(OSError("no space")))
    status = self_update.activate_prepared_update(str(venv_repos.local), "main")
    assert status == ActivationStatus.ACTIVATE_ENVIRONMENT_FAILED and _venv_state(venv_repos) == {"kind": "directory"}
    assert not (venv_repos.local / "data" / self_update.UPDATE_JOURNAL).exists()
    assert self_update._read_prepared_marker() is None


def test_the_in_place_refusal_logs_its_reason(venv_repos, recorder, caplog):
    _commit(venv_repos.upstream, "requirements.txt", "mcp==2\n", "new requirements")
    assert check_and_apply_update(str(venv_repos.local), "origin", "main",
                                  reindex_fn=recorder) == UpdateStatus.SKIPPED_DEPENDENCIES
    assert "changed: requirements.txt" in caplog.text and "AGENTS_AUTO_UPDATE_STAGING=0" in caplog.text


def test_a_start_whose_environment_was_switched_under_it_reexecs(tmp_path, monkeypatch):
    """A resumed activation that switched .venv but failed its store move still re-execs onto .venv."""
    from src import environments
    marker = tmp_path / ".prepared_update.json"
    marker.write_text("{}")
    for name, value in [("AUTO_UPDATE_ENABLED", True), ("AUTO_UPDATE_STAGING", True),
                        ("PREPARED_MARKER", str(marker)), ("STAGING_ROOT", str(tmp_path / "staging")),
                        ("LOCK_FILE", str(tmp_path / ".update.lock"))]:
        monkeypatch.setattr(self_update, name, value)
    monkeypatch.setattr(self_update, "activate_prepared_update", lambda *a, **k: ActivationStatus.ACTIVATE_MOVE_FAILED)
    execs = []
    monkeypatch.setattr(self_update, "_reexec_updated_server", lambda: execs.append(1))
    monkeypatch.setattr(environments, "replaced", lambda root: False)
    self_update.run_activation_safely()
    assert execs == []
    monkeypatch.setattr(environments, "replaced", lambda root: True)
    self_update.run_activation_safely()
    assert execs == [1]
