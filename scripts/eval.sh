#!/usr/bin/env bash
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
#   show [path]      cat the report at <path> (default: evals/reports/baseline.md).
#   diff <new>       Show unified diff of baseline.md vs <new>.
#   help             Print this message.
#
# Examples:
#   ./scripts/eval.sh run
#   ./scripts/eval.sh save && ./scripts/eval.sh diff evals/reports/2026-05-03_*.md
#   ./scripts/eval.sh routing --json | jq '.top1_accuracy'

set -e

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m'

# ---- venv discovery (mirrors scripts/run_tests.sh) ----
if [ -d ".venv" ]; then
    PYTHON_BIN="$REPO_ROOT/.venv/bin/python"
elif [ -d "venv" ]; then
    PYTHON_BIN="$REPO_ROOT/venv/bin/python"
elif command -v pyenv &> /dev/null && [ -f "$HOME/.pyenv/versions/3.12.4/bin/python" ]; then
    PYTHON_BIN="$HOME/.pyenv/versions/3.12.4/bin/python"
else
    echo -e "${RED}❌ No virtual environment found.${NC}"
    echo "Create one: python -m venv .venv && pip install -e '.[evals]'"
    exit 1
fi

if ! "$PYTHON_BIN" -c 'import datasets' &> /dev/null; then
    echo -e "${RED}❌ 'datasets' not installed in this venv.${NC}"
    echo "Install eval deps: $PYTHON_BIN -m pip install -e '.[evals]'"
    exit 1
fi

usage() {
    sed -n '2,22p' "$0" | sed 's/^# \?//'
}

cmd="${1:-run}"
shift || true

case "$cmd" in
    run)
        echo -e "${GREEN}▶ Running all evals (deterministic, ~30s)${NC}"
        echo "================================================"
        "$PYTHON_BIN" -m evals.runners.run_all "$@"
        ;;

    baseline)
        echo -e "${GREEN}▶ Updating baseline.md${NC}"
        echo "================================================"
        "$PYTHON_BIN" -m evals.runners.run_all --baseline "$@"
        echo ""
        echo -e "${BLUE}Diff vs HEAD:${NC}"
        git diff --stat -- evals/reports/baseline.md || true
        ;;

    save)
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
        "$PYTHON_BIN" -m evals.runners.run_routing "$@"
        ;;

    retrieval)
        "$PYTHON_BIN" -m evals.runners.run_retrieval "$@"
        ;;

    tier)
        "$PYTHON_BIN" -m evals.runners.run_tier "$@"
        ;;

    validate)
        echo -e "${GREEN}▶ Validating source datasets via HuggingFace${NC}"
        echo "================================================"
        "$PYTHON_BIN" -m evals.scripts.fetch --validate
        ;;

    prepare)
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
