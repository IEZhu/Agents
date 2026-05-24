#!/usr/bin/env python3
"""Legacy (pre-3-tier) migration. Kept for git-archaeology only — do not run.

Remove `skill-content-structure` from `preferred_skills:` lists, on the
assumption that it would then be auto-injected via the now-removed
`core_skills.yaml` mechanism. The `core_skills.yaml` file, the global
auto-injection path, and the `capabilities:` registry were all dropped by
the 3-tier per-agent migration (see PR #51); use
`scripts/migrations/05-migrate-agents-3tier.py` instead.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

# Match the preferred_skills block: starts at `^preferred_skills:` and ends at
# the next top-level key (line starting with a letter) or the closing `---`.
BLOCK_RE = re.compile(
    r"^(preferred_skills:[ \t]*\n)((?:[ \t]+-.*\n|-.*\n|[ \t]*\n)*)",
    re.MULTILINE,
)
# Match a single list item for skill-content-structure (quoted/unquoted).
ITEM_RE = re.compile(
    r"^[ \t]*-[ \t]*\"?skill-content-structure\"?[ \t]*\n",
    re.MULTILINE,
)


def migrate(text: str) -> tuple[str, bool]:
    m = BLOCK_RE.search(text)
    if not m:
        return text, False
    header, body = m.group(1), m.group(2)
    new_body, n = ITEM_RE.subn("", body)
    if n == 0:
        return text, False
    # If after removal there are no non-blank items, emit an inline empty list.
    items = [ln for ln in new_body.splitlines() if ln.strip().startswith("-")]
    if not items:
        replacement = "preferred_skills: []\n"
    else:
        replacement = header + new_body
    new_text = text[: m.start()] + replacement + text[m.end():]
    return new_text, True


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: migrate_core_skill.py <path> [<path> ...]", file=sys.stderr)
        return 2
    modified = 0
    for raw in sys.argv[1:]:
        path = Path(raw)
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8")
        new_text, changed = migrate(text)
        if changed:
            path.write_text(new_text, encoding="utf-8")
            modified += 1
            print(f"migrated: {path}")
    print(f"\n{modified} files migrated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
