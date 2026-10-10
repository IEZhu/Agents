"""Deny the Claude desktop app's Agents-Core entry in a Claude Code settings file (#231).

Usage: python deny_desktop_mcp.py <settings_path>

The desktop app injects its servers into the Code-tab sessions it launches. With these
`permissions.deny` rules, such a session uses Claude Code's own per-project Agents-Core
instead of the app's `Agents-Core-Desktop`, as `python -m src.daemon migrate` does for
the shared service. Other settings and rules are kept; the file is created if missing.
"""
import json
import os
from pathlib import Path
import shutil
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from src.client_paths import DESKTOP_DENY_RULES  # noqa: E402


def main():
    if len(sys.argv) != 2:
        print(f"Usage: {sys.argv[0]} <settings_path>", file=sys.stderr)
        sys.exit(1)
    path = Path(sys.argv[1]).resolve()  # a symlinked settings file (dotfiles) keeps its link
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
    permissions = settings.setdefault("permissions", {})
    if not isinstance(permissions, dict):
        print(f"ERROR: {path} 'permissions' must be a JSON object", file=sys.stderr)
        sys.exit(1)
    deny = permissions.setdefault("deny", [])
    if not isinstance(deny, list):
        print(f"ERROR: {path} 'permissions.deny' must be a JSON array", file=sys.stderr)
        sys.exit(1)
    missing = [rule for rule in DESKTOP_DENY_RULES if rule not in deny]
    if missing:
        deny.extend(missing)
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
    print("OK")


if __name__ == "__main__":
    main()
