"""Exercise install.sh and init_repo.sh's --yes helpers against disposable directories."""
import json
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
    # A new session has no controlling terminal: install.sh cannot prompt on /dev/tty, even when
    # the test runs in a developer's terminal and a test leaves AGENTS_ASSUME_YES out.
    return subprocess.run([BASH, str(ROOT / "install.sh"), *args], env=env, capture_output=True, text=True,
                          stdin=subprocess.DEVNULL, start_new_session=True, timeout=120)


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


TERMINAL = r"""
import json, os, pty, select, sys, time
argv, typed, timeout = json.loads(sys.argv[1]), sys.argv[2].encode(), float(sys.argv[3])
pid, controller = pty.fork()
if pid == 0:  # the child: nothing but exec
    os.execv(argv[0], argv)
os.write(controller, typed)
output, deadline = b"", time.monotonic() + timeout
while time.monotonic() < deadline:
    if select.select([controller], [], [], 0.1)[0]:
        try:
            chunk = os.read(controller, 65536)
        except OSError:  # EIO: the child's side closed (Linux)
            chunk = b""
        output += chunk
        if not chunk:
            break
else:
    os.kill(pid, 9)
os.close(controller)
status = os.waitpid(pid, 0)[1]
print(json.dumps({"code": os.waitstatus_to_exitcode(status), "output": output.decode(errors="replace")}))
"""


def run_with_terminal(argv, env, typed: bytes, timeout=60):
    """Run ``argv`` as the leader of a new session whose controlling terminal is a pseudo-terminal,
    as a user's shell would: ``/dev/tty`` works, and ``typed`` waits in the terminal's input. A fresh,
    single-threaded Python forks it: forking this multi-threaded test process could deadlock."""
    result = subprocess.run([sys.executable, "-c", TERMINAL, json.dumps(argv), typed.decode(), str(timeout)],
                            env=env, capture_output=True, text=True, timeout=timeout + 30, start_new_session=True)
    answer = json.loads(result.stdout)
    return answer["code"], answer["output"]


def library_git(checkout):
    """A personal library with its own .git, as sync (#173) or the user leaves it; its files' bytes."""
    library = checkout / "flows/.user"
    library.mkdir(parents=True)
    git("init", "-q", "-b", "main", cwd=library)
    (library / "notes.md").write_text("mine")
    git("add", "-A", cwd=library)
    git("commit", "-q", "-m", "library", cwd=library)
    return lambda: {str(path.relative_to(library)): path.read_bytes()
                    for path in sorted((library / ".git").rglob("*")) if path.is_file()}


TOKEN = "ghp_OneLinerToken0123456789abcdef"
# Blanked so that the user's own never reach a test; AGENTS_ASSUME_YES stays, or install.sh would ask.
SYNC_VARIABLES = ("AGENTS_USER_SYNC_REPO", "AGENTS_USER_SYNC_REMOTE", "AGENTS_USER_SYNC_NAME",
                  "AGENTS_USER_SYNC_EMAIL", "AGENTS_USER_SYNC_LABEL", "AGENTS_GITHUB_TOKEN")


@pytest.fixture
def sync_upstream(tmp_path):
    """A repository whose init_repo.sh records its arguments and whether it got the GitHub token,
    then runs the real sync section of scripts/init_repo.sh with the real step (a copy of src)."""
    from tests.test_installer_sync import unix_section
    repo = tmp_path / "sync-upstream"
    shutil.copytree(ROOT / "src", repo / "src", ignore=shutil.ignore_patterns("__pycache__"))
    (repo / "scripts").mkdir()
    stub = repo / "scripts/init_repo.sh"
    stub.write_text('#!/usr/bin/env bash\nhere="${0%/*}"\n'
                    'echo "init:$* token:${AGENTS_GITHUB_TOKEN:-none}" > "$here/../ran.txt"\n'
                    'REPO_ROOT="$(cd "$here/.." && pwd)"\nPYTHON_ABS="$TEST_PYTHON"\n'
                    + unix_section((ROOT / "scripts/init_repo.sh").read_text(encoding="utf-8")))
    stub.chmod(0o755)
    git("init", "-q", "-b", "main", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def test_an_update_from_a_terminal_with_sync_variables_leaves_the_library_git_byte_for_byte(tmp_path, sync_upstream):
    """install.sh asks only its own confirmation; init_repo.sh --yes runs the real sync step, whose
    setup from the environment refuses the library's own .git and leaves it as it was (#171)."""
    sync_env = {name: "" for name in SYNC_VARIABLES}
    sync_env.update(TEST_PYTHON=sys.executable, XDG_STATE_HOME=str(tmp_path / "state"),
                    AGENTS_SERVICE_DIR=str(tmp_path / "service"))
    assert run_install(tmp_path, sync_upstream, extra_env=sync_env).returncode == 0
    checkout = tmp_path / "home/.agents-core"
    snapshot = library_git(checkout)
    before = snapshot()
    (sync_upstream / "new.txt").write_text("x")
    git("add", "-A", cwd=sync_upstream)
    git("commit", "-q", "-m", "more", cwd=sync_upstream)
    env = {key: value for key, value in os.environ.items()
           if key not in ("AGENTS_HOME", "GIT_DIR", "GIT_WORK_TREE", "PYTHONPATH")}
    env.update(sync_env, AGENTS_ASSUME_YES="", HOME=str(tmp_path / "home"), AGENTS_REPO_URL=str(sync_upstream),
               AGENTS_USER_SYNC_REMOTE="git@git.example.com:me/library.git", AGENTS_USER_SYNC_NAME="Owner",
               AGENTS_USER_SYNC_EMAIL="owner@example.com", AGENTS_GITHUB_TOKEN=TOKEN)
    code, output = run_with_terminal([BASH, str(ROOT / "install.sh")], env, b"y\n")
    assert code == 0, output
    assert "Proceed with these defaults? [Y/n]" in output and (checkout / "new.txt").exists()
    assert (checkout / "ran.txt").read_text().strip() == f"init:--yes token:{TOKEN}"
    assert "Setting up sync between machines from AGENTS_USER_SYNC_REMOTE" in output
    assert "Sync was not set up" in output and "REACHED THE END" in output
    assert snapshot() == before
    assert not (tmp_path / "state").exists() and not (tmp_path / "service").exists()


@pytest.mark.parametrize("exported_copy", [False, True], ids=["plain", "caller-exports-the-copy-name"])
def test_install_sh_hands_the_github_token_to_init_repo_only_never_to_git(tmp_path, upstream, exported_copy):
    """Also when the caller happens to export install.sh's own name for the copy: a local inherits
    the export attribute, and without `export -n` its children would get the token under that name.
    Dropped, they see the caller's own value, as before."""
    events = tmp_path / "git-events.txt"
    wrappers = tmp_path / "wrappers"
    wrappers.mkdir()
    wrapper = wrappers / "git"
    wrapper.write_text(f'#!/bin/sh\necho "git ${{AGENTS_GITHUB_TOKEN:-none}} ${{sync_github_token:-none}}" >> "{events}"\n'
                       f'exec {shutil.which("git")} "$@"\n')
    wrapper.chmod(0o755)
    stub = upstream / "scripts/init_repo.sh"
    stub.write_text('#!/usr/bin/env bash\necho "init:$* token:${AGENTS_GITHUB_TOKEN:-none} '
                    'copy:${sync_github_token:-none}" > "${0%/*}/../ran.txt"\n')
    git("add", "-A", cwd=upstream)
    git("commit", "-q", "-m", "stub", cwd=upstream)
    path = str(wrappers) + os.pathsep + os.environ["PATH"]
    extra = {**{name: "" for name in SYNC_VARIABLES}, "AGENTS_GITHUB_TOKEN": TOKEN}
    if exported_copy:
        extra["sync_github_token"] = "the caller's own value"
    copy = "the caller's own value" if exported_copy else "none"
    for attempt in ("clone", "update"):
        result = run_install(tmp_path, upstream, path=path, extra_env=extra)
        assert result.returncode == 0, result.stderr
        ran = (tmp_path / "home/.agents-core/ran.txt").read_text().strip()
        assert ran == f"init:--yes token:{TOKEN} copy:{copy}", ran
    lines = events.read_text().splitlines()
    assert lines and set(lines) == {f"git none {copy}"}, lines
    assert TOKEN not in result.stdout + result.stderr


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
