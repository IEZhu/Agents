#!/usr/bin/env python3
"""
Consolidate system_prompt.mdc files:
1. Remove 'skills' field from frontmatter
2. Remove '## Context' boilerplate section
"""

import os
import re
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent

def _resolve_path(primary: Path, fallback: Path) -> Path:
    """Use the new path if it exists, fall back to legacy .cursor/ path."""
    if primary.exists():
        return primary
    if fallback.exists():
        return fallback
    return primary

AGENTS_DIR = _resolve_path(
    REPO_ROOT / "agents",
    REPO_ROOT / ".cursor" / "agents",
)

def process_agent_prompt(file_path: Path) -> bool:
    """Process a single agent system_prompt.mdc file."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        if not content.startswith("---"):
            print(f"  ⚠️  {file_path.parent.name}: No frontmatter")
            return False

        parts = content.split("---", 2)
        if len(parts) < 3:
            print(f"  ⚠️  {file_path.parent.name}: Invalid frontmatter")
            return False

        frontmatter = parts[1]
        body = parts[2]

        # 1. Remove 'skills: []' or 'skills:\n  - ...' from frontmatter
        frontmatter_orig = frontmatter
        frontmatter = re.sub(r'\nskills:.*?(?=\n[a-z_]+:|$)', '', frontmatter, flags=re.DOTALL)

        # 2. Remove '## Context' boilerplate from body
        body_orig = body
        # Pattern: ## Context\nYou operate under the router...
        body = re.sub(
            r'\n## Context\s*\nYou operate under the router[^\n]*\n',
            '\n',
            body,
            flags=re.MULTILINE
        )

        # Write back if changed
        if frontmatter != frontmatter_orig or body != body_orig:
            new_content = f"---{frontmatter}---{body}"
            with open(file_path, 'w', encoding='utf-8') as f:
                f.write(new_content)
            print(f"  ✅ {file_path.parent.name}: Updated")
            return True
        else:
            print(f"  ⏭️  {file_path.parent.name}: No changes")
            return False

    except Exception as e:
        print(f"  ❌ {file_path.parent.name}: Error - {e}")
        return False

def main():
    print("🔧 Consolidating system_prompt.mdc files...")
    print()

    agents = []
    for entry in AGENTS_DIR.iterdir():
        if entry.is_dir() and not entry.name.startswith(".") and entry.name != "common":
            prompt_file = entry / "system_prompt.mdc"
            if prompt_file.exists():
                agents.append(prompt_file)

    if not agents:
        print("⚠️  No agent prompts found!")
        return 1

    print(f"Found {len(agents)} agents\n")

    updated = 0
    for prompt_file in sorted(agents):
        if process_agent_prompt(prompt_file):
            updated += 1

    print()
    print(f"✅ Updated {updated}/{len(agents)} agents")
    return 0

if __name__ == "__main__":
    sys.exit(main())
