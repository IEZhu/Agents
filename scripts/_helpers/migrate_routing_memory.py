"""Migrate only exact, known installer-generated routing memory and index entries."""
import argparse
from pathlib import Path
import sys

from inject_claude_md import write_with_backup

TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
FILENAME = "feedback_agents_core_routing.md"
INDEX_ENTRIES = {
    1: "- [Agents-Core routing is mandatory](feedback_agents_core_routing.md) — always call route_and_load() before any response, no exceptions",
    2: "- [Agents-Core persona continuity](feedback_agents_core_routing.md) — assess locally; route only when the current role no longer fits",
}


def warn_custom(path: Path, protocol: int) -> None:
    if protocol == 2:
        change = "replace any unconditional route_and_load requirement with local keep/switch/refresh/restore assessment"
    else:
        change = "align routing instructions with the version 1 managed CLAUDE.md section"
    print(f"WARNING: Preserving user-edited {path}. Manually {change}.", file=sys.stderr)


def migrate(directory: Path, protocol: int, *, existing_only: bool = False) -> bool:
    memory = directory / FILENAME
    if existing_only and not memory.exists():
        print(f"No existing routing memory to migrate: {memory}")
        return False
    known = {version: (TEMPLATES / f"memory-routing-v{version}.md").read_bytes()
             for version in (1, 2)}
    if memory.exists() and memory.read_bytes() not in known.values():
        warn_custom(memory, protocol)
        return False
    changed = write_with_backup(memory, known[protocol])
    index = directory / "MEMORY.md"
    contents = index.read_bytes() if index.exists() else b""
    lines = contents.splitlines(keepends=True)
    references = [line for line in lines if FILENAME.encode() in line]
    if references:
        # An exact known line is replaceable; edited lines and duplicate references
        # require a manual decision. Preserve unrelated index text byte for byte.
        line = references[0]
        bare = line.rstrip(b"\r\n")
        if len(references) != 1 or bare not in [entry.encode() for entry in INDEX_ENTRIES.values()]:
            warn_custom(index, protocol)
            return changed
        ending = line[len(bare):]
        replacement = INDEX_ENTRIES[protocol].encode() + ending
        contents = b"".join(replacement if value == line else value for value in lines)
    else:
        separator = b"" if not contents or contents.endswith(b"\n") else b"\n"
        contents += separator + INDEX_ENTRIES[protocol].encode() + b"\n"
    return write_with_backup(index, contents) or changed


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    parser.add_argument("--protocol", type=int, choices=(1, 2), required=True)
    parser.add_argument("--existing-only", action="store_true")
    args = parser.parse_args()
    try:
        changed = migrate(args.directory, args.protocol, existing_only=args.existing_only)
    except (OSError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    print("Routing memory updated" if changed else "Routing memory preserved")
    return 0


if __name__ == "__main__":
    sys.exit(main())
