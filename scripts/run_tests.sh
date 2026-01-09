#!/usr/bin/env bash
# Script to run tests from virtual environment
# Usage: ./scripts/run_tests.sh [pytest arguments]

set -e  # Exit on error

# Repository root
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${GREEN}🧪 Running Language Detection Tests${NC}"
echo "================================================"

# Detect and activate virtual environment
if [ -d ".venv" ]; then
    echo -e "${YELLOW}📦 Using .venv virtual environment${NC}"
    source .venv/bin/activate
    PYTHON_BIN="python"
elif [ -d "venv" ]; then
    echo -e "${YELLOW}📦 Using venv virtual environment${NC}"
    source venv/bin/activate
    PYTHON_BIN="python"
elif command -v pyenv &> /dev/null; then
    # Try to find a specific Python version with dependencies
    # Priority: 3.12.4 (from logs) > any non-system version
    if [ -f "$HOME/.pyenv/versions/3.12.4/bin/python" ]; then
        echo -e "${YELLOW}📦 Using pyenv Python: 3.12.4${NC}"
        PYTHON_BIN="$HOME/.pyenv/versions/3.12.4/bin/python"
    else
        # Find any installed pyenv version (excluding system)
        AVAILABLE_VERSIONS=$(pyenv versions --bare | grep -v "system" | head -n 1 || echo "")
        if [ -n "$AVAILABLE_VERSIONS" ]; then
            echo -e "${YELLOW}📦 Using pyenv Python: $AVAILABLE_VERSIONS${NC}"
            PYTHON_BIN="$HOME/.pyenv/versions/$AVAILABLE_VERSIONS/bin/python"
        else
            echo -e "${RED}❌ No pyenv Python versions found (excluding system)${NC}"
            echo "Install a Python version with: pyenv install 3.12.4"
            exit 1
        fi
    fi
else
    echo -e "${RED}❌ No virtual environment found (.venv, venv) and pyenv not available${NC}"
    echo ""
    echo "Please either:"
    echo "  1. Create a virtual environment: python -m venv .venv"
    echo "  2. Install pyenv: https://github.com/pyenv/pyenv"
    exit 1
fi

# Use python from venv or pyenv
if [ -z "$PYTHON_BIN" ]; then
    PYTHON_BIN="python"
fi

# Check if pytest is installed
if ! $PYTHON_BIN -m pytest --version &> /dev/null; then
    echo -e "${RED}❌ pytest not installed in the environment${NC}"
    echo "Run: pip install -r requirements.txt"
    exit 1
fi

# Run tests with any additional arguments passed to script
echo ""
echo -e "${GREEN}Running: $PYTHON_BIN -m pytest tests/test_language.py -v $@${NC}"
echo ""

$PYTHON_BIN -m pytest tests/test_language.py -v "$@"

# Capture exit code
TEST_EXIT_CODE=$?

echo ""
if [ $TEST_EXIT_CODE -eq 0 ]; then
    echo -e "${GREEN}✅ All tests passed!${NC}"
else
    echo -e "${RED}❌ Some tests failed (exit code: $TEST_EXIT_CODE)${NC}"
fi

exit $TEST_EXIT_CODE
