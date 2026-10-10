"""Inject Agents-Core MCP server entry into a JSON config file.

Usage: python inject_mcp.py <config_path> <python_abs> <server_abs> [--desktop]

``--desktop`` writes the Claude desktop app's entry, ``Agents-Core-Desktop``, which takes over an
older ``Agents-Core`` entry with its other fields: Claude Code denies that name, so the app's
Code-tab sessions use Claude Code's own Agents-Core (``src.client_paths.DESKTOP_SERVER``, #231).

While the shared service is installed for this installation (``data/.shared-service.json``), an
entry that ``python -m src.daemon migrate`` wrote (the stdio bridge, or HTTP) stays as it is: setup
writes standalone stdio registrations again only after ``uninstall``.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.client_paths import DESKTOP_SERVER  # noqa: E402

SERVER = "Agents-Core"


def service_managed(entry):
    """An entry migrate wrote: HTTP to the service, or the stdio bridge to it."""
    if not isinstance(entry, dict):
        return False
    arguments = entry.get("args")
    bridge = isinstance(arguments, list) and arguments and str(arguments[0]).replace("\\", "/").endswith("bridge/stdio.mjs")
    return bool(entry.get("url") or bridge)


def main():
    parser = argparse.ArgumentParser(description="Inject the Agents-Core MCP server entry into a JSON config")
    parser.add_argument("config_path")
    parser.add_argument("python_abs")
    parser.add_argument("server_abs")
    parser.add_argument("--desktop", action="store_true",
                        help=f"the Claude desktop app's entry: {DESKTOP_SERVER}, taking over {SERVER}")
    args = parser.parse_args()
    config_path = args.config_path
    name = DESKTOP_SERVER if args.desktop else SERVER

    try:
        with open(config_path, encoding="utf-8-sig") as f:  # also a file saved with a BOM
            config = json.load(f)
    except FileNotFoundError:
        config = {}
    except json.JSONDecodeError as e:
        print(f"ERROR: {config_path} contains invalid JSON: {e}", file=sys.stderr)
        print("Please fix the file manually or restore from backup", file=sys.stderr)
        sys.exit(1)

    # Refuse to rewrite a config whose shape we do not understand: resetting it
    # would drop the user's other settings and MCP servers.
    if not isinstance(config, dict):
        print(f"ERROR: {config_path} root must be a JSON object", file=sys.stderr)
        sys.exit(1)
    servers = config.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        print(f"ERROR: {config_path} 'mcpServers' must be a JSON object", file=sys.stderr)
        sys.exit(1)

    shared = (Path(args.server_abs).resolve().parents[1] / "data/.shared-service.json").exists()
    if shared and service_managed(servers.get(name)):
        if name != SERVER:
            servers.pop(SERVER, None)  # the older duplicate of the kept entry
    else:
        # Preserve existing entry to avoid clobbering user-added fields (e.g. env),
        # matching init_repo.sh; a non-object entry is ours and unusable, so start over.
        entry = servers.pop(SERVER, servers.get(name)) if name != SERVER else servers.get(name)
        if not isinstance(entry, dict):
            entry = {}
        entry["command"] = args.python_abs
        entry["args"] = [args.server_abs]
        servers[name] = entry

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    print("OK")


if __name__ == "__main__":
    main()
