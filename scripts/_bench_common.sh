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

_bench_match_local_origin() {
    # If $1 is an `http(s)://(localhost|127.0.0.1)[:port]…` URL, print the
    # `scheme://host[:port]` prefix. Empty output otherwise. The hostname is
    # anchored with `:` / `/` / end-of-string so `localhost.example.com` is
    # NOT misclassified as a local proxy.
    local url="${1:-}"
    if [[ "$url" =~ ^(https?://(localhost|127\.0\.0\.1)(:[0-9]+)?)(/|$) ]]; then
        echo "${BASH_REMATCH[1]}"
    fi
}

_bench_local_proxy_url_for() {
    # Provider-specific lookup. Reads the matching `*_BASE_URL` from .env so
    # split-proxy setups (e.g. OpenAI on one local port, Anthropic on another)
    # do not collapse onto whichever was listed first.
    case "${1:-}" in
        openai)    _bench_match_local_origin "${OPENAI_BASE_URL:-}" ;;
        anthropic) _bench_match_local_origin "${ANTHROPIC_BASE_URL:-}" ;;
        *)         _bench_local_proxy_url ;;
    esac
}

_bench_local_proxy_url() {
    # Print the first local origin found among OPENAI_BASE_URL /
    # ANTHROPIC_BASE_URL. Used when no provider hint is available (e.g.
    # `_bench_probe_endpoint` only needs to know whether SOMETHING points at
    # a local proxy). Prefer `_bench_local_proxy_url_for <provider>` when
    # the caller knows which side of the bench it is checking.
    local url
    for url in "${OPENAI_BASE_URL:-}" "${ANTHROPIC_BASE_URL:-}"; do
        local origin; origin="$(_bench_match_local_origin "$url")"
        if [ -n "$origin" ]; then
            echo "$origin"
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

_bench_origin_to_host_port() {
    # Translate "scheme://host[:port]" to "host port" — explicit port falls
    # back to 80 / 443 by scheme when omitted. Echoes the pair separated by
    # a space so the caller can `read host port`.
    local origin="${1:-}"
    [ -z "$origin" ] && return 0
    local hp="${origin#*://}"
    local host="${hp%:*}"
    local port="${hp##*:}"
    if [ "$host" = "$port" ]; then
        case "$origin" in
            https://*) port=443 ;;
            *) port=80 ;;
        esac
    fi
    echo "$host $port"
}

_bench_probe_endpoint() {
    # Probe EVERY distinct local origin among OPENAI_BASE_URL /
    # ANTHROPIC_BASE_URL. In split-proxy setups (different ports), both
    # need to be reachable; checking only the first matched origin would
    # mask the other side being down.
    local origins=()
    local seen=""
    local url origin
    for url in "${OPENAI_BASE_URL:-}" "${ANTHROPIC_BASE_URL:-}"; do
        origin="$(_bench_match_local_origin "$url")"
        if [ -n "$origin" ] && [[ "$seen" != *"|${origin}|"* ]]; then
            seen="${seen}|${origin}|"
            origins+=("$origin")
        fi
    done
    [ "${#origins[@]}" -eq 0 ] && return 0
    for origin in "${origins[@]}"; do
        local host port
        read -r host port < <(_bench_origin_to_host_port "$origin")
        if ! (echo > /dev/tcp/"$host"/"$port") 2>/dev/null; then
            echo -e "${RED}❌ Configured endpoint not reachable on ${host}:${port}.${NC}"
            echo "   Start your local proxy, or change *_BASE_URL in .env to a direct provider endpoint."
            exit 1
        fi
        echo -e "${DIM}→ Local proxy: reachable on ${host}:${port}${NC}"
    done
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

_bench_required_sdk_modules() {
    # Echo provider SDK module names required for THIS run, given the arm
    # provider as $1. Includes the judge provider (from env JUDGE_PROVIDER,
    # falling back to the arm) when it differs. The provider name maps 1:1
    # to the SDK module name (`openai`, `anthropic`). No duplicates.
    local arm="${1:-}"
    [ -z "$arm" ] && return 0
    echo "$arm"
    local judge="${JUDGE_PROVIDER:-$arm}"
    if [ -n "$judge" ] && [ "$judge" != "$arm" ]; then
        echo "$judge"
    fi
}

_bench_models_have_id() {
    # Structured JSON check: does `data[].id` of the response contain $1?
    # Avoids the false negatives that `grep "\"id\":\"$model\""` produced on
    # pretty-printed or reordered JSON output. Echoes "yes" or "no" so
    # callers can branch without subshell exit-status pitfalls.
    local target="$1"
    "$PYTHON_BIN" -c '
import json, sys
target = sys.argv[1]
try:
    data = json.load(sys.stdin)
except Exception:
    print("no")
    sys.exit(0)
items = data.get("data", []) if isinstance(data, dict) else data
found = any(isinstance(item, dict) and item.get("id") == target for item in (items or []))
print("yes" if found else "no")
' "$target" 2>/dev/null || echo "no"
}

_bench_check_model() {
    local model="$1" connect_hint="$2" arm_provider="${3:-}" models_json
    # Only meaningful when going through a local proxy. Endpoint is derived
    # from .env *_BASE_URL — there is no hardcoded host here. When the caller
    # passes the arm provider, we look up THAT provider's BASE_URL so a
    # split-proxy setup (OpenAI on one local port, Anthropic on another)
    # queries the right side. Falls back to "first local match" otherwise
    # for backwards compatibility with older bench scripts.
    local origin
    if [ -n "$arm_provider" ]; then
        origin="$(_bench_local_proxy_url_for "$arm_provider")"
    else
        origin="$(_bench_local_proxy_url)"
    fi
    [ -z "$origin" ] && return 0
    models_json=$(curl -sS --max-time 3 "${origin}/v1/models" 2>/dev/null) || {
        echo -e "${RED}❌ Failed to query ${origin}/v1/models.${NC}"
        exit 1
    }
    if [ "$(echo "$models_json" | _bench_models_have_id "$model")" != "yes" ]; then
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
    # JUDGE_PROVIDER (from .env) tells us which *_BASE_URL the judge uses;
    # this matters when arm and judge live on different local proxies.
    # Falls back to the first-local-match helper if JUDGE_PROVIDER is unset.
    local origin
    if [ -n "${JUDGE_PROVIDER:-}" ]; then
        origin="$(_bench_local_proxy_url_for "$JUDGE_PROVIDER")"
    else
        origin="$(_bench_local_proxy_url)"
    fi
    [ -z "$origin" ] && return 0
    local models_json
    # Fail fast on curl errors — silently returning 0 hid judge preflight
    # failures and deferred them to runtime. Match `_bench_check_model`.
    models_json=$(curl -sS --max-time 3 "${origin}/v1/models" 2>/dev/null) || {
        echo -e "${RED}❌ Failed to query ${origin}/v1/models (judge preflight).${NC}"
        exit 1
    }
    if [ "$(echo "$models_json" | _bench_models_have_id "$JUDGE_MODEL")" != "yes" ]; then
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
