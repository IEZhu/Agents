"""The drift check of the runner's code against pinned (#338)."""
from pathlib import Path
import subprocess

import pytest

from src.schedules import drift


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def installation(tmp_path):
    root = tmp_path / "installation"
    for relative in ("src/schedules/runner.py", "agents/a/system_prompt.mdc", "rules/rule.mdc", "docs/x.md"):
        (root / relative).parent.mkdir(parents=True, exist_ok=True)
        (root / relative).write_text("original\n")
    git(tmp_path, "init", "--quiet", "-b", "main", str(root))
    git(root, "-c", "user.name=T", "-c", "user.email=t@example.com", "add", ".")
    git(root, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "--quiet", "-m", "init")
    git(root, "update-ref", "refs/remotes/origin/main", "HEAD")
    return root


def test_matching_runner_has_no_drift(installation):
    pinned = drift.pinned(installation)
    assert pinned == git(installation, "rev-parse", "HEAD")
    assert drift.check(installation, pinned, ["src/schedules/runner.py", *drift.TREES]) == []


def test_changed_module_untracked_component_and_moved_pin_are_drift(installation):
    pinned = drift.pinned(installation)
    paths = ["src/schedules/runner.py", *drift.TREES]
    (installation / "src/schedules/runner.py").write_text("changed\n")
    (installation / "rules/planted.mdc").write_text("new\n")
    (installation / "docs/x.md").write_text("docs are not the runner\n")
    assert drift.check(installation, pinned, paths) == ["rules/planted.mdc", "src/schedules/runner.py"]


def test_main_ahead_of_the_installation_is_drift(installation):
    git(installation, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "--quiet",
        "--allow-empty", "-m", "local")
    (installation / "agents/a/system_prompt.mdc").write_text("next\n")
    git(installation, "-c", "user.name=T", "-c", "user.email=t@example.com", "commit", "--quiet", "-am", "next")
    git(installation, "update-ref", "refs/remotes/origin/main", "HEAD")
    git(installation, "reset", "--quiet", "--hard", "HEAD~1")
    assert drift.check(installation, drift.pinned(installation), list(drift.TREES)) == ["agents/a/system_prompt.mdc"]


def test_imported_modules_of_this_checkout_are_checked():
    root = Path(__file__).resolve().parents[1]
    files = drift.imported_files(root)
    assert "src/schedules/drift.py" in files and all(not name.startswith(".venv") for name in files)


def test_git_failure_is_reported(tmp_path):
    with pytest.raises(drift.DriftCheckFailed):
        drift.pinned(tmp_path)
