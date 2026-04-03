import logging
import yaml
from typing import Dict, List, Any

from src.engine.config import CAPABILITIES_FILE

logger = logging.getLogger(__name__)

_registry: Dict[str, Any] | None = None

def _load_registry() -> Dict[str, Any]:
    global _registry
    if _registry is not None:
        return _registry
    try:
        with open(CAPABILITIES_FILE, "r", encoding="utf-8") as f:
            _registry = yaml.safe_load(f) or {}
    except Exception as e:
        logger.error(f"Failed to load capabilities registry: {e}")
        _registry = {}
    return _registry

def resolve_capabilities(capability_names: List[str]) -> tuple[List[str], str]:
    """Resolve a list of capability names into (skill_names, combined_directive).

    Returns deduplicated skill names and a single directive string built from all
    matching capabilities.
    """
    registry = _load_registry()
    skills: list[str] = []
    directives: list[str] = []
    seen_skills: set[str] = set()

    for cap_name in capability_names:
        entry = registry.get(cap_name)
        if not entry:
            logger.warning(f"Unknown capability: {cap_name}")
            continue
        for skill in entry.get("skills", []):
            if skill not in seen_skills:
                skills.append(skill)
                seen_skills.add(skill)
        directive = entry.get("directive", "")
        if directive:
            directives.append(directive)

    return skills, " ".join(directives)

def reload_registry() -> None:
    """Force-reload the registry from disk (e.g. after editing)."""
    global _registry
    _registry = None
    _load_registry()
