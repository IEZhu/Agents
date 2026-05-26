#!/usr/bin/env bash
# ---HELP-BEGIN---
# Bench MCP vs vanilla on OpenAI gpt-5.5 (arms).
#
# Endpoints, API keys, and judge are all controlled by .env — this script only
# picks the arm model. Switch judge by setting JUDGE_PROVIDER/JUDGE_MODEL in .env.
#
# Defaults: --provider openai --model gpt-5.5 --dataset wildbench --n 1
# ---HELP-END---

set -euo pipefail
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

MODEL="gpt-5.5"
PROVIDER="openai"
LABEL="gpt-5.5"
CONNECT_HINT="ChatGPT (Codex)"
OUT_SLUG="gpt55"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; BLUE='\033[0;34m'; DIM='\033[2m'; NC='\033[0m'
usage() { awk '/^# ---HELP-BEGIN---$/{f=1;next} /^# ---HELP-END---$/{f=0} f{sub(/^# ?/,"");print}' "$0"; }
case "${1:-}" in -h|--help|help) usage; exit 0 ;; esac

source "$REPO_ROOT/scripts/_bench_common.sh"
_bench_load_env
_bench_resolve_python
_bench_print_endpoints
_bench_probe_endpoint
_bench_check_extras datasets jinja2 openai anthropic
_bench_check_model "$MODEL" "$CONNECT_HINT" "$PROVIDER"
_bench_check_judge_model

OUT="$REPO_ROOT/evals/reports/$(date +%F)_mcp_vs_vanilla_${OUT_SLUG}.html"
DEFAULT_ARGS=(--provider "$PROVIDER" --model "$MODEL" --dataset wildbench --n 1 --out "$OUT")

_bench_run "$LABEL" "${DEFAULT_ARGS[@]}" "$@"
_bench_open_report "$OUT" "$@"
