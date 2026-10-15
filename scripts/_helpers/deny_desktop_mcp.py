"""Keep the Claude desktop app's Agents-Core entry out of Claude Code where it needs that (#231).

Usage: python deny_desktop_mcp.py <settings_path> [--desktop-config <claude_desktop_config.json>]

The desktop app injects its servers into the Code-tab sessions it launches. A standalone
`Agents-Core-Desktop` is a process started outside any project, so these `permissions.deny`
rules make such a session use Claude Code's own per-project Agents-Core instead. When the
desktop config's entry is served by the shared service (the stdio bridge or HTTP that
`python -m src.daemon migrate` writes), the bridge resolves each call's workspace (#253) and
the rules would only block Code-tab turns, so they are removed instead (#421).

Prints `denied` or `shared`. Other settings and rules are kept; the file is created only to deny.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from inject_mcp import service_managed  # noqa: E402  (a sibling helper)
from src.client_paths import DESKTOP_DENY_RULES, DESKTOP_SERVER  # noqa: E402


def desktop_shared(config_path):
    """Whether the desktop config's Agents-Core-Desktop entry is served by the shared service."""
    try:
        config = json.loads(Path(config_path).read_bytes())
    except (OSError, ValueError):
        return False  # unreadable: keep the standalone denial
    servers = config.get("mcpServers") if isinstance(config, dict) else None
    return isinstance(servers, dict) and service_managed(servers.get(DESKTOP_SERVER))


def write_settings(path, settings):
    path.parent.mkdir(parents=True, exist_ok=True)
    # Claude Code watches this file: replace it whole, never let it read a half-written one.
    # mkstemp creates the copy private, and it takes the original's mode before replacing it.
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.")
    try:
        with os.fdopen(fd, "wb") as output:
            output.write((json.dumps(settings, indent=2, ensure_ascii=False) + "\n").encode("utf-8"))
        if path.exists():
            shutil.copymode(path, temporary)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def main():
    parser = argparse.ArgumentParser(description="Deny or allow Agents-Core-Desktop in Claude Code settings")
    parser.add_argument("settings_path")
    parser.add_argument("--desktop-config", help="the desktop app's config; a shared-service entry lifts the denial")
    args = parser.parse_args()
    path = Path(args.settings_path).resolve()  # a symlinked settings file (dotfiles) keeps its link
    shared = bool(args.desktop_config) and desktop_shared(args.desktop_config)
    try:
        settings = json.loads(path.read_bytes())  # bytes: a file saved with a BOM (PowerShell 5.1) reads too
    except FileNotFoundError:
        settings = {}
    except json.JSONDecodeError as error:
        print(f"ERROR: {path} contains invalid JSON: {error}", file=sys.stderr)
        sys.exit(1)
    # Refuse a shape we do not understand rather than drop the user's settings.
    if not isinstance(settings, dict):
        print(f"ERROR: {path} root must be a JSON object", file=sys.stderr)
        sys.exit(1)
    permissions = settings.get("permissions", {})
    if not isinstance(permissions, dict):
        print(f"ERROR: {path} 'permissions' must be a JSON object", file=sys.stderr)
        sys.exit(1)
    deny = permissions.get("deny", [])
    if not isinstance(deny, list):
        print(f"ERROR: {path} 'permissions.deny' must be a JSON array", file=sys.stderr)
        sys.exit(1)
    if shared:
        kept = [rule for rule in deny if rule not in DESKTOP_DENY_RULES]
        if kept != deny:
            if kept:
                permissions["deny"] = kept
            else:
                del permissions["deny"]
            if not permissions:
                del settings["permissions"]
            write_settings(path, settings)
        print("shared")
        return
    missing = [rule for rule in DESKTOP_DENY_RULES if rule not in deny]
    if missing:
        settings["permissions"] = permissions
        permissions["deny"] = deny + missing
        write_settings(path, settings)
    print("denied")


if __name__ == "__main__":
    main()
