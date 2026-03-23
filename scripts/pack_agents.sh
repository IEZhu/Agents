#!/usr/bin/env bash
#
# Упаковка настроек агентов и MCP-сервера
#
# Включает:
#   - .cursor/ (agents, commands, hooks, implants, rules, skills, capabilities, schemas)
#   - src/ (MCP-сервер и движок)
#   - scripts/ (init, setup, pack)
#   - requirements.txt, pyproject.toml, mcp.json (sanitized), env.example
#   - README.md, .cursorrules, .gitignore
#
# Исключает приватные данные:
#   - .env, chroma_db/, .venv/, .git/
#   - mcp.json env-секреты (токены, ключи) — заменяются плейсхолдерами
#   - .cursor/plans/, .cursor/state/ (runtime-состояние)
#
# Поддерживает архиваторы: 7z, zip, tar

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DATE=$(date +%Y-%m-%d)
BASE_NAME="Agents_${DATE}"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

log_info()    { echo -e "  ${CYAN}ℹ️${NC}  $1"; }
log_warn()    { echo -e "  ${YELLOW}⚠️${NC}  $1"; }
log_error()   { echo -e "  ${RED}❌${NC} $1"; }
log_success() { echo -e "  ${GREEN}✅${NC} $1"; }

echo ""
echo -e "${CYAN}══════════════════════════════════════════════════${NC}"
echo -e "  ${GREEN}Упаковка Agents (настройки + MCP-сервер)${NC}"
echo -e "${CYAN}══════════════════════════════════════════════════${NC}"
echo ""

cd "$REPO_ROOT"

# ── Временные файлы и cleanup ─────────────────────────────────────
SANITIZED_MCP="$REPO_ROOT/.pack_mcp_sanitized.json"
TEMP_STATE_DIR="$REPO_ROOT/.pack_state_tmp"
STAGING_DIR=$(mktemp -d "${TMPDIR:-/tmp}/pack_agents_XXXXXX")

cleanup() {
    rm -f "$SANITIZED_MCP"
    rm -rf "$TEMP_STATE_DIR"
    rm -rf "$STAGING_DIR"
}
trap cleanup EXIT

# ── Санитизация mcp.json ──────────────────────────────────────────
if [ -f "$REPO_ROOT/mcp.json" ]; then
    PACK_MCP_SRC="$REPO_ROOT/mcp.json" PACK_MCP_DST="$SANITIZED_MCP" \
    python3 -c "
import json, re, os

with open(os.environ['PACK_MCP_SRC']) as f:
    data = json.load(f)

REDACT_PATTERNS = [
    (r'glpat-.*',          'glpat-YOUR_TOKEN_HERE'),
    (r'sk-[A-Za-z0-9-]+',  'sk-YOUR_KEY_HERE'),
    (r'pk-lf-.*',          'pk-lf-YOUR_KEY_HERE'),
    (r'sk-lf-.*',          'sk-lf-YOUR_KEY_HERE'),
    (r'ghp_[A-Za-z0-9]+',  'ghp_YOUR_TOKEN_HERE'),
    (r'xoxb-.*',           'xoxb-YOUR_TOKEN_HERE'),
]

def redact(val):
    if not isinstance(val, str):
        return val
    for pattern, replacement in REDACT_PATTERNS:
        if re.search(pattern, val):
            return replacement
    return val

def walk(obj):
    if isinstance(obj, dict):
        return {k: walk(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [walk(i) for i in obj]
    return redact(obj)

with open(os.environ['PACK_MCP_DST'], 'w') as f:
    json.dump(walk(data), f, indent=2, ensure_ascii=False)

print('OK')
" && log_success "mcp.json санитизирован (токены заменены плейсхолдерами)" \
  || { log_error "Не удалось санитизировать mcp.json"; exit 1; }
else
    log_warn "mcp.json не найден — пропускаем"
fi

# ── Создание временного .cursor/state для архива ──────────────────
mkdir -p "$TEMP_STATE_DIR"
cat > "$TEMP_STATE_DIR/states.md" << 'EOF'
## MCP Status
Enabled: true
Strategy: Call `get_agent_context(agent_name, query)`
EOF

# ── Список включаемых путей ──────────────────────────────────────
INCLUDES=(
    ".cursor/agents"
    ".cursor/capabilities"
    ".cursor/commands"
    ".cursor/hooks"
    ".cursor/implants"
    ".cursor/rules"
    ".cursor/schemas"
    ".cursor/skills"
    ".cursorrules"
    "src"
    "scripts"
    "requirements.txt"
    "pyproject.toml"
    "env.example"
    "README.md"
    ".gitignore"
)

# ── Паттерны исключений ──────────────────────────────────────────
EXCLUDE_PATTERNS=(
    "__pycache__"
    "*.pyc"
    "*.pyo"
    ".DS_Store"
    "*.log"
    "src/ReplicatedStorage"
    "src/ServerScriptService"
    "src/StarterPlayer"
)

# ── Проверка включаемых путей ─────────────────────────────────────
EXISTING_INCLUDES=()
for inc in "${INCLUDES[@]}"; do
    if [ -e "$REPO_ROOT/$inc" ]; then
        EXISTING_INCLUDES+=("$inc")
    else
        log_warn "Путь не найден, пропущен: $inc"
    fi
done

# ── Выбор архиватора ──────────────────────────────────────────────
if command -v 7z &> /dev/null; then
    ARCHIVER="7z"; EXT=".7z"
elif command -v zip &> /dev/null; then
    ARCHIVER="zip"; EXT=".zip"
elif command -v tar &> /dev/null; then
    ARCHIVER="tar"; EXT=".tar.gz"
else
    log_error "Не найден архиватор (7z, zip, tar)!"
    exit 1
fi

ARCHIVE_NAME="${BASE_NAME}${EXT}"
ARCHIVE_PATH="${REPO_ROOT}/${ARCHIVE_NAME}"

[ -f "$ARCHIVE_PATH" ] && rm -f "$ARCHIVE_PATH" && log_info "Удалён старый архив: ${ARCHIVE_NAME}"

log_info "Архиватор: ${ARCHIVER}"
echo ""
echo "  📦 Включаемые пути:"
for inc in "${EXISTING_INCLUDES[@]}"; do
    echo "     • $inc"
done
echo "     • mcp.json (sanitized)"
echo "     • .cursor/state/states.md (template)"
echo ""

# ── Архивация ─────────────────────────────────────────────────────
case "$ARCHIVER" in
    "7z")
        SEVENZIP_EXCLUDES=()
        for p in "${EXCLUDE_PATTERNS[@]}"; do
            SEVENZIP_EXCLUDES+=("-xr!$p")
        done

        7z a -t7z -mx=9 -mmt=on "$ARCHIVE_PATH" \
            "${EXISTING_INCLUDES[@]}" \
            "${SEVENZIP_EXCLUDES[@]}"

        mkdir -p "$STAGING_DIR/.cursor/state"
        cp "$SANITIZED_MCP" "$STAGING_DIR/mcp.json"
        cp "$TEMP_STATE_DIR/states.md" "$STAGING_DIR/.cursor/state/states.md"
        cd "$STAGING_DIR" && 7z a "$ARCHIVE_PATH" mcp.json .cursor/state/states.md
        cd "$REPO_ROOT"
        ;;
    "zip")
        ZIP_EXCLUDES=()
        for excl in "${EXCLUDE_PATTERNS[@]}"; do
            ZIP_EXCLUDES+=("-x" "*/$excl" "-x" "$excl")
        done

        zip -r -9 "$ARCHIVE_PATH" "${EXISTING_INCLUDES[@]}" "${ZIP_EXCLUDES[@]}"

        mkdir -p "$STAGING_DIR/.cursor/state"
        cp "$SANITIZED_MCP" "$STAGING_DIR/mcp.json"
        cp "$TEMP_STATE_DIR/states.md" "$STAGING_DIR/.cursor/state/states.md"
        cd "$STAGING_DIR" && zip -g "$ARCHIVE_PATH" mcp.json .cursor/state/states.md
        cd "$REPO_ROOT"
        ;;
    "tar")
        TAR_EXCLUDES=()
        for excl in "${EXCLUDE_PATTERNS[@]}"; do
            TAR_EXCLUDES+=("--exclude=$excl")
        done

        mkdir -p "$STAGING_DIR/.cursor/state"
        cp "$SANITIZED_MCP" "$STAGING_DIR/mcp.json"
        cp "$TEMP_STATE_DIR/states.md" "$STAGING_DIR/.cursor/state/states.md"

        tar -czf "$ARCHIVE_PATH" "${TAR_EXCLUDES[@]}" \
            "${EXISTING_INCLUDES[@]}" \
            -C "$STAGING_DIR" mcp.json .cursor/state/states.md
        ;;
esac

echo ""
if [[ -f "$ARCHIVE_PATH" ]]; then
    SIZE=$(du -h "$ARCHIVE_PATH" | cut -f1)
    log_success "Архив создан: ${ARCHIVE_PATH}"
    echo "     Размер: ${SIZE}"
    echo ""

    echo -e "  ${YELLOW}⚠️  Перед передачей убедитесь:${NC}"
    echo "     • Нет .env / токенов / ключей в архиве"
    echo "     • Нет chroma_db/ (пересоздаётся автоматически)"
    echo "     • mcp.json содержит плейсхолдеры вместо реальных токенов"
    echo ""
    echo -e "  ${GREEN}🎉 Готово!${NC}"
else
    log_error "Не удалось создать архив."
    exit 1
fi
