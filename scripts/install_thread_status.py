#!/usr/bin/env python3
"""Install the opt-in emoji thread-status hooks for Claude, Codex and Cursor."""
from __future__ import annotations

import argparse
import base64
import binascii
import json
from pathlib import Path
import re
import shlex
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from src.client_paths import client_home  # noqa: E402
from scripts._helpers.backup_file import backup_file  # noqa: E402
from scripts import install_session_readiness as helpers  # noqa: E402

CLIENTS = ("claude", "codex", "cursor")
SCRIPT_NAME = "agents-core-thread-status.py"
CORE_NAME = "agents_core_thread_titles.py"
CODEX_NAME = "agents_core_codex_titles.py"
# A separate Claude runtime, so an older installer replacing the main runtime never
# leaves these handlers calling a mode it lacks (a usage error exits 2 and would wake Claude).
INTERRUPT_NAME = "agents-core-thread-status-interrupt.py"
# The background watcher follows a whole turn; Claude Code enforces this timeout on it.
WATCH_TIMEOUT = 6 * 3600
command_for = helpers.command_for
atomic_write = helpers.atomic_write


def parse_clients(value: str) -> tuple[str, ...]:
    clients = tuple(dict.fromkeys(part.strip() for part in value.split(",")))
    if clients == ("all",):
        return CLIENTS
    if not clients or any(client not in CLIENTS for client in clients):
        raise argparse.ArgumentTypeError("clients must be all or a comma-separated selection of claude,codex,cursor")
    return clients


def client_paths(client: str) -> tuple[Path, Path]:
    if client in ("claude", "codex"):
        directory = client_home(client)
        target = directory / ("settings.json" if client == "claude" else "hooks.json")
    elif client == "cursor":
        directory = Path.home() / ".cursor"
        target = directory / "hooks.json"
    else:
        raise ValueError("Unknown client: " + client)
    return target, directory / "hooks" / SCRIPT_NAME


def _python_arguments(handler: dict) -> list[str] | None:
    """Decode a generated command into its arguments when a Python interpreter runs it."""
    command = handler.get("command")
    if handler.get("type", "command") != "command" or not isinstance(command, str):
        return None
    try:
        arguments = shlex.split(command, posix=not helpers.WINDOWS_HOOKS)
    except ValueError:
        return None
    if arguments[:4] == ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand"]:
        if len(arguments) != 5:
            return None
        try:
            decoded = base64.b64decode(arguments[4], validate=True).decode("utf-16-le")
        except (binascii.Error, UnicodeDecodeError):
            return None
        match = re.search(r"\$readinessInputJson \| & (.+); exit \$LASTEXITCODE$", decoded)
        if not match:
            return None
        arguments = [value.replace("''", "'") for value in re.findall(r"'((?:[^']|'')*)'", match[1])]
        if decoded != helpers.powershell_payload(arguments):
            return None
    if not arguments:
        return None
    executable = arguments[0].strip('"')
    basename = executable.replace("\\", "/").rsplit("/", 1)[-1]
    if executable != sys.executable and not re.fullmatch(r"(?:pythonw?|pypy)(?:\d+(?:\.\d+)*)?(?:\.exe)?", basename, re.IGNORECASE):
        return None
    return arguments


def owns_handler(handler: dict, script: Path) -> bool:
    arguments = _python_arguments(handler)
    return (arguments is not None and len(arguments) == 4 and arguments[1].strip('"') == str(script)
            and arguments[2] == "--client" and arguments[3] in CLIENTS)


def owns_interrupt_handler(handler: dict, script: Path, mode: str) -> bool:
    """Recognize the Claude interrupt watcher (``--watch``) or its Stop marker (``--done``)."""
    arguments = _python_arguments(handler)
    return (arguments is not None and len(arguments) == 3
            and arguments[1].strip('"') == str(script.with_name(INTERRUPT_NAME)) and arguments[2] == mode)


def interrupt_handlers(script: Path) -> dict[str, tuple[dict, str]]:
    """Claude's asyncRewake watcher and Stop marker, keyed by hook event."""
    interrupt = script.with_name(INTERRUPT_NAME)
    return {"UserPromptSubmit": ({"command": command_for(interrupt, "--watch"), "timeout": WATCH_TIMEOUT,
                                  "asyncRewake": True, "type": "command"}, "--watch"),
            "Stop": ({"command": command_for(interrupt, "--done"), "timeout": 5, "type": "command"}, "--done")}


def _place(items: list, event: str, handler: dict, owned) -> None:
    """Replace our handler in one dedicated group, keeping every other handler in place."""
    replacement = {"hooks": [handler]}
    updated = []
    placed = False
    for group in items:
        handlers = group.get("hooks")
        if not isinstance(handlers, list) or any(not isinstance(item, dict) for item in handlers):
            raise ValueError(f"Expected command hook objects at hooks.{event}")
        remaining = [item for item in handlers if not owned(item)]
        if len(remaining) == len(handlers):
            updated.append(group)
        elif remaining:
            updated.append({**group, "hooks": remaining})
        elif not placed:
            updated.append(replacement)
            placed = True
    if not placed:
        updated.append(replacement)
    items[:] = updated


def _groups(items: object) -> bool:
    return isinstance(items, list) and all(
        isinstance(group, dict) and isinstance(group.get("hooks"), list)
        and all(isinstance(item, dict) for item in group["hooks"]) for group in items)


def configure(document: dict, client: str, script: Path) -> bool:
    """Add our handlers; return whether Claude's optional interrupt watcher was configured."""
    hooks = document.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Expected an object at hooks")
    if client == "cursor":
        version = document.setdefault("version", 1)
        if type(version) is not int or version != 1:
            raise ValueError("Only Cursor hooks version 1 is supported")
    events = (("sessionStart",) if client == "cursor" else
              ("SessionStart", "UserPromptSubmit", "Interrupt") if client == "codex" else
              ("SessionStart", "UserPromptSubmit"))
    for event in events:
        items = hooks.setdefault(event, [])
        if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
            raise ValueError(f"Expected hook objects at hooks.{event}")
        handler = {"command": command_for(script, "--client", client),
                   "timeout": 3 if event == "Interrupt" else 5}
        if client == "cursor":
            helpers.upsert(items, handler, lambda item: owns_handler(item, script))
            continue
        handler["type"] = "command"
        # Remove only our handlers, even when sharing a group with another hook.
        # Keep the first dedicated group position to avoid needless reordering.
        _place(items, event, handler, lambda item: owns_handler(item, script))
    if client != "claude":
        return False
    pairs = interrupt_handlers(script)
    # The watcher is optional: an unsupported existing Stop layout keeps it out,
    # and leaves that event and every other setting exactly as they were.
    if not all(_groups(hooks.get(event, [])) for event in pairs):
        return False
    for event, (handler, mode) in pairs.items():
        _place(hooks.setdefault(event, []), event, handler,
               lambda item, mode=mode: owns_interrupt_handler(item, script, mode))
    return True


def refuse_symlinks(path: Path) -> None:
    for candidate in (path, *path.parents):
        if candidate.is_symlink():
            raise ValueError(f"Refusing to overwrite through a symbolic link: {candidate}")


def install(client: str, *, dry_run: bool = False) -> dict:
    target, script = client_paths(client)
    core = script.with_name(CORE_NAME)
    codex = script.with_name(CODEX_NAME)
    interrupt = script.with_name(INTERRUPT_NAME)
    for path in (target, script, core, codex, interrupt):
        refuse_symlinks(path)
    original = target.read_bytes() if target.exists() else None
    document = json.loads(original) if original is not None else {}
    if not isinstance(document, dict):
        raise ValueError(f"Expected a JSON object in {target}")
    watcher = configure(document, client, script)
    runtimes = {script: (ROOT / "scripts/thread_status.py").read_bytes(),
                core: (ROOT / "src/thread_status.py").read_bytes(),
                codex: (ROOT / "src/codex_thread_titles.py").read_bytes()}
    if watcher:
        runtimes[interrupt] = (ROOT / "scripts/thread_status_interrupt.py").read_bytes()
    changes = {path: content for path, content in runtimes.items()
               if not path.exists() or path.read_bytes() != content}
    if document != (json.loads(original) if original is not None else {}):
        changes[target] = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode()
    backups = {}
    if not dry_run:
        helpers.require_unchanged(target, original)
        # Reserve every backup before changing runtime or config bytes.
        for path in changes:
            if path.exists():
                backups[str(path)] = str(backup_file(path))
        helpers.require_unchanged(target, original)
        for path, content in changes.items():
            refuse_symlinks(path)
            if path == target:
                helpers.require_unchanged(target, original)
            atomic_write(path, content)
    return {"client": client, "config": str(target), "script": str(script),
            "scripts": [str(path) for path in runtimes], "changed": bool(changes),
            "dry_run": dry_run, "backup": backups.get(str(target)), "backups": backups,
            "interrupt_watcher": watcher}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--clients", type=parse_clients, default=CLIENTS,
                        help="all (default), or select claude,codex,cursor")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)
    failed = False
    for client in args.clients:
        try:
            print(json.dumps(install(client, dry_run=args.dry_run), ensure_ascii=False))
        except (OSError, ValueError, TypeError) as error:
            print(f"ERROR ({client}): {error}", file=sys.stderr)
            failed = True
    return int(failed)


if __name__ == "__main__":
    raise SystemExit(main())
