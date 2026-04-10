"""Inject Agents-Core MCP server entry into a JSON config file.

Usage: python inject_mcp.py <config_path> <python_abs> <server_abs>
"""
import json
import os
import sys


def main():
    config_path = sys.argv[1]
    python_abs = sys.argv[2]
    server_abs = sys.argv[3]

    try:
        with open(config_path, encoding="utf-8") as f:
            config = json.load(f)
    except (json.JSONDecodeError, FileNotFoundError):
        config = {}

    if "mcpServers" not in config:
        config["mcpServers"] = {}

    config["mcpServers"]["Agents-Core"] = {
        "command": python_abs,
        "args": [server_abs],
    }

    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

    print("OK")


if __name__ == "__main__":
    main()
