#!/usr/bin/env bash
# Provision Agents-Core in a Claude Code cloud environment.
#
# Paste into the environment's "Setup script" field (see
# docs/cloud-runs.md#cloud-environment-with-agents-core):
#
#   #!/bin/bash
#   curl -fsSL https://raw.githubusercontent.com/IEZhu/Agents/main/scripts/setup_cloud_env.sh | bash
#
# The setup script runs as root before Claude Code starts, and the environment
# caches the resulting filesystem. Every session of the environment then starts
# with:
#   - the checkout in AGENTS_HOME, its .venv, the embedding model and indexes;
#   - Agents-Core registered as a user-scope stdio server in ~/.claude.json;
#   - the protocol 2 routing section in ~/.claude/CLAUDE.md;
#   - repository-memory files (history.md, its monthly archives, data/memory/)
#     in git's global excludes, so log_interaction never leaves untracked files
#     in the session's repository.
#
# Any failure exits non-zero, which fails the session start instead of caching
# an environment without a working server: the last step starts the server over
# stdio and routes a query. Rerunning the script updates the checkout and
# rebuilds only what changed.
#
# Variables (set them in the environment's "Environment variables" field):
#   AGENTS_HOME             checkout directory      [~/.agents-core]
#   AGENTS_REPO_URL         repository to clone     [https://github.com/IEZhu/Agents.git]
#   AGENTS_BRANCH           branch to install       [main]
#   AGENTS_EMBEDDING_MODEL  EMBEDDING_MODEL for a .env that has none
#                           [sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2]
#
# The model downloads from Hugging Face, which the default Trusted network level
# blocks: allow huggingface.co, *.huggingface.co, hf.co and *.hf.co.

set -euo pipefail
unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_INDEX_FILE \
      GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_PREFIX

home_dir="${AGENTS_HOME:-$HOME/.agents-core}"
repo_url="${AGENTS_REPO_URL:-https://github.com/IEZhu/Agents.git}"
branch="${AGENTS_BRANCH:-main}"
model="${AGENTS_EMBEDDING_MODEL:-sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2}"
export AGENTS_HOME="$home_dir" AGENTS_REPO_URL="$repo_url" AGENTS_BRANCH="$branch"

log() { printf '[agents-core] %s\n' "$*"; }
fail() { printf '[agents-core] ERROR: %s\n' "$*" >&2; exit 1; }

# 1. Checkout. install.sh (step 3) updates an existing one and refuses a
#    directory that is not an Agents-Core checkout.
if [ ! -e "$home_dir" ] || [ -z "$(ls -A "$home_dir")" ]; then
    log "Cloning $repo_url ($branch) into $home_dir"
    git clone --quiet --branch "$branch" "$repo_url" "$home_dir"
fi

# 2. Seed .env before setup reads it. Keys already present are kept, so manual
#    edits survive reruns; init_repo.sh adds the remaining env.example keys.
#    Auto-update stays off: every session starts from the cached snapshot, so the
#    background updater would fetch and re-embed in each new VM. The cache is
#    rebuilt from this script when it expires or the script changes.
if [ -f "$home_dir/scripts/init_repo.sh" ]; then
    env_file="$home_dir/.env"
    touch "$env_file"
    if [ -s "$env_file" ] && [ -n "$(tail -c 1 "$env_file")" ]; then
        echo >> "$env_file"
    fi
    seed_env() {
        grep -q "^$1=" "$env_file" || printf '%s=%s\n' "$1" "$2" >> "$env_file"
    }
    seed_env EMBEDDING_MODEL "$model"
    seed_env AGENTS_AUTO_UPDATE 0
fi

# 3. Update, dependencies and client configuration. init_repo.sh detects Claude
#    Code by its configuration directory, which may not exist yet in a fresh VM.
#    Indexing runs separately in step 4: init_repo.sh only warns when it fails.
mkdir -p "$HOME/.claude"
log "Running install.sh (dependencies, MCP registration, ~/.claude/CLAUDE.md)"
AGENTS_ASSUME_YES=1 bash "$home_dir/install.sh" --skip-index </dev/null

python_bin="$home_dir/.venv/bin/python"
[ -x "$python_bin" ] || fail "$python_bin was not created"

# 4. Download the embedding model and build the skill and implant indexes.
log "Downloading the embedding model and building indexes"
(cd "$home_dir" && "$python_bin" -m src.reindex) \
    || fail "indexing failed; check that the network allows huggingface.co, *.huggingface.co, hf.co and *.hf.co"

# 5. Keep repository-memory files out of the session repository's git status.
excludes="$(git config --global --path --get core.excludesFile || true)"
excludes="${excludes:-${XDG_CONFIG_HOME:-$HOME/.config}/git/ignore}"
marker="# Agents-Core repository memory (scripts/setup_cloud_env.sh)"
mkdir -p "$(dirname "$excludes")"
if ! grep -qxF "$marker" "$excludes" 2>/dev/null; then
    printf '%s\n/history.md\n/history/[0-9][0-9][0-9][0-9]-[0-9][0-9].md\n/data/memory/\n' \
        "$marker" >> "$excludes"
fi

# 6. Verify the registration Claude Code reads, then start the server the same
#    way and route a query. This also builds the router index into the snapshot.
log "Verifying the Claude Code registration and the server"
(cd "$home_dir" && "$python_bin" - "$home_dir" "$HOME/.claude.json" "$HOME/.claude/CLAUDE.md") <<'PY'
import asyncio
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

home, claude_json, claude_md = sys.argv[1:4]
with open(claude_json, encoding="utf-8") as stream:
    entry = json.load(stream).get("mcpServers", {}).get("Agents-Core")
server = os.path.realpath(os.path.join(home, "src", "server.py"))
if not entry or [os.path.realpath(arg) for arg in entry.get("args", [])] != [server]:
    sys.exit(f"Agents-Core is not registered for {home} in {claude_json}: {entry!r}")
with open(claude_md, encoding="utf-8") as stream:
    if "Agents-Core Routing Protocol" not in stream.read():
        sys.exit(f"{claude_md} has no Agents-Core routing section")


async def smoke():
    params = StdioServerParameters(
        command=entry["command"], args=entry["args"], cwd=home,
        env={**os.environ, **entry.get("env", {}), "WARMUP_WAIT_SECONDS": "300"})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = {tool.name for tool in (await session.list_tools()).tools}
            missing = {"route_and_load", "get_agent_context", "log_interaction"} - tools
            if missing:
                sys.exit(f"server lacks tools: {sorted(missing)}")
            result = await session.call_tool("route_and_load", {
                "query": "Set up a CI pipeline with GitHub Actions", "protocol_version": 2})
            payload = json.loads(result.content[0].text)
            if payload.get("status") not in {"SUCCESS", "ROUTE_REQUIRED"}:
                sys.exit(f"route_and_load returned {payload.get('status')}: {payload.get('message')}")
            print(f"[agents-core] Server OK: {len(tools)} tools, route_and_load -> {payload['status']}")


asyncio.run(smoke())
PY

log "Ready: $(git -C "$home_dir" log -1 --format='%h %s'), EMBEDDING_MODEL=$(grep '^EMBEDDING_MODEL=' "$home_dir/.env" | cut -d= -f2-)"
