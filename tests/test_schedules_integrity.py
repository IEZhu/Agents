"""The post-session .git check (src/schedules/integrity.py, #347) on real one-off repositories.

Each test makes a one-off repository the way a run does (`TrustedRepository.create_session_repository`
from a throwaway "GitHub" repository), takes a snapshot, changes .git as a session might, and
compares. No network.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil

import pytest

from src.schedules import integrity
from tests.test_schedules_paths import fixture_git
from tests.test_schedules_repos import BRANCH, commit_all, github, trusted, write  # noqa: F401 (fixtures)


@pytest.fixture
def one_off(trusted, tmp_path):
    return trusted.create_session_repository(tmp_path / "session").path


def changes(repository: Path, change) -> list[str]:
    before = integrity.git_snapshot(repository)
    change(repository)
    return integrity.git_changes(before, integrity.git_snapshot(repository))


def normal_work(repository: Path) -> None:
    fixture_git(repository, "switch", "--quiet", "--no-track", "-c", BRANCH)
    write(repository, {"src/app.py": "print('app')\n"})
    commit_all(repository, "work")
    fixture_git(repository, "bundle", "create", "--quiet", str(repository.parent / "handover.bundle"), BRANCH)
    fixture_git(repository, "gc", "--quiet")


def test_normal_session_work_changes_nothing_watched(one_off):
    assert changes(one_off, normal_work) == []


def test_a_snapshot_is_plain_json(one_off):
    snapshot = integrity.git_snapshot(one_off)
    assert json.loads(json.dumps(snapshot)) == snapshot
    assert snapshot["git"]["kind"] == "directory" and snapshot["config"]["kind"] == "file"
    assert snapshot["hooks"] == {"kind": "missing"}  # clone --template= makes no hooks directory
    assert {name: entry["kind"] for name, entry in snapshot["watched"].items()} == {
        "info/grafts": "missing", "objects/info/alternates": "missing", "commondir": "missing"}


def test_a_changed_config_is_detected(one_off):
    assert changes(one_off, lambda repo: fixture_git(repo, "config", "--local", "core.fsmonitor", "touch /tmp/x")) == \
        [".git/config changed"]


@pytest.mark.parametrize("existing", [False, True])
def test_an_added_hook_is_detected(one_off, existing):
    if existing:
        (one_off / ".git" / "hooks").mkdir()
        (one_off / ".git" / "hooks" / "post-checkout").write_text("#!/bin/sh\n")

    def plant(repo):
        (repo / ".git" / "hooks").mkdir(exist_ok=True)
        hook = repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\ncurl example.org\n")
        hook.chmod(0o755)
    found = changes(one_off, plant)
    assert found == ([".git/hooks changed: added pre-commit"] if existing else
                     [".git/hooks changed (missing -> directory)"])


def test_a_changed_hook_mode_or_content_is_detected(one_off):
    hooks = one_off / ".git" / "hooks"
    hooks.mkdir()
    (hooks / "pre-push").write_text("#!/bin/sh\n")
    assert changes(one_off, lambda repo: (repo / ".git" / "hooks" / "pre-push").chmod(0o755)) == \
        [".git/hooks changed: changed pre-push"]


def test_renamed_away_changed_and_restored_is_detected(one_off):
    def rename(repo):
        os.rename(repo / ".git", repo / ".g")
        with open(repo / ".g" / "config", "a") as stream:
            stream.write("[core]\n\thooksPath = /tmp/hooks\n")
        os.rename(repo / ".g", repo / ".git")
    assert changes(one_off, rename) == [".git/config changed"]


def test_a_replaced_directory_with_the_same_content_is_detected(one_off):
    def swap(repo):
        shutil.copytree(repo / ".git", repo / ".g2", symlinks=True, copy_function=shutil.copy2)
        os.rename(repo / ".git", repo / ".old")
        os.rename(repo / ".g2", repo / ".git")
    assert changes(one_off, swap) == [".git is another directory (it was replaced)"]


def test_a_redirector_file_is_detected(one_off, tmp_path):
    def redirect(repo):
        os.rename(repo / ".git", tmp_path / "elsewhere.git")
        (repo / ".git").write_text(f"gitdir: {tmp_path / 'elsewhere.git'}\n")
    before = integrity.git_snapshot(one_off)
    redirect(one_off)
    after = integrity.git_snapshot(one_off)
    assert integrity.git_changes(before, after) == [".git is a file (a gitdir redirector) now"]
    assert after["git"]["kind"] == "file" and len(after["git"]["sha256"]) == 64


def test_a_symlinked_git_directory_is_detected_and_not_followed(one_off, tmp_path):
    def link(repo):
        os.rename(repo / ".git", tmp_path / "moved.git")
        (repo / ".git").symlink_to(tmp_path / "moved.git")
    before = integrity.git_snapshot(one_off)
    link(one_off)
    after = integrity.git_snapshot(one_off)
    assert integrity.git_changes(before, after) == [".git is a symbolic link now"]
    assert set(after) == {"git"}  # nothing behind the link was read


@pytest.mark.parametrize("name", integrity.WATCHED)
def test_a_redirect_file_appearing_is_detected(one_off, name):
    def add(repo):
        path = repo / ".git" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("/elsewhere\n")
    assert changes(one_off, add) == [f".git/{name} appeared"]


def test_a_directory_on_the_way_replaced_by_a_link_is_detected(one_off, tmp_path):
    def link_objects_info(repo):
        target = tmp_path / "planted-info"
        target.mkdir()
        (target / "alternates").write_text("/elsewhere/objects\n")
        shutil.rmtree(repo / ".git" / "objects" / "info")
        (repo / ".git" / "objects" / "info").symlink_to(target)
    assert changes(one_off, link_objects_info) == [
        ".git/objects/info is not a directory now (on the way to objects/info/alternates)"]


def test_a_config_link_is_recorded_and_never_followed(one_off, tmp_path):
    outside = tmp_path / "outside-config"
    outside.write_text("[core]\n")

    def link(repo):
        os.unlink(repo / ".git" / "config")
        (repo / ".git" / "config").symlink_to(outside)
    before = integrity.git_snapshot(one_off)
    link(one_off)
    after = integrity.git_snapshot(one_off)
    assert integrity.git_changes(before, after) == [".git/config changed"]
    assert after["config"]["kind"] == "link" and "sha256" not in after["config"]


def test_a_removed_git_directory_and_the_reason(one_off):
    found = changes(one_off, lambda repo: shutil.rmtree(repo / ".git"))
    assert found == [".git is gone now"]
    assert integrity.reason(found) == "sandbox: the one-off repository's .git changed (.git is gone now)"


# --- edges the snapshots alone show (#347 review, coverage) ---------------------------------------

def test_the_same_inode_with_another_birth_time_is_a_replaced_directory(one_off):
    """A replaced .git that reuses the inode number still has another birth time."""
    before = integrity.git_snapshot(one_off)
    after = json.loads(json.dumps(before))
    after["git"]["birth"] = (before["git"]["birth"] or 0) + 1
    assert after["git"]["inode"] == before["git"]["inode"]
    assert integrity.git_changes(before, after) == [".git is another directory (it was replaced)"]


def test_a_hook_beyond_the_listed_ones_is_still_detected(one_off, monkeypatch):
    monkeypatch.setattr(integrity, "MAX_HOOKS", 2)
    hooks = one_off / ".git" / "hooks"
    hooks.mkdir()
    for name in ("a-hook", "b-hook"):
        (hooks / name).write_text("#!/bin/sh\n")

    def plant(repo):
        (repo / ".git" / "hooks" / "z-hook").write_text("#!/bin/sh\ncurl example.org\n")
    found = changes(one_off, plant)
    assert found == [".git/hooks changed: 3 entries"]
