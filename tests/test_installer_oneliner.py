"""Exercise install.sh and init_repo.sh's --yes helpers against disposable directories."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(sys.platform == "win32" or not BASH, reason="requires bash on Unix")


def git(*args, cwd=None):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def upstream(tmp_path):
    """A local repo whose scripts/init_repo.sh only records its arguments."""
    repo = tmp_path / "upstream"
    (repo / "scripts").mkdir(parents=True)
    stub = repo / "scripts/init_repo.sh"
    stub.write_text('#!/usr/bin/env bash\necho "init:$*" > "$(dirname "$0")/../ran.txt"\n')
    stub.chmod(0o755)
    git("init", "-q", "-b", "main", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def run_install(tmp_path, upstream, *args, extra_env=None, path=None):
    env = dict(os.environ, HOME=str(tmp_path / "home"), AGENTS_REPO_URL=str(upstream),
               AGENTS_ASSUME_YES="1")
    env.pop("AGENTS_HOME", None)
    env.update(extra_env or {})
    if path is not None:
        env["PATH"] = path
    (tmp_path / "home").mkdir(exist_ok=True)
    return subprocess.run([BASH, str(ROOT / "install.sh"), *args], env=env,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)


def test_fresh_clone_runs_init_with_yes(tmp_path, upstream):
    result = run_install(tmp_path, upstream, "--skip-index")
    assert result.returncode == 0, result.stderr
    ran = tmp_path / "home/.agents-core/ran.txt"
    assert ran.read_text().strip() == "init:--yes --skip-index"


def test_agents_home_override(tmp_path, upstream):
    target = tmp_path / "custom"
    result = run_install(tmp_path, upstream, extra_env={"AGENTS_HOME": str(target)})
    assert result.returncode == 0, result.stderr
    assert (target / "ran.txt").exists()


def test_update_fast_forwards(tmp_path, upstream):
    assert run_install(tmp_path, upstream).returncode == 0
    (upstream / "new.txt").write_text("x")
    git("add", "-A", cwd=upstream)
    git("commit", "-q", "-m", "more", cwd=upstream)
    result = run_install(tmp_path, upstream)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "home/.agents-core/new.txt").exists()


def test_dirty_checkout_is_refused(tmp_path, upstream):
    assert run_install(tmp_path, upstream).returncode == 0
    (tmp_path / "home/.agents-core/scripts/init_repo.sh").write_text("dirty")
    result = run_install(tmp_path, upstream)
    assert result.returncode != 0
    assert "local changes" in result.stderr


def test_non_checkout_directory_is_refused(tmp_path, upstream):
    target = tmp_path / "home/.agents-core"
    target.mkdir(parents=True)
    (target / "file").write_text("x")
    result = run_install(tmp_path, upstream)
    assert result.returncode != 0
    assert "not an Agents-Core checkout" in result.stderr


def test_missing_git_fails_clearly(tmp_path, upstream):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    result = run_install(tmp_path, upstream, path=str(empty))
    assert result.returncode != 0
    assert "git is required" in result.stderr


@pytest.mark.parametrize("gb_kb,expected", [(64 * 1024 * 1024, "1"), (16 * 1024 * 1024, "2"),
                                             (8 * 1024 * 1024, "3"), (16 * 1024 * 1024 - 400000, "2"), (32 * 1024 * 1024 - 800000, "1")])
def test_ram_to_model_choice(tmp_path, gb_kb, expected):
    source = (ROOT / "scripts/init_repo.sh").read_text()
    start = source.index("detect_default_model_choice() {")
    body = source[start:source.index("\n}\n", start) + 3]
    meminfo = tmp_path / "meminfo"
    meminfo.write_text(f"MemTotal:       {gb_kb} kB\n")
    body = body.replace("/proc/meminfo", str(meminfo))
    out = subprocess.run([BASH, "-c", body + "\ndetect_default_model_choice"],
                         capture_output=True, text=True, check=True).stdout.strip()
    assert out == expected


def test_init_repo_documents_yes_flag():
    source = (ROOT / "scripts/init_repo.sh").read_text()
    assert "--yes|-y)" in source and "AGENTS_ASSUME_YES" in source
    assert source.count('if [ "$ASSUME_YES" = true ]; then REPLY=y') == 2
