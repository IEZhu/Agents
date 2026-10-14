"""Repository steps of the run sequence (#347): a session repository holding the run's branch, the
quarantine of a blocked branch, the patch and bundle export, merge bases and flow blob IDs.

Real throwaway repositories under ``tmp_path``, as in test_schedules_repos; no network.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
import stat
import subprocess

import pytest

from src.schedules import repos
from src.schedules.repos import RepositoryError
from tests.test_schedules_paths import fixture_environment, fixture_git
from tests.test_schedules_repos import (BRANCH, RUN_ID, bundle, commit_all, exchange, github, refs,  # noqa: F401
                                        session, trusted, work, write)  # exchange, github, trusted: fixtures


def imported(trusted, tmp_path, exchange, files=None, name="session"):
    """A session's commits on the run's branch, imported into the trusted repository."""
    one_off = session(trusted, tmp_path, name)
    work(one_off.path, files or {"docs/new.md": "new\n", "src/app.py": "print('app')\n"})
    made = bundle(one_off.path, exchange, BRANCH, f"^{one_off.pinned}")
    result = trusted.import_bundle(made, exchange, BRANCH, RUN_ID, one_off.pinned)
    made.unlink()
    return one_off, result


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- a session repository with the run's branch --------------------------------------------------

def test_a_session_repository_can_hold_the_runs_branch(trusted, tmp_path, exchange):
    first, result = imported(trusted, tmp_path, exchange)
    second = trusted.create_session_repository(tmp_path / "submit", BRANCH)
    assert second.pinned == trusted.pinned() == first.pinned and second.head == result.head
    assert fixture_git(second.path, "rev-parse", "HEAD") == result.head
    assert fixture_git(second.path, "symbolic-ref", "--short", "HEAD") == BRANCH
    assert (second.path / "docs" / "new.md").read_text() == "new\n"
    assert fixture_git(second.path, "remote") == ""
    upstream = subprocess.run(["git", "-C", str(second.path), "config", "--get", f"branch.{BRANCH}.remote"],
                              env=fixture_environment(), capture_output=True, text=True)
    assert upstream.returncode == 1  # no upstream of any name
    assert fixture_git(second.path, "config", "--local", "user.email") == repos.SESSION_EMAIL
    plain = trusted.create_session_repository(tmp_path / "select")
    assert plain.head is None and fixture_git(plain.path, "rev-parse", "HEAD") == plain.pinned


def test_a_branch_the_trusted_repository_lacks_is_refused(trusted, tmp_path):
    with pytest.raises(RepositoryError, match="has no imported"):
        trusted.create_session_repository(tmp_path / "submit", "claude/autodev-339-missing")
    assert not (tmp_path / "submit").exists()
    with pytest.raises(ValueError, match="not an autodev branch"):
        trusted.create_session_repository(tmp_path / "submit", "main")


def test_the_branch_after_main_moved(trusted, tmp_path, exchange, github):
    first, result = imported(trusted, tmp_path, exchange)
    write(github, {"README.md": "# moved\n"})
    moved = commit_all(github)
    assert trusted.fetch() == moved
    later = trusted.create_session_repository(tmp_path / "resume", BRANCH)
    assert later.pinned == moved and later.head == result.head
    assert trusted.merge_base(moved, result.head) == first.pinned


# --- quarantine and export -----------------------------------------------------------------------

def test_quarantine_branch_moves_the_branch_out_of_refs_heads(trusted, tmp_path, exchange):
    _, result = imported(trusted, tmp_path, exchange)
    ref = trusted.quarantine_branch(BRANCH, RUN_ID)
    assert ref == f"refs/autodev/quarantine/{RUN_ID}/final"
    listed = refs(trusted.path)
    assert listed[ref] == result.head and f"refs/heads/{BRANCH}" not in listed
    with pytest.raises(RepositoryError, match="has no"):
        trusted.quarantine_branch(BRANCH, RUN_ID)
    with pytest.raises(ValueError, match="not a run ID"):
        trusted.quarantine_branch(BRANCH, "today")


def test_export_writes_a_patch_and_a_verified_bundle_of_the_imported_commits(trusted, tmp_path, exchange, github):
    first, result = imported(trusted, tmp_path, exchange)
    run_dir = tmp_path / "runs" / RUN_ID
    run_dir.mkdir(parents=True)
    exported = trusted.export_commits(first.pinned, result.head, run_dir / repos.EXPORT_DIR, RUN_ID)
    patch, made = run_dir / "export" / f"{RUN_ID}.patch", run_dir / "export" / f"{RUN_ID}.bundle"
    assert (exported["patch"], exported["bundle"]) == (str(patch), str(made))
    assert exported["commits"] == list(result.commits) and exported["ref"] == f"refs/autodev/export/{RUN_ID}"
    assert mode(patch) == mode(made) == 0o400 and mode(run_dir / "export") == 0o700
    assert exported["sha256"] == {"patch": hashlib.sha256(patch.read_bytes()).hexdigest(),
                                  "bundle": hashlib.sha256(made.read_bytes()).hexdigest()}
    assert not any(name.startswith("refs/autodev/export/") for name in refs(trusted.path))
    # The owner's side: a clone of main at base verifies the bundle, fetches it, and the patch applies.
    owner = tmp_path / "owner"
    fixture_git(tmp_path, "clone", "--quiet", str(github), str(owner))
    assert fixture_git(owner, "rev-parse", "HEAD") == first.pinned
    fixture_git(owner, "bundle", "verify", "--quiet", str(made))
    fixture_git(owner, "fetch", "--quiet", str(made), f"{exported['ref']}:refs/heads/rescued")
    assert fixture_git(owner, "rev-parse", "refs/heads/rescued") == result.head
    fixture_git(owner, "apply", "--check", str(patch))
    fixture_git(owner, "am", "--quiet", str(patch))
    assert fixture_git(owner, "rev-parse", "HEAD^{tree}") == fixture_git(owner, "rev-parse", f"{result.head}^{{tree}}")


def test_export_after_quarantine_and_of_several_commits(trusted, tmp_path, exchange):
    first = session(trusted, tmp_path)
    work(first.path, {"a.txt": "a\n"})
    write(first.path, {"b.txt": "b\n"})
    head = commit_all(first.path, "second")
    made = bundle(first.path, exchange, BRANCH, f"^{first.pinned}")
    trusted.import_bundle(made, exchange, BRANCH, RUN_ID, first.pinned)
    trusted.quarantine_branch(BRANCH, RUN_ID)
    exported = trusted.export_commits(first.pinned, head, tmp_path / "export", "339-repos")
    assert len(exported["commits"]) == 2 and exported["commits"][-1] == head
    assert Path(exported["patch"]).read_text().count("\nSubject: [PATCH") == 2


@pytest.mark.parametrize("problem", ["same", "unrelated", "unknown", "exists", "stem", "inside"])
def test_export_refuses(trusted, tmp_path, exchange, problem):
    first, result = imported(trusted, tmp_path, exchange)
    base, head, destination, stem = first.pinned, result.head, tmp_path / "export", RUN_ID
    if problem == "same":
        head = base
    elif problem == "unrelated":
        base, head = head, base
    elif problem == "unknown":
        head = "f" * 40
    elif problem == "exists":
        destination.mkdir()
        (destination / f"{RUN_ID}.bundle").write_text("")
    elif problem == "stem":
        stem = "../escape"
    elif problem == "inside":
        destination = trusted.path / "export"
    with pytest.raises((RepositoryError, ValueError)):
        trusted.export_commits(base, head, destination, stem)
    if problem != "exists":
        assert not (destination / f"{stem}.patch").exists()


# --- reads -------------------------------------------------------------------------------------------

def test_merge_base_without_a_common_ancestor(trusted, tmp_path, github):
    fixture_git(github, "switch", "--quiet", "--orphan", "other")
    write(github, {"other.txt": "other\n"})
    orphan = commit_all(github, "orphan")
    fixture_git(trusted.path, "fetch", "--quiet", str(github), "refs/heads/other:refs/heads/other")
    with pytest.raises(RepositoryError, match="no common ancestor"):
        trusted.merge_base(trusted.pinned(), orphan)
    with pytest.raises(ValueError):
        trusted.merge_base("main", orphan)


def test_blob_ids_read_the_flows_at_a_commit_of_main(trusted):
    pinned = trusted.pinned()
    found = trusted.blob_ids(pinned, ["README.md", "src/schedules/runner.py", "absent.md", "src"])
    assert found["README.md"] == fixture_git(trusted.path, "rev-parse", f"{pinned}:README.md")
    runner = fixture_git(trusted.path, "rev-parse", f"{pinned}:src/schedules/runner.py")
    assert found["src/schedules/runner.py"] == runner
    assert found["absent.md"] is None and found["src"] is None


def test_blob_ids_refuse_a_session_head(trusted, tmp_path, exchange):
    _, result = imported(trusted, tmp_path, exchange)
    with pytest.raises(RepositoryError, match="not a commit of main"):
        trusted.blob_ids(result.head, ["README.md"])


def test_branch_head(trusted, tmp_path, exchange):
    assert trusted.branch_head(BRANCH) is None
    _, result = imported(trusted, tmp_path, exchange)
    assert trusted.branch_head(BRANCH) == result.head
