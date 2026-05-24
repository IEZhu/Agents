#!/usr/bin/env python3
"""Remove `context:` block from agent system_prompt.mdc frontmatter.

The block always starts at a top-level `context:` line and contains only
`file_globs` (a dead field never consumed by the engine — verified empty
for all 51 agents). Removing the whole `context:` block is equivalent to
removing just file_globs because there's nothing else there.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Match the whole context block:
#   ^context:\n
#   then any number of indented continuation lines (sub-keys, list items, blanks)
#   stop at the next top-level key (letter at column 0) or frontmatter terminator
BLOCK_RE = re.compile(
    r"^context:[ \t]*\n(?:[ \t]+.*\n|[ \t]*\n)*",
    re.MULTILINE,
)


def strip_context(text: str) -> tuple[str, bool]:
    new_text, n = BLOCK_RE.subn("", text, count=1)
    return new_text, n > 0


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: strip_file_globs.py <path> [<path> ...]", file=sys.stderr)
        return 2

    modified = 0
    skipped = 0
    for raw in sys.argv[1:]:
        path = Path(raw)
        if not path.is_file():
            print(f"skip (not a file): {path}", file=sys.stderr)
            skipped += 1
            continue
        text = path.read_text(encoding="utf-8")
        new_text, changed = strip_context(text)
        if not changed:
            print(f"no context block: {path}")
            skipped += 1
            continue
        path.write_text(new_text, encoding="utf-8")
        modified += 1
        print(f"stripped: {path}")
    print(f"\nsummary: {modified} modified, {skipped} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
