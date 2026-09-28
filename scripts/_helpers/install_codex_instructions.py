"""Install managed persona instructions into the effective Codex global file."""
import os
from pathlib import Path
import shutil
import sys

from inject_claude_md import inject


def detect_codex_home() -> Path | None:
    """Honor an explicit home, and avoid creating one for an absent client."""
    configured_home = os.environ.get("CODEX_HOME")
    if configured_home:
        return Path(configured_home).expanduser()
    default_home = Path.home() / ".codex"
    if default_home.is_dir() or shutil.which("codex"):
        return default_home
    return None


def instruction_target(codex_home: Path) -> Path:
    """Codex gives a nonempty global override precedence over AGENTS.md."""
    override = codex_home / "AGENTS.override.md"
    if override.exists() and override.read_bytes().strip():
        return override
    return codex_home / "AGENTS.md"


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(f"Usage: {sys.argv[0]} <source_md_path>", file=sys.stderr)
        return 1
    try:
        codex_home = detect_codex_home()
        if codex_home is None:
            print("Codex not detected; global instructions skipped. "
                  "Install Codex or set CODEX_HOME, then rerun the installer.")
            return 0
        target = instruction_target(codex_home)
        changed = inject(target, Path(args[0]))
    except (OSError, ValueError) as exc:
        print(f"ERROR: Could not configure Codex instructions: {exc}. "
              "Check the source template, target path, permissions, and routing markers, "
              "then rerun the installer.", file=sys.stderr)
        return 1
    state = "configured" if changed else "already current"
    print(f"Codex global instructions {state}: {target}")
    print("Start a fresh Codex session to load these instructions.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
