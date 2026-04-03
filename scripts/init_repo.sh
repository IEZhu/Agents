#!/usr/bin/env bash
#
# Agents Repository Initialization Script
# ========================================
# This script sets up the development environment after cloning.
#
# Usage:
#   ./scripts/init_repo.sh [--skip-env] [--skip-chroma]
#
# Flags:
#   --skip-env     Skip .env file creation (useful if already configured)
#   --skip-chroma  Skip ChromaDB initialization
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
        --help|-h)
            head -20 "$0" | tail -n +2 | sed 's/^# //' | sed 's/^#//'
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
    $1 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
}

version_gte() {
    # Returns 0 (true) if $1 >= $2
    printf '%s\n%s' "$2" "$1" | sort -V -C
}

version_lt() {
    # Returns 0 (true) if $1 < $2
    ! version_gte "$1" "$2"
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
             VER=$(get_python_version "$CANDIDATE")
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

# ============== Cursor Integration & MCP Settings ==============

print_header "🖥️  Cursor IDE & MCP Integration"

# Detect asset directories (new root-level layout, fallback to legacy .cursor/)
if [ -d "$REPO_ROOT/agents" ]; then
    AGENTS_BASE="$REPO_ROOT/agents"
    SKILLS_BASE="$REPO_ROOT/skills"
    IMPLANTS_BASE="$REPO_ROOT/implants"
    COMMANDS_BASE="$REPO_ROOT/commands"
elif [ -d "$REPO_ROOT/.cursor/agents" ]; then
    AGENTS_BASE="$REPO_ROOT/.cursor/agents"
    SKILLS_BASE="$REPO_ROOT/.cursor/skills"
    IMPLANTS_BASE="$REPO_ROOT/.cursor/implants"
    COMMANDS_BASE="$REPO_ROOT/.cursor/commands"
else
    AGENTS_BASE=""
fi

if [ -n "$AGENTS_BASE" ] && [ -d "$AGENTS_BASE" ]; then
    AGENT_COUNT=$(find "$AGENTS_BASE" -maxdepth 1 -type d | wc -l)
    SKILL_COUNT=$(find "$SKILLS_BASE" -name "*.mdc" 2>/dev/null | wc -l)
    IMPLANT_COUNT=$(find "$IMPLANTS_BASE" -name "*.mdc" 2>/dev/null | wc -l)
    COMMAND_COUNT=$(find "$COMMANDS_BASE" -name "*.md" 2>/dev/null | wc -l)

    print_success "Agents directory found: $AGENTS_BASE"
    echo -e "    • ${CYAN}$((AGENT_COUNT - 1))${NC} agents"
    echo -e "    • ${CYAN}$SKILL_COUNT${NC} skills"
    echo -e "    • ${CYAN}$IMPLANT_COUNT${NC} implants"
    echo -e "    • ${CYAN}$COMMAND_COUNT${NC} commands"
else
    print_error "Agents directory not found (checked agents/ and .cursor/agents/)"
fi

# Detect OS and set Cursor config path
# Cursor now uses a global mcp.json in ~/.cursor/mcp.json
if [[ "$OSTYPE" == "darwin"* ]] || [[ "$OSTYPE" == "linux-gnu"* ]]; then
    CURSOR_GLOBAL_DIR="$HOME/.cursor"
    MCP_SETTINGS_FILE="$CURSOR_GLOBAL_DIR/mcp.json"
else
    print_warn "Unsupported OS for automatic MCP configuration: $OSTYPE"
    CURSOR_GLOBAL_DIR=""
fi

if [ -n "$CURSOR_GLOBAL_DIR" ]; then
    # Ensure ~/.cursor directory exists
    if [ ! -d "$CURSOR_GLOBAL_DIR" ]; then
        print_step "Creating $CURSOR_GLOBAL_DIR directory..."
        mkdir -p "$CURSOR_GLOBAL_DIR"
    fi

    # Create empty mcp.json if not exists
    if [ ! -f "$MCP_SETTINGS_FILE" ]; then
        print_step "Creating new global MCP config..."
        echo '{ "mcpServers": {} }' > "$MCP_SETTINGS_FILE"
    fi

    if [ -f "$MCP_SETTINGS_FILE" ]; then
        print_step "Found global Cursor MCP settings at ~/.cursor/mcp.json"

        # Backup existing settings
        BACKUP_FILE="${MCP_SETTINGS_FILE}.backup.$(date +%s)"
        cp "$MCP_SETTINGS_FILE" "$BACKUP_FILE"

        # Merge MCP configs using Python
        print_step "Merging MCP server configs..."

        # Validate mcp.json first
        MCP_CONFIG="$REPO_ROOT/mcp.json"
        if [ -f "$MCP_CONFIG" ] && python3 -c "import json; json.load(open('$MCP_CONFIG'))" 2>/dev/null; then

            # Export variables for Python script
            export REPO_ROOT
            export MCP_SETTINGS_FILE

            python3 << 'PYTHON_SCRIPT'
import json
import sys
import os

repo_root = os.environ['REPO_ROOT']
mcp_settings_file = os.environ['MCP_SETTINGS_FILE']
mcp_json_path = os.path.join(repo_root, 'mcp.json')

# Load existing Cursor settings
try:
    with open(mcp_settings_file, 'r') as f:
        cursor_settings = json.load(f)
except json.JSONDecodeError:
    cursor_settings = {"mcpServers": {}}

# Load our mcp.json
with open(mcp_json_path, 'r') as f:
    our_mcp = json.load(f)

# Ensure mcpServers exists
if 'mcpServers' not in cursor_settings:
    cursor_settings['mcpServers'] = {}

# Merge servers (convert relative paths to absolute)
for server_name, server_config in our_mcp.get('mcpServers', {}).items():
    # Convert relative command path to absolute
    if server_config['command'].startswith('.venv/'):
        server_config['command'] = os.path.join(repo_root, server_config['command'])

    # Convert relative args to absolute
    updated_args = []
    for arg in server_config.get('args', []):
        if arg.startswith('src/'):
            updated_args.append(os.path.join(repo_root, arg))
        else:
            updated_args.append(arg)
    server_config['args'] = updated_args

    # Add or update
    cursor_settings['mcpServers'][server_name] = server_config
    print(f"  ✓ Added/Updated: {server_name}", file=sys.stderr)

# Write back
with open(mcp_settings_file, 'w') as f:
    json.dump(cursor_settings, f, indent=2)

print("SUCCESS", file=sys.stderr)
PYTHON_SCRIPT

            if [ $? -eq 0 ]; then
                print_success "MCP servers added to ~/.cursor/mcp.json"
            else
                print_error "Failed to merge MCP settings"
            fi
        else
            print_error "mcp.json missing or invalid"
        fi

    else
        print_error "Failed to create/access $MCP_SETTINGS_FILE"
    fi
else
    print_step "Skipping MCP injection (OS detection failed)"
fi

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

# ============== Final Summary ==============

print_header "✅ Initialization Complete!"

echo ""
echo -e "  ${GREEN}Next steps:${NC}"
echo ""
echo "  1. Configure API keys in .env (if you haven't yet):"
echo -e "     ${CYAN}nano $ENV_FILE${NC}"
echo ""
echo "  2. Restart Cursor IDE to activate new MCP servers"
echo ""
echo "  3. Test with a command:"
echo -e "     ${CYAN}/route${NC} - check available agents"
echo ""

# ============== Health Check ==============

if [ -f "$ENV_FILE" ]; then
    source "$ENV_FILE" 2>/dev/null || true
    if [ -z "$ANTHROPIC_API_KEY" ] || [ "$ANTHROPIC_API_KEY" = "sk-ant-..." ]; then
        print_warn "ANTHROPIC_API_KEY not configured - document OCR will be unavailable"
    fi
fi

echo -e "${GREEN}Happy coding! 🚀${NC}"
echo ""
