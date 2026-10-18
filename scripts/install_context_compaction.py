#!/usr/bin/env python3
"""Install optional native compaction adapters; installation leaves the switch off."""
from __future__ import annotations

import argparse
import base64
import binascii
import io
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.context_compaction.config import config_path, load, refuse_links, write  # noqa: E402
from scripts._helpers.backup_file import backup_file  # noqa: E402
from scripts.install_session_readiness import atomic_write, command_for, powershell_payload, upsert  # noqa: E402

SCRIPT_NAME = "agents-core-context-compaction.py"


def owns_handler(item: dict, script: Path) -> bool:
    """Recognize only this installer's exact native or PowerShell hook command."""
    command = item.get("command")
    if not isinstance(command, str):
        return False
    try:
        arguments = shlex.split(command)
        if arguments[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand"]:
            if len(arguments) != 5:
                return False
            decoded = base64.b64decode(arguments[4], validate=True).decode("utf-16-le")
            match = re.search(r"\$readinessInputJson \| & (.+); exit \$LASTEXITCODE$", decoded)
            if not match:
                return False
            arguments = [value.replace("''", "'") for value in re.findall(r"'((?:[^']|'')*)'", match[1])]
            if decoded != powershell_payload(arguments):
                return False
        return len(arguments) == 4 and arguments[1] == str(script) and arguments[2] == "--config"
    except (ValueError, binascii.Error, UnicodeError):
        return False


def cursor_plan(target: Path, settings_path: Path, *, remove=False) -> tuple[dict, dict[Path, bytes]]:
    """Plan the reserved Cursor hook without enabling native compaction."""
    refuse_links(target)
    with target.open("rb") if target.exists() else io.BytesIO(b"{}") as stream:
        raw = stream.read(1048577)
    if len(raw) > 1048576:
        raise ValueError("Cursor hooks configuration exceeds 1 MiB")
    document = json.loads(raw)
    if not isinstance(document, dict) or type(document.get("version", 1)) is not int or document.get("version", 1) != 1:
        raise ValueError("Expected Cursor hooks version 1 object")
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Expected hooks object")
    items = hooks.setdefault("stop", [])
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise ValueError("Expected hook objects at hooks.stop")
    script = target.parent / "hooks" / SCRIPT_NAME
    if remove:
        items[:] = [item for item in items if not owns_handler(item, script)]
        return document, {}
    document["version"] = 1
    upsert(items, {"command": command_for(script, "--config", str(settings_path)), "timeout": 30},
           lambda item: owns_handler(item, script))
    package = script.parent / "agents_core_context_compaction"
    files = {script: (ROOT / "scripts/context_compaction.py").read_bytes()}
    for source in (ROOT / "src/context_compaction").iterdir():
        if source.is_file() and source.suffix in {".py", ".js", ".applescript"}:
            files[package / source.name] = source.read_bytes()
    return document, files


def install(*, clients=("claude",), path=None, enabled=None, dry_run=False,
            cursor_home=None, runner=subprocess.run) -> dict:
    """Install selected adapters and report Cursor's unavailable native capability.

    Only Claude is selected by default. An explicit Cursor selection installs
    its reserved hook and policy runtime, which cannot dispatch compaction.
    """
    target = config_path(path)
    settings, error = load(target)
    if error == "invalid_config":
        raise ValueError("Existing compaction settings are invalid; repair them first")
    # Plan and validate all filesystem targets before any mutation.
    outputs = {}
    hook_path = (Path(cursor_home) if cursor_home is not None else Path.home() / ".cursor") / "hooks.json"
    if "cursor" in clients:
        document, outputs = cursor_plan(hook_path, target)
        outputs[hook_path] = (json.dumps(document, indent=2, ensure_ascii=False) + "\n").encode()
    for item in outputs:
        refuse_links(item)
    command = ["claude", "plugin", "install", "agents-core-context-compaction", "--scope", "user",
               "--marketplace", str(ROOT), "--config", "configPath=" + str(target), "--json"]
    result = {"config": str(target), "enabled": settings["enabled"] if enabled is None else enabled,
              "clients": list(clients), "dry_run": dry_run, "files": [str(p) for p in outputs],
              "claude_command": command if "claude" in clients else None, "backups": [],
              "capabilities": {client: ({"status": "unsupported", "reason": "native_compaction_unavailable"}
                                       if client == "cursor" else {"status": "available"})
                               for client in clients}}
    if dry_run:
        return result
    changed = {item: content for item, content in outputs.items()
               if not item.exists() or item.read_bytes() != content}
    # Reserve every backup first; a failure cannot leave a half-written hook configuration.
    for item in changed:
        if item.exists():
            result["backups"].append(str(backup_file(item)))
    if error == "not_configured":
        write(target, settings)  # false until installation succeeds and an explicit --enable was supplied
    elif enabled is False and settings["enabled"]:
        backup_file(target)
        settings["enabled"] = False
        write(target, settings)
    for item, content in changed.items():
        refuse_links(item)
        atomic_write(item, content)
    if "claude" in clients:
        completed = runner(command, capture_output=True, text=True, timeout=120, check=False)
        if completed.returncode:
            raise ValueError("Native Claude plugin installation failed: " + completed.stderr.strip()[:2000])
    if enabled is not None and enabled != settings["enabled"]:
        backup_file(target)
        settings["enabled"] = enabled
        write(target, settings)
    return result


def main(argv=None):
    """Parse explicit client and owner-switch choices, then print the install plan."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", default="claude",
                        help="claude (default); cursor installs a reserved, unsupported adapter")
    parser.add_argument("--config")
    parser.add_argument("--dry-run", action="store_true")
    switch = parser.add_mutually_exclusive_group()
    switch.add_argument("--enable", dest="enabled", action="store_const", const=True, default=None)
    switch.add_argument("--disable", dest="enabled", action="store_const", const=False)
    args = parser.parse_args(argv)
    clients = tuple(dict.fromkeys(args.clients.split(",")))
    if not clients or any(client not in {"claude", "cursor"} for client in clients):
        parser.error("--clients must select claude and/or cursor")
    try:
        print(json.dumps(install(clients=clients, path=args.config, enabled=args.enabled, dry_run=args.dry_run)))
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        parser.exit(1, f"ERROR: {error}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
