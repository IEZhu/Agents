#!/usr/bin/env bash
#
# Agents Repository Initialization Script
# ========================================
# This script sets up the development environment after cloning.
#
# Usage:
#   ./scripts/init_repo.sh [--yes] [--skip-env] [--skip-index] [--skip-mcp]
#   python3 scripts/install_instructions.py [--clients codex,claude]
#     Update only client instructions, without running MCP or dependency setup.
#
# Flags:
#   --yes, -y      Accept defaults without prompting (also AGENTS_ASSUME_YES=1).
#                  Reuses an existing venv and refreshes its dependencies
#                  (recreating it if its Python version is unknown or older
#                  than 3.11), accepts the Claude instruction and
#                  routing-reminder prompts (Codex instructions never ask),
#                  and does not ask about sync between machines.
#   --skip-env     Skip creating .env from env.example, or adding missing keys
#                  to an existing one (useful if already configured). The
#                  embedding model steps can still write .env; see --skip-index.
#   --skip-index   Skip the embedding model download, the index pre-build and
#                  writing the default model to .env when none is set. The
#                  one-time switch of an .env from an earlier model generation
#                  to the default still runs.
#   --skip-mcp     Skip MCP configuration and client instruction updates
#   --help         Show this help message
#
# One-command install (macOS/Linux): see install.sh and README "Quick Start".
#
# Installs the persona protocol: the model keeps its role across turns and routes
# only when the task needs another specialization (docs/routing_flow.md).

set -e
# ERR trap inherited into shell functions/subshells (see _fatal_on_err below).
set -o errtrace
# `pip install | while read` pipelines below would otherwise mask pip failures
# behind the while-loop's zero exit — surface the failing command instead.
set -o pipefail

# A GitHub token for setting up sync between machines (AGENTS_GITHUB_TOKEN) goes to
# the sync step only, as a prefix assignment on its own command: keep it in a
# variable that is not exported, out of the environment of every other child
# (pip, git, python helpers, the summary).
sync_github_token="${AGENTS_GITHUB_TOKEN:-}"
export -n sync_github_token
unset AGENTS_GITHUB_TOKEN

# ============== ANSI Colors ==============
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

# ============== Configuration ==============
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PATH="$REPO_ROOT/.venv"
PYTHON_MIN_VERSION="3.11"
ROUTING_TEMPLATE="$REPO_ROOT/scripts/templates/routing-protocol-core.md"

# Canonical managed-section markers — must match scripts/_helpers/inject_claude_md.py.
# Referenced both by the CLAUDE.md injector and by the fallback instructions block
# so users can paste the fallback verbatim and have future runs replace (not duplicate) it.
MARKER_BEGIN="# >>> Agents-Core Routing Protocol (managed by init_repo) >>>"
MARKER_END="# <<< Agents-Core Routing Protocol (managed by init_repo) <<<"

# Where users should report unexpected script failures (see _fatal_on_err below).
# Override via `AGENTS_ISSUES_URL` for divergent forks / GHE mirrors. Not derived
# from `git remote` on purpose: contributors who cloned a personal fork usually
# still want their installer bug reports to land on upstream.
REPO_URL_ISSUES="${AGENTS_ISSUES_URL:-https://github.com/IEZhu/Agents/issues}"

# NixOS detection: Nix Python uses /nix/store linker, so nix-ld doesn't help it.
# We pass LD_LIBRARY_PATH via MCP env config (not globally — that breaks Firefox etc.)
IS_NIXOS=false
NIX_LD_LIB_PATH=""
if [ -f /etc/NIXOS ]; then
    IS_NIXOS=true
    NIX_LD_LIB_PATH="/run/current-system/sw/share/nix-ld/lib"
    if [ ! -d "$NIX_LD_LIB_PATH" ]; then
        NIX_LD_LIB_PATH=""
    fi
fi

# ============== Parse Arguments ==============
SKIP_ENV=false
SKIP_INDEX=false
SKIP_MCP=false
case "${AGENTS_ASSUME_YES:-}" in
    1|true|yes) ASSUME_YES=true ;;
    *) ASSUME_YES=false ;;
esac

for arg in "$@"; do
    case $arg in
        --skip-env)
            SKIP_ENV=true
            shift
            ;;
        --skip-index)
            SKIP_INDEX=true
            shift
            ;;
        --skip-mcp)
            SKIP_MCP=true
            shift
            ;;
        --yes|-y)
            ASSUME_YES=true
            shift
            ;;
        --help|-h)
            sed -n '2,/^$/{ s/^# //; s/^#//; p; }' "$0"
            exit 0
            ;;
    esac
done

# ============== Helper Functions ==============

print_header() {
    echo ""
    echo -e "${CYAN}╔════════════════════════════════════════════════════════════════╗${NC}"
    echo -e "${CYAN}║${NC}  ${BLUE}$1${NC}"
    echo -e "${CYAN}╚════════════════════════════════════════════════════════════════╝${NC}"
}

print_step() {
    echo -e "  ${GREEN}→${NC} $1"
}

print_warn() {
    echo -e "  ${YELLOW}⚠${NC} $1"
}

print_error() {
    echo -e "  ${RED}✗${NC} $1"
}

print_success() {
    echo -e "  ${GREEN}✓${NC} $1"
}

# Fatal error handler — fires on any command failing under `set -e` that we did
# not explicitly handle (e.g. pip crash, python subprocess traceback). Controlled
# `exit 1` calls (missing Python, missing pip) bypass ERR intentionally: they
# already print user-actionable guidance, not a bug to report.
_fatal_on_err() {
    local exit_code=$?
    # Respect `set +e` regions: the caller has opted out of auto-abort (e.g. the
    # pre-indexing block, where a failed model download is non-fatal by design).
    # ERR still fires there, so we must no-op instead of exiting.
    case $- in *e*) ;; *) return 0 ;; esac
    # Pipeline subshells inherit ERR via errtrace. Defer to the main shell so
    # we emit exactly one FATAL block; pipefail propagates the subshell's
    # non-zero exit and refires ERR at the top level.
    [ "${BASH_SUBSHELL:-0}" -gt 0 ] && return 0
    local line_no="${BASH_LINENO[0]}"
    # Collapse multi-line commands (e.g. heredoc'd `python -c "..."`) to the
    # first line + ellipsis so the issue body's inline-code formatting doesn't
    # break and the copy-paste stays readable.
    local cmd="${BASH_COMMAND}"
    local cmd_first="${cmd%%$'\n'*}"
    [ "$cmd" != "$cmd_first" ] && cmd="${cmd_first} …"
    [ "${#cmd}" -gt 200 ] && cmd="${cmd:0:197}…"
    trap - ERR
    set +e

    local distro=""
    [ -f /etc/os-release ] && distro=$(. /etc/os-release 2>/dev/null && printf '%s' "${PRETTY_NAME:-unknown}")
    # Prefer the interpreter the script actually selected — plain `python` may
    # be missing or a different version on distros that expose only python3.x.
    local py_ver
    if [ -n "${SELECTED_PYTHON:-}" ]; then
        py_ver=$($SELECTED_PYTHON --version 2>&1 || echo "${SELECTED_PYTHON} (version query failed)")
    else
        py_ver=$(python --version 2>&1 || echo 'not found')
    fi

    {
        echo ""
        echo -e "${RED}╔══════════════════════════════════════════════════════════════════╗${NC}"
        echo -e "${RED}║${NC}  ${RED}FATAL: init_repo.sh aborted unexpectedly${NC}"
        echo -e "${RED}╚══════════════════════════════════════════════════════════════════╝${NC}"
        echo ""
        echo "  Exit code : ${exit_code}"
        echo "  Line      : ${line_no}"
        echo "  Command   : ${cmd}"
        echo ""
        echo -e "  ${CYAN}Please open an issue:${NC} ${REPO_URL_ISSUES}/new"
        echo ""
        echo "  Copy/paste into the issue form:"
        echo ""
        echo "  ── Title ──"
        echo "  [init_repo.sh] aborted at line ${line_no} (exit ${exit_code})"
        echo ""
        echo "  ── Body ──"
        echo "  ### Environment"
        echo "  - OS: $(uname -srmo 2>/dev/null || uname -a || echo unknown)"
        [ -n "$distro" ] && echo "  - Distro: ${distro}"
        echo "  - Python: ${py_ver}"
        echo "  - Bash: ${BASH_VERSION}"
        echo ""
        echo "  ### Failure"
        echo "  - Exit code: ${exit_code}"
        echo "  - Line: ${line_no}"
        echo "  - Command: \`${cmd}\`"
        echo ""
        echo "  ### How to reproduce"
        echo "  <steps — flags passed, context>"
        echo ""
        echo "  ### Logs"
        echo "  <paste the last ~50 lines of output above>"
        echo ""
    } >&2

    exit "$exit_code"
}
trap _fatal_on_err ERR

check_command() {
    if ! command -v "$1" &> /dev/null; then
        return 1
    fi
    return 0
}

get_python_version() {
    # Suppress stderr to avoid pyenv shim noise when a version isn't activated
    $1 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null
}

version_gte() {
    # Returns 0 (true) if $1 >= $2
    printf '%s\n%s' "$2" "$1" | sort -V -C
}

# Resolve client paths from the same contract used by migration and audit.
resolve_client_path() {
    "$PYTHON_ABS" -c '
import sys
sys.path.insert(0, sys.argv[1])
from src.client_paths import client_config_path, client_home
resolver = client_home if sys.argv[2] == "home" else client_config_path
print(resolver(sys.argv[3]))
' "$REPO_ROOT" "$@"
}

# Inject Agents-Core MCP server entry into a JSON config file.
# Usage: inject_mcp_config <config_path> <label> <client>
inject_mcp_config() {
    local config_path="$1"
    local label="$2"
    local client="$3"

    CLAUDE_CONFIG_PATH="$config_path" \
    MCP_CLIENT="$client" \
    MCP_PYTHON="$PYTHON_ABS" \
    MCP_SERVER="$SERVER_ABS" \
    MCP_IS_NIXOS="$IS_NIXOS" \
    MCP_NIX_LD_LIB_PATH="$NIX_LD_LIB_PATH" \
    python -c "
import json, os

config_path = os.environ['CLAUDE_CONFIG_PATH']
python_abs  = os.environ['MCP_PYTHON']
server_abs  = os.environ['MCP_SERVER']
is_nixos    = os.environ.get('MCP_IS_NIXOS', 'false') == 'true'
nix_ld_path = os.environ.get('MCP_NIX_LD_LIB_PATH', '')

# Installed macOS shared services remain HTTP/bridge on subsequent setup runs.
import sys
from pathlib import Path
installation = Path(server_abs).resolve().parents[1]
marker = installation / 'data/.shared-service.json'
if sys.platform == 'darwin' and marker.exists():
    sys.path.insert(0, str(installation))
    from src.daemon.clients import ClientMigration
    from src.daemon.bootstrap import assert_service_safe
    from src.file_lock import file_lock
    directory = json.loads(marker.read_text())['directory']
    destination = Path(config_path)
    client = os.environ['MCP_CLIENT']
    if client not in ('claude', 'cursor', 'desktop', 'antigravity'):
        raise ValueError('Unsupported installer client')
    with file_lock(Path(directory) / 'control.lock', blocking=False):
        assert_service_safe(directory)
        migration = ClientMigration(directory)
        changes = [migration.prepare(client, config_path=destination)]
        if client == 'desktop':
            changes.append(migration.prepare('claude-deny-desktop'))
        migration.apply(changes)
    print('OK: shared daemon')
    sys.exit(0)

try:
    with open(config_path, encoding='utf-8-sig') as f:  # also a file saved with a BOM
        config = json.load(f)
except (json.JSONDecodeError, FileNotFoundError):
    config = {}

# Refuse to rewrite a config whose shape we do not understand: resetting it
# would drop the user's other settings and MCP servers.
if not isinstance(config, dict):
    print(f'ERROR: {config_path} root must be a JSON object', file=sys.stderr)
    sys.exit(1)
servers = config.setdefault('mcpServers', {})
if not isinstance(servers, dict):
    print(f'ERROR: {config_path} mcpServers must be a JSON object', file=sys.stderr)
    sys.exit(1)

# The Claude desktop app's entry has a name of its own, which Claude Code denies: the app
# injects its servers into the Code-tab sessions it launches, and those must use Claude
# Code's per-project Agents-Core (#231). It takes over an older Agents-Core entry.
name = 'Agents-Core-Desktop' if os.environ['MCP_CLIENT'] == 'desktop' else 'Agents-Core'
# While the shared service is installed, an entry migrate wrote (HTTP, or the stdio bridge)
# stays: setup writes standalone stdio registrations again only after uninstall.
current = servers.get(name)
arguments = current.get('args') if isinstance(current, dict) else None
bridge = isinstance(arguments, list) and arguments and str(arguments[0]).replace(chr(92), '/').endswith('bridge/stdio.mjs')
if marker.exists() and isinstance(current, dict) and (current.get('url') or bridge):
    if name != 'Agents-Core':
        servers.pop('Agents-Core', None)
    with open(config_path, 'w') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
    print('OK: kept the shared service entry')
    sys.exit(0)
# Preserve existing entry to avoid clobbering user-added fields (e.g. env);
# a non-object entry is ours and unusable, so start over.
entry = servers.pop('Agents-Core', servers.get(name)) if name != 'Agents-Core' else servers.get(name)
if not isinstance(entry, dict):
    entry = {}
entry['command'] = python_abs
entry['args'] = [server_abs]

# On NixOS, Nix Python's linker is from /nix/store (not /lib64),
# so nix-ld can't help it. Pass LD_LIBRARY_PATH per-process via env
# to avoid setting it globally (which breaks Firefox and other apps).
# Prepend to any user value instead of replacing it; re-runs stay idempotent.
if is_nixos and nix_ld_path:
    env = entry.get('env')
    if not isinstance(env, dict):
        env = {}
    existing = env.get('LD_LIBRARY_PATH')
    existing = existing if isinstance(existing, str) else ''
    if nix_ld_path not in existing.split(':'):
        env['LD_LIBRARY_PATH'] = f'{nix_ld_path}:{existing}' if existing else nix_ld_path
    entry['env'] = env

servers[name] = entry

with open(config_path, 'w') as f:
    json.dump(config, f, indent=2, ensure_ascii=False)

print('OK')
" && print_success "$([ "$client" = desktop ] && echo Agents-Core-Desktop || echo Agents-Core) added to $label" \
  || { print_error "Failed to update $label"; return 1; }
}

# ============== Pre-flight Checks & Python Selection ==============

print_header "🔍 Pre-flight Checks"

# NixOS notice (only relevant when MCP config will actually be written).
# NIX_LD_LIB_PATH is cleared above when the nix-ld lib dir is missing, and
# inject_mcp_config then skips the env, so say so instead of claiming success.
if [ "$IS_NIXOS" = true ] && [ "$SKIP_MCP" = false ]; then
    if [ -n "$NIX_LD_LIB_PATH" ]; then
        print_success "NixOS detected — MCP config will include LD_LIBRARY_PATH env"
    else
        print_warn "NixOS detected but /run/current-system/sw/share/nix-ld/lib not found — LD_LIBRARY_PATH will NOT be added to MCP config; enable programs.nix-ld and re-run"
    fi
fi

SELECTED_PYTHON=""

if ! check_command python3; then
    print_error "python3 not found — please install Python $PYTHON_MIN_VERSION or newer"
    exit 1
fi

# `|| true` so a failing interpreter (broken pyenv shim, missing python in a
# shim-but-no-version setup) does not fire the ERR trap and emit the generic
# "please file an issue" FATAL block — this is a user-environment problem
# that deserves a clear, actionable message instead.
VER=$(get_python_version python3 || true)
if [ -z "$VER" ]; then
    print_error "python3 was found but failed to report its version"
    print_error "Check for a broken interpreter or an unconfigured pyenv shim; install Python >= $PYTHON_MIN_VERSION"
    exit 1
fi
if ! version_gte "$VER" "$PYTHON_MIN_VERSION"; then
    print_error "python3 is version $VER — requires >= $PYTHON_MIN_VERSION"
    exit 1
fi

SELECTED_PYTHON="python3"
print_success "Found suitable Python: $SELECTED_PYTHON ($VER)"

# Check pip
print_step "Checking pip..."
if ! check_command pip3; then
    print_error "pip3 not found. Install with: $SELECTED_PYTHON -m ensurepip"
    exit 1
fi
print_success "pip available"

# Appends each env.example line whose key has no assignment in the .env file,
# recording the keys in MISSING_KEYS. Any form python-dotenv reads counts as an
# assignment, including `export KEY=`, `'KEY'=` and spaces around the key: otherwise the
# appended default would come last and win.
# Usage: merge_missing_env_keys <env_file> <env_example>
merge_missing_env_keys() {
    local env_file="$1" env_example="$2" line key
    # Never glue an appended line onto an unterminated last line.
    if [ -s "$env_file" ] && [ -n "$(tail -c 1 "$env_file")" ]; then
        echo >> "$env_file"
    fi
    while IFS= read -r line; do
        # Skip empty lines and comments
        [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue

        # Extract key (before =)
        key=$(echo "$line" | cut -d'=' -f1 | xargs)
        [[ -z "$key" ]] && continue

        if ! grep -Eq "^[[:space:]]*(export[[:space:]]+)?('${key}'|${key})[[:space:]]*=" "$env_file" 2>/dev/null; then
            MISSING_KEYS+=("$key")
            # Append the whole line to .env
            echo "$line" >> "$env_file"
        fi
    done < "$env_example"
}

# ============== Environment Configuration ==============

print_header "⚙️  Environment Configuration"

ENV_FILE="$REPO_ROOT/.env"
ENV_EXAMPLE="$REPO_ROOT/env.example"

if [ "$SKIP_ENV" = false ]; then
    if [ -f "$ENV_FILE" ]; then
        print_warn ".env file already exists"
        print_step "Checking for missing keys..."

        # Check for missing keys and collect them with values
        MISSING_KEYS=()
        merge_missing_env_keys "$ENV_FILE" "$ENV_EXAMPLE"

        if [ ${#MISSING_KEYS[@]} -gt 0 ]; then
            print_success "Added ${#MISSING_KEYS[@]} missing keys: ${MISSING_KEYS[*]}"
            print_warn "Please configure the new keys in .env"
        else
            print_success "All required keys present in .env"
        fi
    else
        print_step "Creating .env from env.example..."
        cp "$ENV_EXAMPLE" "$ENV_FILE"
        print_success ".env created successfully!"
        echo ""
        echo -e "  ${YELLOW}⚠ Optional keys in .env:${NC}"
        echo "    • LANGFUSE_PUBLIC_KEY / LANGFUSE_SECRET_KEY - Langfuse tracing (empty: off)"
        echo "    • ANTHROPIC_API_KEY   - For document OCR (replace the sk-ant-... placeholder)"
        echo ""
    fi
else
    print_step "Skipping .env configuration (--skip-env)"
fi

# ============== Virtual Environment & Dependencies ==============

print_header "🐍 Virtual Environment & Dependencies"

SKIP_INSTALL=false

if [ -d "$VENV_PATH" ]; then
    # Check existing venv python version
    VENV_PYTHON_VER=$("$VENV_PATH/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null) || VENV_PYTHON_VER=unknown
    if [[ ! "$VENV_PYTHON_VER" =~ ^[0-9]+\.[0-9]+$ ]]; then
        VENV_PYTHON_VER=unknown
    fi

    print_success "Virtual environment exists ($VENV_PYTHON_VER)"

    if [ "$VENV_PYTHON_VER" != "unknown" ] && [ "$VENV_PYTHON_VER" != "$(get_python_version $SELECTED_PYTHON)" ]; then
         print_warn "Venv python version ($VENV_PYTHON_VER) differs from selected ($SELECTED_PYTHON)"
    fi

    echo ""
    print_warn "Do you want to recreate it and reinstall all packages?"
    if [ "$ASSUME_YES" = true ]; then
        # Destructive: recreate only when the existing venv is unusable.
        REPLY=n
        if [ "$VENV_PYTHON_VER" = "unknown" ] || ! version_gte "$VENV_PYTHON_VER" "$PYTHON_MIN_VERSION"; then
            REPLY=y
        fi
        print_step "--yes: reinstall answer '$REPLY'"
    else
        read -p "  Reinstall? [y/N]: " -r
        echo
    fi
    if [[ $REPLY =~ ^[Yy] ]]; then
        print_step "Removing existing venv..."
        rm -rf "$VENV_PATH"
        print_step "Creating fresh virtual environment using $SELECTED_PYTHON..."
        "$SELECTED_PYTHON" -m venv "$VENV_PATH"
    else
        if [ "$VENV_PYTHON_VER" = "unknown" ] || ! version_gte "$VENV_PYTHON_VER" "$PYTHON_MIN_VERSION"; then
            if [ "$VENV_PYTHON_VER" = "unknown" ]; then
                print_error "Could not verify existing virtual environment Python version"
            else
                print_error "Existing virtual environment uses Python $VENV_PYTHON_VER"
            fi
            print_error "Existing virtual environment requires Python >= $PYTHON_MIN_VERSION; rerun setup and choose to recreate it"
            exit 1
        fi
        print_step "Using existing virtual environment"
        # Under --yes (also used by install.sh updates) still refresh dependencies.
        if [ "$ASSUME_YES" = true ]; then SKIP_INSTALL=false; else SKIP_INSTALL=true; fi
    fi
else
    print_step "Creating virtual environment using $SELECTED_PYTHON..."
    "$SELECTED_PYTHON" -m venv "$VENV_PATH"
fi

# Activate venv
print_step "Activating virtual environment..."
source "$VENV_PATH/bin/activate"
print_success "Activated: $(which python)"

if [ "$SKIP_INSTALL" = false ]; then
    print_header "📦 Installing Dependencies"

    print_step "Upgrading pip..."
    echo ""
    pip install --upgrade pip 2>&1 | while IFS= read -r line; do
        echo "    $line"
    done
    echo ""

    print_step "Installing requirements (this may take a few minutes)..."
    echo ""
    pip install -r "$REPO_ROOT/requirements.txt" 2>&1 | while IFS= read -r line; do
        # Show only package installation lines to avoid clutter
        if [[ "$line" =~ ^Collecting || "$line" =~ ^Downloading || "$line" =~ ^Installing || "$line" =~ ^Successfully || "$line" =~ ^Requirement ]]; then
            echo "    $line"
        fi
    done
    echo ""

    print_success "All dependencies installed"
else
    print_step "Skipping package installation"
fi

# An earlier install pinned the default embedding model of its time: move it to
# the current default once (src/model_migration.py); a model chosen after that
# stays. Runs with --skip-index too, so the server's next start re-embeds.
if [ -f "$ENV_FILE" ]; then
    (cd "$REPO_ROOT" && python -m src.model_migration "$ENV_FILE") || print_warn "Embedding model migration failed; keeping the configured model"
fi

# Pre-download embedding model AND pre-index vector stores so MCP server starts instantly.
# Without this, first startup takes 30-60s for model download,
# causing Claude Desktop to time out with "Request timed out" (-32001).
# Runs regardless of SKIP_INSTALL — .mdc files may have changed even if deps are unchanged.
if [ "$SKIP_INDEX" = false ]; then
    print_header "🧠 Embedding Model Selection & Pre-indexing"

    # Check if model is already configured
    CURRENT_MODEL=""
    if [ -f "$ENV_FILE" ]; then
        # Every assignment form python-dotenv reads (`export`, quoted key), last one wins.
        CURRENT_MODEL=$(cd "$REPO_ROOT" && python -m src.model_migration --print-model "$ENV_FILE" 2>/dev/null || true)
    fi

    if [ -n "$CURRENT_MODEL" ]; then
        print_success "Embedding model already configured: $CURRENT_MODEL"
    else
        # One model for every machine: multilingual, ~1.1 GB download, ~0.9 GB loaded.
        CURRENT_MODEL="microsoft/harrier-oss-v1-270m"

        # Write to .env (use Python for macOS/Linux portability). The generation
        # marker keeps a later update from replacing this choice.
        ENV_FILE_PATH="$ENV_FILE" NEW_MODEL="$CURRENT_MODEL" REPO_ROOT="$REPO_ROOT" python -c "
import os, sys
sys.path.insert(0, os.environ['REPO_ROOT'])
from src.model_migration import GENERATION, GENERATION_KEY
env_path = os.environ['ENV_FILE_PATH']
new_model = os.environ['NEW_MODEL']
lines = []
if os.path.exists(env_path):
    with open(env_path) as f:
        lines = [l for l in f.readlines() if not l.startswith(('EMBEDDING_MODEL=', GENERATION_KEY + '='))]
if lines and not lines[-1].endswith('\n'):
    lines[-1] += '\n'
lines.append(f'EMBEDDING_MODEL={new_model}\n')
lines.append(f'{GENERATION_KEY}={GENERATION}\n')
with open(env_path, 'w') as f:
    f.writelines(lines)
"
        print_success "Embedding model: $CURRENT_MODEL"
    fi

    print_step "Pre-downloading model and indexing skills/implants..."
    print_step "(this may take a few minutes on first run)"
    set +e
    # NixOS: numpy (via embedder) needs libstdc++ at load time. Same rationale as MCP env above.
    LD_LIB_ENV=()
    if [ "$IS_NIXOS" = true ] && [ -n "$NIX_LD_LIB_PATH" ]; then
        LD_LIB_ENV=(env "LD_LIBRARY_PATH=$NIX_LD_LIB_PATH${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}")
    fi
    EMBEDDING_MODEL="$CURRENT_MODEL" REPO_ROOT="$REPO_ROOT" "${LD_LIB_ENV[@]}" python -c "
import sys, os, shutil, glob
sys.path.insert(0, os.environ['REPO_ROOT'])
os.environ.setdefault('EMBEDDING_MODEL', '$CURRENT_MODEL')

from dotenv import load_dotenv
load_dotenv(os.path.join(os.environ['REPO_ROOT'], '.env'))

# 1. Download/cache the embedding model (with corrupt cache recovery)
from src.engine.embedder import embed_texts
MAX_RETRIES = 2
for attempt in range(MAX_RETRIES):
    try:
        embed_texts(['warmup'])
        print('Embedding model ready', flush=True)
        break
    except Exception as e:
        if attempt < MAX_RETRIES - 1:
            print(f'Model load failed: {e}', flush=True)
            print('Clearing corrupted model cache and retrying...', flush=True)
            from src.engine.embedder import clear_model_cache, reset_model
            clear_model_cache('$CURRENT_MODEL')
            reset_model()
        else:
            raise

# 2. Pre-index skills and implants
print('Indexing skills...', flush=True)
from src.engine.skills import SkillRetriever
sr = SkillRetriever()
print(f'Skills indexed: {sr.store.count()} entries', flush=True)

print('Indexing implants...', flush=True)
from src.engine.implants import ImplantRetriever
ir = ImplantRetriever()
print(f'Implants indexed: {ir.store.count()} entries', flush=True)
" 2>&1
    INDEX_EXIT_CODE=$?
    set -e
    if [ $INDEX_EXIT_CODE -eq 0 ]; then
        print_success "Embedding model cached, skills & implants indexed"
    else
        print_warn "Pre-indexing failed — it will run on first MCP server start"
    fi
else
    print_step "Skipping pre-indexing (--skip-index)"
fi

# ============== MCP Environment Detection & Configuration ==============

print_header "🔌 MCP Environment Detection & Configuration"

# Absolute paths for MCP config entries
PYTHON_ABS="$VENV_PATH/bin/python"
SERVER_ABS="$REPO_ROOT/src/server.py"

# Detect asset directories
if [ -d "$REPO_ROOT/agents" ]; then
    AGENTS_BASE="$REPO_ROOT/agents"
    SKILLS_BASE="$REPO_ROOT/skills"
    IMPLANTS_BASE="$REPO_ROOT/implants"
else
    AGENTS_BASE=""
fi

if [ -n "$AGENTS_BASE" ] && [ -d "$AGENTS_BASE" ]; then
    # `|| true` swallows pipefail when skills/implants dirs are absent.
    # wc -l already emits "0" on empty stdin, so no fallback stdout is needed
    # (using `|| echo 0` would append a second "0" and corrupt the count).
    AGENT_COUNT=$(find "$AGENTS_BASE" -maxdepth 2 -name "system_prompt.mdc" 2>/dev/null | wc -l || true)
    SKILL_COUNT=$(find "$SKILLS_BASE" -name "*.mdc" 2>/dev/null | wc -l || true)
    IMPLANT_COUNT=$(find "$IMPLANTS_BASE" -name "*.mdc" 2>/dev/null | wc -l || true)

    print_success "Agents directory found: $AGENTS_BASE"
    echo -e "    • ${CYAN}${AGENT_COUNT}${NC} agents"
    echo -e "    • ${CYAN}$SKILL_COUNT${NC} skills"
    echo -e "    • ${CYAN}$IMPLANT_COUNT${NC} implants"
else
    print_error "Agents directory not found (checked agents/)"
fi

if [ "$SKIP_MCP" = true ]; then
    print_step "Skipping MCP configuration (--skip-mcp)"
else
    # Track which environments were configured
    CONFIGURED_ENVS=()

    echo ""
    print_step "Detecting IDE environments..."
    echo ""

    # --- Detect Cursor ---
    CURSOR_DETECTED=false
    MCP_SETTINGS_FILE="$(resolve_client_path config cursor)"
    CURSOR_GLOBAL_DIR="$(dirname "$MCP_SETTINGS_FILE")"
    if [ -n "${AGENTS_CURSOR_MCP_CONFIG:-}" ] || [ -d "$CURSOR_GLOBAL_DIR" ]; then
        CURSOR_DETECTED=true
        print_success "Cursor IDE configuration: $MCP_SETTINGS_FILE"
    else
        print_step "Cursor IDE not detected"
    fi

    # --- Detect Claude Desktop ---
    CLAUDE_DESKTOP_DETECTED=false
    CLAUDE_DESKTOP_CONFIG="$(resolve_client_path config desktop)"
    CLAUDE_DESKTOP_DIR="$(dirname "$CLAUDE_DESKTOP_CONFIG")"
    if [ -n "${AGENTS_CLAUDE_DESKTOP_CONFIG:-}" ] || [ -d "$CLAUDE_DESKTOP_DIR" ]; then
        CLAUDE_DESKTOP_DETECTED=true
        print_success "Claude Desktop detected ($CLAUDE_DESKTOP_DIR)"
    else
        print_step "Claude Desktop not detected"
    fi

    # --- Detect Claude Code ---
    CLAUDE_CODE_DETECTED=false
    CLAUDE_CODE_DIR="$(resolve_client_path home claude)"
    CLAUDE_CODE_MCP="$(resolve_client_path config claude)"
    if [ -n "${CLAUDE_CONFIG_DIR:-}" ] || check_command claude || [ -f "$CLAUDE_CODE_MCP" ] || [ -d "$CLAUDE_CODE_DIR" ]; then
        CLAUDE_CODE_DETECTED=true
        print_success "Claude Code detected"
    else
        print_step "Claude Code not detected"
    fi

    # --- Detect Antigravity ---
    ANTIGRAVITY_DETECTED=false
    ANTIGRAVITY_CONFIG="$(resolve_client_path config antigravity)"
    ANTIGRAVITY_DIR="$(dirname "$ANTIGRAVITY_CONFIG")"
    if [ -n "${AGENTS_ANTIGRAVITY_MCP_CONFIG:-}" ] || [ -d "$ANTIGRAVITY_DIR" ] || check_command agy; then
        ANTIGRAVITY_DETECTED=true
        print_success "Antigravity detected ($ANTIGRAVITY_CONFIG)"
    else
        print_step "Antigravity not detected"
    fi

    echo ""

    # --- Configure Cursor ---
    if [ "$CURSOR_DETECTED" = true ]; then
        print_step "Configuring Cursor MCP ($MCP_SETTINGS_FILE)..."
        mkdir -p "$CURSOR_GLOBAL_DIR"

        if [ ! -f "$MCP_SETTINGS_FILE" ]; then
            echo '{ "mcpServers": {} }' > "$MCP_SETTINGS_FILE"
        fi

        # Backup before modifying
        cp "$MCP_SETTINGS_FILE" "${MCP_SETTINGS_FILE}.backup.$(date +%s)"

        if inject_mcp_config "$MCP_SETTINGS_FILE" "$MCP_SETTINGS_FILE" cursor; then
            CONFIGURED_ENVS+=("Cursor")
        fi
    fi

    # --- Configure Claude Desktop ---
    if [ "$CLAUDE_DESKTOP_DETECTED" = true ]; then
        print_step "Configuring Claude Desktop MCP..."
        mkdir -p "$CLAUDE_DESKTOP_DIR"

        if [ ! -f "$CLAUDE_DESKTOP_CONFIG" ]; then
            echo '{}' > "$CLAUDE_DESKTOP_CONFIG"
        fi

        # Backup before modifying
        cp "$CLAUDE_DESKTOP_CONFIG" "${CLAUDE_DESKTOP_CONFIG}.backup.$(date +%s)"

        if inject_mcp_config "$CLAUDE_DESKTOP_CONFIG" "Claude Desktop config" desktop; then
            CONFIGURED_ENVS+=("Claude Desktop")
        fi
    fi

    # --- Configure Claude Code ---
    if [ "$CLAUDE_CODE_DETECTED" = true ]; then
        # The selected profile holds its instructions, permissions, and memory.
        if [ -e "$CLAUDE_CODE_DIR" ] && [ ! -d "$CLAUDE_CODE_DIR" ]; then
            print_error "$CLAUDE_CODE_DIR exists but is not a directory — skipping Claude Code configuration"
        else
            mkdir -p "$CLAUDE_CODE_DIR"

            # 1. MCP server in the selected profile's user-scope registry.
            print_step "Configuring Claude Code MCP ($CLAUDE_CODE_MCP)..."

            if [ ! -f "$CLAUDE_CODE_MCP" ]; then
                echo '{}' > "$CLAUDE_CODE_MCP"
            fi

            # Backup before modifying
            cp "$CLAUDE_CODE_MCP" "${CLAUDE_CODE_MCP}.backup.$(date +%s)"

            if inject_mcp_config "$CLAUDE_CODE_MCP" "$CLAUDE_CODE_MCP" claude; then
                CONFIGURED_ENVS+=("Claude Code")
            fi

            # The desktop app's entry stays out of Claude Code, its Code tab included (#231).
            if [ "$CLAUDE_DESKTOP_DETECTED" = true ]; then
                CLAUDE_CODE_SETTINGS="$(resolve_client_path config claude-deny-desktop)"
                if [ -f "$CLAUDE_CODE_SETTINGS" ]; then
                    cp "$CLAUDE_CODE_SETTINGS" "${CLAUDE_CODE_SETTINGS}.backup.$(date +%s)"
                fi
                if "$PYTHON_ABS" "$REPO_ROOT/scripts/_helpers/deny_desktop_mcp.py" "$CLAUDE_CODE_SETTINGS" >/dev/null; then
                    DESKTOP_DENIED=true
                    print_success "Agents-Core-Desktop denied in Claude Code $CLAUDE_CODE_SETTINGS"
                else
                    print_error "Failed to deny Agents-Core-Desktop in Claude Code settings"
                fi
            fi

            # 2. Replace the managed routing section, preserving personal instructions.
            CLAUDE_CODE_MD="$CLAUDE_CODE_DIR/CLAUDE.md"
            CLAUDE_MD_SRC="$ROUTING_TEMPLATE"
            # --- Ask permission before modifying instruction files ---
            echo ""
            echo -e "  ${CYAN}Agents-Core wants to add routing instructions to:${NC}"
            echo "    $CLAUDE_CODE_MD"
            echo ""
            if [ "$ASSUME_YES" = true ]; then REPLY=y; else read -p "  Allow? [Y/n]: " -r; fi
            echo ""

            CLAUDE_MD_CONFIGURED=false
            if [[ $REPLY =~ ^[Nn] ]]; then
                print_warn "Skipped CLAUDE.md injection — instructions will be printed at the end"
            elif [ -f "$CLAUDE_MD_SRC" ]; then
                print_step "Configuring global CLAUDE.md ($CLAUDE_CODE_MD)..."
                if "$PYTHON_ABS" "$REPO_ROOT/scripts/_helpers/inject_claude_md.py" "$CLAUDE_CODE_MD" "$CLAUDE_MD_SRC"; then
                    print_success "Agents-Core persona protocol configured in global CLAUDE.md"
                    CLAUDE_MD_CONFIGURED=true
                else
                    print_error "Failed to replace section — check markers in $CLAUDE_CODE_MD manually"
                fi
            else
                print_warn "Template not found at $CLAUDE_MD_SRC, skipping"
            fi

            # 3. Only known generated routing reminders may be migrated automatically.
            CLAUDE_MEMORY_DIR="$CLAUDE_CODE_DIR/memory"
            MEMORY_FILE="$CLAUDE_MEMORY_DIR/feedback_agents_core_routing.md"
            if [ "$CLAUDE_MD_CONFIGURED" = true ]; then
                echo ""
                echo -e "  ${CYAN}Agents-Core wants to configure its routing reminder:${NC}"
                echo "    $MEMORY_FILE"
                echo ""
                if [ "$ASSUME_YES" = true ]; then REPLY=y; else read -p "  Allow? [Y/n]: " -r; fi
                echo ""
                if [[ $REPLY =~ ^[Nn] ]]; then
                    print_warn "Skipped memory file; align any old routing reminder with the persona protocol manually"
                else
                    "$PYTHON_ABS" "$REPO_ROOT/scripts/_helpers/migrate_routing_memory.py" \
                        "$CLAUDE_MEMORY_DIR" \
                        || print_error "Memory migration failed; inspect $MEMORY_FILE manually"
                fi
                print_step "Check your project instructions and memory for conflicting 'always route_and_load' requirements."
                print_step "Only the managed section and exact generated reminder are migrated; other project memory is preserved."
            else
                print_warn "Skipping memory setup — global CLAUDE.md routing section was not configured"
            fi

        fi # end: selected Claude profile is a directory check
    fi

    # --- Configure Antigravity ---
    if [ "$ANTIGRAVITY_DETECTED" = true ]; then
        print_step "Configuring Antigravity MCP ($ANTIGRAVITY_CONFIG)..."
        mkdir -p "$ANTIGRAVITY_DIR"

        if [ ! -f "$ANTIGRAVITY_CONFIG" ]; then
            echo '{ "mcpServers": {} }' > "$ANTIGRAVITY_CONFIG"
        fi

        # Backup before modifying
        cp "$ANTIGRAVITY_CONFIG" "${ANTIGRAVITY_CONFIG}.backup.$(date +%s)"

        if inject_mcp_config "$ANTIGRAVITY_CONFIG" "$ANTIGRAVITY_CONFIG" antigravity; then
            CONFIGURED_ENVS+=("Antigravity")
        fi
    fi

    # --- Configure Codex instructions ---
    # Registration is separate: Codex's TOML configuration is not a JSON MCP file.
    print_step "Checking Codex global instructions..."
    if ! "$PYTHON_ABS" "$REPO_ROOT/scripts/_helpers/install_codex_instructions.py" "$ROUTING_TEMPLATE"; then
        print_error "Failed to configure Codex instructions — inspect the reported path"
    fi

    # --- Summary ---
    echo ""
    if [ ${#CONFIGURED_ENVS[@]} -eq 0 ]; then
        print_warn "No MCP client registrations were configured"
        print_step "You can configure MCP manually later:"
        echo "    • Cursor:         Install Cursor, then re-run this script"
        echo "    • Claude Desktop: Install Claude Desktop, then re-run this script"
        echo "    • Claude Code:    Install Claude Code, then re-run this script"
        echo "    • Antigravity:    Install Antigravity, then re-run this script"
    else
        print_success "MCP configured for: ${CONFIGURED_ENVS[*]}"
        # Claude Desktop (its Code tab included) also reads the Claude Code registry.
        case " ${CONFIGURED_ENVS[*]} " in
            *" Claude Desktop "*" Claude Code "*|*" Claude Code "*" Claude Desktop "*)
                if [ "${DESKTOP_DENIED:-false}" != true ]; then
                    print_warn "Claude Code does not deny Agents-Core-Desktop, so a Code-tab session of the"
                    echo "    desktop app can reach the app's shared server, started outside any project. Add"
                    echo "    mcp__Agents-Core-Desktop__* to permissions.deny in $CLAUDE_CODE_SETTINGS."
                else
                    print_step "Claude Desktop runs Agents-Core-Desktop for its chats, outside any project."
                    echo "    Claude Code denies it, so a Code-tab session of the app uses Claude Code's own"
                    echo "    Agents-Core for its project instead of the app's shared server. Each still starts"
                    echo "    its own process: raise MCP_TIMEOUT (milliseconds) for Claude Code if a start is slow."
                fi
                ;;
        esac
    fi
fi

# ============== Sync Between Machines ==============
# The personal library (flows/.user) can sync between the user's machines through a
# private git repository (docs/user-sync.md). The step is src/user_sync/installer.py:
# with a terminal and without --yes it asks once; --yes (install.sh, updates) never
# asks; AGENTS_USER_SYNC_REPO or AGENTS_USER_SYNC_REMOTE in the environment (never
# from .env) set sync up without a question. Only this command receives the GitHub
# token. It never touches an existing flows/.user/.git by itself, and a failure
# here never fails setup.

print_header "🔄 Sync Between Machines"
SYNC_FLAG=""
if [ "$ASSUME_YES" = true ]; then SYNC_FLAG="--yes"; fi
(cd "$REPO_ROOT" && AGENTS_GITHUB_TOKEN="$sync_github_token" "$PYTHON_ABS" -m src.user_sync installer $SYNC_FLAG) \
    || print_warn "The sync step did not finish; setup continues (docs/user-sync.md)"

# ============== Final Summary ==============

print_header "✅ Initialization Complete!"

echo ""
echo -e "  ${GREEN}What was configured:${NC}"

if [ "$SKIP_MCP" = false ] && [ ${#CONFIGURED_ENVS[@]} -gt 0 ]; then
    for env in "${CONFIGURED_ENVS[@]}"; do
        echo -e "    ${GREEN}✓${NC} $env — MCP server registered"
    done
elif [ "$SKIP_MCP" = true ]; then
    echo -e "    ${YELLOW}⚠${NC} MCP configuration skipped (--skip-mcp)"
else
    echo -e "    ${YELLOW}⚠${NC} No IDE environments were detected"
fi
echo ""
echo -e "  ${GREEN}Next steps:${NC}"
echo ""

STEP=1

echo "  $STEP. Configure API keys in .env (if you haven't yet):"
echo -e "     ${CYAN}nano $ENV_FILE${NC}"
echo ""
STEP=$((STEP + 1))

# Dynamic restart/start instructions per environment
if [ "$SKIP_MCP" = false ] && [ ${#CONFIGURED_ENVS[@]} -gt 0 ]; then
    for env in "${CONFIGURED_ENVS[@]}"; do
        case "$env" in
            "Cursor")
                echo "  $STEP. Restart Cursor IDE to activate MCP servers"
                echo ""
                STEP=$((STEP + 1))
                ;;
            "Claude Desktop")
                echo "  $STEP. Restart Claude Desktop to activate MCP servers"
                echo ""
                STEP=$((STEP + 1))
                ;;
            "Claude Code")
                echo "  $STEP. Claude Code is configured globally — start it in any directory:"
                echo -e "     ${CYAN}claude${NC}"
                echo ""
                STEP=$((STEP + 1))
                ;;
        esac
    done
fi

echo "  $STEP. Test it in a new client session: ask the model to call"
echo -e "     ${CYAN}list_agents()${NC}, or run the ${CYAN}ask${NC} MCP prompt with a task"
echo ""

# ============== Health Check ==============

if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE" 2>/dev/null || true
    if [ -z "$ANTHROPIC_API_KEY" ] || [ "$ANTHROPIC_API_KEY" = "sk-ant-..." ]; then
        print_warn "ANTHROPIC_API_KEY not configured — document OCR will be unavailable"
    fi
fi

STEP=$((STEP + 1))
echo "  $STEP. To enable repository memory & history in a project:"
echo -e "     Run ${CYAN}describe_repo()${NC} in your first Claude session inside that repo."
echo "     It writes a compressed overview into the repo's own CLAUDE.md"
echo "     (managed section — not the global ~/.claude/CLAUDE.md)."
echo -e "     If MCP sampling is unavailable it returns ${CYAN}status=\"needs_summary\"${NC}"
echo -e "     and you finalize the write with ${CYAN}write_repo_summary(...)${NC}."
echo "     History is appended to history.md each turn via log_interaction(...) (called by Claude per the routing protocol)."
echo ""

# Sync between machines: its state, background sync and how to turn either on
# (src/user_sync/installer.py). Never the token: the .env sourced above may export one.
(unset AGENTS_GITHUB_TOKEN; cd "$REPO_ROOT" && "$PYTHON_ABS" -m src.user_sync installer --summary 2>/dev/null) || true

# ============== LLM Instructions Block ==============
# Printed only as a fallback — when the routing section could not be injected
# into Claude Code's global CLAUDE.md (Claude Code not detected, consent denied,
# template missing, or injection failed). When injection succeeded, the user
# already has these instructions in place and does not need to paste them manually.

TEMPLATE_FILE="$ROUTING_TEMPLATE"
if [ "${CLAUDE_MD_CONFIGURED:-false}" != "true" ] && [ -f "$TEMPLATE_FILE" ]; then
    echo -e "${CYAN}════════════════════════════════════════════════════════════${NC}"
    echo ""
    echo -e "  ${GREEN}Add the following block to your LLM's instruction file${NC}"
    echo -e "  (CLAUDE.md for Claude, .cursorrules for Cursor, etc.)."
    echo -e "  ${YELLOW}Keep the BEGIN/END marker lines intact${NC} so a later script"
    echo -e "  run can replace the section instead of appending a duplicate."
    echo ""
    echo -e "${CYAN}────────────────────────────────────────────────────────────${NC}"
    echo "$MARKER_BEGIN"
    echo ""
    cat "$TEMPLATE_FILE"
    echo ""
    echo "$MARKER_END"
    echo -e "${CYAN}────────────────────────────────────────────────────────────${NC}"
    echo ""
fi

echo -e "${GREEN}Happy coding! 🚀${NC}"
echo ""
