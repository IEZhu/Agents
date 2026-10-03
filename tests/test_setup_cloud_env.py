"""Exercise scripts/setup_cloud_env.sh against a disposable upstream and HOME.

The upstream carries the real install.sh and a stub init_repo.sh whose
``.venv/bin/python`` records each call, so the test covers the script's own
steps (checkout, .env seeding, git excludes, failure handling) without
installing dependencies or downloading a model.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
BASH = shutil.which("bash")
pytestmark = pytest.mark.skipif(sys.platform == "win32" or not BASH, reason="requires bash on Unix")

STUB_INIT = """#!/usr/bin/env bash
root="$(cd "$(dirname "$0")/.." && pwd)"
echo "init:$*" > "$root/ran.txt"
mkdir -p "$root/.venv/bin"
cat > "$root/.venv/bin/python" <<'EOF'
#!/usr/bin/env bash
echo "python:$*" >> "$(dirname "$0")/../../calls.txt"
cat > /dev/null
if [ "$1 $2" = "-m src.reindex" ] && [ -n "${STUB_FAIL_REINDEX:-}" ]; then exit 1; fi
exit 0
EOF
chmod +x "$root/.venv/bin/python"
"""
MARKER = "# Agents-Core repository memory (scripts/setup_cloud_env.sh)"
BALANCED = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"


def git(*args, cwd=None):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def upstream(tmp_path):
    repo = tmp_path / "upstream"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(ROOT / "install.sh", repo / "install.sh")
    stub = repo / "scripts/init_repo.sh"
    stub.write_text(STUB_INIT)
    stub.chmod(0o755)
    (repo / ".gitignore").write_text(".env\n.venv/\nran.txt\ncalls.txt\n")
    git("init", "-q", "-b", "main", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def run_setup(tmp_path, upstream, extra_env=None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ, HOME=str(home), AGENTS_REPO_URL=str(upstream))
    for key in ("AGENTS_HOME", "AGENTS_BRANCH", "AGENTS_EMBEDDING_MODEL", "XDG_CONFIG_HOME",
                "GIT_CONFIG_GLOBAL", "STUB_FAIL_REINDEX"):
        env.pop(key, None)
    env.update(extra_env or {})
    return subprocess.run([BASH, str(ROOT / "scripts/setup_cloud_env.sh")], env=env,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL)


def env_values(checkout):
    lines = (checkout / ".env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def test_fresh_setup_seeds_env_and_verifies(tmp_path, upstream):
    result = run_setup(tmp_path, upstream)
    assert result.returncode == 0, result.stderr
    checkout = tmp_path / "home/.agents-core"
    assert (checkout / "ran.txt").read_text().strip() == "init:--yes --skip-index"
    assert env_values(checkout) == {"EMBEDDING_MODEL": BALANCED, "AGENTS_AUTO_UPDATE": "0"}
    assert (tmp_path / "home/.claude").is_dir()
    calls = (checkout / "calls.txt").read_text().splitlines()
    assert calls[0] == "python:-m src.reindex"
    assert calls[1].startswith(f"python:- {checkout} ")
    ignore = (tmp_path / "home/.config/git/ignore").read_text()
    assert ignore.splitlines()[0] == MARKER
    assert "/history.md" in ignore.splitlines()


def test_rerun_keeps_edits_and_adds_excludes_once(tmp_path, upstream):
    assert run_setup(tmp_path, upstream).returncode == 0
    checkout = tmp_path / "home/.agents-core"
    env_file = checkout / ".env"
    env_file.write_text(env_file.read_text().replace(BALANCED, "intfloat/multilingual-e5-large"))
    result = run_setup(tmp_path, upstream, {"AGENTS_EMBEDDING_MODEL": "other/model"})
    assert result.returncode == 0, result.stderr
    assert env_values(checkout)["EMBEDDING_MODEL"] == "intfloat/multilingual-e5-large"
    assert (tmp_path / "home/.config/git/ignore").read_text().count(MARKER) == 1


def test_model_override_for_new_env(tmp_path, upstream):
    result = run_setup(tmp_path, upstream, {"AGENTS_EMBEDDING_MODEL": "intfloat/multilingual-e5-large"})
    assert result.returncode == 0, result.stderr
    assert env_values(tmp_path / "home/.agents-core")["EMBEDDING_MODEL"] == "intfloat/multilingual-e5-large"


def test_configured_excludes_file_is_used(tmp_path, upstream):
    home = tmp_path / "home"
    home.mkdir()
    custom = tmp_path / "custom-ignore"
    (home / ".gitconfig").write_text(f"[core]\n\texcludesFile = {custom}\n")
    assert run_setup(tmp_path, upstream).returncode == 0
    assert custom.read_text().splitlines()[0] == MARKER
    assert not (home / ".config/git/ignore").exists()


def test_index_failure_fails_setup(tmp_path, upstream):
    result = run_setup(tmp_path, upstream, {"STUB_FAIL_REINDEX": "1"})
    assert result.returncode != 0
    assert "huggingface.co" in result.stderr
    calls = (tmp_path / "home/.agents-core/calls.txt").read_text().splitlines()
    assert calls == ["python:-m src.reindex"]
