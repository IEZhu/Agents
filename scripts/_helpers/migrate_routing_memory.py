"""Migrate only exact, known installer-generated routing memory and index entries."""
import argparse
from pathlib import Path
import sys

from inject_claude_md import write_with_backup

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
FILENAME = "feedback_agents_core_routing.md"
INDEX_ENTRY = "- [Agents-Core persona continuity](feedback_agents_core_routing.md) — assess locally; route only when the current role no longer fits"
# Index lines earlier installers wrote; an exact match is replaced with INDEX_ENTRY.
LEGACY_INDEX_ENTRIES = (
    "- [Agents-Core routing is mandatory](feedback_agents_core_routing.md) — always call route_and_load() before any response, no exceptions",
)


def warn_custom(path: Path) -> None:
    print(f"WARNING: Preserving user-edited {path}. Manually replace any unconditional "
          "route_and_load requirement with local keep/switch/refresh/restore assessment.",
          file=sys.stderr)


def migrate(directory: Path, *, existing_only: bool = False) -> bool:
    memory = directory / FILENAME
    if existing_only and not memory.exists():
        print(f"No existing routing memory to migrate: {memory}")
        return False
    current = (TEMPLATES / "memory-routing.md").read_bytes()
    known = {current, *(path.read_bytes() for path in (TEMPLATES / "legacy").glob("memory-routing-*.md"))}
    if memory.exists() and memory.read_bytes() not in known:
        warn_custom(memory)
        return False
    changed = write_with_backup(memory, current)
    index = directory / "MEMORY.md"
    contents = index.read_bytes() if index.exists() else b""
    lines = contents.splitlines(keepends=True)
    references = [line for line in lines if FILENAME.encode() in line]
    if references:
        # An exact known line is replaceable; edited lines and duplicate references
        # require a manual decision. Preserve unrelated index text byte for byte.
        line = references[0]
        bare = line.rstrip(b"\r\n")
        if len(references) != 1 or bare not in [entry.encode() for entry in (INDEX_ENTRY, *LEGACY_INDEX_ENTRIES)]:
            warn_custom(index)
            return changed
        ending = line[len(bare):]
        replacement = INDEX_ENTRY.encode() + ending
        contents = b"".join(replacement if value == line else value for value in lines)
    else:
        separator = b"" if not contents or contents.endswith(b"\n") else b"\n"
        contents += separator + INDEX_ENTRY.encode() + b"\n"
    return write_with_backup(index, contents) or changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--existing-only", action="store_true")
    args = parser.parse_args()
    try:
        changed = migrate(args.directory, existing_only=args.existing_only)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("Routing memory updated" if changed else "Routing memory preserved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
