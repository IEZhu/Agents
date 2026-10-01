"""Read-only listing of rules, skills and implants for the web UI.

Every entry carries its ID, description, body and the current on/off state from
``src.component_toggles``. Skills also list the agents that declare them (and in
which tier), implants the agents that prefer them. Nothing here edits a file.
"""
from __future__ import annotations

import glob
import os

import yaml

from src import component_toggles
from src.engine.config import AGENTS_DIR, IMPLANTS_DIR, RULES_DIR, SKILLS_DIR
from src.engine.rules import load_all_rules
from src.utils.prompt_loader import split_frontmatter

_TIERS = (("core_skills", "core"), ("preferred_skills", "preferred"), ("capable_skills", "capable"))


def _frontmatter(path: str) -> tuple[dict, str]:
    try:
        with open(path, "r", encoding="utf-8") as stream:
            raw = stream.read()
        fm_str, body = split_frontmatter(raw)
        meta = yaml.safe_load(fm_str) if fm_str is not None else {}
    except (OSError, UnicodeError, yaml.YAMLError):
        return {}, ""
    return (meta if isinstance(meta, dict) else {}), body.strip()


def _declarations() -> tuple[dict[str, list[dict]], dict[str, list[dict]]]:
    skills: dict[str, list[dict]] = {}
    implants: dict[str, list[dict]] = {}
    for path in sorted(glob.glob(os.path.join(AGENTS_DIR, "*", "system_prompt.mdc"))):
        agent = os.path.basename(os.path.dirname(path))
        meta, _ = _frontmatter(path)
        for key, tier in _TIERS:
            for value in meta.get(key) or []:
                if isinstance(value, str):
                    skills.setdefault(value.removesuffix(".mdc"), []).append(
                        {"agent": agent, "tier": tier})
        for value in meta.get("preferred_implants") or []:
            if isinstance(value, str):
                implants.setdefault(value.removesuffix(".mdc"), []).append(
                    {"agent": agent, "tier": "preferred"})
    return skills, implants


def _files(directory: str, kind: str, declared: dict[str, list[dict]]) -> list[dict]:
    off = component_toggles.disabled(kind)
    items = []
    for path in sorted(glob.glob(os.path.join(directory, "*.mdc"))):
        component_id = os.path.basename(path).removesuffix(".mdc")
        meta, body = _frontmatter(path)
        items.append({
            "id": component_id,
            "description": str(meta.get("description", "")),
            "short_name": str(meta.get("short_name", "")) if kind == "implants" else "",
            "body": body,
            "declared_by": declared.get(component_id, []),
            "enabled": component_id not in off,
        })
    return items


def list_components(kind: str) -> list[dict]:
    """Entries for ``rules``, ``skills`` or ``implants``, sorted by ID."""
    if kind not in component_toggles.KINDS:
        raise component_toggles.ToggleError(f"unknown component kind: {kind!r}")
    if kind == "rules":
        off = component_toggles.disabled("rules")
        return [{"id": rule.name, "description": rule.description, "short_name": "",
                 "body": rule.body, "declared_by": [], "enabled": rule.name not in off,
                 "category": rule.category, "priority": rule.priority}
                for rule in load_all_rules()]
    skills, implants = _declarations()
    if kind == "skills":
        return _files(SKILLS_DIR, "skills", skills)
    return _files(IMPLANTS_DIR, "implants", implants)


def known_ids(kind: str) -> set[str]:
    return {item["id"] for item in list_components(kind)}
