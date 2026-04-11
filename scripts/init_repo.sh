#!/usr/bin/env bash
#
# Agents Repository Initialization Script
# ========================================
# This script sets up the development environment after cloning.
#
# Usage:
#   ./scripts/init_repo.sh [--skip-env] [--skip-index] [--skip-mcp]
#
# Flags:
#   --skip-env     Skip .env file creation (useful if already configured)
#   --skip-index   Skip embedding model download and index pre-build
#   --skip-mcp     Skip MCP environment detection and configuration
#   --help         Show this help message

set -e

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
PYTHON_MIN_VERSION="3.10"
# Upper bound for ML compatibility (exclusive)
PYTHON_MAX_VERSION_MAJOR="3"
PYTHON_MAX_VERSION_MINOR="13" # 3.13 is the first unsafe version

# ============== Parse Arguments ==============
SKIP_ENV=false
SKIP_INDEX=false
SKIP_MCP=false

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

version_lt() {
    # Returns 0 (true) if $1 < $2
    ! version_gte "$1" "$2"
}

# Inject Agents-Core MCP server entry into a JSON config file.
# Usage: inject_mcp_config <config_path> <label>
inject_mcp_config() {
    local config_path="$1"
    local label="$2"

    CLAUDE_CONFIG_PATH="$config_path" \
    MCP_PYTHON="$PYTHON_ABS" \
    MCP_SERVER="$SERVER_ABS" \
    python -c "
import json, os

config_path = os.environ['CLAUDE_CONFIG_PATH']
python_abs  = os.environ['MCP_PYTHON']
server_abs  = os.environ['MCP_SERVER']

try:
    with open(config_path) as f:
        config = json.load(f)
except (json.JSONDecodeError, FileNotFoundError):
    config = {}

if 'mcpServers' not in config:
    config['mcpServers'] = {}

config['mcpServers']['Agents-Core'] = {
    'command': python_abs,
    'args': [server_abs],
}

with open(config_path, 'w') as f:
    json.dump(config, f, indent=2, ensure_ascii=False)

print('OK')
" && print_success "Agents-Core added to $label" \
  || { print_error "Failed to update $label"; return 1; }
}

# ============== Pre-flight Checks & Python Selection ==============

print_header "🔍 Pre-flight Checks"

# Find suitable Python interpreter
SELECTED_PYTHON=""

# 1. Check default python3
if check_command python3; then
    VER=$(get_python_version python3)
    if version_gte "$VER" "$PYTHON_MIN_VERSION" && version_lt "$VER" "$PYTHON_MAX_VERSION_MAJOR.$PYTHON_MAX_VERSION_MINOR"; then
        SELECTED_PYTHON="python3"
        print_success "Found suitable Python: $SELECTED_PYTHON ($VER)"
    else
        print_warn "Default python3 is version $VER (supported: $PYTHON_MIN_VERSION - <$PYTHON_MAX_VERSION_MAJOR.$PYTHON_MAX_VERSION_MINOR)"
    fi
fi

# 2. If default not suitable, search for specific versions
if [ -z "$SELECTED_PYTHON" ]; then
    print_step "Searching for alternative Python versions..."
    # Order of preference: 3.11 -> 3.10 -> 3.12 (3.11 is most stable for ML now)
    for CANDIDATE in python3.11 python3.10 python3.12; do
        if check_command "$CANDIDATE"; then
             VER=$(get_python_version "$CANDIDATE") || continue
             [ -z "$VER" ] && continue
             if version_gte "$VER" "$PYTHON_MIN_VERSION"; then
                 SELECTED_PYTHON="$CANDIDATE"
                 print_success "Found suitable Python: $SELECTED_PYTHON ($VER)"
                 break
             fi
        fi
    done
fi

# 3. Final check
if [ -z "$SELECTED_PYTHON" ]; then
    print_error "No suitable Python version found!"
    echo "       Please install Python 3.10, 3.11, or 3.12."
    echo "       (Python 3.13+ has compatibility issues with some ML libraries)"
    exit 1
fi

# Check pip
print_step "Checking pip..."
if ! check_command pip3; then
    print_error "pip3 not found. Install with: $SELECTED_PYTHON -m ensurepip"
    exit 1
fi
print_success "pip available"

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
        ADDED_KEYS=0

        while IFS= read -r line; do
            # Skip empty lines and comments
            [[ -z "$line" || "$line" =~ ^[[:space:]]*# ]] && continue

            # Extract key (before =)
            key=$(echo "$line" | cut -d'=' -f1 | xargs)
            [[ -z "$key" ]] && continue

            # Check if key exists in .env
            if ! grep -q "^${key}=" "$ENV_FILE" 2>/dev/null; then
                MISSING_KEYS+=("$key")
                # Append the whole line to .env
                echo "$line" >> "$ENV_FILE"
                ADDED_KEYS=$((ADDED_KEYS + 1))
            fi
        done < "$ENV_EXAMPLE"

        if [ ${#MISSING_KEYS[@]} -gt 0 ]; then
            print_success "Added $ADDED_KEYS missing keys: ${MISSING_KEYS[*]}"
            print_warn "Please configure the new keys in .env"
        else
            print_success "All required keys present in .env"
        fi
    else
        print_step "Creating .env from env.example..."
        cp "$ENV_EXAMPLE" "$ENV_FILE"
        print_success ".env created successfully!"
        echo ""
        echo -e "  ${YELLOW}⚠ Required configuration:${NC}"
        echo "    • LANGFUSE_PUBLIC_KEY - LangFuse public key (optional)"
        echo "    • LANGFUSE_SECRET_KEY - LangFuse secret key (optional)"
        echo "    • ANTHROPIC_API_KEY   - For document OCR (optional)"
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
    VENV_PYTHON_VER=$("$VENV_PATH/bin/python" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null || echo "unknown")

    print_success "Virtual environment exists ($VENV_PYTHON_VER)"

    if [ "$VENV_PYTHON_VER" != "unknown" ] && [ "$VENV_PYTHON_VER" != "$(get_python_version $SELECTED_PYTHON)" ]; then
         print_warn "Venv python version ($VENV_PYTHON_VER) differs from selected ($SELECTED_PYTHON)"
         RECREATE_DEFAULT="y"
    else
         RECREATE_DEFAULT="N"
    fi

    echo ""
    print_warn "Do you want to recreate it and reinstall all packages?"
    read -p "  Reinstall? [y/N]: " -n 1 -r
    echo
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        print_step "Removing existing venv..."
        rm -rf "$VENV_PATH"
        print_step "Creating fresh virtual environment using $SELECTED_PYTHON..."
        "$SELECTED_PYTHON" -m venv "$VENV_PATH"
    else
        print_step "Using existing virtual environment"
        SKIP_INSTALL=true
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

# Pre-download embedding model AND pre-index vector stores so MCP server starts instantly.
# Without this, first startup takes 30-60s for model download,
# causing Claude Desktop to time out with "Request timed out" (-32001).
# Runs regardless of SKIP_INSTALL — .mdc files may have changed even if deps are unchanged.
if [ "$SKIP_INDEX" = false ]; then
    print_header "🧠 Embedding Model Selection & Pre-indexing"

    # Check if model is already configured
    CURRENT_MODEL=""
    if [ -f "$ENV_FILE" ]; then
        CURRENT_MODEL=$(grep '^EMBEDDING_MODEL=' "$ENV_FILE" 2>/dev/null | cut -d'=' -f2- | sed "s/[[:space:]]*#.*//; s/^['\"]//; s/['\"]$//" | xargs || true)
    fi

    if [ -n "$CURRENT_MODEL" ]; then
        print_success "Embedding model already configured: $CURRENT_MODEL"
    else
        echo ""
        echo -e "  ${CYAN}Select embedding model:${NC}"
        echo ""
        echo -e "    ${GREEN}1)${NC} Full     — intfloat/multilingual-e5-large                    ~1.1 GB  1024d  multilingual"
        echo -e "               Best quality. For powerful machines (32+ GB RAM)."
        echo ""
        echo -e "    ${GREEN}2)${NC} Balanced — sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2  ~120 MB  384d   multilingual"
        echo -e "               Good quality, 9x lighter. For 16 GB machines. ${GREEN}(Recommended)${NC}"
        echo ""
        echo -e "    ${GREEN}3)${NC} Light    — sentence-transformers/all-MiniLM-L6-v2            ~22 MB   384d   English"
        echo -e "               Minimal footprint. English queries only."
        echo ""
        read -r -p "  Choice [1/2/3] (default: 2): " MODEL_CHOICE
        MODEL_CHOICE="${MODEL_CHOICE:-2}"

        case "$MODEL_CHOICE" in
            1)
                CURRENT_MODEL="intfloat/multilingual-e5-large"
                ;;
            3)
                CURRENT_MODEL="sentence-transformers/all-MiniLM-L6-v2"
                ;;
            *)
                CURRENT_MODEL="sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
                ;;
        esac

        # Write to .env (use Python for macOS/Linux portability)
        ENV_FILE_PATH="$ENV_FILE" NEW_MODEL="$CURRENT_MODEL" python -c "
import os
env_path = os.environ['ENV_FILE_PATH']
new_model = os.environ['NEW_MODEL']
lines = []
if os.path.exists(env_path):
    with open(env_path) as f:
        lines = [l for l in f.readlines() if not l.startswith('EMBEDDING_MODEL=')]
lines.append(f'EMBEDDING_MODEL={new_model}\n')
with open(env_path, 'w') as f:
    f.writelines(lines)
"
        print_success "Embedding model: $CURRENT_MODEL"
    fi

    print_step "Pre-downloading model and indexing skills/implants..."
    print_step "(this may take a few minutes on first run)"
    set +e
    EMBEDDING_MODEL="$CURRENT_MODEL" REPO_ROOT="$REPO_ROOT" python -c "
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
    AGENT_COUNT=$(find "$AGENTS_BASE" -maxdepth 2 -name "system_prompt.mdc" | wc -l)
    SKILL_COUNT=$(find "$SKILLS_BASE" -name "*.mdc" 2>/dev/null | wc -l)
    IMPLANT_COUNT=$(find "$IMPLANTS_BASE" -name "*.mdc" 2>/dev/null | wc -l)

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
    CURSOR_GLOBAL_DIR="$HOME/.cursor"
    if [ -d "$CURSOR_GLOBAL_DIR" ]; then
        CURSOR_DETECTED=true
        print_success "Cursor IDE detected (~/.cursor/ exists)"
    else
        print_step "Cursor IDE not detected"
    fi

    # --- Detect Claude Desktop ---
    CLAUDE_DESKTOP_DETECTED=false
    CLAUDE_DESKTOP_DIR=""
    CLAUDE_DESKTOP_CONFIG=""
    if [[ "$OSTYPE" == "darwin"* ]]; then
        CLAUDE_DESKTOP_DIR="$HOME/Library/Application Support/Claude"
    elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
        CLAUDE_DESKTOP_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/Claude"
    fi

    if [ -n "$CLAUDE_DESKTOP_DIR" ] && [ -d "$CLAUDE_DESKTOP_DIR" ]; then
        CLAUDE_DESKTOP_DETECTED=true
        CLAUDE_DESKTOP_CONFIG="$CLAUDE_DESKTOP_DIR/claude_desktop_config.json"
        print_success "Claude Desktop detected ($CLAUDE_DESKTOP_DIR)"
    else
        print_step "Claude Desktop not detected"
    fi

    # --- Detect Claude Code ---
    CLAUDE_CODE_DETECTED=false
    if check_command claude || [ -f "$HOME/.claude.json" ] || [ -d "$HOME/.claude" ]; then
        CLAUDE_CODE_DETECTED=true
        print_success "Claude Code detected"
    else
        print_step "Claude Code not detected"
    fi

    echo ""

    # --- Configure Cursor ---
    if [ "$CURSOR_DETECTED" = true ]; then
        print_step "Configuring Cursor MCP (~/.cursor/mcp.json)..."
        MCP_SETTINGS_FILE="$CURSOR_GLOBAL_DIR/mcp.json"

        if [ ! -f "$MCP_SETTINGS_FILE" ]; then
            echo '{ "mcpServers": {} }' > "$MCP_SETTINGS_FILE"
        fi

        # Backup before modifying
        cp "$MCP_SETTINGS_FILE" "${MCP_SETTINGS_FILE}.backup.$(date +%s)"

        if inject_mcp_config "$MCP_SETTINGS_FILE" "~/.cursor/mcp.json"; then
            CONFIGURED_ENVS+=("Cursor")
        fi
    fi

    # --- Configure Claude Desktop ---
    if [ "$CLAUDE_DESKTOP_DETECTED" = true ]; then
        print_step "Configuring Claude Desktop MCP..."

        if [ ! -f "$CLAUDE_DESKTOP_CONFIG" ]; then
            echo '{}' > "$CLAUDE_DESKTOP_CONFIG"
        fi

        # Backup before modifying
        cp "$CLAUDE_DESKTOP_CONFIG" "${CLAUDE_DESKTOP_CONFIG}.backup.$(date +%s)"

        if inject_mcp_config "$CLAUDE_DESKTOP_CONFIG" "Claude Desktop config"; then
            CONFIGURED_ENVS+=("Claude Desktop")
        fi
    fi

    # --- Configure Claude Code ---
    if [ "$CLAUDE_CODE_DETECTED" = true ]; then
        CLAUDE_CODE_DIR="$HOME/.claude"
        # MCP servers must go in ~/.claude.json (not settings.json)
        CLAUDE_CODE_MCP="$HOME/.claude.json"

        # Ensure ~/.claude/ directory exists
        if [ -e "$CLAUDE_CODE_DIR" ] && [ ! -d "$CLAUDE_CODE_DIR" ]; then
            print_error "$CLAUDE_CODE_DIR exists but is not a directory — skipping Claude Code configuration"
        else
        mkdir -p "$CLAUDE_CODE_DIR"

        # 1. MCP server in ~/.claude.json (the only user-scope MCP config Claude Code reads)
        print_step "Configuring Claude Code MCP ($CLAUDE_CODE_MCP)..."

        if [ ! -f "$CLAUDE_CODE_MCP" ]; then
            echo '{}' > "$CLAUDE_CODE_MCP"
        fi

        # Backup before modifying
        cp "$CLAUDE_CODE_MCP" "${CLAUDE_CODE_MCP}.backup.$(date +%s)"

        if inject_mcp_config "$CLAUDE_CODE_MCP" "~/.claude.json"; then
            CONFIGURED_ENVS+=("Claude Code")
        fi

        # 2. Global CLAUDE.md with routing instructions (append, not overwrite)
        CLAUDE_CODE_MD="$CLAUDE_CODE_DIR/CLAUDE.md"
        CLAUDE_MD_SRC="$REPO_ROOT/CLAUDE.md"
        # Markers to delimit managed section
        MARKER_BEGIN="# >>> Agents-Core Routing Protocol (managed by init_repo.sh) >>>"
        MARKER_END="# <<< Agents-Core Routing Protocol (managed by init_repo.sh) <<<"

        print_step "Configuring global CLAUDE.md ($CLAUDE_CODE_MD)..."

        CLAUDE_MD_CONFIGURED=false
        if [ -f "$CLAUDE_MD_SRC" ]; then
            SECTION_CONTENT=$(cat "$CLAUDE_MD_SRC")

            if [ -f "$CLAUDE_CODE_MD" ]; then
                if grep -qF "$MARKER_BEGIN" "$CLAUDE_CODE_MD" 2>/dev/null \
                   && grep -qF "$MARKER_END" "$CLAUDE_CODE_MD" 2>/dev/null; then
                    # Both markers found — replace existing managed section
                    print_step "Found existing Agents-Core section — replacing..."
                    cp "$CLAUDE_CODE_MD" "${CLAUDE_CODE_MD}.backup.$(date +%s)"
                    print_step "Backup created: ${CLAUDE_CODE_MD}.backup.*"

                    # Remove old section and inject new one
                    CLAUDE_CODE_MD="$CLAUDE_CODE_MD" \
                    MARKER_BEGIN="$MARKER_BEGIN" \
                    MARKER_END="$MARKER_END" \
                    SECTION_CONTENT="$SECTION_CONTENT" \
                    "$PYTHON_ABS" -c "
import os, sys

md_path = os.environ['CLAUDE_CODE_MD']
marker_begin = os.environ['MARKER_BEGIN']
marker_end = os.environ['MARKER_END']
section = os.environ['SECTION_CONTENT']

with open(md_path, 'r') as f:
    content = f.read()

# Validate exactly one begin/end pair exists
begin_count = content.count(marker_begin)
end_count = content.count(marker_end)
if begin_count != 1 or end_count != 1:
    print(f'ERROR: expected exactly 1 begin and 1 end marker, found {begin_count} begin and {end_count} end', file=sys.stderr)
    print(f'Please fix markers in {md_path} manually', file=sys.stderr)
    sys.exit(1)

begin_idx = content.find(marker_begin)
end_idx = content.find(marker_end, begin_idx)

if end_idx > begin_idx:
    end_idx += len(marker_end)
    # Consume trailing newlines after marker
    while end_idx < len(content) and content[end_idx] == '\n':
        end_idx += 1
    new_block = f'{marker_begin}\n\n{section}\n\n{marker_end}\n'
    content = content[:begin_idx] + new_block + content[end_idx:]
else:
    print('ERROR: end marker appears before begin marker', file=sys.stderr)
    sys.exit(1)

with open(md_path, 'w') as f:
    f.write(content)
" && { print_success "Agents-Core section replaced in global CLAUDE.md"; CLAUDE_MD_CONFIGURED=true; } \
  || print_error "Failed to replace section — check markers in $CLAUDE_CODE_MD manually"
                else
                    # No managed section yet — append
                    print_step "No existing Agents-Core section — appending..."
                    cp "$CLAUDE_CODE_MD" "${CLAUDE_CODE_MD}.backup.$(date +%s)"
                    print_step "Backup created: ${CLAUDE_CODE_MD}.backup.*"
                    {
                        echo ""
                        echo "$MARKER_BEGIN"
                        echo ""
                        cat "$CLAUDE_MD_SRC"
                        echo ""
                        echo "$MARKER_END"
                    } >> "$CLAUDE_CODE_MD"
                    print_success "Agents-Core section appended to global CLAUDE.md"
                    CLAUDE_MD_CONFIGURED=true
                fi
            else
                # No global CLAUDE.md yet — create with managed section
                print_step "Creating new global CLAUDE.md..."
                {
                    echo "$MARKER_BEGIN"
                    echo ""
                    cat "$CLAUDE_MD_SRC"
                    echo ""
                    echo "$MARKER_END"
                } > "$CLAUDE_CODE_MD"
                print_success "Global CLAUDE.md created with Agents-Core section"
                CLAUDE_MD_CONFIGURED=true
            fi
        else
            print_warn "CLAUDE.md not found in repo root, skipping"
        fi

        # 3. Global memory — persistent reminder to always call route_and_load
        CLAUDE_MEMORY_DIR="$CLAUDE_CODE_DIR/memory"
        MEMORY_FILE="$CLAUDE_MEMORY_DIR/feedback_agents_core_routing.md"
        MEMORY_INDEX="$CLAUDE_MEMORY_DIR/MEMORY.md"

        # Only configure memory if the global CLAUDE.md routing section was successfully written
        if [ "$CLAUDE_MD_CONFIGURED" = true ]; then
            print_step "Configuring global Claude Code memory ($CLAUDE_MEMORY_DIR)..."

            mkdir -p "$CLAUDE_MEMORY_DIR"

            # Only create memory file if it doesn't exist — preserve user edits on re-runs
            if [ ! -f "$MEMORY_FILE" ]; then
                cat > "$MEMORY_FILE" << 'MEMORY_EOF'
---
name: Agents-Core routing is mandatory
description: ALWAYS call route_and_load() before answering any query — no exceptions, no judgment calls
type: feedback
---

Always call `route_and_load(query)` from Agents-Core MCP before responding to any user query.

**Why:** The user explicitly configured this as a BLOCKING REQUIREMENT in CLAUDE.md. The instruction is unconditional — not at the assistant's discretion. There was an incident where routing was skipped "for efficiency" and the user flagged it.

**How to apply:** Before generating any response — call `route_and_load(query)` via the Agents-Core MCP server, then handle the response status per the routing protocol in CLAUDE.md. No exceptions, even if the task seems straightforward.
MEMORY_EOF
                print_success "Memory file written: $MEMORY_FILE"
            else
                print_success "Memory file already exists, preserving: $MEMORY_FILE"
            fi

            # Update MEMORY.md index — add entry if not already present
            MEMORY_ENTRY="- [Agents-Core routing is mandatory](feedback_agents_core_routing.md) — always call route_and_load() before any response, no exceptions"

            if [ -f "$MEMORY_INDEX" ]; then
                if ! grep -qF "feedback_agents_core_routing.md" "$MEMORY_INDEX" 2>/dev/null; then
                    # Ensure a trailing newline before appending
                    [ -s "$MEMORY_INDEX" ] && [ "$(tail -c1 "$MEMORY_INDEX")" != "" ] && printf '\n' >> "$MEMORY_INDEX"
                    echo "$MEMORY_ENTRY" >> "$MEMORY_INDEX"
                    print_success "Entry added to MEMORY.md index"
                else
                    print_success "MEMORY.md index already contains routing entry"
                fi
            else
                echo "$MEMORY_ENTRY" > "$MEMORY_INDEX"
                print_success "MEMORY.md index created"
            fi
        else
            print_warn "Skipping memory setup — global CLAUDE.md routing section was not configured"
        fi

        fi # end: ~/.claude is a directory check
    fi

    # --- Summary ---
    echo ""
    if [ ${#CONFIGURED_ENVS[@]} -eq 0 ]; then
        print_warn "No IDE environments detected"
        print_step "You can configure MCP manually later:"
        echo "    • Cursor:         Install Cursor, then re-run this script"
        echo "    • Claude Desktop: Install Claude Desktop, then re-run this script"
        echo "    • Claude Code:    Install Claude Code, then re-run this script"
    else
        print_success "MCP configured for: ${CONFIGURED_ENVS[*]}"
    fi
fi

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

echo "  $STEP. Test with a command:"
echo -e "     ${CYAN}/route${NC} — check available agents"
echo ""

# ============== Health Check ==============

if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE" 2>/dev/null || true
    if [ -z "$ANTHROPIC_API_KEY" ] || [ "$ANTHROPIC_API_KEY" = "sk-ant-..." ]; then
        print_warn "ANTHROPIC_API_KEY not configured — document OCR will be unavailable"
    fi
fi

echo -e "${GREEN}Happy coding! 🚀${NC}"
echo ""
