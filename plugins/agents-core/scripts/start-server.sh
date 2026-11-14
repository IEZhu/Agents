#!/bin/sh
# Start the Agents-Core MCP server of the local installation over stdio.
#
# The server is not part of this plugin: install.sh puts it in ~/.agents-core (or the directory
# AGENTS_HOME names), with its Python environment in .venv. This script only starts that
# installation's interpreter on its src/server.py, the same command the installer registers for
# Claude Code. It downloads nothing and sends nothing anywhere.
#
# It runs code outside the plugin folder: the separately installed server. AGENTS_HOME must be an
# absolute path, so the server never depends on the directory the client starts this script in.
installation="${AGENTS_HOME:-$HOME/.agents-core}"
case "$installation" in
    /*) ;;
    *) echo "AGENTS_HOME must be an absolute path: $installation" >&2; exit 1 ;;
esac
# On NixOS the Nix Python's linker ignores nix-ld, so the server needs its library path, set for
# this process only, as scripts/init_repo.sh writes it into the client configurations it registers.
if [ -f /etc/NIXOS ] && [ -d /run/current-system/sw/share/nix-ld/lib ]; then
    LD_LIBRARY_PATH="/run/current-system/sw/share/nix-ld/lib${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
    export LD_LIBRARY_PATH
fi
# The setup hint names the README instead of printing an installer command line: the plugin
# directory's policy scan asks that a script the plugin runs be shipped inside the plugin, and it
# reads this file as code without knowing that the line only reaches stderr.
if [ ! -x "$installation/.venv/bin/python" ] || [ ! -f "$installation/src/server.py" ]; then
    echo "Agents-Core is not installed in $installation. Install it first, following Setup:" >&2
    echo "  https://github.com/IEZhu/Agents/tree/main/plugins/agents-core#setup" >&2
    exit 1
fi
exec "$installation/.venv/bin/python" "$installation/src/server.py"
