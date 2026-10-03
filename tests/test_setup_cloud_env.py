"""Exercise scripts/setup_cloud_env.sh against a disposable upstream and HOME.

The upstream carries the real install.sh, src/client_paths.py,
scripts/_helpers/inject_claude_md.py and the routing template, a stub
init_repo.sh and a small fake MCP server. The stub registers that server the way
init_repo.sh registers Agents-Core, writes ~/.claude/CLAUDE.md with the real
inject_claude_md.py, and creates a ``.venv/bin/python`` that records each call:
it runs the script's verification program with the test interpreter and succeeds
for the model download and indexing. The tests cover the script's own steps
(checkout, .env seeding, git excludes, verification, failure handling) without
installing dependencies or downloading a model.
"""
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
case "${STUB_INSTRUCTIONS:-}" in
    skip) ;;
    truncate) echo "# >>> Agents-Core Routing Protocol (managed by init_repo) >>>" > "$instructions" ;;
    *)
        "$REAL_PYTHON" "$root/scripts/_helpers/inject_claude_md.py" "$instructions" \\
            "$root/scripts/templates/routing-protocol-core.md" > /dev/null
        case "${STUB_INSTRUCTIONS:-}" in
            duplicate) cat "$instructions" "$instructions" > "$instructions.2" && mv "$instructions.2" "$instructions" ;;
            legacy) printf '%s\\nold\\n%s\\n' "# >>> Agents-Core Routing Protocol (managed by init_repo.sh) >>>" \\
                "# <<< Agents-Core Routing Protocol (managed by init_repo.sh) <<<" >> "$instructions" ;;
        esac ;;
esac
"""
FAKE_SERVER = '''import json
import os
import time
from typing import Optional

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("Agents-Core")


def answer():
    if os.environ.get("FAKE_HANG"):
        time.sleep(60)
    return json.dumps({"protocol_version": int(os.environ.get("FAKE_PROTOCOL_VERSION", "2")),
                       "status": os.environ.get("FAKE_ROUTE_STATUS", "ROUTE_REQUIRED"), "message": "fake"})


if os.environ.get("FAKE_OLD_SCHEMA") == "route":
    @mcp.tool()
    def route_and_load(query: str) -> str:
        return answer()
else:
    @mcp.tool()
    def route_and_load(query: str, protocol_version: int = 2, current_persona: Optional[dict] = None) -> str:
        return answer()


if os.environ.get("FAKE_OLD_SCHEMA") == "context":
    @mcp.tool()
    def get_agent_context(agent_name: str, query: str) -> str:
        return "{}"
else:
    @mcp.tool()
    def get_agent_context(agent_name: str, query: str, protocol_version: int = 2,
                          current_persona: Optional[dict] = None) -> str:
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


def clean_env(**overrides):
    """The caller's environment without git, XDG, client or script settings."""
    env = {key: value for key, value in os.environ.items()
           if not key.startswith(("GIT_", "AGENTS_", "STUB_", "FAKE_"))
           and key not in ("XDG_CONFIG_HOME", "CLAUDE_CONFIG_DIR", "EMBEDDING_MODEL")}
    env.update(overrides)
    return env


def git(*args, cwd=None):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com",
                    "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null",
                    "-c", "core.excludesFile=/dev/null", *args],
                   cwd=cwd, check=True, capture_output=True,
                   env=clean_env(GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_NOSYSTEM="1"))


@pytest.fixture
def upstream(tmp_path):
    repo = tmp_path / "upstream"
    (repo / "scripts").mkdir(parents=True)
    (repo / "src").mkdir()
    shutil.copy(ROOT / "install.sh", repo / "install.sh")
    shutil.copy(ROOT / "src/client_paths.py", repo / "src/client_paths.py")
    for helper in ("scripts/_helpers/inject_claude_md.py", "scripts/templates/routing-protocol-core.md"):
        (repo / helper).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(ROOT / helper, repo / helper)
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
    env = clean_env(HOME=str(home), AGENTS_REPO_URL=str(upstream), REAL_PYTHON=sys.executable)
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
    assert "Agents-Core Persona Protocol" in (tmp_path / "home/.claude/CLAUDE.md").read_text()
    ignore = (tmp_path / "home/.config/git/ignore").read_text().splitlines()
    assert ignore[0] == BEGIN and ignore[-1] == END


@pytest.mark.parametrize("extra_env, message", [
    ({"STUB_SKIP_REGISTER": "1"}, "Agents-Core is not registered for"),
    ({"STUB_INSTRUCTIONS": "skip"}, "lacks exactly one current Agents-Core routing section"),
    ({"STUB_INSTRUCTIONS": "truncate"}, "lacks exactly one current Agents-Core routing section"),
    ({"STUB_INSTRUCTIONS": "duplicate"}, "lacks exactly one current Agents-Core routing section"),
    ({"STUB_INSTRUCTIONS": "legacy"}, "lacks exactly one current Agents-Core routing section"),
    ({"FAKE_OLD_SCHEMA": "route"}, "route_and_load lacks protocol 2 parameters"),
    ({"FAKE_OLD_SCHEMA": "context"}, "get_agent_context lacks protocol 2 parameters"),
    ({"FAKE_PROTOCOL_VERSION": "1"}, "route_and_load returned protocol 1"),
    ({"FAKE_ROUTE_STATUS": "ERROR"}, "route_and_load returned protocol 2, ERROR"),
    ({"FAKE_IMPLANTS": "Error loading implants: no model"}, "load_implants returned: Error loading implants"),
    ({"FAKE_HANG": "1", "AGENTS_SETUP_VERIFY_TIMEOUT": "2"}, "Agents-Core server did not answer"),
], ids=["no-registration", "no-instructions", "truncated-instructions", "duplicate-instructions",
        "legacy-instructions", "old-route-schema", "old-context-schema", "protocol-1", "route-error",
        "implants-error", "server-hangs"])
def test_verification_failures_fail_setup(tmp_path, upstream, extra_env, message):
    result = run_setup(tmp_path, upstream, extra_env)
    assert result.returncode != 0
    assert message in result.stderr


def test_implant_query_without_match_passes(tmp_path, upstream):
    result = run_setup(tmp_path, upstream, {"FAKE_IMPLANTS": ""})
    assert result.returncode == 0, result.stderr
    assert "load_implants -> no match" in result.stdout


def test_registration_for_another_checkout_fails(tmp_path, upstream):
    assert run_setup(tmp_path, upstream).returncode == 0
    config = tmp_path / "home/.claude.json"
    config.write_text(config.read_text().replace("/.agents-core/src/", "/elsewhere/src/"))
    result = run_setup(tmp_path, upstream, {"STUB_SKIP_REGISTER": "1"})
    assert result.returncode != 0
    assert "Agents-Core is not registered for" in result.stderr


def test_disabled_registration_fails(tmp_path, upstream):
    assert run_setup(tmp_path, upstream).returncode == 0
    config = tmp_path / "home/.claude.json"
    document = json.loads(config.read_text())
    document["mcpServers"]["Agents-Core"]["disabled"] = True
    config.write_text(json.dumps(document))
    result = run_setup(tmp_path, upstream, {"STUB_SKIP_REGISTER": "1"})
    assert result.returncode != 0
    assert "Agents-Core is disabled in" in result.stderr


def test_excludes_hide_only_memory_files(tmp_path, upstream):
    assert run_setup(tmp_path, upstream).returncode == 0
    repo = tmp_path / "client"
    for name in MEMORY_FILES + ["notes.md", "history/notes.md", "data/keep.json", "sub/history.md"]:
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text("x")
    git("init", "-q", cwd=repo)
    status = subprocess.run(["git", "status", "--porcelain", "-uall"], cwd=repo, check=True,
                            capture_output=True, text=True,
                            env=clean_env(HOME=str(tmp_path / "home"), GIT_CONFIG_NOSYSTEM="1")).stdout
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


def test_symlinked_excludes_file_stays_a_symlink(tmp_path, upstream):
    dotfiles = tmp_path / "dotfiles/git-ignore"
    dotfiles.parent.mkdir()
    dotfiles.write_text("*.swp\n")
    link = tmp_path / "home/.config/git/ignore"
    link.parent.mkdir(parents=True)
    link.symlink_to(dotfiles)
    assert run_setup(tmp_path, upstream).returncode == 0
    assert link.is_symlink()
    lines = dotfiles.read_text().splitlines()
    assert lines[0] == "*.swp" and lines[1] == BEGIN and lines[-1] == END


def test_failed_excludes_read_keeps_user_rules(tmp_path, upstream):
    ignore = tmp_path / "home/.config/git/ignore"
    ignore.parent.mkdir(parents=True)
    ignore.write_text("*.swp\n")
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "awk").write_text("#!/bin/sh\nexit 2\n")
    (fake_bin / "awk").chmod(0o755)
    result = run_setup(tmp_path, upstream, {"PATH": f"{fake_bin}:{os.environ['PATH']}"})
    assert result.returncode != 0
    assert ignore.read_text() == "*.swp\n"


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0,
                    reason="as root, a regression would replace /dev/null")
def test_dev_null_excludes_file_is_left_alone(tmp_path, upstream):
    home = tmp_path / "home"
    home.mkdir()
    (home / ".gitconfig").write_text("[core]\n\texcludesFile = /dev/null\n")
    result = run_setup(tmp_path, upstream)
    assert result.returncode == 0, result.stderr
    assert "/dev/null is not a regular file" in result.stdout
    assert Path("/dev/null").is_char_device()


def test_rerun_keeps_excludes_file_mode(tmp_path, upstream):
    assert run_setup(tmp_path, upstream).returncode == 0
    ignore = tmp_path / "home/.config/git/ignore"
    ignore.chmod(0o600)
    assert run_setup(tmp_path, upstream).returncode == 0
    assert ignore.stat().st_mode & 0o777 == 0o600


def test_model_override_for_new_env(tmp_path, upstream):
    result = run_setup(tmp_path, upstream, {"AGENTS_EMBEDDING_MODEL": "intfloat/multilingual-e5-large"})
    assert result.returncode == 0, result.stderr
    assert env_values(tmp_path / "home/.agents-core")["EMBEDDING_MODEL"] == "intfloat/multilingual-e5-large"


def test_exported_model_seeds_a_new_env(tmp_path, upstream):
    result = run_setup(tmp_path, upstream, {"EMBEDDING_MODEL": "intfloat/multilingual-e5-large"})
    assert result.returncode == 0, result.stderr
    assert env_values(tmp_path / "home/.agents-core")["EMBEDDING_MODEL"] == "intfloat/multilingual-e5-large"


def test_exported_model_conflicting_with_env_fails(tmp_path, upstream):
    assert run_setup(tmp_path, upstream).returncode == 0
    result = run_setup(tmp_path, upstream, {"EMBEDDING_MODEL": "intfloat/multilingual-e5-large"})
    assert result.returncode != 0
    assert f"sets {BALANCED}; make them match" in result.stderr
    assert run_setup(tmp_path, upstream, {"EMBEDDING_MODEL": BALANCED}).returncode == 0


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
