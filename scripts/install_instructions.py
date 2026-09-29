#!/usr/bin/env python3
"""Refresh Agents-Core global instructions for detected Codex and Claude clients."""
import argparse
import os
from pathlib import Path
import shutil
import sys


if sys.version_info < (3, 11):
    print("ERROR: Python 3.11 or newer is required. Rerun this command with a supported Python.",
          file=sys.stderr)
    sys.exit(1)

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR.parent))
sys.path.insert(0, str(SCRIPT_DIR / "_helpers"))

from src.client_paths import client_config_path, client_home  # noqa: E402
import install_codex_instructions as codex_instructions  # noqa: E402
from inject_claude_md import inject  # noqa: E402
from migrate_routing_memory import migrate  # noqa: E402


def parse_clients(value: str) -> tuple[str, ...]:
    clients = tuple(dict.fromkeys(client.strip() for client in value.split(",")))
    if any(client not in ("codex", "claude") for client in clients):
        raise argparse.ArgumentTypeError("clients must be codex, claude, or codex,claude")
    return clients


def detect_claude_home() -> Path | None:
    """Use the same Claude detection signals as the full installers."""
    claude_home = client_home("claude")
    if (os.environ.get("CLAUDE_CONFIG_DIR") or claude_home.is_dir()
            or client_config_path("claude").is_file() or shutil.which("claude")):
        return claude_home
    return None


def install_claude_instructions(template: Path) -> int:
    """Preserve personal instructions and migrate only an existing routing reminder."""
    claude_home = detect_claude_home()
    if claude_home is None:
        print("Claude Code not detected; global instructions skipped. "
              "Install Claude Code, then rerun this command.")
        return 0
    target = claude_home / "CLAUDE.md"
    changed = inject(target, template)
    state = "configured" if changed else "already current"
    print(f"Claude Code global instructions {state}: {target}")
    migrate(claude_home / "memory", existing_only=True)
    print("Start a fresh Claude Code session to load these instructions.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        epilog="Updates managed instruction sections and existing known Claude routing memory. "
               "Client connections and the Agents-Core installation stay unchanged.",
    )
    parser.add_argument(
        "--clients", type=parse_clients, default=("codex", "claude"), metavar="codex,claude",
        help="clients to refresh (default: both, when detected)",
    )
    args = parser.parse_args(argv)
    template = SCRIPT_DIR / "templates" / "routing-protocol-core.md"
    try:
        template.read_bytes()
    except OSError as exc:
        print(f"ERROR: Could not read the routing template: {exc}. "
              "Restore the template from the repository and rerun this command.", file=sys.stderr)
        return 1

    failed = False
    for client in args.clients:
        try:
            if client == "codex":
                result = codex_instructions.main([str(template)])
            else:
                result = install_claude_instructions(template)
        except (OSError, ValueError) as exc:
            print(f"ERROR: Could not configure {client} instructions: {exc}. "
                  "Check the reported path, permissions, and routing markers, "
                  "then rerun this command.", file=sys.stderr)
            result = 1
        failed = failed or result != 0
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
