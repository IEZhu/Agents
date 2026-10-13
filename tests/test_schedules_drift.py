"""The drift check of the runner's code against pinned (#338, #339).

``pinned`` comes from the trusted repository (tests/test_schedules_repos.py); here a bare copy of
the installation's history stands in for it. The installation is compared by content only.
"""
import os
from pathlib import Path
import subprocess

import pytest

from src.schedules import drift


def git(root, *args, check=True):
    return subprocess.run(["git", "-C", str(root), *args], check=check, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def installation(tmp_path):
    root = tmp_path / "installation"
    for relative in ("src/schedules/runner.py", "agents/a/system_prompt.mdc", "rules/rule.mdc", "docs/x.md"):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text("original\n")
    git(tmp_path, "init", "--quiet", "-b", "main", str(root))
    git(root, "-c", "user.name=T", "-c", "user.email=t@example.com", "add", ".")
    git(root, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "--quiet", "-m", "init")
    return root


@pytest.fixture
def trusted(tmp_path, installation):
    """A bare copy of the installation's history, standing in for the trusted repository."""
    path = tmp_path / "trusted.git"
    git(tmp_path, "clone", "--quiet", "--bare", str(installation), str(path))
    return path


def test_matching_runner_has_no_drift(installation, trusted):
    pinned = git(installation, "rev-parse", "HEAD")
    assert drift.check(installation, pinned, ["src/schedules/runner.py", *drift.TREES], repository=trusted) == []


def test_changed_module_untracked_component_and_moved_pin_are_drift(installation, trusted):
    pinned = git(installation, "rev-parse", "HEAD")
    paths = ["src/schedules/runner.py", *drift.TREES]
    (installation / "src/schedules/runner.py").write_text("changed\n")
    (installation / "rules/planted.mdc").write_text("new\n")
    (installation / "docs/x.md").write_text("docs are not the runner\n")
    (installation / "agents/a/system_prompt.mdc").unlink()
    assert drift.check(installation, pinned, paths, repository=trusted) == [
        "agents/a/system_prompt.mdc", "rules/planted.mdc", "src/schedules/runner.py"]


def test_main_ahead_of_the_installation_is_drift(installation):
    git(installation, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "--quiet",
        "--allow-empty", "-m", "local")
    (installation / "agents/a/system_prompt.mdc").write_text("next\n")
    git(installation, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "--quiet", "-am", "next")
    pinned = git(installation, "rev-parse", "HEAD")
    git(installation, "reset", "--quiet", "--hard", "HEAD~1")
    assert drift.check(installation, pinned, list(drift.TREES), repository=installation) == [
        "agents/a/system_prompt.mdc"]


def test_pinned_missing_from_the_installation_is_compared_by_content(installation, trusted, tmp_path):
    """The installation need not have pinned: a docs-only commit on main is no drift (#339 F6)."""
    work = tmp_path / "work"
    git(tmp_path, "clone", "--quiet", str(trusted), str(work))
    (work / "docs/x.md").write_text("docs only\n")
    git(work, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "--quiet", "-am", "docs")
    git(work, "push", "--quiet", "origin", "HEAD:main")
    pinned = git(trusted, "rev-parse", "main")
    assert git(installation, "cat-file", "-t", pinned, check=False) == ""  # unknown to the installation
    assert drift.check(installation, pinned, ["src/schedules/runner.py", *drift.TREES], repository=trusted) == []


def test_pinned_unknown_to_the_trusted_repository_is_a_check_failure(installation, trusted):
    unknown = "0123456789abcdef0123456789abcdef01234567"
    with pytest.raises(drift.DriftCheckFailed, match="trusted repository"):
        drift.check(installation, unknown, list(drift.TREES), repository=trusted)


@pytest.mark.parametrize("flag", ["--skip-worktree", "--assume-unchanged"])
def test_index_flags_do_not_hide_a_changed_file(installation, trusted, flag):
    pinned = git(installation, "rev-parse", "HEAD")
    git(installation, "update-index", flag, "rules/rule.mdc")
    (installation / "rules/rule.mdc").write_text("changed behind the index\n")
    assert git(installation, "status", "--porcelain") == ""  # git itself no longer sees it
    assert drift.check(installation, pinned, list(drift.TREES), repository=trusted) == ["rules/rule.mdc"]


def test_ignored_dot_file_and_local_configuration_do_not_hide_drift(installation, trusted):
    pinned = git(installation, "rev-parse", "HEAD")
    (installation / ".gitignore").write_text(".*\n")
    (installation / "agents/a/.hidden.mdc").write_text("planted\n")
    (installation / "agents/a/.DS_Store").write_text("finder\n")
    with (installation / ".git" / "config").open("a") as config:
        config.write("[core]\n\tfsmonitor = /bin/false\n\tfilemode = false\n")
    (installation / "skills").mkdir()
    os.symlink("../rules/rule.mdc", installation / "skills" / "link")
    assert drift.check(installation, pinned, list(drift.TREES), repository=trusted) == [
        "agents/a/.hidden.mdc", "skills/link"]


def test_mode_change_is_drift(installation, trusted):
    pinned = git(installation, "rev-parse", "HEAD")
    (installation / "rules/rule.mdc").chmod(0o755)
    assert drift.check(installation, pinned, list(drift.TREES), repository=trusted) == ["rules/rule.mdc"]


def test_imported_modules_of_this_checkout_are_checked():
    root = Path(__file__).resolve().parents[1]
    files = drift.imported_files(root)
    assert "src/schedules/drift.py" in files and all(not name.startswith(".venv") for name in files)


def test_git_failure_is_reported(tmp_path):
    with pytest.raises(drift.DriftCheckFailed):
        drift.check(tmp_path, "a" * 40, list(drift.TREES), repository=tmp_path)
    with pytest.raises(drift.DriftCheckFailed, match="40-character"):
        drift.check(tmp_path, "origin/main", list(drift.TREES), repository=tmp_path)
