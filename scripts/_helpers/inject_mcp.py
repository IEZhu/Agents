"""Inject Agents-Core MCP server entry into a JSON config file.

Usage: python inject_mcp.py <config_path> <python_abs> <server_abs>
"""
import json
import sys


def main():
    if len(sys.argv) < 4:
        print(f"Usage: {sys.argv[0]} <config_path> <python_abs> <server_abs>", file=sys.stderr)
        sys.exit(1)

    config_path = sys.argv[1]
    python_abs = sys.argv[2]
    server_abs = sys.argv[3]

    try:
        with open(config_path, encoding="utf-8") as f:
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

    # Preserve existing entry to avoid clobbering user-added fields (e.g. env),
    # matching init_repo.sh; a non-object entry is ours and unusable, so start over.
    entry = servers.get("Agents-Core")
    if not isinstance(entry, dict):
        entry = {}
    entry["command"] = python_abs
    entry["args"] = [server_abs]
    servers["Agents-Core"] = entry

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    print("OK")


if __name__ == "__main__":
    main()
