#!/usr/bin/env python3
"""Apply extracted keywords to skill frontmatter (preserving rest of YAML).

Strategy: text-based insertion (not YAML round-trip) to preserve formatting
of description/compiled fields. Insert `keywords:` block just before the
closing `---` of frontmatter.
"""
from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

# Load the sibling extractor (03-extract-keywords.py) — the dash in the
# stem makes a regular `import` impossible, so we resolve it dynamically.
_EXTRACTOR_PATH = Path(__file__).with_name("03-extract-keywords.py")
_spec = importlib.util.spec_from_file_location("extract_keywords", _EXTRACTOR_PATH)
if _spec is None or _spec.loader is None:
    raise RuntimeError(f"Unable to load extractor module: {_EXTRACTOR_PATH}")
_extractor = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_extractor)
extract = _extractor.extract
split_frontmatter = _extractor.split_frontmatter


def insert_keywords(text: str, keywords: list[str]) -> str:
    """Insert keywords: block into frontmatter, just before closing ---."""
    m = re.match(r"^(---\s*\n)(.*?\n)(---\s*\n)(.*)$", text, re.DOTALL)
    if not m:
        return text
    fm_open, fm_body, fm_close, rest = m.groups()

    # Build YAML for keywords list (preserve indent style: 2-space, no quotes if safe)
    kw_lines = ["keywords:"]
    for k in keywords:
        # Quote if contains special chars; otherwise plain
        if re.search(r"[:\"'#&*?\[\]{}|>]", k) or k.startswith("-") or k.startswith("@"):
            kw_lines.append(f'  - "{k}"')
        else:
            kw_lines.append(f"  - {k}")
    new_block = "\n".join(kw_lines) + "\n"

    # Ensure fm_body ends with newline
    if not fm_body.endswith("\n"):
        fm_body += "\n"

    return fm_open + fm_body + new_block + fm_close + rest


def main() -> int:
    skills_dir = Path("skills")
    paths = sorted(skills_dir.glob("skill-*.mdc"))

    modified = 0
    skipped_has = 0
    skipped_empty = 0
    for p in paths:
        text = p.read_text(encoding="utf-8")
        fm, body = split_frontmatter(text)
        if fm is None:
            continue
        if "keywords" in fm:
            skipped_has += 1
            continue
        kws = extract(fm, body)
        if not kws:
            skipped_empty += 1
            print(f"SKIP (no kws extracted): {p.stem}", file=sys.stderr)
            continue
        new_text = insert_keywords(text, kws)
        p.write_text(new_text, encoding="utf-8")
        modified += 1
        print(f"  ✓ {p.stem}: {len(kws)} keywords")

    print(f"\nsummary: {modified} modified, {skipped_has} already had keywords, {skipped_empty} empty extraction")
    return 0


if __name__ == "__main__":
    sys.exit(main())
