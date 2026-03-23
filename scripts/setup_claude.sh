#!/usr/bin/env bash
#
# Настройка Agents-Core MCP для Claude Code
#
# Что делает:
#   1. Создаёт/проверяет Python venv и устанавливает зависимости
#   2. Инжектит Agents-Core в ~/.claude.json (глобальный MCP-конфиг)
#   3. Генерирует CLAUDE.md с протоколом маршрутизации
#   4. Опционально: создаёт .mcp.json (проектный конфиг, --local)
#
# Использование:
#   ./scripts/setup_claude.sh [--local] [--skip-venv]
#
# Флаги:
#   --local       Также создать .mcp.json в корне репо (проектный конфиг)
#   --skip-venv   Пропустить создание venv (если уже есть)
#   --help        Показать справку

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV_PATH="$REPO_ROOT/.venv"
PYTHON_MIN_VERSION="3.10"

LOCAL_MCP=false
SKIP_VENV=false

show_help() {
    echo "Настройка Agents-Core MCP для Claude Code"
    echo ""
    echo "Использование: ./scripts/setup_claude.sh [--local] [--skip-venv]"
    echo ""
    echo "По умолчанию настраивает глобально (~/.claude.json)."
    echo ""
    echo "Флаги:"
    echo "  --local       Также создать .mcp.json в корне репо (проектный конфиг)"
    echo "  --skip-venv   Пропустить создание venv (если уже есть)"
    echo "  --help        Показать эту справку"
    exit 0
}

for arg in "$@"; do
    case $arg in
        --local)     LOCAL_MCP=true; shift ;;
        --skip-venv) SKIP_VENV=true; shift ;;
        --help|-h)   show_help ;;
    esac
done

# ── Цвета ─────────────────────────────────────────────────────────
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

print_header()  { echo ""; echo -e "${CYAN}━━━ $1 ━━━${NC}"; }
print_step()    { echo -e "  ${GREEN}→${NC} $1"; }
print_warn()    { echo -e "  ${YELLOW}⚠${NC} $1"; }
print_error()   { echo -e "  ${RED}✗${NC} $1"; }
print_success() { echo -e "  ${GREEN}✓${NC} $1"; }

echo ""
echo -e "${CYAN}══════════════════════════════════════════════════${NC}"
echo -e "  ${GREEN}Настройка Agents-Core MCP для Claude Code${NC}"
echo -e "${CYAN}══════════════════════════════════════════════════${NC}"

# ── Утилиты ───────────────────────────────────────────────────────
check_command() { command -v "$1" &> /dev/null; }

get_python_version() {
    "$1" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")'
}

version_gte() {
    printf '%s\n%s' "$2" "$1" | sort -V -C
}

# ── 1. Python & venv ─────────────────────────────────────────────
print_header "1/5  Python & Virtual Environment"

if [ "$SKIP_VENV" = true ]; then
    if [ -d "$VENV_PATH" ]; then
        print_step "Пропуск создания venv (--skip-venv)"
    else
        print_error "--skip-venv указан, но venv не найден: $VENV_PATH"
        exit 1
    fi
else
    SELECTED_PYTHON=""
    for candidate in python3.11 python3.10 python3.12 python3; do
        if check_command "$candidate"; then
            VER=$(get_python_version "$candidate")
            if version_gte "$VER" "$PYTHON_MIN_VERSION"; then
                SELECTED_PYTHON="$candidate"
                break
            fi
        fi
    done

    if [ -z "$SELECTED_PYTHON" ]; then
        print_error "Python >= $PYTHON_MIN_VERSION не найден!"
        exit 1
    fi
    print_success "Python: $SELECTED_PYTHON ($(get_python_version "$SELECTED_PYTHON"))"

    if [ ! -d "$VENV_PATH" ]; then
        print_step "Создание venv..."
        "$SELECTED_PYTHON" -m venv "$VENV_PATH"
        print_success "venv создан: $VENV_PATH"
    else
        print_success "venv уже существует: $VENV_PATH"
    fi

    print_step "Установка зависимостей..."
    "$VENV_PATH/bin/pip" install --quiet --upgrade pip
    "$VENV_PATH/bin/pip" install --quiet -r "$REPO_ROOT/requirements.txt"
    print_success "Зависимости установлены"
fi

PYTHON_ABS="$VENV_PATH/bin/python"
SERVER_ABS="$REPO_ROOT/src/server.py"

if [ ! -f "$PYTHON_ABS" ]; then
    print_error "Не найден: $PYTHON_ABS"
    exit 1
fi
if [ ! -f "$SERVER_ABS" ]; then
    print_error "Не найден: $SERVER_ABS"
    exit 1
fi

# ── Helper: inject MCP into a JSON config file ──────────────────
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
" && print_success "Agents-Core добавлен в $label" \
  || { print_error "Не удалось обновить $label"; return 1; }
}

# ── 2. Глобальный конфиг Claude Code (~/.claude.json) ────────────
print_header "2/5  Claude Code MCP-конфиг (~/.claude.json)"

CLAUDE_GLOBAL="$HOME/.claude.json"

if [ ! -f "$CLAUDE_GLOBAL" ]; then
    print_step "Создание ~/.claude.json..."
    echo '{}' > "$CLAUDE_GLOBAL"
fi

inject_mcp_config "$CLAUDE_GLOBAL" "~/.claude.json"

# ── 3. Claude Desktop конфиг ─────────────────────────────────────
print_header "3/5  Claude Desktop MCP-конфиг"

CLAUDE_DESKTOP_DIR="$HOME/Library/Application Support/Claude"
CLAUDE_DESKTOP_CONFIG="$CLAUDE_DESKTOP_DIR/claude_desktop_config.json"

if [ -d "$CLAUDE_DESKTOP_DIR" ]; then
    if [ ! -f "$CLAUDE_DESKTOP_CONFIG" ]; then
        print_step "Создание claude_desktop_config.json..."
        echo '{}' > "$CLAUDE_DESKTOP_CONFIG"
    fi
    inject_mcp_config "$CLAUDE_DESKTOP_CONFIG" "Claude Desktop config"
else
    print_step "Claude Desktop не установлен — пропущено"
    print_step "Путь: $CLAUDE_DESKTOP_DIR"
fi

# ── 3. CLAUDE.md (протокол маршрутизации) ─────────────────────────
print_header "4/5  CLAUDE.md (протокол маршрутизации)"

CLAUDE_MD="$REPO_ROOT/CLAUDE.md"

cat > "$CLAUDE_MD" << 'CLAUDEEOF'
# Agents Framework — Router Protocol for Claude Code

You operate under the **Agents-Core** multi-agent routing system.
Call `route_and_load(query)` to route any user query to the best specialist agent.

## Routing Flow

### Step 1: Route
Call `route_and_load(query)` with the user's message.
- On multi-turn: pass previous `context_hash` for delta mode.

### Step 2: Handle Response

| Status | Action |
|--------|--------|
| **SUCCESS_SAMPLED** | `response` contains the agent's ready-made answer. Display it to the user as-is. |
| **SUCCESS** | `system_prompt` is provided. Use it as context for your answer. |
| **ROUTE_REQUIRED** | Pick the best agent from `candidates`, then call `get_agent_context(agent_name, query)`. |
| **NO_CHANGE** | Context unchanged. Keep current persona. |
| **ERROR** | Answer directly. |

### Step 3: Post-flight
- **Язык ответа**: Отвечай на **русском языке** (кроме блоков кода и цитат).
- **Footer** (в конце каждого ответа):
  > **Agent**: [Agent Name] · **Skills**: [loaded skills] · **Implants**: [loaded implants]
- **Logging**: Call `log_interaction(agent_name, query, response_content)` after responding.

## Available MCP Tools

| Tool | Purpose |
|---|---|
| `route_and_load(query)` | Auto-route: returns agent response (sampling) or system_prompt (fallback) |
| `get_agent_context(agent_name, query)` | Load a specific agent (after ROUTE_REQUIRED) |
| `load_implants(task_type)` | Load reasoning strategies (debugging/analysis/creative/planning) |
| `list_agents()` | List all available agents |
| `log_interaction(...)` | Log the turn to observability backend |
| `clear_session_cache()` | Clear routing cache (use when switching contexts) |

## Environment

- MCP server: `Agents-Core` (stdio transport, Python/FastMCP)
- Agents: `.cursor/agents/[name]/system_prompt.mdc`
- Skills: `.cursor/skills/skill-*.mdc`
- Implants: `.cursor/implants/implant-*.mdc`
- Config: `.env` (LANGFUSE_* optional, ANTHROPIC_API_KEY for document OCR)

## Fallback (if MCP is unavailable)

If `route_and_load` fails:
1. Read `.cursor/agents/` to find the right agent directory
2. Read `.cursor/agents/[name]/system_prompt.mdc`
3. Follow the prompt manually
CLAUDEEOF

print_success "Создан: $CLAUDE_MD"

# ── 4. Проектный .mcp.json (опционально) ─────────────────────────
print_header "5/5  Проектный MCP-конфиг (.mcp.json)"

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

    print_success "Создан: $MCP_PROJECT"
else
    print_step "Пропущено (используйте --local для проектного конфига)"
    print_step "Глобальный ~/.claude.json уже настроен — этого достаточно"
fi

# ── .env проверка ─────────────────────────────────────────────────
echo ""
ENV_FILE="$REPO_ROOT/.env"
if [ ! -f "$ENV_FILE" ]; then
    print_warn ".env не найден — копирую из env.example"
    if [ -f "$REPO_ROOT/env.example" ]; then
        cp "$REPO_ROOT/env.example" "$ENV_FILE"
        print_warn "Настройте .env — LANGFUSE_* опционально (observability)"
    else
        print_error "env.example не найден!"
    fi
else
    print_success ".env существует"
fi

# ── Итог ──────────────────────────────────────────────────────────
echo ""
echo -e "${CYAN}━━━ Готово! ━━━${NC}"
echo ""
echo -e "  ${GREEN}Созданные файлы:${NC}"
echo "    • ~/.claude.json                  — Claude Code MCP-конфиг"
[ -d "$HOME/Library/Application Support/Claude" ] && \
echo "    • Claude Desktop config            — Claude Desktop MCP-конфиг"
echo "    • CLAUDE.md                        — Протокол маршрутизации"
[ "$LOCAL_MCP" = true ] && echo "    • .mcp.json                        — Проектный MCP-конфиг"
echo ""
echo -e "  ${GREEN}Следующие шаги:${NC}"
echo "    1. Настройте .env (LANGFUSE_* опционально, ANTHROPIC_API_KEY для OCR)"
echo "    2. Запустите Claude Code:"
echo -e "       ${CYAN}cd $REPO_ROOT && claude${NC}"
echo "    3. Проверьте подключение MCP:"
echo -e "       ${CYAN}/mcp${NC}  — должен показать Agents-Core"
echo ""
echo -e "  ${YELLOW}Для Claude Desktop Chat (опционально):${NC}"
echo "    Settings → General → Personal preferences"
echo "    Вставьте:"
echo ""
echo -e "    ${CYAN}When Agents-Core MCP server is connected, call route_and_load(query)"
echo "    before answering. If status=SUCCESS_SAMPLED, display the response as-is."
echo "    If status=SUCCESS, use system_prompt as context. If status=ROUTE_REQUIRED,"
echo "    pick best agent from candidates and call get_agent_context(agent_name, query)."
echo "    Отвечай на русском языке (кроме кода)."
echo -e "    В конце: **Agent**: [имя] · **Skills**: [навыки]${NC}"
echo ""
