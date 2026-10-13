#!/bin/sh
# Start the Agents-Core MCP server of the local installation over stdio.
#
# The server is not part of this plugin: install.sh puts it in ~/.agents-core (or the directory
# AGENTS_HOME names), with its Python environment in .venv. This script only starts that
# installation's interpreter on its src/server.py, the same command the installer registers for
# Claude Code. It downloads nothing and sends nothing anywhere.
installation="${AGENTS_HOME:-$HOME/.agents-core}"
if [ ! -x "$installation/.venv/bin/python" ] || [ ! -f "$installation/src/server.py" ]; then
    echo "Agents-Core is not installed in $installation. Install it first:" >&2
    echo "  curl -fsSL https://raw.githubusercontent.com/IEZhu/Agents/main/install.sh | bash -s -- --skip-mcp" >&2
    exit 1
fi
exec "$installation/.venv/bin/python" "$installation/src/server.py"
