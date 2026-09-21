"""Read-only scope inventory. Values of headers, env and tokens are never returned."""
from pathlib import Path
import json
import tomllib


def inventory(home=None, workspace=None):
    home = Path(home or Path.home())
    roots = {Path(workspace).resolve()} if workspace else set()
    claude = home / ".claude.json"
    result = []
    if claude.exists():
        try:
            data = json.loads(claude.read_text())
        except (ValueError, OSError):
            result.append({"path": str(claude), "scope": "claude:user", "error": "unreadable configuration"})
        else:
            scopes = [("claude:user", data.get("mcpServers", {}))]
            for root, entry in data.get("projects", {}).items():
                path = Path(root)
                if path.is_dir():
                    roots.add(path.resolve())
                scopes.append(("claude:local:" + root, entry.get("mcpServers", {})))
            for scope, servers in scopes:
                result.extend(describe(claude, scope, servers))
    paths = [(home / ".codex/config.toml", "codex:user"),
             (home / ".cursor/mcp.json", "cursor:user"),
             (home / "Library/Application Support/Claude/claude_desktop_config.json", "desktop")]
    for root in roots:
        paths += [(root / ".codex/config.toml", "codex:project"),
                  (root / ".cursor/mcp.json", "cursor:project"),
                  (root / ".mcp.json", "claude:project")]
    # MCP manifests in installed plugins can introduce an additional server even
    # when normal project/user scopes look correct.
    for plugin_root in (home / ".codex/plugins/cache", home / ".claude/plugins/cache"):
        if plugin_root.exists():
            paths.extend((path, "plugin") for path in plugin_root.rglob(".mcp.json"))
    for path, scope in paths:
        if not path.exists(): continue
        try:
            data = tomllib.loads(path.read_text()) if path.suffix == ".toml" else json.loads(path.read_text())
            servers = data.get("mcp_servers", data.get("mcpServers", {}))
            result.extend(describe(path, scope, servers))
        except (ValueError, OSError):
            result.append({"path": str(path), "scope": scope, "error": "unreadable configuration"})
    return result


def describe(path, scope, servers):
    for name, entry in servers.items():
        if not isinstance(entry, dict): continue
        yield {"path": str(path), "scope": scope, "server": name,
               "transport": "http" if entry.get("url") else "stdio",
               "header_names": sorted(entry.get("http_headers", entry.get("headers", {}))),
               "command_name": Path(entry.get("command", "")).name,
               "agents_core": "agents" in name.lower() and "core" in name.lower()}
