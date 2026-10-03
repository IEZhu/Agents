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


def test_untracked_file_survives_update(tmp_path, upstream):
    assert run_install(tmp_path, upstream).returncode == 0
    checkout = tmp_path / "home/.agents-core"
    (checkout / "notes.txt").write_text("mine")
    (upstream / "new.txt").write_text("x")
    git("add", "-A", cwd=upstream)
    git("commit", "-q", "-m", "more", cwd=upstream)
    result = run_install(tmp_path, upstream)
    assert result.returncode == 0, result.stderr
    assert (checkout / "new.txt").exists()
    assert (checkout / "notes.txt").read_text() == "mine"


def test_non_checkout_directory_is_refused(tmp_path, upstream):
    target = tmp_path / "home/.agents-core"
    target.mkdir(parents=True)
    (target / "file").write_text("x")
    result = run_install(tmp_path, upstream)
    assert result.returncode != 0
    assert "not an Agents-Core checkout" in result.stderr


def test_unrelated_git_repo_is_refused_early(tmp_path, upstream):
    target = tmp_path / "home/.agents-core"
    target.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=target)
    result = run_install(tmp_path, upstream)
    assert result.returncode != 0
    assert "not an Agents-Core checkout" in result.stderr
    assert "Agents-Core installer" not in result.stdout


def test_inherited_git_dir_is_ignored(tmp_path, upstream):
    other = tmp_path / "other"
    other.mkdir()
    git("init", "-q", "-b", "main", cwd=other)
    result = run_install(tmp_path, upstream, extra_env={"GIT_DIR": str(other / ".git")})
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "home/.agents-core/ran.txt").exists()


def test_missing_git_fails_clearly(tmp_path, upstream):
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    result = run_install(tmp_path, upstream, path=str(empty))
    assert result.returncode != 0
    assert "git is required" in result.stderr


def test_init_repo_installs_one_embedding_model_without_asking():
    for name in ("init_repo.sh", "init_repo.bat"):
        source = (ROOT / "scripts" / name).read_text()
        assert "detect_default_model_choice" not in source and "MODEL_CHOICE" not in source
        assert "all-MiniLM-L6-v2" not in source and "multilingual-e5-large" not in source
        assert source.count("microsoft/harrier-oss-v1-270m") == 1
        assert "src.model_migration" in source  # an older .env moves to it on rerun


def test_init_repo_documents_yes_flag():
    source = (ROOT / "scripts/init_repo.sh").read_text()
    assert "--yes|-y)" in source and "AGENTS_ASSUME_YES" in source
    assert source.count('if [ "$ASSUME_YES" = true ]; then REPLY=y') == 2


ENV_EXAMPLE = ("# comment\nAGENTS_AUTO_UPDATE=1\nEMBEDDING_MODEL=default/model\nQUOTED=default\n"
               "NEW_KEY=z\n# COMMENTED=1\n")
CUSTOM_ENV = "export AGENTS_AUTO_UPDATE=0\n  EMBEDDING_MODEL = custom/model\n'QUOTED'=mine\n"


@pytest.mark.parametrize("ending", ["\n", ""], ids=["terminated", "unterminated"])
def test_env_merge_keeps_dotenv_assignment_forms(tmp_path, ending):
    source = (ROOT / "scripts/init_repo.sh").read_text()
    start = source.index("merge_missing_env_keys() {")
    body = source[start:source.index("\n}\n", start) + 3]
    env_file, example = tmp_path / ".env", tmp_path / "env.example"
    env_file.write_text(CUSTOM_ENV.rstrip("\n") + ending)
    example.write_text(ENV_EXAMPLE)
    script = body + 'MISSING_KEYS=()\nmerge_missing_env_keys "$1" "$2"\necho "${MISSING_KEYS[*]}"\n'
    result = subprocess.run([BASH, "-c", script, "bash", str(env_file), str(example)],
                            capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "NEW_KEY"
    assert env_file.read_text() == CUSTOM_ENV + "NEW_KEY=z\n"


def test_windows_env_merge_keeps_dotenv_assignment_forms(tmp_path):
    env_file, example = tmp_path / ".env", tmp_path / "env.example"
    env_file.write_text(CUSTOM_ENV)
    example.write_text(ENV_EXAMPLE)
    subprocess.run([sys.executable, str(ROOT / "scripts/_helpers/merge_env.py"), str(env_file), str(example)],
                   capture_output=True, text=True, check=True)
    assert env_file.read_text() == CUSTOM_ENV + "NEW_KEY=z\n"
