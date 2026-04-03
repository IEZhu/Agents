#!/usr/bin/env bash
#
# Agents-Core MCP Setup for Claude Code
#
# What it does:
#   1. Creates/validates Python venv and installs dependencies
#   2. Injects Agents-Core into ~/.claude.json (global MCP config)
#   3. Injects into Claude Desktop config (macOS + Linux)
#   4. Generates CLAUDE.md with the routing protocol
#   5. Optionally creates .mcp.json (project-level config, --local)
#
# Usage:
#   ./scripts/setup_claude.sh [--local] [--skip-venv]
#
# Flags:
#   --local       Also create .mcp.json in the repo root (project-level config)
#   --skip-venv   Skip venv creation (if it already exists)
#   --help        Show this help message

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PATH="$REPO_ROOT/.venv"
PYTHON_MIN_VERSION="3.10"

LOCAL_MCP=false
SKIP_VENV=false

show_help() {
    echo "Agents-Core MCP Setup for Claude Code"
    echo ""
    echo "Usage: ./scripts/setup_claude.sh [--local] [--skip-venv]"
    echo ""
    echo "By default, configures globally (~/.claude.json)."
    echo ""
    echo "Flags:"
    echo "  --local       Also create .mcp.json in the repo root (project-level config)"
    echo "  --skip-venv   Skip venv creation (if it already exists)"
    echo "  --help        Show this help message"
    exit 0
}

for arg in "$@"; do
    case $arg in
        --local)     LOCAL_MCP=true; shift ;;
        --skip-venv) SKIP_VENV=true; shift ;;
        --help|-h)   show_help ;;
    esac
done

# -- Colors ----------------------------------------------------------------
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

print_header()  { echo ""; echo -e "${CYAN}--- $1 ---${NC}"; }
print_step()    { echo -e "  ${GREEN}>${NC} $1"; }
print_warn()    { echo -e "  ${YELLOW}!${NC} $1"; }
print_error()   { echo -e "  ${RED}x${NC} $1"; }
print_success() { echo -e "  ${GREEN}+${NC} $1"; }

echo ""
echo -e "${CYAN}==================================================${NC}"
echo -e "  ${GREEN}Agents-Core MCP Setup for Claude Code${NC}"
echo -e "${CYAN}==================================================${NC}"

# -- Utilities -------------------------------------------------------------
check_command() { command -v "$1" &> /dev/null; }

get_python_version() {
    # Suppress stderr to avoid pyenv shim noise when a version isn't activated
    "$1" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")' 2>/dev/null
}

version_gte() {
    printf '%s\n%s' "$2" "$1" | sort -V -C
}

# -- 1. Python & venv -----------------------------------------------------
print_header "1/5  Python & Virtual Environment"

if [ "$SKIP_VENV" = true ]; then
    if [ -d "$VENV_PATH" ]; then
        print_step "Skipping venv creation (--skip-venv)"
    else
        print_error "--skip-venv specified, but venv not found: $VENV_PATH"
        exit 1
    fi
else
    SELECTED_PYTHON=""
    for candidate in python3.11 python3.10 python3.12 python3; do
        if check_command "$candidate"; then
            VER=$(get_python_version "$candidate") || continue
            [ -z "$VER" ] && continue
            if version_gte "$VER" "$PYTHON_MIN_VERSION"; then
                SELECTED_PYTHON="$candidate"
                break
            fi
        fi
    done

    if [ -z "$SELECTED_PYTHON" ]; then
        print_error "Python >= $PYTHON_MIN_VERSION not found!"
        exit 1
    fi
    print_success "Python: $SELECTED_PYTHON ($(get_python_version "$SELECTED_PYTHON"))"

    if [ ! -d "$VENV_PATH" ]; then
        print_step "Creating venv..."
        "$SELECTED_PYTHON" -m venv "$VENV_PATH"
        print_success "venv created: $VENV_PATH"
    else
        print_success "venv already exists: $VENV_PATH"
    fi

    print_step "Installing dependencies..."
    "$VENV_PATH/bin/pip" install --quiet --upgrade pip
    "$VENV_PATH/bin/pip" install --quiet -r "$REPO_ROOT/requirements.txt"
    print_success "Dependencies installed"
fi

PYTHON_ABS="$VENV_PATH/bin/python"
SERVER_ABS="$REPO_ROOT/src/server.py"

if [ ! -f "$PYTHON_ABS" ]; then
    print_error "Not found: $PYTHON_ABS"
    exit 1
fi
if [ ! -f "$SERVER_ABS" ]; then
    print_error "Not found: $SERVER_ABS"
    exit 1
fi

# -- Helper: inject MCP into a JSON config file ---------------------------
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

# -- 2. Claude Code global config (~/.claude.json) ------------------------
print_header "2/5  Claude Code MCP config (~/.claude.json)"

CLAUDE_GLOBAL="$HOME/.claude.json"

if [ ! -f "$CLAUDE_GLOBAL" ]; then
    print_step "Creating ~/.claude.json..."
    echo '{}' > "$CLAUDE_GLOBAL"
fi

inject_mcp_config "$CLAUDE_GLOBAL" "~/.claude.json"

# -- 3. Claude Desktop config ---------------------------------------------
print_header "3/5  Claude Desktop MCP config"

CLAUDE_DESKTOP_DIR=""
if [[ "$OSTYPE" == "darwin"* ]]; then
    CLAUDE_DESKTOP_DIR="$HOME/Library/Application Support/Claude"
elif [[ "$OSTYPE" == "linux-gnu"* ]]; then
    CLAUDE_DESKTOP_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/Claude"
fi

if [ -n "$CLAUDE_DESKTOP_DIR" ] && [ -d "$CLAUDE_DESKTOP_DIR" ]; then
    CLAUDE_DESKTOP_CONFIG="$CLAUDE_DESKTOP_DIR/claude_desktop_config.json"
    if [ ! -f "$CLAUDE_DESKTOP_CONFIG" ]; then
        print_step "Creating claude_desktop_config.json..."
        echo '{}' > "$CLAUDE_DESKTOP_CONFIG"
    fi
    inject_mcp_config "$CLAUDE_DESKTOP_CONFIG" "Claude Desktop config"
else
    print_step "Claude Desktop not installed -- skipped"
fi

# -- 4. CLAUDE.md (routing protocol) --------------------------------------
print_header "4/5  CLAUDE.md (routing protocol)"

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

print_success "Created: $CLAUDE_MD"

# -- 5. Project-level .mcp.json (optional) --------------------------------
print_header "5/5  Project MCP config (.mcp.json)"

if [ "$LOCAL_MCP" = true ]; then
    MCP_PROJECT="$REPO_ROOT/.mcp.json"

    cat > "$MCP_PROJECT" << MCPEOF
{
  "mcpServers": {
    "Agents-Core": {
      "command": "$PYTHON_ABS",
      "args": ["$SERVER_ABS"]
    }
  }
}
MCPEOF

    print_success "Created: $MCP_PROJECT"
else
    print_step "Skipped (use --local for project-level config)"
    print_step "Global ~/.claude.json is already configured -- that's enough"
fi

# -- .env check -----------------------------------------------------------
echo ""
ENV_FILE="$REPO_ROOT/.env"
if [ ! -f "$ENV_FILE" ]; then
    print_warn ".env not found -- copying from env.example"
    if [ -f "$REPO_ROOT/env.example" ]; then
        cp "$REPO_ROOT/env.example" "$ENV_FILE"
        print_warn "Configure .env -- LANGFUSE_* is optional (observability)"
    else
        print_error "env.example not found!"
    fi
else
    print_success ".env exists"
fi

# -- Summary ---------------------------------------------------------------
echo ""
echo -e "${CYAN}--- Done! ---${NC}"
echo ""
echo -e "  ${GREEN}Created files:${NC}"
echo "    * ~/.claude.json                  -- Claude Code MCP config"
if [ -n "$CLAUDE_DESKTOP_DIR" ] && [ -d "$CLAUDE_DESKTOP_DIR" ]; then
    echo "    * Claude Desktop config            -- Claude Desktop MCP config"
fi
echo "    * CLAUDE.md                        -- Routing protocol"
[ "$LOCAL_MCP" = true ] && echo "    * .mcp.json                        -- Project MCP config"
echo ""
echo -e "  ${GREEN}Next steps:${NC}"
echo "    1. Configure .env (LANGFUSE_* optional, ANTHROPIC_API_KEY for OCR)"
echo "    2. Start Claude Code:"
echo -e "       ${CYAN}cd $REPO_ROOT && claude${NC}"
echo "    3. Verify MCP connection:"
echo -e "       ${CYAN}/mcp${NC}  -- should show Agents-Core"
echo ""
