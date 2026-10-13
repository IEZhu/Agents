#!/usr/bin/env bash
#
# Remove Agents-Core from this user account (macOS and Linux).
#
#   ~/.agents-core/scripts/uninstall.sh [--dry-run] [--yes] [--keep-data] [--include-credentials]
#
# Stops and removes the service and every scheduled job, removes the Agents-Core entries from
# client configurations and the managed routing section from instruction files (backups first),
# and removes state, data, caches, logs, the autodev clone and the ~/.agents-core installation.
# Credentials (the autodev keychain item and deploy key) need a separate confirmation, or
# --yes --include-credentials. --dry-run lists everything and changes nothing; --keep-data keeps
# the user library and histories. The logic is src/uninstall.py; README "Uninstall".
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

PYTHON=""
for candidate in python3 python; do
    if command -v "$candidate" >/dev/null 2>&1 &&
        "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 11))' 2>/dev/null; then
        PYTHON="$(command -v "$candidate")"
        break
    fi
done
if [ -z "$PYTHON" ]; then
    echo "uninstall.sh: Python 3.11 or newer is required." >&2
    exit 1
fi

# Not the installation's virtual environment, which this removes, and not its directory as the
# working directory: -P keeps the working directory off sys.path, PYTHONPATH names the code.
cd "$HOME"
PYTHONPATH="$REPO_ROOT" exec "$PYTHON" -P -m src.uninstall "$@"
