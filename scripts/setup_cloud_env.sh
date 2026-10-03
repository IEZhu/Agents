#!/usr/bin/env bash
# Provision Agents-Core in a Claude Code cloud environment.
#
# Paste into the environment's "Setup script" field (see
# docs/cloud-runs.md#cloud-environment-with-agents-core):
#
#   #!/bin/bash
#   set -eo pipefail
#   curl -fsSL https://raw.githubusercontent.com/IEZhu/Agents/main/scripts/setup_cloud_env.sh | bash
#
# pipefail makes a failed download fail the setup; the body below runs only from
# the last line, so a truncated download runs nothing.
#
# The setup script runs as root before Claude Code starts, and the environment
# caches the resulting filesystem. Every session of the environment then starts
# with:
#   - the checkout in AGENTS_HOME, its .venv, the embedding model and indexes;
#   - Agents-Core registered as a user-scope stdio server in ~/.claude.json;
#   - the protocol 2 routing section in ~/.claude/CLAUDE.md;
#   - the files Agents-Core writes into a client repository (history.md, its
#     lock and archives, describe_repo's hash and locks) in git's global
#     excludes, so they never show up as untracked files in the session's
#     repository.
#
# Any failure exits non-zero, which fails the session start instead of caching
# an environment without a working server: the last step starts the server over
# stdio and routes a query. Rerunning the script updates the checkout and
# rebuilds only what changed.
#
# Variables (set them on the bash side of the pipe, e.g. `| AGENTS_BRANCH=x bash`):
#   AGENTS_HOME             checkout directory      [~/.agents-core]
#   AGENTS_REPO_URL         repository to clone     [https://github.com/IEZhu/Agents.git]
#   AGENTS_BRANCH           branch to install       [main]
#   AGENTS_EMBEDDING_MODEL  EMBEDDING_MODEL for a .env that has none [an exported
#                           EMBEDDING_MODEL, else
#                           sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2]
#   AGENTS_SETUP_VERIFY_TIMEOUT  seconds to wait for each server answer in the
#                           verification step [360]
#
# The model downloads from Hugging Face, which the default Trusted network level
# blocks: allow huggingface.co, *.huggingface.co, hf.co and *.hf.co.

log() { printf '[agents-core] %s\n' "$*"; }
fail() { printf '[agents-core] ERROR: %s\n' "$*" >&2; exit 1; }

main() {
    set -euo pipefail
    unset GIT_DIR GIT_WORK_TREE GIT_COMMON_DIR GIT_INDEX_FILE \
          GIT_OBJECT_DIRECTORY GIT_ALTERNATE_OBJECT_DIRECTORIES GIT_PREFIX

    local home_dir="${AGENTS_HOME:-$HOME/.agents-core}"
    local repo_url="${AGENTS_REPO_URL:-https://github.com/IEZhu/Agents.git}"
    local branch="${AGENTS_BRANCH:-main}"
    local model="${AGENTS_EMBEDDING_MODEL:-${EMBEDDING_MODEL:-sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2}}"
    # The registration holds absolute paths; resolve a relative or ~ value now.
    case "$home_dir" in "~" | "~/"*) home_dir="$HOME${home_dir#"~"}" ;; esac
    case "$home_dir" in /*) ;; *) home_dir="$PWD/$home_dir" ;; esac
    export AGENTS_HOME="$home_dir" AGENTS_REPO_URL="$repo_url" AGENTS_BRANCH="$branch"

    # 1. Checkout. install.sh (step 3) updates an existing one.
    if [ ! -e "$home_dir" ] || [ -z "$(ls -A "$home_dir")" ]; then
        log "Cloning $repo_url ($branch) into $home_dir"
        git clone --quiet --branch "$branch" "$repo_url" "$home_dir"
    elif [ ! -d "$home_dir/.git" ] || [ ! -f "$home_dir/scripts/init_repo.sh" ]; then
        fail "$home_dir exists and is not an Agents-Core checkout"
    fi

    # 2. Seed .env before setup reads it. Keys already present are kept, so
    #    manual edits survive reruns; init_repo.sh adds the remaining env.example
    #    keys. Auto-update stays off: every session starts from the cached
    #    snapshot, so the background updater would fetch and re-embed in each new
    #    VM. Updates arrive when the cache is rebuilt, which reruns this script:
    #    after about seven days, or when the environment's setup-script field or
    #    allowed hosts change (a change to this file alone does not rebuild it).
    local env_file="$home_dir/.env"
    touch "$env_file"
    if [ -s "$env_file" ] && [ -n "$(tail -c 1 "$env_file")" ]; then
        echo >> "$env_file"
    fi
    local key value
    for key in EMBEDDING_MODEL AGENTS_AUTO_UPDATE; do
        value="$model"
        [ "$key" = AGENTS_AUTO_UPDATE ] && value=0
        # Any assignment python-dotenv accepts counts, including `export KEY=`.
        grep -Eq "^[[:space:]]*(export[[:space:]]+)?$key[[:space:]]*=" "$env_file" \
            || printf '%s=%s\n' "$key" "$value" >> "$env_file"
    done

    # 3. Update, dependencies and client configuration. init_repo.sh detects
    #    Claude Code by its configuration directory, which may not exist yet in a
    #    fresh VM. Indexing runs separately in step 4: init_repo.sh only warns
    #    when it fails.
    mkdir -p "${CLAUDE_CONFIG_DIR:-$HOME/.claude}"
    log "Running install.sh (dependencies, MCP registration, ~/.claude/CLAUDE.md)"
    AGENTS_ASSUME_YES=1 bash "$home_dir/install.sh" --skip-index </dev/null

    local python_bin="$home_dir/.venv/bin/python"
    [ -x "$python_bin" ] || fail "$python_bin was not created"

    # An exported EMBEDDING_MODEL overrides .env in the setup's processes but
    # not necessarily in the sessions' server, so it must match the value the
    # server reads from .env (python-dotenv: the last assignment wins).
    local persisted
    persisted="$("$python_bin" -c 'import sys; from dotenv import dotenv_values
print(dotenv_values(sys.argv[1]).get("EMBEDDING_MODEL") or "")' "$env_file")"
    if [ "${EMBEDDING_MODEL+set}" = set ] && [ "$EMBEDDING_MODEL" != "$persisted" ]; then
        fail "EMBEDDING_MODEL=$EMBEDDING_MODEL is exported, but $env_file sets $persisted; make them match"
    fi

    # 4. Download the embedding model, then build the skill and implant indexes.
    #    The download comes first, in its own process: the index fingerprint
    #    includes the model revision read from the cache (src/engine/fingerprint.py),
    #    and indexes built before the download would be rebuilt by the server.
    log "Downloading the embedding model"
    (cd "$home_dir" && "$python_bin" -c 'from dotenv import load_dotenv; load_dotenv(".env")
from src.engine.embedder import embed_texts; embed_texts(["warmup"])') \
        || fail "model download failed; check that the network allows huggingface.co, *.huggingface.co, hf.co and *.hf.co"
    log "Building indexes"
    (cd "$home_dir" && "$python_bin" -m src.reindex) || fail "indexing failed"

    # 5. Keep the files the server writes into the session repository out of
    #    its git status: the history log with its lock, rotation and monthly
    #    archives (src/memory/history.py), and describe_repo's hash, locks and
    #    temporary files (src/memory/describer.py, src/memory/managed_section.py).
    #    A stdio server keeps its history index in the installation, not here.
    #    The block between the markers is replaced on every run.
    local excludes begin end
    excludes="$(git config --global --path --get core.excludesFile || true)"
    excludes="${excludes:-${XDG_CONFIG_HOME:-$HOME/.config}/git/ignore}"
    begin="# >>> Agents-Core repository memory (scripts/setup_cloud_env.sh) >>>"
    end="# <<< Agents-Core repository memory <<<"
    mkdir -p "$(dirname "$excludes")"
    [ -e "$excludes" ] || : > "$excludes"
    # Replace the file a symlink points to (managed dotfiles keep their link),
    # and only after the new content is complete; the copy keeps its mode.
    # Anything but a regular file, such as /dev/null set to disable global
    # excludes, is left alone.
    local target
    target="$(readlink -f -- "$excludes")"
    if [ -f "$target" ]; then
        # Unbalanced markers would make the replacement drop the user's rules.
        awk -v b="$begin" -v e="$end" '$0 == b {bad = bad || open; open = 1}
            $0 == e {bad = bad || !open; open = 0} END {exit bad || open}' "$target" \
            || fail "$target has unbalanced Agents-Core markers; fix or remove them"
        cp -p "$target" "$target.tmp"
        {
            awk -v b="$begin" -v e="$end" '$0 == b {skip = 1} !skip {print} $0 == e {skip = 0}' "$target"
            printf '%s\n' "$begin" \
                /history.md /.history.md.lock /history.md.rotating \
                '/history/[0-9][0-9][0-9][0-9]-[0-9][0-9].md' \
                '/history/[0-9][0-9][0-9][0-9]-[0-9][0-9].md.tmp' \
                /data/memory/.describe_hash '/data/memory/.managed_section.*.tmp' \
                /.agents-description.lock /.CLAUDE.md.lock '/.managed_section.*.tmp' \
                "$end"
        } > "$target.tmp"
        mv "$target.tmp" "$target"
    else
        log "WARNING: git excludes file $excludes is not a regular file; repository-memory files are not excluded"
    fi

    # 6. Verify what Claude Code reads (located as init_repo.sh does, through
    #    src/client_paths.py): the registration, and the routing section exactly
    #    as scripts/_helpers/inject_claude_md.py writes it from the template.
    #    Then start the server the same way: the protocol 2 tool schemas and a
    #    protocol 2 answer from route_and_load, and load_implants, which embeds
    #    a query in the server process when the implant index built in step 4
    #    is not empty (the stdio warm-up alone does not prove it).
    log "Verifying the Claude Code registration and the server"
    (cd "$home_dir" && "$python_bin" - "$home_dir") <<'PY'
import asyncio
from datetime import timedelta
import json
import os
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.shared.exceptions import McpError

home = sys.argv[1]
sys.path[:0] = [home, os.path.join(home, "scripts", "_helpers")]
from inject_claude_md import LEGACY_MARKER_BEGIN, LEGACY_MARKER_END, MARKER_BEGIN, MARKER_END  # noqa: E402
from src.client_paths import client_config_path, client_home  # noqa: E402

claude_json = client_config_path("claude")
claude_md = client_home("claude") / "CLAUDE.md"
template = os.path.join(home, "scripts", "templates", "routing-protocol-core.md")


def read_text(path):
    try:
        with open(path, encoding="utf-8") as stream:
            return stream.read()
    except FileNotFoundError:
        return ""


entry = json.loads(read_text(claude_json) or "{}").get("mcpServers", {}).get("Agents-Core")
server = os.path.realpath(os.path.join(home, "src", "server.py"))
if not entry or [os.path.realpath(arg) for arg in entry.get("args", [])] != [server]:
    sys.exit(f"Agents-Core is not registered for {home} in {claude_json}: {entry!r}")
if entry.get("disabled"):
    sys.exit(f"Agents-Core is disabled in {claude_json}; remove its \"disabled\" field")
# The registration's env (kept by the installer) and the inherited environment
# both override .env: a model set there would not match the indexes, and
# auto-update would run in every session.
from dotenv import dotenv_values  # noqa: E402
settings = dotenv_values(os.path.join(home, ".env"))
for source, values in ((f"the Agents-Core registration in {claude_json}", entry.get("env") or {}),
                       ("the environment", os.environ)):
    for key in ("EMBEDDING_MODEL", "AGENTS_AUTO_UPDATE"):
        if key in values and values[key] != settings.get(key):
            sys.exit(f"{source} sets {key}={values[key]}, but .env sets {settings.get(key)}; "
                     "make them match")
protocol = read_text(template).rstrip()
instructions = read_text(claude_md)
if (not protocol or f"{MARKER_BEGIN}\n\n{protocol}\n\n{MARKER_END}" not in instructions
        or instructions.count(MARKER_BEGIN) != 1 or instructions.count(MARKER_END) != 1
        or LEGACY_MARKER_BEGIN in instructions or LEGACY_MARKER_END in instructions):
    sys.exit(f"{claude_md} lacks exactly one current Agents-Core routing section from {template}")


# The server waits up to WARMUP_WAIT_SECONDS for its model; every answer must
# arrive within the timeout, so a server that stops answering fails the setup.
timeout = timedelta(seconds=float(os.environ.get("AGENTS_SETUP_VERIFY_TIMEOUT", "360")))


async def smoke():
    params = StdioServerParameters(
        command=entry["command"], args=entry["args"], cwd=entry.get("cwd") or home,
        env={**os.environ, **entry.get("env", {}), "WARMUP_WAIT_SECONDS": "300"})
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write, read_timeout_seconds=timeout) as session:
            await session.initialize()
            tools = {tool.name: tool.inputSchema for tool in (await session.list_tools()).tools}
            missing = {"route_and_load", "get_agent_context", "log_interaction", "load_implants"} - set(tools)
            if missing:
                sys.exit(f"server lacks tools: {sorted(missing)}")
            for name in ("route_and_load", "get_agent_context"):
                absent = {"protocol_version", "current_persona"} - set(tools[name].get("properties", {}))
                if absent:
                    sys.exit(f"{name} lacks protocol 2 parameters: {sorted(absent)}")
            result = await session.call_tool("route_and_load", {
                "query": "Set up a CI pipeline with GitHub Actions", "protocol_version": 2,
                "current_persona": None})
            payload = json.loads(result.content[0].text)
            if payload.get("protocol_version") != 2 or payload.get("status") not in {"SUCCESS", "ROUTE_REQUIRED"}:
                sys.exit(f"route_and_load returned protocol {payload.get('protocol_version')!r}, "
                         f"{payload.get('status')}: {payload.get('message')}")
            implants = await session.call_tool("load_implants", {
                "query": "Plan a database migration step by step", "limit": 2})
            text = implants.content[0].text
            # Empty is a valid answer: no implant passed the relevance threshold.
            if text and not text.startswith("## Dynamic Implants"):
                sys.exit(f"load_implants returned: {text[:300]}")
            print(f"[agents-core] Server OK: {len(tools)} tools, route_and_load -> {payload['status']}, "
                  f"load_implants -> {'implants' if text else 'no match'}")


def server_error(error):
    """The first launch or protocol error in *error*, which anyio may wrap in groups."""
    if isinstance(error, (McpError, OSError)):
        return error
    for inner in getattr(error, "exceptions", ()):
        if found := server_error(inner):
            return found
    return None


try:
    asyncio.run(smoke())
except BaseException as error:
    if not (found := server_error(error)):
        raise
    sys.exit(f"Agents-Core server did not start or answer: {found}")
PY

    log "Ready: $(git -C "$home_dir" log -1 --format='%h %s'), EMBEDDING_MODEL=$persisted"
}

main "$@"
