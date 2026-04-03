#!/usr/bin/env python3
"""
Remove 'globs' field from all skill files as it's not used by MCP.
"""

import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

def _resolve_path(primary: Path, fallback: Path) -> Path:
    """Use the new path if it exists, fall back to legacy .cursor/ path."""
    return primary if primary.exists() else fallback

SKILLS_DIR = _resolve_path(
    REPO_ROOT / "skills",
    REPO_ROOT / ".cursor" / "skills",
)

def process_skill_file(file_path: Path) -> bool:
    """Remove globs from a skill file."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        if not content.startswith("---"):
            return False

        parts = content.split("---", 2)
        if len(parts) < 3:
            return False

        frontmatter = parts[1]
        body = parts[2]

        # Remove globs field
        frontmatter_new = re.sub(r'\nglobs:.*?(?=\n[a-z_]+:|$)', '', frontmatter, flags=re.DOTALL)

        if frontmatter_new != frontmatter:
            new_content = f"---{frontmatter_new}---{body}"
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            print(f"  ✅ {file_path.name}")
            return True
        else:
            print(f"  ⏭️  {file_path.name}")
            return False

    except Exception as e:
        print(f"  ❌ {file_path.name}: {e}")
        return False

def main():
    print("🔧 Removing globs from skill files...")
    print()

    skill_files = list(SKILLS_DIR.glob("*.mdc"))

    if not skill_files:
        print("⚠️  No skill files found!")
        return 1

    print(f"Found {len(skill_files)} skills\n")

    updated = 0
    for skill_file in sorted(skill_files):
        if process_skill_file(skill_file):
            updated += 1

    print()
    print(f"✅ Updated {updated}/{len(skill_files)} skills")
    return 0

if __name__ == "__main__":
    sys.exit(main())
