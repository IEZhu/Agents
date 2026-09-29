"""Client configuration locations. No runtime, model, or service imports."""
import os
from pathlib import Path
import sys


CLIENTS = ("codex", "claude", "cursor", "desktop", "claude-project", "claude-deny-desktop")


def absolute_path(value):
    # Do not resolve a final symlink: migration must reject it, not overwrite its target.
    return Path(os.path.abspath(Path(value).expanduser()))


def client_home(client, *, home=None, environ=None):
    env = os.environ if environ is None else environ
    home = absolute_path(home or Path.home())
    if client == "claude":
        return absolute_path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    if client == "codex":
        return absolute_path(env.get("CODEX_HOME") or home / ".codex")
    raise ValueError("Client has no supported configuration-home override: " + client)


def client_config_path(client, workspace=None, *, home=None, config_path=None, environ=None):
    if client not in CLIENTS:
        raise ValueError("Unknown client: " + client)
    env = os.environ if environ is None else environ
    home = absolute_path(home or Path.home())
    root = absolute_path(workspace) if workspace is not None else None
    if config_path is not None:
        return absolute_path(config_path)
    if client == "codex":
        return root / ".codex/config.toml" if root else client_home("codex", home=home, environ=env) / "config.toml"
    if client == "claude":
        return client_home("claude", home=home, environ=env) / ".claude.json" if env.get("CLAUDE_CONFIG_DIR") else home / ".claude.json"
    if client == "claude-deny-desktop":
        return client_home("claude", home=home, environ=env) / "settings.json"
    if client == "claude-project":
        if root is None:
            raise ValueError("claude-project requires a workspace")
        return root / ".mcp.json"
    if client == "cursor":
        if root:
            return root / ".cursor/mcp.json"
        return absolute_path(env["AGENTS_CURSOR_MCP_CONFIG"]) if env.get("AGENTS_CURSOR_MCP_CONFIG") else home / ".cursor/mcp.json"
    if env.get("AGENTS_CLAUDE_DESKTOP_CONFIG"):
        return absolute_path(env["AGENTS_CLAUDE_DESKTOP_CONFIG"])
    if sys.platform == "darwin":
        directory = home / "Library/Application Support/Claude"
    elif sys.platform == "win32":
        directory = absolute_path(env.get("APPDATA") or home / "AppData/Roaming") / "Claude"
    else:
        directory = absolute_path(env.get("XDG_CONFIG_HOME") or home / ".config") / "Claude"
    return directory / "claude_desktop_config.json"


def parse_client_configs(values, *, multiple=False):
    """Parse explicit known files without guessing a client's type from its filename."""
    result = []
    seen = set()
    for value in values or ():
        client, separator, path = value.partition("=")
        if not separator or client not in CLIENTS or not path.strip():
            raise ValueError("Expected CLIENT=PATH; clients: " + ", ".join(CLIENTS))
        if not multiple and client in seen:
            raise ValueError("Duplicate client configuration override: " + client)
        seen.add(client)
        result.append((client, absolute_path(path)))
    return result
