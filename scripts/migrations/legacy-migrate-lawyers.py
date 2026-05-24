#!/usr/bin/env python3
"""Migrate the 9 lawyer agents to use `legal-reasoning` capability.

Changes per file:
1. `preferred_skills:` block → empty list `preferred_skills: []`.
2. `capabilities:` block → drop entries `critical-analysis` and `dense-summary`,
   insert `legal-reasoning` at the top of the list (just after the
   `capabilities:` header).

The capabilities block uses indented list items (`  - name`) consistently
across the lawyer cluster; we preserve that style.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

LAWYERS = [
    "colombian_lawyer",
    "cypriot_lawyer",
    "georgian_lawyer",
    "kazakh_lawyer",
    "mexican_lawyer",
    "russian_lawyer",
    "serbian_lawyer",
    "spanish_lawyer",
    "us_lawyer",
]

PREFERRED_BLOCK_RE = re.compile(
    r"^preferred_skills:[ \t]*\n(?:[ \t]+-.*\n|-.*\n|[ \t]*\n)*",
    re.MULTILINE,
)
CAPS_BLOCK_RE = re.compile(
    r"^(capabilities:[ \t]*\n)((?:[ \t]+-.*\n|-.*\n|[ \t]*\n)*)",
    re.MULTILINE,
)
# Match a single capability list item by name (quoted/unquoted, any indent).
CAP_ITEM_TEMPLATE = r'^[ \t]*-[ \t]*"?{name}"?[ \t]*\n'


def _strip_cap(body: str, name: str) -> str:
    return re.sub(CAP_ITEM_TEMPLATE.format(name=re.escape(name)), "", body, flags=re.MULTILINE)


def _detect_indent(body: str) -> str:
    """Pick up the indent prefix of the first list item."""
    m = re.search(r"^([ \t]*-)", body, re.MULTILINE)
    return m.group(1) if m else "  -"


def migrate(text: str) -> str:
    # 1. preferred_skills → []
    new_text = PREFERRED_BLOCK_RE.sub("preferred_skills: []\n", text, count=1)

    # 2. capabilities block: drop two old entries, insert legal-reasoning first.
    m = CAPS_BLOCK_RE.search(new_text)
    if not m:
        return new_text
    header, body = m.group(1), m.group(2)
    body = _strip_cap(body, "critical-analysis")
    body = _strip_cap(body, "dense-summary")
    indent_prefix = _detect_indent(body)
    new_body = f"{indent_prefix} legal-reasoning\n" + body
    replacement = header + new_body
    return new_text[: m.start()] + replacement + new_text[m.end():]


def main() -> int:
    repo = Path(__file__).resolve().parents[1] / "Documents" / "Agents"
    if not repo.is_dir():
        # Fall back to cwd.
        repo = Path.cwd()
    modified = 0
    for name in LAWYERS:
        path = repo / "agents" / name / "system_prompt.mdc"
        if not path.is_file():
            print(f"missing: {path}", file=sys.stderr)
            continue
        original = path.read_text(encoding="utf-8")
        updated = migrate(original)
        if updated != original:
            path.write_text(updated, encoding="utf-8")
            modified += 1
            print(f"migrated: {name}")
        else:
            print(f"no-change: {name}")
    print(f"\n{modified} files migrated")
    return 0


if __name__ == "__main__":
    sys.exit(main())
