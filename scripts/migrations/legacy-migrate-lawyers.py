#!/usr/bin/env python3
"""Legacy (pre-3-tier) migration. Kept for git-archaeology only — do not run.

Migrates the 9 lawyer agents to the old `legal-reasoning` capability inside
`capabilities:`. Both the `capabilities:` frontmatter field and the
`legal-reasoning` capability bundle were removed by the 3-tier per-agent
migration (see PR #51); the lawyer cluster now uses `core_skills:
[skill-legal-citation]` directly. Use
`scripts/migrations/05-migrate-agents-3tier.py` instead.

Original behavior (preserved for the record):
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
    if not re.search(r'^[ \t]*-[ \t]*"?legal-reasoning"?[ \t]*$', body, flags=re.MULTILINE):
        body = f"{indent_prefix} legal-reasoning\n" + body
    replacement = header + body
    return new_text[: m.start()] + replacement + new_text[m.end():]


def main() -> int:
    repo = Path(__file__).resolve().parents[2]
    if not (repo / "agents").is_dir():
        print(f"Cannot locate repo root from script path: {repo}", file=sys.stderr)
        return 1
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
