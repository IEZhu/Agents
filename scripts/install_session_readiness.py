#!/usr/bin/env python3
"""Install session readiness adapters for all supported Agents-Core clients."""
from __future__ import annotations

import argparse
import base64
import binascii
import json
import os
from pathlib import Path
import re
import shlex
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.client_paths import client_config_path, client_home  # noqa: E402
from scripts._helpers.backup_file import backup_file  # noqa: E402

SCRIPT_NAME = "agents-core-session-readiness.py"
MATCHER = "^(startup|clear)$"
COMPACT_MATCHER = "^compact$"
CLIENTS = ("claude", "codex", "cursor", "antigravity", "desktop")
READINESS_NAME = "Agents-Readiness"
WINDOWS_HOOKS = os.name == "nt"


def powershell_payload(arguments: list[str]) -> str:
    invocation = " ".join("'" + argument.replace("'", "''") + "'" for argument in arguments)
    return ("[Console]::InputEncoding = [System.Text.UTF8Encoding]::new($false); "
            "[Console]::OutputEncoding = [System.Text.UTF8Encoding]::new($false); "
            "$OutputEncoding = [Console]::OutputEncoding; "
            "$readinessInputJson = [Console]::In.ReadToEnd(); "
            "$readinessInputJson | & " + invocation + "; exit $LASTEXITCODE")


def parse_clients(value: str) -> tuple[str, ...]:
    clients = tuple(dict.fromkeys(part.strip() for part in value.split(",")))
    if clients == ("all",):
        return CLIENTS
    if not clients or any(client not in CLIENTS for client in clients):
        raise argparse.ArgumentTypeError("clients must be all or a comma-separated selection of " + ", ".join(CLIENTS))
    return clients


def command_for(script: Path, *options: str) -> str:
    arguments = [sys.executable, str(script), *options]
    if WINDOWS_HOOKS:
        # Explicit PowerShell avoids cmd/Git Bash/PowerShell disagreeing on Windows quoting.
        encoded = base64.b64encode(powershell_payload(arguments).encode("utf-16-le")).decode("ascii")
        return "powershell.exe -NoProfile -NonInteractive -EncodedCommand " + encoded
    return shlex.join(arguments)


def atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(content)
        if path.exists():
            os.chmod(temporary, path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def require_unchanged(path: Path, expected: bytes | None) -> None:
    """Refuse to replace a configuration another client changed during setup."""
    if path.is_symlink():
        raise ValueError("Hook configuration became a symbolic link; inspect it before retrying")
    try:
        current = path.read_bytes()
    except FileNotFoundError:
        current = None
    if current != expected:
        raise ValueError("Hook configuration changed during setup; rerun the installer")


def owns_handler(handler: dict, script: Path) -> bool:
    command = handler.get("command")
    if handler.get("type", "command") != "command" or not isinstance(command, str):
        return False
    try:
        arguments = shlex.split(command, posix=not WINDOWS_HOOKS)
    except ValueError:
        return False
    if arguments[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand"]:
        if len(arguments) != 5:
            return False
        try:
            decoded = base64.b64decode(arguments[4], validate=True).decode("utf-16-le")
        except (binascii.Error, UnicodeDecodeError):
            return False
        match = re.search(r"\$readinessInputJson \| & (.+); exit \$LASTEXITCODE$", decoded)
        if not match:
            return False
        arguments = [value.replace("''", "'") for value in re.findall(r"'((?:[^']|'')*)'", match[1])]
        if decoded != powershell_payload(arguments):
            return False
    return (len(arguments) in (2, 4) and arguments[1].strip('"') == str(script)
            and (len(arguments) == 2 or arguments[2:] in (["--client", "cursor"], ["--client", "antigravity"])))


def owns_group(group: dict, script: Path) -> bool:
    handlers = group.get("hooks")
    return (isinstance(handlers, list) and len(handlers) == 1 and isinstance(handlers[0], dict)
            and owns_handler(handlers[0], script))


def upsert(items: list, item: dict, owned) -> None:
    indices = [index for index, existing in enumerate(items) if owned(existing)]
    if indices:
        items[:] = [item if index == indices[0] else existing for index, existing in enumerate(items)
                    if index == indices[0] or index not in indices]
    else:
        items.append(item)


def client_paths(client: str) -> tuple[Path, Path]:
    if client in ("claude", "codex"):
        directory = client_home(client)
        target = directory / ("settings.json" if client == "claude" else "hooks.json")
    elif client == "cursor":
        directory = Path.home() / ".cursor"
        target = directory / "hooks.json"
    elif client == "antigravity":
        directory = Path.home() / ".gemini/config"
        target = directory / "hooks.json"
    elif client == "desktop":
        target = client_config_path("desktop")
        directory = target.parent / "agents-core-readiness"
    else:
        raise ValueError("Unknown client: " + client)
    return target, directory / "hooks" / SCRIPT_NAME


def configure(document: dict, client: str, script: Path) -> tuple[dict, dict[Path, bytes], str]:
    runtime = (ROOT / "scripts/session_readiness.py").read_bytes()
    if client == "desktop":
        helper = script.parent / "session_readiness_mcp.py"
        runtime_path = helper.with_name("session_readiness.py")
        servers = document.setdefault("mcpServers", {})
        if not isinstance(servers, dict):
            raise ValueError("Expected an object at mcpServers")
        existing = servers.get(READINESS_NAME)
        if existing is not None and (not isinstance(existing, dict) or existing.get("args") != [str(helper)]):
            raise ValueError(f"An unrelated MCP server already uses the name {READINESS_NAME}")
        entry = {"command": sys.executable, "args": [str(helper)]}
        servers[READINESS_NAME] = entry
        return entry, {runtime_path: runtime, helper: (ROOT / "scripts/session_readiness_mcp.py").read_bytes()}, "mcp-first-response"
    if client == "antigravity":
        name = "agents-core-readiness"
        entry = document.get(name, {})
        if not isinstance(entry, dict):
            raise ValueError(f"Expected an object at {name}")
        items = entry.setdefault("SessionStart", [])
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError(f"Expected handlers at {name}.SessionStart")
        if entry.get("enabled") is False:
            unrelated = (any(key not in ("enabled", "SessionStart") and value for key, value in entry.items())
                         or any(not owns_handler(item, script) for item in items))
            if unrelated:
                raise ValueError(f"Disabled {name} contains unrelated handlers; rename that conflicting "
                                 "entry before installing readiness. Existing handlers were left disabled.")
        handler = {"type": "command", "command": command_for(script, "--client", client), "timeout": 5}
        upsert(items, handler, lambda item: owns_handler(item, script))
        entry["enabled"] = True
        document[name] = entry
        return entry, {script: runtime}, "hook-first-response"
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Expected an object at hooks")
    event = "sessionStart" if client == "cursor" else "SessionStart"
    items = hooks.setdefault(event, [])
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError(f"Expected hook objects at hooks.{event}")
    if client == "cursor":
        if document.setdefault("version", 1) != 1:
            raise ValueError("Only Cursor hooks version 1 is supported")
        entry = {"command": command_for(script, "--client", client), "timeout": 5}
        upsert(items, entry, lambda item: owns_handler(item, script))
        mode = "hook-first-response"
    else:
        handler = {"type": "command", "command": command_for(script), "timeout": 5}
        entry = {"matcher": MATCHER, "hooks": [handler]}
        upsert(items, entry, lambda item: owns_group(item, script) and item.get("matcher") != COMPACT_MATCHER)
        # Same command; on source "compact" the runtime asks the model to restore its persona.
        restore = {"matcher": COMPACT_MATCHER, "hooks": [dict(handler)]}
        upsert(items, restore, lambda item: owns_group(item, script) and item.get("matcher") == COMPACT_MATCHER)
        mode = "native-message"
    return entry, {script: runtime}, mode


def install(client: str, *, dry_run: bool = False) -> dict:
    target, script = client_paths(client)
    if target.is_symlink():
        raise ValueError(f"Refusing to overwrite a symbolic link: {target}")
    original = target.read_bytes() if target.exists() else None
    document = json.loads(original) if original is not None else {}
    if not isinstance(document, dict):
        raise ValueError(f"Expected a JSON object in {target}")
    entry, scripts, mode = configure(document, client, script)
    if any(path.is_symlink() for path in scripts):
        raise ValueError("Refusing to overwrite a symbolic link in readiness runtime files")
    content = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
    config_changed = document != (json.loads(original) if original is not None else {})
    script_changed = any(not path.exists() or path.read_bytes() != runtime for path, runtime in scripts.items())
    backup = None
    if not dry_run:
        require_unchanged(target, original)
        # Back up before changing even an existing runtime; failure preserves the installation.
        if config_changed and original is not None:
            backup = backup_file(target)
        require_unchanged(target, original)
        for path, runtime in scripts.items():
            if not path.exists() or path.read_bytes() != runtime:
                atomic_write(path, runtime)
        if config_changed:
            require_unchanged(target, original)
            atomic_write(target, content)
    return {"client": client, "config": str(target), "script": str(next(iter(scripts))),
            "scripts": [str(path) for path in scripts], "delivery": mode,
            "changed": config_changed or script_changed, "dry_run": dry_run,
            "backup": str(backup) if backup else None, "hook": entry}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=parse_clients, default=CLIENTS,
                        help="all (default), or select claude,codex,cursor,antigravity,desktop")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    failed = False
    for client in args.clients:
        try:
            print(json.dumps(install(client, dry_run=args.dry_run), ensure_ascii=False))
        except (OSError, ValueError, TypeError) as error:
            print(f"ERROR ({client}): {error}", file=sys.stderr)
            failed = True
    if "codex" in args.clients:
        print("Codex: restart/start a fresh chat, then review and trust the new hooks in /hooks.")
    if any(client in args.clients for client in ("cursor", "antigravity", "desktop")):
        print("Cursor/Antigravity: session-start context requests the banner in the first response. "
              "Claude Desktop Chat: enable Agents-Readiness after restarting the app; it is an MCP "
              "first-response fallback, not a native clear hook. Code-tab sessions use Claude Code hooks.")
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
