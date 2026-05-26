#!/usr/bin/env bash
# ---HELP-BEGIN---
# Wrapper for the Agents-Core eval harness (evals/).
# Usage: ./scripts/eval.sh <command> [args]
#
# Commands:
#   run              Run all deterministic evals; print report to stdout.
#   baseline         Run all evals and write evals/reports/baseline.md (committed).
#   save             Run all evals and save to evals/reports/YYYY-MM-DD_<sha>.md.
#   routing          Run only the routing accuracy eval.
#   retrieval        Run only the skill/implant retrieval eval.
#   tier             Run only the tier inference eval.
#   validate         Probe the configured HuggingFace datasets (no API).
#   prepare          Re-sample queries → evals/datasets/_unlabeled.jsonl and refresh batches.
#   aggregate        Join labeled batches into evals/datasets/routing.jsonl (run after labeling).
#   bench [args]     Benchmark MCP vs vanilla LLM on N queries; render HTML report.
#                    Provider via --provider openai (default) | anthropic.
#                    Passes args through (e.g. --n 10 --dataset wildbench --dry-run).
#   show [path]      cat the report at <path> (default: evals/reports/baseline.md).
#   diff <new>       Show unified diff of baseline.md vs <new>.
#   help             Print this message.
#
# Examples:
#   ./scripts/eval.sh run
#   ./scripts/eval.sh save && ./scripts/eval.sh diff evals/reports/2026-05-03_*.md
#   ./scripts/eval.sh routing --json | jq '.top1_accuracy'
# ---HELP-END---

set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

# ---- venv discovery (mirrors scripts/_bench_common.sh::_bench_resolve_python) ----
# `pyenv root` is honored so a custom PYENV_ROOT (e.g. /opt/pyenv) works.
if [ -d ".venv" ]; then
    PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
elif [ -d "venv" ]; then
    PYTHON_BIN="$REPO_ROOT/venv/bin/python"
elif command -v pyenv &> /dev/null; then
    PYENV_ROOT_DIR="$(pyenv root 2>/dev/null || echo "$HOME/.pyenv")"
    if [ -f "$PYENV_ROOT_DIR/versions/3.12.4/bin/python" ]; then
        PYTHON_BIN="$PYENV_ROOT_DIR/versions/3.12.4/bin/python"
    else
        AVAILABLE_VERSION="$(pyenv versions --bare | grep -v '^system$' | head -n 1 || echo '')"
        if [ -n "$AVAILABLE_VERSION" ] && [ -f "$PYENV_ROOT_DIR/versions/$AVAILABLE_VERSION/bin/python" ]; then
            PYTHON_BIN="$PYENV_ROOT_DIR/versions/$AVAILABLE_VERSION/bin/python"
        else
            echo -e "${RED}❌ No pyenv Python versions found (excluding system).${NC}"
            echo "Install one: pyenv install 3.12.4"
            exit 1
        fi
    fi
else
    echo -e "${RED}❌ No virtual environment found.${NC}"
    echo "Create one: python -m venv .venv && pip install -e '.[evals]'"
    exit 1
fi

require_datasets() {
    # Only commands that hit HuggingFace need the optional `datasets` extra.
    # Help, show, diff, and aggregate operate on local files only — checking
    # at the top blocks them in venvs without the extra (and also blocks
    # `label_with_claude --preview 0`, which short-circuits to zero rows).
    if ! "$PYTHON_BIN" -c 'import datasets' &> /dev/null; then
        echo -e "${RED}❌ 'datasets' not installed in this venv.${NC}"
        echo "Install eval deps: $PYTHON_BIN -m pip install -e '.[evals]'"
        exit 1
    fi
}

usage() {
    # Print everything between ---HELP-BEGIN--- and ---HELP-END--- markers,
    # stripped of the leading "# " prefix. Robust against new commands being
    # added without bumping a hard-coded line range. Uses awk for portability
    # (BSD/macOS sed disagrees with GNU sed on BRE alternation).
    awk '
        /^# ---HELP-BEGIN---$/ { flag = 1; next }
        /^# ---HELP-END---$/   { flag = 0 }
        flag                   { sub(/^# ?/, ""); print }
    ' "$0"
}

cmd="${1:-run}"
shift || true

case "$cmd" in
    run)
        require_datasets
        echo -e "${GREEN}▶ Running all evals (deterministic, ~30s)${NC}"
        echo "================================================"
        "$PYTHON_BIN" -m evals.runners.run_all "$@"
        ;;

    baseline)
        require_datasets
        echo -e "${GREEN}▶ Updating baseline.md${NC}"
        echo "================================================"
        "$PYTHON_BIN" -m evals.runners.run_all --baseline "$@"
        echo ""
        echo -e "${BLUE}Diff vs HEAD:${NC}"
        git diff --stat -- evals/reports/baseline.md || true
        ;;

    save)
        require_datasets
        sha="$(git rev-parse --short HEAD 2>/dev/null || echo nogit)"
        date_str="$(date +%F)"
        out="evals/reports/${date_str}_${sha}.md"
        echo -e "${GREEN}▶ Saving report to ${out}${NC}"
        echo "================================================"
        "$PYTHON_BIN" -m evals.runners.run_all --out "$out" "$@"
        echo ""
        echo "Path: $out  (gitignored)"
        ;;

    routing)
        require_datasets
        "$PYTHON_BIN" -m evals.runners.run_routing "$@"
        ;;

    retrieval)
        require_datasets
        "$PYTHON_BIN" -m evals.runners.run_retrieval "$@"
        ;;

    tier)
        require_datasets
        "$PYTHON_BIN" -m evals.runners.run_tier "$@"
        ;;

    validate)
        require_datasets
        echo -e "${GREEN}▶ Validating source datasets via HuggingFace${NC}"
        echo "================================================"
        "$PYTHON_BIN" -m evals.scripts.fetch --validate
        ;;

    prepare)
        require_datasets
        echo -e "${GREEN}▶ Sampling 110 queries → _unlabeled.jsonl${NC}"
        echo "================================================"
        "$PYTHON_BIN" -m evals.scripts.label_with_claude --prepare "$@"
        echo ""
        echo -e "${GREEN}▶ Splitting into 5 labeling batches${NC}"
        "$PYTHON_BIN" -m evals.scripts.prepare_batches --batches 5
        echo ""
        echo -e "${YELLOW}Next:${NC} dispatch labeling (Claude Code Agent tool or --label with API key)"
        echo "       → after labeling: ./scripts/eval.sh aggregate"
        ;;

    aggregate)
        echo -e "${GREEN}▶ Aggregating batch labels → routing.jsonl${NC}"
        echo "================================================"
        "$PYTHON_BIN" -m evals.scripts.aggregate_labels "$@"
        ;;

    bench)
        require_datasets
        # Source .env so OPENAI_API_KEY / ANTHROPIC_API_KEY / OPENAI_BASE_URL /
        # ANTHROPIC_BASE_URL / JUDGE_PROVIDER / JUDGE_MODEL reach the runner.
        # The thin scripts/bench_*.sh wrappers do this via _bench_load_env; the
        # `eval.sh bench` path needs the same so it doesn't fail preflight
        # purely because .env was not exported into the calling shell.
        # shellcheck disable=SC1091
        source "$REPO_ROOT/scripts/_bench_common.sh"
        _bench_load_env
        # Reuse the bench common preflight so dependency / proxy mistakes fail
        # at the script edge rather than mid-run with a Python traceback.
        # Provider-specific model checks need to know --provider, which is
        # inside "$@" — we parse it out without disturbing the original args.
        BENCH_PROVIDER="openai"  # matches run_mcp_vs_vanilla.parse_args default
        prev=""
        for arg in "$@"; do
            if [ "$prev" = "--provider" ]; then BENCH_PROVIDER="$arg"; prev=""; continue; fi
            case "$arg" in
                --provider=*) BENCH_PROVIDER="${arg#--provider=}" ;;
                --provider)   prev="--provider" ;;
            esac
        done
        _bench_print_endpoints
        _bench_probe_endpoint
        # shellcheck disable=SC2046
        _bench_check_extras datasets jinja2 $(_bench_required_sdk_modules "$BENCH_PROVIDER")
        echo -e "${GREEN}▶ Benchmarking MCP vs vanilla LLM${NC}"
        echo "================================================"
        "$PYTHON_BIN" -m evals.runners.run_mcp_vs_vanilla "$@"
        ;;

    show)
        path="${1:-evals/reports/baseline.md}"
        if [ ! -f "$path" ]; then
            echo -e "${RED}❌ ${path} not found${NC}"
            exit 1
        fi
        if command -v glow &> /dev/null; then
            glow "$path"
        elif command -v bat &> /dev/null; then
            bat --style=plain --paging=never "$path"
        else
            cat "$path"
        fi
        ;;

    diff)
        if [ $# -lt 1 ]; then
            echo -e "${RED}❌ Usage: $0 diff <new-report-path>${NC}"
            exit 1
        fi
        new="$1"
        old="evals/reports/baseline.md"
        if [ ! -f "$old" ]; then
            echo -e "${RED}❌ ${old} missing — run './scripts/eval.sh baseline' first${NC}"
            exit 1
        fi
        if [ ! -f "$new" ]; then
            echo -e "${RED}❌ ${new} not found${NC}"
            exit 1
        fi
        echo -e "${BLUE}▶ Diff: ${old} → ${new}${NC}"
        echo "================================================"
        diff -u "$old" "$new" || true
        ;;

    help|-h|--help)
        usage
        ;;

    *)
        echo -e "${RED}❌ Unknown command: ${cmd}${NC}"
        echo ""
        usage
        exit 1
        ;;
esac
