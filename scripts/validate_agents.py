#!/usr/bin/env python3
"""
Agent Frontmatter Validator

Validates all agent system_prompt.mdc files against the schema.
Checks for:
- Required fields presence
- Deprecated fields usage
- Schema compliance
"""

import os
import sys
import json
import yaml
from pathlib import Path
from typing import Dict, List, Tuple

# Add project root to path
SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent
sys.path.insert(0, str(REPO_ROOT))

def _resolve_path(primary: Path, fallback: Path) -> Path:
    """Use the new path if it exists, fall back to legacy .cursor/ path."""
    return primary if primary.exists() else fallback

AGENTS_DIR = _resolve_path(
    REPO_ROOT / "agents",
    REPO_ROOT / ".cursor" / "agents",
)
SCHEMA_PATH = _resolve_path(
    REPO_ROOT / "agents" / "common" / "agent-schema.json",
    REPO_ROOT / ".cursor" / "agents" / "common" / "agent-schema.json",
)

def load_schema() -> dict:
    """Load the agent schema."""
    with open(SCHEMA_PATH, 'r', encoding='utf-8') as f:
        return json.load(f)

def extract_frontmatter(file_path: Path) -> Dict | None:
    """Extract YAML frontmatter from an MDC file."""
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        if not content.startswith("---"):
            return None

        parts = content.split("---", 2)
        if len(parts) < 3:
            return None

        frontmatter = yaml.safe_load(parts[1])
        return frontmatter if frontmatter else {}
    except Exception as e:
        print(f"  ❌ Error parsing {file_path.name}: {e}")
        return None

def validate_agent(agent_name: str, frontmatter: Dict, schema: Dict) -> Tuple[bool, List[str]]:
    """Validate agent frontmatter against schema."""
    errors = []
    warnings = []

    # Check required fields
    required_fields = schema.get("required", [])
    for field in required_fields:
        if field not in frontmatter:
            errors.append(f"Missing required field: '{field}'")

    # Check for deprecated 'skills' field
    if "skills" in frontmatter:
        warnings.append("DEPRECATED: 'skills' field should be removed (use 'static_skills' and 'preferred_skills')")

    # Check identity structure
    if "identity" in frontmatter:
        identity = frontmatter["identity"]
        required_identity_fields = ["name", "display_name", "role", "tone"]
        for field in required_identity_fields:
            if field not in identity:
                errors.append(f"Missing identity.{field}")

    # Check routing structure
    if "routing" in frontmatter:
        routing = frontmatter["routing"]
        if "domain_keywords" not in routing:
            errors.append("Missing routing.domain_keywords")
        if "trigger_command" not in routing:
            errors.append("Missing routing.trigger_command")

    # Check context structure
    if "context" in frontmatter:
        context = frontmatter["context"]
        if "file_globs" not in context:
            errors.append("Missing context.file_globs")

    # Check static_skills
    if "static_skills" in frontmatter:
        skills = frontmatter["static_skills"]
        if not isinstance(skills, list):
            errors.append("static_skills must be an array")
        else:
            for skill in skills:
                if not skill.endswith(".mdc"):
                    warnings.append(f"static_skills entry '{skill}' should end with .mdc")

    # Check preferred_skills
    if "preferred_skills" in frontmatter:
        skills = frontmatter["preferred_skills"]
        if not isinstance(skills, list):
            errors.append("preferred_skills must be an array")
        else:
            for skill in skills:
                if skill.endswith(".mdc"):
                    warnings.append(f"preferred_skills entry '{skill}' should NOT include .mdc extension")

    return (len(errors) == 0, errors + warnings)

def main():
    """Main validation routine."""
    print("🔍 Validating Agent Frontmatter...")
    print(f"Schema: {SCHEMA_PATH.relative_to(REPO_ROOT)}")
    print(f"Agents: {AGENTS_DIR.relative_to(REPO_ROOT)}")
    print()

    # Load schema
    try:
        schema = load_schema()
    except Exception as e:
        print(f"❌ Failed to load schema: {e}")
        return 1

    # Find all agents
    agents = []
    for entry in AGENTS_DIR.iterdir():
        if entry.is_dir() and not entry.name.startswith(".") and entry.name != "common":
            system_prompt = entry / "system_prompt.mdc"
            if system_prompt.exists():
                agents.append((entry.name, system_prompt))

    if not agents:
        print("⚠️  No agents found!")
        return 1

    print(f"Found {len(agents)} agents\n")

    # Validate each agent
    all_valid = True
    results = []

    for agent_name, prompt_path in sorted(agents):
        frontmatter = extract_frontmatter(prompt_path)

        if frontmatter is None:
            print(f"❌ {agent_name}: No valid frontmatter")
            all_valid = False
            results.append((agent_name, False, ["No frontmatter found"]))
            continue

        is_valid, messages = validate_agent(agent_name, frontmatter, schema)

        if is_valid:
            print(f"✅ {agent_name}")
        else:
            print(f"❌ {agent_name}")
            all_valid = False

        for msg in messages:
            if "DEPRECATED" in msg or "should" in msg:
                print(f"   ⚠️  {msg}")
            else:
                print(f"   ❌ {msg}")

        results.append((agent_name, is_valid, messages))

    # Summary
    print()
    print("=" * 60)
    valid_count = sum(1 for _, is_valid, _ in results if is_valid)
    print(f"Results: {valid_count}/{len(agents)} agents valid")

    if all_valid:
        print("✅ All agents are schema-compliant!")
        return 0
    else:
        print("❌ Some agents have issues. Please fix them.")
        return 1

if __name__ == "__main__":
    sys.exit(main())
