"""Exercise scripts/setup_cloud_env.sh against a disposable upstream and HOME.

The upstream carries the real install.sh and src/client_paths.py, a stub
init_repo.sh and a small fake MCP server. The stub registers that server the way
init_repo.sh registers Agents-Core and creates a ``.venv/bin/python`` that records
each call: it runs the script's verification program with the test interpreter
and succeeds for the model download and indexing. The tests cover the script's
own steps (checkout, .env seeding, git excludes, verification, failure handling)
without installing dependencies or downloading a model.
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
printf 'python:%s\\n' "$(printf '%s' "$*" | tr '\\n' ' ')" >> "$(dirname "$0")/../../calls.txt"
if [ "$1" = "-" ]; then exec "$REAL_PYTHON" "$@"; fi
if [ "$1" = "-c" ] && [ -n "${STUB_FAIL_MODEL:-}" ]; then exit 1; fi
if [ "$1 $2" = "-m src.reindex" ] && [ -n "${STUB_FAIL_REINDEX:-}" ]; then exit 1; fi
exit 0
EOF
chmod +x "$root/.venv/bin/python"
if [ -n "${CLAUDE_CONFIG_DIR:-}" ]; then
    config="$CLAUDE_CONFIG_DIR/.claude.json"; instructions="$CLAUDE_CONFIG_DIR/CLAUDE.md"
else
    config="$HOME/.claude.json"; instructions="$HOME/.claude/CLAUDE.md"
fi
if [ -z "${STUB_SKIP_REGISTER:-}" ]; then
    printf '{"mcpServers": {"Agents-Core": {"command": "%s", "args": ["%s/src/server.py"]}}}' \\
        "$REAL_PYTHON" "$root" > "$config"
fi
if [ -z "${STUB_SKIP_INSTRUCTIONS:-}" ]; then
    echo "# >>> Agents-Core Routing Protocol (managed by init_repo) >>>" > "$instructions"
fi
"""
FAKE_SERVER = '''import json
import os

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Agents-Core")


@mcp.tool()
def route_and_load(query: str, protocol_version: int = 2) -> str:
    return json.dumps({"status": os.environ.get("FAKE_ROUTE_STATUS", "ROUTE_REQUIRED"),
                       "message": "fake"})


@mcp.tool()
def get_agent_context(agent_name: str, query: str) -> str:
    return "{}"


@mcp.tool()
def log_interaction(agent_name: str, query: str, response_content: str) -> str:
    return "{}"


@mcp.tool()
def load_implants(query: str = "", limit: int = 5) -> str:
    return os.environ.get("FAKE_IMPLANTS", "## Dynamic Implants (Contextually Loaded)\\n")


mcp.run()
'''
BEGIN = "# >>> Agents-Core repository memory (scripts/setup_cloud_env.sh) >>>"
END = "# <<< Agents-Core repository memory <<<"
BALANCED = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
# Every file Agents-Core can leave in a client repository (src/memory/).
MEMORY_FILES = ["history.md", ".history.md.lock", "history.md.rotating", "history/2026-10.md",
                "history/2026-10.md.tmp", "data/memory/.describe_hash", ".agents-description.lock",
                ".CLAUDE.md.lock", ".managed_section.abc123.tmp"]


def git(*args, cwd=None):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                    "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null", *args],
                   cwd=cwd, check=True, capture_output=True)


@pytest.fixture
def upstream(tmp_path):
    repo = tmp_path / "upstream"
    (repo / "scripts").mkdir(parents=True)
    (repo / "src").mkdir()
    shutil.copy(ROOT / "install.sh", repo / "install.sh")
    shutil.copy(ROOT / "src/client_paths.py", repo / "src/client_paths.py")
    (repo / "src/__init__.py").write_text("")
    (repo / "src/server.py").write_text(FAKE_SERVER)
    stub = repo / "scripts/init_repo.sh"
    stub.write_text(STUB_INIT)
    stub.chmod(0o755)
    (repo / ".gitignore").write_text(".env\n.venv/\nran.txt\ncalls.txt\n__pycache__/\n")
    git("init", "-q", "-b", "main", cwd=repo)
    git("add", "-A", cwd=repo)
    git("commit", "-q", "-m", "init", cwd=repo)
    return repo


def setup_env(tmp_path, upstream, extra_env=None):
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    env = dict(os.environ, HOME=str(home), AGENTS_REPO_URL=str(upstream), REAL_PYTHON=sys.executable)
    for key in list(env):
        if key.startswith(("AGENTS_HOME", "AGENTS_BRANCH", "AGENTS_EMBEDDING_MODEL", "STUB_", "FAKE_")):
            del env[key]
    for key in ("XDG_CONFIG_HOME", "GIT_CONFIG_GLOBAL", "CLAUDE_CONFIG_DIR"):
        env.pop(key, None)
    env.update(extra_env or {})
    return env


def run_setup(tmp_path, upstream, extra_env=None):
    return subprocess.run([BASH, str(ROOT / "scripts/setup_cloud_env.sh")],
                          env=setup_env(tmp_path, upstream, extra_env), cwd=tmp_path,
                          capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=120)


def env_values(checkout):
    lines = (checkout / ".env").read_text().splitlines()
    return dict(line.split("=", 1) for line in lines if "=" in line)


def calls(checkout):
    return (checkout / "calls.txt").read_text().splitlines()


def test_fresh_setup_seeds_env_and_verifies(tmp_path, upstream):
    result = run_setup(tmp_path, upstream)
    assert result.returncode == 0, result.stderr
    checkout = tmp_path / "home/.agents-core"
    assert (checkout / "ran.txt").read_text().strip() == "init:--yes --skip-index"
    assert env_values(checkout) == {"EMBEDDING_MODEL": BALANCED, "AGENTS_AUTO_UPDATE": "0"}
    assert (tmp_path / "home/.claude").is_dir()
    recorded = calls(checkout)
    assert recorded[0].startswith("python:-c ") and "embed_texts" in recorded[0]
    assert recorded[1:] == ["python:-m src.reindex", f"python:- {checkout}"]
    assert "Server OK: 4 tools, route_and_load -> ROUTE_REQUIRED, load_implants -> implants" in result.stdout
    ignore = (tmp_path / "home/.config/git/ignore").read_text().splitlines()
    assert ignore[0] == BEGIN and ignore[-1] == END


@pytest.mark.parametrize("extra_env, message", [
    ({"STUB_SKIP_REGISTER": "1"}, "Agents-Core is not registered for"),
    ({"STUB_SKIP_INSTRUCTIONS": "1"}, "has no Agents-Core routing section"),
    ({"FAKE_ROUTE_STATUS": "ERROR"}, "route_and_load returned ERROR"),
    ({"FAKE_IMPLANTS": "Error loading implants: no model"}, "load_implants returned: Error loading implants"),
], ids=["no-registration", "no-instructions", "route-error", "implants-error"])
def test_verification_failures_fail_setup(tmp_path, upstream, extra_env, message):
    result = run_setup(tmp_path, upstream, extra_env)
    assert result.returncode != 0
    assert message in result.stderr


def test_registration_for_another_checkout_fails(tmp_path, upstream):
    assert run_setup(tmp_path, upstream).returncode == 0
    config = tmp_path / "home/.claude.json"
    config.write_text(config.read_text().replace("/.agents-core/src/", "/elsewhere/src/"))
    result = run_setup(tmp_path, upstream, {"STUB_SKIP_REGISTER": "1"})
    assert result.returncode != 0
    assert "Agents-Core is not registered for" in result.stderr


def test_excludes_hide_only_memory_files(tmp_path, upstream):
    assert run_setup(tmp_path, upstream).returncode == 0
    repo = tmp_path / "client"
    for name in MEMORY_FILES + ["notes.md", "history/notes.md", "data/keep.json", "sub/history.md"]:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text("x")
    git("init", "-q", cwd=repo)
    status = subprocess.run(["git", "status", "--porcelain", "-uall"], cwd=repo, check=True,
                            capture_output=True, text=True,
                            env=dict(os.environ, HOME=str(tmp_path / "home"), GIT_CONFIG_NOSYSTEM="1")).stdout
    assert sorted(line[3:] for line in status.splitlines()) == [
        "data/keep.json", "history/notes.md", "notes.md", "sub/history.md"]


def test_rerun_keeps_edits_and_replaces_excludes_block(tmp_path, upstream):
    assert run_setup(tmp_path, upstream).returncode == 0
    ignore = tmp_path / "home/.config/git/ignore"
    ignore.write_text("*.swp\n" + ignore.read_text() + "*.bak")
    checkout = tmp_path / "home/.agents-core"
    env_file = checkout / ".env"
    env_file.write_text(env_file.read_text().replace(BALANCED, "intfloat/multilingual-e5-large"))
    result = run_setup(tmp_path, upstream, {"AGENTS_EMBEDDING_MODEL": "other/model"})
    assert result.returncode == 0, result.stderr
    assert env_values(checkout)["EMBEDDING_MODEL"] == "intfloat/multilingual-e5-large"
    lines = ignore.read_text().splitlines()
    assert lines.count(BEGIN) == 1
    assert lines[:2] == ["*.swp", "*.bak"]


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
    assert custom.read_text().splitlines()[0] == BEGIN
    assert not (home / ".config/git/ignore").exists()


def test_model_download_failure_fails_setup(tmp_path, upstream):
    result = run_setup(tmp_path, upstream, {"STUB_FAIL_MODEL": "1"})
    assert result.returncode != 0
    assert "huggingface.co" in result.stderr
    recorded = calls(tmp_path / "home/.agents-core")
    assert len(recorded) == 1 and recorded[0].startswith("python:-c ")


def test_index_failure_fails_setup(tmp_path, upstream):
    result = run_setup(tmp_path, upstream, {"STUB_FAIL_REINDEX": "1"})
    assert result.returncode != 0
    assert "indexing failed" in result.stderr
    assert calls(tmp_path / "home/.agents-core")[-1] == "python:-m src.reindex"


def test_relative_agents_home_is_made_absolute(tmp_path, upstream):
    result = run_setup(tmp_path, upstream, {"AGENTS_HOME": "rel/core"})
    assert result.returncode == 0, result.stderr
    checkout = tmp_path / "rel/core"
    assert calls(checkout)[-1] == f"python:- {checkout}"


def test_claude_config_dir_is_used(tmp_path, upstream):
    profile = tmp_path / "profile"
    result = run_setup(tmp_path, upstream, {"CLAUDE_CONFIG_DIR": str(profile)})
    assert result.returncode == 0, result.stderr
    assert (profile / ".claude.json").is_file()
    assert not (tmp_path / "home/.claude").exists()


def test_empty_directory_is_cloned_into(tmp_path, upstream):
    (tmp_path / "home/.agents-core").mkdir(parents=True)
    result = run_setup(tmp_path, upstream)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "home/.agents-core/ran.txt").exists()


@pytest.mark.parametrize("git_repo", [False, True], ids=["plain-directory", "unrelated-repo"])
def test_non_checkout_directory_is_refused(tmp_path, upstream, git_repo):
    target = tmp_path / "home/.agents-core"
    target.mkdir(parents=True)
    (target / "file").write_text("x")
    if git_repo:
        git("init", "-q", cwd=target)
    result = run_setup(tmp_path, upstream)
    assert result.returncode != 0
    assert "is not an Agents-Core checkout" in result.stderr
    assert not (target / ".env").exists()


def test_truncated_download_runs_nothing(tmp_path, upstream):
    script = (ROOT / "scripts/setup_cloud_env.sh").read_text()
    cut = script[:script.index('\nmain "$@"')]
    result = subprocess.run([BASH], input=cut, env=setup_env(tmp_path, upstream), cwd=tmp_path,
                            capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "home/.agents-core").exists()
