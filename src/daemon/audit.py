"""Read-only scope inventory. Values of headers, env and tokens are never returned."""
from pathlib import Path
import json
import os
import sys
import tomllib

from src.client_paths import CLIENTS, absolute_path, client_config_path, client_home, desktop_config_paths
from .state import read_json


def _unreadable(path, scope):
    return {"path": str(path), "scope": scope, "error": "unreadable configuration"}


def _same_file(path, other):
    try:
        return os.path.samefile(path, other)
    except OSError:
        return False


def _same_bytes(path, other):
    try:
        return Path(path).read_bytes() == Path(other).read_bytes()
    except OSError:
        return False


def registered_configs(document):
    """Validate the private inventory before using it for either reads or writes."""
    if document == {}:
        return []
    if not isinstance(document, dict) or document.get("version") != 1 or not isinstance(document.get("configs"), list):
        raise ValueError("Invalid client configuration registry")
    for record in document["configs"]:
        if (not isinstance(record, dict) or record.get("client") not in CLIENTS
                or not isinstance(record.get("path"), str) or not Path(record["path"]).is_absolute()
                or (record.get("workspace") is not None and
                    (not isinstance(record["workspace"], str) or not Path(record["workspace"]).is_absolute()))):
            raise ValueError("Invalid client configuration registry entry")
    return document["configs"]


def inventory(home=None, workspace=None, *, client_configs=(), directory=None):
    home = absolute_path(home or Path.home())
    roots = {Path(workspace).resolve()} if workspace else set()
    result, candidates = [], []
    # Windows: the AppData file of the Claude desktop app, which it no longer reads once its MSIX
    # copy exists (#270). When it differs from the copy, its servers are listed as "desktop:unread",
    # never as the app's; otherwise it shows nothing the copy does not. A process inside the package
    # reads the copy through the AppData path. A file named by an override is listed as it is.
    unread, hidden = set(), set()
    explicit = {absolute_path(path) for client, path in client_configs if client == "desktop"}
    for env in ({}, None):
        for client in ("claude", "codex", "cursor", "desktop", "antigravity"):
            candidates.append((client, client_config_path(client, home=home, environ=env), None))
        override = (os.environ if env is None else env).get("AGENTS_CLAUDE_DESKTOP_CONFIG")
        if override:
            explicit.add(absolute_path(override))
        if sys.platform == "win32":
            appdata, package_copy = desktop_config_paths(home=home, environ=env)
            if package_copy is None or not os.path.lexists(appdata):
                continue
            if _same_file(appdata, package_copy) or _same_bytes(appdata, package_copy):
                hidden.add(appdata)
            else:
                unread.add(appdata)
                candidates.append(("desktop", appdata, None))
    for client, path in client_configs:
        if client not in CLIENTS:
            raise ValueError("Unknown client: " + client)
        candidates.append((client, absolute_path(path), workspace))
    if directory is not None:
        registry = Path(directory) / "client-configs.json"
        try:
            records = registered_configs(read_json(registry, {}))
        except (ValueError, OSError):
            result.append(_unreadable(registry, "managed"))
        else:
            candidates += [(record["client"], Path(record["path"]), record.get("workspace")) for record in records]
    paths, plugins, seen = [], set(), set()
    default_claude = client_config_path("claude", home=home, environ={})
    for client, path, target in candidates:
        if target and Path(target).is_dir():
            roots.add(Path(target).resolve())
        if client == "claude-deny-desktop" or (client == "desktop" and path in hidden and path not in explicit):
            continue
        if client == "desktop":
            scope = "desktop:unread" if path in unread and path not in explicit else "desktop"
        else:
            scope = "claude:project" if client == "claude-project" else client + (":project" if target and client != "claude" else ":user")
        try:
            key = (client, path.resolve(), scope)
        except (OSError, RuntimeError):
            result.append(_unreadable(path, scope))
            continue
        if key in seen:
            continue
        seen.add(key)
        if client == "codex" and not target:
            plugins.add(path.parent / "plugins/cache")
            try:
                paths.extend((p, "codex:profile", True) for p in path.parent.glob("*.config.toml") if p != path)
            except OSError:
                result.append(_unreadable(path.parent, "codex:profile"))
        if client != "claude":
            paths.append((path, scope, client == "codex"))
            continue
        profile = client_home("claude", home=home, environ={}) if path == default_claude else path.parent
        plugins.add(profile / "plugins/cache")
        if not path.exists() and not path.is_symlink():
            continue
        try:
            data = json.loads(path.read_bytes())  # UTF-8, as migrate writes it; not the ANSI code page
            if not isinstance(data, dict) or not isinstance(data.get("projects", {}), dict):
                raise ValueError("Configuration tables must be mappings")
        except (ValueError, OSError):
            result.append(_unreadable(path, "claude:user"))
            continue
        scopes = [("claude:user", data.get("mcpServers", {}))]
        for root, entry in data.get("projects", {}).items():
            if not isinstance(entry, dict):
                result.append(_unreadable(path, "claude:local:" + root))
                continue
            if Path(root).is_dir():
                roots.add(Path(root).resolve())
            scopes.append(("claude:local:" + root, entry.get("mcpServers", {})))
        for label, servers in scopes:
            result.extend(describe(path, label, servers))
    for root in roots:
        paths += [(root / ".codex/config.toml", "codex:project", True),
                  (root / ".cursor/mcp.json", "cursor:project", False),
                  (root / ".mcp.json", "claude:project", False)]
    # Plugin manifests are reported, never rewritten by migration.
    for plugin_root in sorted(plugins):
        try:
            paths.extend((path, "plugin", False) for path in plugin_root.rglob(".mcp.json"))
        except OSError:
            result.append(_unreadable(plugin_root, "plugin"))
    seen_paths = set()
    for path, scope, is_toml in paths:
        try:
            key = (path.resolve(), scope, is_toml)
        except (OSError, RuntimeError):
            result.append(_unreadable(path, scope))
            continue
        if key in seen_paths or (not path.exists() and not path.is_symlink()):
            continue
        seen_paths.add(key)
        try:
            data = tomllib.loads(path.read_bytes().decode("utf-8")) if is_toml else json.loads(path.read_bytes())
            if not isinstance(data, dict):
                raise ValueError("Configuration must be a mapping")
            servers = data.get("mcp_servers", data.get("mcpServers", {}))
            result.extend(describe(path, scope, servers))
        except (ValueError, OSError):
            result.append(_unreadable(path, scope))
    return result


def describe(path, scope, servers):
    if not isinstance(servers, dict):
        yield _unreadable(path, scope)
        return
    for name, entry in servers.items():
        if not isinstance(entry, dict): continue
        headers = entry.get("http_headers", entry.get("headers", {}))
        command = entry.get("command", "")
        if not isinstance(headers, dict) or not isinstance(command, str):
            yield _unreadable(path, scope)
            continue
        yield {"path": str(path), "scope": scope, "server": name,
               "transport": "http" if entry.get("url") else "stdio",
               "header_names": sorted(headers),
               "command_name": Path(command).name,
               "agents_core": "agents" in name.lower() and "core" in name.lower()}
