"""Install authenticated native workspace hints for standalone Antigravity MCP."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import secrets
import shutil
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.client_paths import absolute_path
from src.daemon.clients import ClientMigration
from src.daemon.state import private_dir
from src.file_lock import file_lock
from src.stdio_workspace import AUTH_CONFIG_ENV, load_auth_config


SERVER = "Agents-Core"


class ConfigurationError(ValueError):
    """Content-free errors suitable for installer diagnostics."""


def _regular_or_missing(path):
    return not path.is_symlink() and (not path.exists() or path.is_file())


def configure(python: Path, server: Path, config_path: Path, *, home=None) -> str:
    """Journal standalone registration, native plugin and a stable private key."""
    python, server = absolute_path(python), absolute_path(server)
    target = absolute_path(config_path)
    home = absolute_path(home or Path.home())
    profile = home / ".gemini" / "config"
    if target.parent.resolve() != profile.resolve():
        return "Antigravity workspace hook skipped: external profile is not managed"
    if not _regular_or_missing(target):
        return "Antigravity workspace hook skipped: custom configuration path preserved"
    if not python.is_file() or not server.is_file():
        raise ConfigurationError("The interpreter or server file is missing")
    installation = server.resolve().parents[1]
    if (installation / "data/.shared-service.json").exists():
        return "Antigravity workspace hook skipped: shared-service registration preserved"
    directory = installation / "data" / "native-workspace"
    signing_file = directory / "antigravity.json"
    plugin = profile / "plugins" / "agents-core"
    assets = [plugin / "plugin.json", plugin / "hooks.json", plugin / "rules/AGENTS.md", profile / "config.json"]
    if target in assets and target != profile / "config.json":
        raise ConfigurationError("MCP configuration collides with a plugin asset")
    if any(path.is_symlink() for path in (profile, profile / "plugins", plugin, plugin / "rules")):
        return "Antigravity workspace hook skipped: custom plugin path preserved"
    if any(not _regular_or_missing(path) for path in assets):
        return "Antigravity workspace hook skipped: custom plugin asset preserved"
    if (installation / "data").is_symlink() or directory.is_symlink():
        raise ConfigurationError("Private workspace state must not be a symlink")
    # Serialize key creation and the complete migration, so repeat installers
    # cannot silently replace the key of an already registered native hook.
    private_dir(directory)
    with file_lock(directory / "install.lock"):
        config = {"installation": str(installation), "python": str(python), "node": shutil.which("node")}
        migration = ClientMigration(directory, config=config, token="")
        document = migration.read_config(target)
        if not isinstance(document, dict):
            raise ConfigurationError("MCP configuration must be a JSON object")
        servers = document.get("mcpServers", {})
        if not isinstance(servers, dict):
            raise ConfigurationError("mcpServers must be a JSON object")
        previous = servers.get(SERVER, {})
        if not isinstance(previous, dict):
            raise ConfigurationError("Agents-Core must be a JSON object")
        if previous and (previous.get("command") != str(python) or previous.get("args") != [str(server)]):
            return "Antigravity workspace hook skipped: existing custom or service transport preserved"
        environment = previous.get("env", {})
        if not isinstance(environment, dict):
            raise ConfigurationError("Agents-Core env must be a JSON object")
        if AUTH_CONFIG_ENV in environment and environment[AUTH_CONFIG_ENV] != str(signing_file):
            return "Antigravity workspace hook skipped: existing authentication configuration preserved"
        if signing_file.exists() or signing_file.is_symlink():
            signing = load_auth_config(signing_file, installation=installation)
            if migration.read_config(signing_file) != signing:
                raise ConfigurationError("Workspace authentication changed during setup")
        else:
            signing = {"transport": "stdio", "installation": str(installation),
                       "headers": {"Authorization": "Bearer " + secrets.token_urlsafe(32)}}
            migration.read_config(signing_file)
        migration.token = signing["headers"]["Authorization"][len("Bearer "):]
        # Native plugins always load from the profile, even when MCP uses the
        # profile's combined config.json rather than mcp_config.json.
        shared_registry = target == profile / "config.json"
        plugin_changes = migration.provision_antigravity_plugin(
            home=home, bridge_config=signing_file,
            registry_document=document if shared_registry else None,
        )
        updated = dict(document)
        entry = {**previous, "command": str(python), "args": [str(server)],
                 "env": {**environment, AUTH_CONFIG_ENV: str(signing_file)}}
        updated["mcpServers"] = {**servers, SERVER: entry}
        changes = []
        for path, content, secret in plugin_changes:
            if shared_registry and path == target:
                updated["plugins"] = json.loads(content)["plugins"]
            else:
                changes.append((path, content, secret))
        changes.extend([
            (target, json.dumps(updated, ensure_ascii=False, indent=2) + "\n", False),
            (signing_file, json.dumps(signing, ensure_ascii=False, indent=2) + "\n", True),
        ])
        changes = [(path, content, secret) for path, content, secret in changes
                   if not path.exists() or path.read_bytes() != content.encode("utf-8")]
        if not changes:
            return "Antigravity standalone workspace hook already current"
        migration.apply(changes)
    return "Antigravity standalone workspace hook configured"


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("python", type=Path)
    parser.add_argument("server", type=Path)
    parser.add_argument("config_path", type=Path)
    args = parser.parse_args(argv)
    try:
        print(configure(args.python, args.server, args.config_path))
    except (OSError, ValueError, TypeError, RuntimeError, KeyError, AttributeError):
        # Neither a parser's user values nor a signing key may reach diagnostics.
        print("ERROR: Could not configure Antigravity workspace hook. "
              "Check configuration syntax, paths and permissions. Existing configuration was preserved.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
