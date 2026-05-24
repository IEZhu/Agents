#!/usr/bin/env python3
"""Remove `static_skills:` block from agent system_prompt.mdc frontmatter.

The block starts at a line `static_skills:` (at column 0) and continues
through any number of subsequent list-item lines (matching `^\s*-`).
It ends at the next top-level YAML key (line starting with a letter/underscore
at column 0) or the frontmatter terminator `---`.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

BLOCK_RE = re.compile(
    r"^static_skills:[ \t]*\n(?:[ \t]+-.*\n|-.*\n|[ \t]*\n)*",
    re.MULTILINE,
)


def strip_static_skills(text: str) -> tuple[str, bool]:
    new_text, n = BLOCK_RE.subn("", text, count=1)
    return new_text, n > 0


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: strip_static_skills.py <path> [<path> ...]", file=sys.stderr)
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
        new_text, changed = strip_static_skills(text)
        if not changed:
            print(f"no static_skills block: {path}")
            skipped += 1
            continue
        path.write_text(new_text, encoding="utf-8")
        modified += 1
        print(f"stripped: {path}")
    print(f"\nsummary: {modified} modified, {skipped} skipped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
