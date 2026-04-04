#!/usr/bin/env bash
#
# Agents Repository Initialization Script
# ========================================
# This script sets up the development environment after cloning.
#
# Usage:
#   ./scripts/init_repo.sh [--skip-env] [--skip-chroma] [--skip-mcp]
#
# Flags:
#   --skip-env     Skip .env file creation (useful if already configured)
#   --skip-chroma  Skip ChromaDB initialization
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
SKIP_CHROMA=false
SKIP_MCP=false

for arg in "$@"; do
    case $arg in
        --skip-env)
            SKIP_ENV=true
            shift
            ;;
        --skip-chroma)
            SKIP_CHROMA=true
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
    python3 -c "
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

    # Pre-download embedding model AND pre-index ChromaDB so MCP server starts instantly.
    # Without this, first startup takes 30-60s for embedding generation,
    # causing Claude Desktop to time out with "Request timed out" (-32001).
    print_header "🧠 Pre-downloading Embedding Model & Indexing ChromaDB"

    print_step "Downloading BAAI/bge-m3 and indexing skills/implants..."
    print_step "(this may take a few minutes on first run)"
    set +e
    python -c "
import sys, os
sys.path.insert(0, os.getcwd())

# 1. Download/cache the embedding model
from sentence_transformers import SentenceTransformer
model = SentenceTransformer('BAAI/bge-m3')
print('Embedding model ready')

# 2. Pre-index skills and implants into ChromaDB
from src.engine.skills import SkillRetriever
from src.engine.implants import ImplantRetriever

sr = SkillRetriever()
print(f'Skills indexed: {sr.collection.count()} entries')

ir = ImplantRetriever()
print(f'Implants indexed: {ir.collection.count()} entries')
" 2>&1
    INDEX_EXIT=$?
    set -e

    if [ $INDEX_EXIT -eq 0 ]; then
        print_success "Embedding model cached, skills & implants indexed"
    else
        print_warn "Pre-indexing failed — it will run on first MCP server start"
    fi
else
    print_step "Skipping package installation"
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
    if check_command claude || [ -f "$HOME/.claude.json" ]; then
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

        inject_mcp_config "$MCP_SETTINGS_FILE" "~/.cursor/mcp.json"
        CONFIGURED_ENVS+=("Cursor")
    fi

    # --- Configure Claude Desktop ---
    if [ "$CLAUDE_DESKTOP_DETECTED" = true ]; then
        print_step "Configuring Claude Desktop MCP..."

        if [ ! -f "$CLAUDE_DESKTOP_CONFIG" ]; then
            echo '{}' > "$CLAUDE_DESKTOP_CONFIG"
        fi

        # Backup before modifying
        cp "$CLAUDE_DESKTOP_CONFIG" "${CLAUDE_DESKTOP_CONFIG}.backup.$(date +%s)"

        inject_mcp_config "$CLAUDE_DESKTOP_CONFIG" "Claude Desktop config"
        CONFIGURED_ENVS+=("Claude Desktop")
    fi

    # --- Configure Claude Code ---
    if [ "$CLAUDE_CODE_DETECTED" = true ]; then
        print_step "Configuring Claude Code MCP (~/.claude.json)..."
        CLAUDE_GLOBAL="$HOME/.claude.json"

        if [ ! -f "$CLAUDE_GLOBAL" ]; then
            echo '{}' > "$CLAUDE_GLOBAL"
        fi

        inject_mcp_config "$CLAUDE_GLOBAL" "~/.claude.json"
        CONFIGURED_ENVS+=("Claude Code")
    fi

    # --- Summary ---
    echo ""
    if [ ${#CONFIGURED_ENVS[@]} -eq 0 ]; then
        print_warn "No IDE environments detected"
        print_step "You can configure MCP manually later:"
        echo "    • Cursor:         Install Cursor, then re-run this script"
        echo "    • Claude Desktop: Install Claude Desktop, then re-run this script"
        echo "    • Claude Code:    Run ./scripts/setup_claude.sh"
    else
        print_success "MCP configured for: ${CONFIGURED_ENVS[*]}"
    fi
fi

# ============== CLAUDE.md Routing Protocol ==============

print_header "📋 CLAUDE.md Routing Protocol"

CLAUDE_MD="$REPO_ROOT/CLAUDE.md"

cat > "$CLAUDE_MD" << 'CLAUDEEOF'
# CRITICAL: Agents-Core Routing Protocol

IMPORTANT: These instructions OVERRIDE any default behavior. You MUST follow them exactly.

## BLOCKING REQUIREMENT — MCP Routing

**Before answering ANY user query, you MUST call `route_and_load(query)` from the Agents-Core MCP server.** This is not optional. Do NOT answer directly — always route first.

This applies to ALL queries: coding, research, questions, documentation, debugging — everything.

### Routing Flow

1. **ALWAYS call `route_and_load(query)` FIRST** with the user's message.
   - On multi-turn: pass previous `context_hash` for delta mode.

2. **Handle the response based on status:**
   - `SUCCESS_SAMPLED` → Display `response` to the user as-is. Do not modify.
   - `SUCCESS` → Use `system_prompt` as context for your answer.
   - `ROUTE_REQUIRED` → **STOP all other actions.** Do NOT call any other tools in parallel.
     Pick best agent from `candidates`, call `get_agent_context(agent_name, query)` as your ONLY next action.
     Wait for its response before doing anything else.
   - `NO_CHANGE` → Context unchanged. Keep current persona.
   - `ERROR` → Answer directly (only in this case).

3. **Post-flight (after EVERY response):**
   - Respond in the same language as the user's query (auto-detect). Exceptions: code blocks, technical terms, and tool/CLI output stay in English.
   - Append at the end: **Agent**: [name] · **Skills**: [skills] · **Implants**: [implants]
   - Call `log_interaction(agent_name, query, response_content)`.

## Available MCP Tools

| Tool | Purpose |
|---|---|
| `route_and_load(query)` | **MUST call first** — routes to best specialist agent |
| `get_agent_context(agent_name, query)` | Load a specific agent (after ROUTE_REQUIRED) |
| `load_implants(task_type)` | Load reasoning strategies (debugging/analysis/creative/planning) |
| `list_agents()` | List all available agents |
| `log_interaction(...)` | Log the turn to observability backend |
| `clear_session_cache()` | Clear routing cache (use when switching contexts) |

## Environment

- MCP server: `Agents-Core` (stdio transport, Python/FastMCP)
- Agents: `agents/[name]/system_prompt.mdc`
- Skills: `skills/skill-*.mdc`
- Implants: `implants/implant-*.mdc`
- Capabilities: `agents/capabilities/registry.yaml`
- Config: `.env` (LANGFUSE_* optional, ANTHROPIC_API_KEY for document OCR)

## Fallback (if MCP is unavailable)

If `route_and_load` fails or Agents-Core MCP is not connected:
1. Read `agents/` to find the right agent directory
2. Read `agents/[name]/system_prompt.mdc`
3. Follow the prompt manually
CLAUDEEOF

print_success "Created: CLAUDE.md"

# ============== ChromaDB Initialization ==============

print_header "🗄️  ChromaDB Initialization"

CHROMA_PATH="$REPO_ROOT/chroma_db"

if [ "$SKIP_CHROMA" = false ]; then
    if [ -d "$CHROMA_PATH" ] && [ "$(ls -A $CHROMA_PATH 2>/dev/null)" ]; then
        print_success "ChromaDB already initialized at $CHROMA_PATH"
    else
        print_step "ChromaDB will be initialized on first server run"
    fi
else
    print_step "Skipping ChromaDB check (--skip-chroma)"
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
echo -e "    ${GREEN}✓${NC} CLAUDE.md — routing protocol for Claude agents"

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
                echo "  $STEP. Start Claude Code in this directory:"
                echo -e "     ${CYAN}cd $REPO_ROOT && claude${NC}"
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
