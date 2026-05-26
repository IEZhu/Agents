#!/usr/bin/env bash
# Shared bench helpers. Source from bench_*.sh scripts. Not directly runnable.
#
# All proxy endpoint values come from .env (OPENAI_BASE_URL / ANTHROPIC_BASE_URL).
# This file derives the local-proxy URL by parsing those env vars — there is no
# hardcoded host:port in the script. If neither base URL points at a localhost /
# 127.0.0.1 address, the proxy-specific preflight checks no-op.
#
# Functions exposed (all start with _bench_):
#   _bench_load_env             — load .env (sources it, exports all KEY=VAL)
#   _bench_resolve_python       — find Python (.venv / venv / pyenv)
#   _bench_local_proxy_url      — print the local proxy origin (scheme://host:port) derived from *_BASE_URL, empty if none
#   _bench_print_endpoints      — echo current OPENAI_BASE_URL, ANTHROPIC_BASE_URL, JUDGE
#   _bench_probe_endpoint       — TCP probe the local proxy when a base_url points there
#   _bench_check_extras <mods…> — verify Python modules are importable
#   _bench_check_model <id> <hint> — verify model is present at /v1/models
#   _bench_check_judge_model    — same for the JUDGE_MODEL env var
#   _bench_warn_if_gemini_judge — soft-warn if JUDGE_MODEL is a Gemini alias
#   _bench_run <label> <args…>  — invoke the Python runner with args
#   _bench_open_report <path> <orig args…> — auto-open HTML unless --dry-run

# Colours expected to be defined by the caller. Fallback if not.
: "${GREEN:=\033[0;32m}"; : "${YELLOW:=\033[1;33m}"; : "${RED:=\033[0;31m}"
: "${BLUE:=\033[0;34m}"; : "${DIM:=\033[2m}"; : "${NC:=\033[0m}"

_bench_load_env() {
    if [ -f "$REPO_ROOT/.env" ]; then
        set -a
        # shellcheck disable=SC1091
        . "$REPO_ROOT/.env"
        set +a
    fi
}

_bench_resolve_python() {
    # Matches the fallback chain used by scripts/eval.sh and scripts/run_tests.sh:
    # prefer a local venv, then the preferred pyenv version, then any non-system
    # pyenv version so machines without exactly 3.12.4 do not fail unexpectedly.
    # `pyenv root` is honored so a custom PYENV_ROOT (e.g. /opt/pyenv) works.
    if [ -d "$REPO_ROOT/.venv" ]; then
        PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
    elif [ -d "$REPO_ROOT/venv" ]; then
        PYTHON_BIN="$REPO_ROOT/venv/bin/python"
    elif command -v pyenv >/dev/null 2>&1; then
        local pyenv_root
        pyenv_root="$(pyenv root 2>/dev/null || echo "$HOME/.pyenv")"
        if [ -f "$pyenv_root/versions/3.12.4/bin/python" ]; then
            PYTHON_BIN="$pyenv_root/versions/3.12.4/bin/python"
        else
            local available_version
            available_version="$(pyenv versions --bare | grep -v '^system$' | head -n 1 || echo '')"
            if [ -n "$available_version" ] && [ -f "$pyenv_root/versions/$available_version/bin/python" ]; then
                PYTHON_BIN="$pyenv_root/versions/$available_version/bin/python"
            else
                echo -e "${RED}❌ No pyenv Python versions found (excluding system).${NC}"
                echo "Install one: pyenv install 3.12.4"
                exit 1
            fi
        fi
    else
        echo -e "${RED}❌ No Python venv found and pyenv not available.${NC}"
        exit 1
    fi
    echo -e "${DIM}→ python: $PYTHON_BIN${NC}"
}

_bench_print_endpoints() {
    echo -e "${DIM}→ OPENAI_BASE_URL: ${OPENAI_BASE_URL:-https://api.openai.com/v1 (default)}${NC}"
    echo -e "${DIM}→ ANTHROPIC_BASE_URL: ${ANTHROPIC_BASE_URL:-https://api.anthropic.com (default)}${NC}"
    echo -e "${DIM}→ GEMINI_BASE_URL: ${GEMINI_BASE_URL:-(unset — Gemini arms route via OPENAI_BASE_URL when proxied)}${NC}"
    echo -e "${DIM}→ JUDGE: ${JUDGE_PROVIDER:-(arm provider)} / ${JUDGE_MODEL:-(provider default)}${NC}"
}

_bench_local_proxy_url() {
    # Print "scheme://host:port" (no trailing slash, no path) if either
    # OPENAI_BASE_URL or ANTHROPIC_BASE_URL points at a local proxy
    # (localhost / 127.0.0.1). Empty output otherwise.
    local url
    for url in "${OPENAI_BASE_URL:-}" "${ANTHROPIC_BASE_URL:-}"; do
        if [[ "$url" =~ ^(https?://(localhost|127\.0\.0\.1)(:[0-9]+)?) ]]; then
            echo "${BASH_REMATCH[1]}"
            return 0
        fi
    done
}

_bench_local_proxy_host_port() {
    # "host:port" form, used for bash's /dev/tcp probe.
    local origin; origin="$(_bench_local_proxy_url)"
    [ -z "$origin" ] && return 0
    # Strip scheme.
    echo "${origin#*://}"
}

_bench_probe_endpoint() {
    # Only probe if any *_BASE_URL targets a local proxy. The host:port is
    # whatever the user put in .env — there is no hardcoded value here.
    local origin; origin="$(_bench_local_proxy_url)"
    [ -z "$origin" ] && return 0
    local hp; hp="$(_bench_local_proxy_host_port)"
    local host="${hp%:*}"
    local port="${hp##*:}"
    # If the URL had no explicit port, `hp` equals `host` and `port` ends up
    # the same — fall back to the scheme default so the probe is meaningful.
    if [ "$host" = "$port" ]; then
        case "$origin" in
            https://*) port=443 ;;
            *) port=80 ;;
        esac
    fi
    if ! (echo > /dev/tcp/"$host"/"$port") 2>/dev/null; then
        echo -e "${RED}❌ Configured endpoint not reachable on ${host}:${port}.${NC}"
        echo "   Start your local proxy, or change *_BASE_URL in .env to a direct provider endpoint."
        exit 1
    fi
    echo -e "${DIM}→ Local proxy: reachable on ${host}:${port}${NC}"
}

_bench_check_extras() {
    local MISSING=""
    for mod in "$@"; do
        "$PYTHON_BIN" -c "import $mod" >/dev/null 2>&1 || MISSING="$MISSING $mod"
    done
    if [ -n "$MISSING" ]; then
        echo -e "${RED}❌ Missing Python packages:${NC}$MISSING"
        echo -e "   Install: ${BLUE}$PYTHON_BIN -m pip install -e '.[evals]'${NC}"
        exit 1
    fi
    echo -e "${DIM}→ eval extras: ok${NC}"
}

_bench_check_model() {
    local model="$1" connect_hint="$2" models_json
    # Only meaningful when going through a local proxy. Endpoint is derived
    # from .env *_BASE_URL — there is no hardcoded host here.
    local origin; origin="$(_bench_local_proxy_url)"
    [ -z "$origin" ] && return 0
    models_json=$(curl -sS --max-time 3 "${origin}/v1/models" 2>/dev/null) || {
        echo -e "${RED}❌ Failed to query ${origin}/v1/models.${NC}"
        exit 1
    }
    if ! echo "$models_json" | grep -qF "\"id\":\"$model\""; then
        echo -e "${RED}❌ Model '$model' is not available at ${origin}.${NC}"
        echo "   Hint: connect '${connect_hint}' in your local proxy's settings,"
        echo -e "   or list available models: ${BLUE}curl -s ${origin}/v1/models | python3 -m json.tool${NC}"
        exit 1
    fi
    echo -e "${DIM}→ Endpoint: '$model' available (arm)${NC}"
}

_bench_check_judge_model() {
    # Read JUDGE_MODEL from env. If unset, runner uses provider default — skip check.
    if [ -z "${JUDGE_MODEL:-}" ]; then
        echo -e "${DIM}→ JUDGE_MODEL not set in .env — runner will use arm provider's default judge${NC}"
        return 0
    fi
    local origin; origin="$(_bench_local_proxy_url)"
    [ -z "$origin" ] && return 0
    local models_json
    models_json=$(curl -sS --max-time 3 "${origin}/v1/models" 2>/dev/null) || return 0
    if ! echo "$models_json" | grep -qF "\"id\":\"$JUDGE_MODEL\""; then
        echo -e "${RED}❌ JUDGE_MODEL='$JUDGE_MODEL' is not available at ${origin}.${NC}"
        echo "   Connect it via your local proxy's settings,"
        echo -e "   or pick a different judge by editing ${BLUE}JUDGE_PROVIDER${NC} / ${BLUE}JUDGE_MODEL${NC} in ${BLUE}.env${NC}."
        exit 1
    fi
    echo -e "${DIM}→ Endpoint: '$JUDGE_MODEL' available (judge)${NC}"
}

_bench_warn_if_gemini_judge() {
    if [[ "${JUDGE_MODEL:-}" == gemini-* ]]; then
        echo -e "${YELLOW}⚠  JUDGE_MODEL='$JUDGE_MODEL' — Gemini-as-judge is unreliable via OpenAI-compat layers (malformed function call / schema ignored).${NC}"
        echo -e "   ${YELLOW}Recommended: set ${BLUE}JUDGE_PROVIDER=anthropic${YELLOW} and ${BLUE}JUDGE_MODEL=claude-sonnet-4-6${YELLOW} (or any non-Gemini) in .env.${NC}"
    fi
}

_bench_run() {
    local label="$1"; shift
    echo ""
    echo -e "${GREEN}▶ Bench: MCP vs vanilla on $label${NC}"
    echo "================================================"
    echo -e "${DIM}args: $*${NC}"
    echo ""
    "$PYTHON_BIN" -m evals.runners.run_mcp_vs_vanilla "$@"
}

_bench_open_report() {
    local out_path="$1"; shift
    # Honour CLI overrides: a user-supplied --out FILE or --out=FILE replaces
    # the default path passed in by the caller. Without this, custom outputs
    # are never opened.
    local expect_out=""
    for arg in "$@"; do
        case "$arg" in
            --dry-run|--dry_run) echo -e "${YELLOW}→ --dry-run: no report to open${NC}"; return 0 ;;
            --out=*) out_path="${arg#--out=}" ;;
            --out) expect_out=1 ;;
            *) if [ -n "$expect_out" ]; then out_path="$arg"; expect_out=""; fi ;;
        esac
    done
    if [ -f "$out_path" ]; then
        echo -e "${GREEN}✓ Report:${NC} $out_path"
        if command -v open >/dev/null 2>&1; then
            open "$out_path"
        elif command -v xdg-open >/dev/null 2>&1; then
            xdg-open "$out_path" >/dev/null 2>&1 &
        fi
    fi
}
