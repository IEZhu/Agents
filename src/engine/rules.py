"""Always-on universal rules layer.

Rules apply to every agent without exception. Anything per-agent belongs in
``skills/``, listed in the agent's ``core_skills``/``preferred_skills``
frontmatter. The architectural invariant is enforced in ``load_all_rules`` —
any rule with ``applies_to`` or ``exclude_agents`` fields is rejected and logged.

Rules are lazy-loaded via ``get_rules()``, sorted by ``priority`` (lower first),
and formatted as a single ``## Rules`` markdown block prepended to the dynamic
context in ``enrichment.py``. The loaded set is memoized into a process-local
cache after the first call; there is no semantic retrieval, and rules are only
re-read from disk when ``invalidate_cache()`` is called (e.g. by tests or after
editing files at runtime).
"""

from __future__ import annotations

import glob
import logging
import os
import re
from dataclasses import dataclass, replace
from typing import List, Optional

import yaml

from src.engine.config import RULES_DIR, RULES_ENABLED
from src.utils.prompt_loader import process_imports, resolve_path, split_frontmatter

logger = logging.getLogger(__name__)


_FORBIDDEN_FIELDS = ("applies_to", "exclude_agents")

# Strip a single leading H1 heading from rule bodies when rendering.
# Each rule body is wrapped under a "### Rule: <name>" subheader inside the
# enrichment prompt; an authoring H1 inside the body would create mixed
# heading levels (### then # then text), which breaks markdown semantics.
# The rule's name is already shown in the per-rule header, so the H1 is
# always redundant and safe to drop.
#
# `[ \t]+` (horizontal whitespace only) — NOT `\s+` — so a rule body that
# happens to start with a bare `#` followed by a newline doesn't have its
# first content line consumed along with the (non-existent) heading text.
_LEADING_H1_RE = re.compile(r"^#[ \t]+[^\n]*\n+")


@dataclass(frozen=True)
class Rule:
    name: str
    description: str
    priority: int
    category: str
    body: str
    filename: str = ""


_cache: Optional[List[Rule]] = None


def _parse_rule_file(path: str, *, strict: bool = False) -> Optional[Rule]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    except OSError as e:
        logger.error("Failed to read rule file %s: %s", path, e)
        return None

    fm_str, body = split_frontmatter(content)
    if fm_str is None:
        logger.warning("Rule file %s has no frontmatter — skipped", path)
        return None

    try:
        fm = yaml.safe_load(fm_str) or {}
    except yaml.YAMLError as e:
        logger.error("Bad frontmatter YAML in %s: %s", path, e)
        return None

    if not isinstance(fm, dict):
        logger.error("Frontmatter in %s is not a mapping — skipped", path)
        return None

    if strict:
        for key in ("name", "description", "category"):
            if key in fm and not isinstance(fm[key], str):
                raise ValueError(f"Rule {key} must be text in {path}")
        if "priority" in fm and (
            not isinstance(fm["priority"], int) or isinstance(fm["priority"], bool)
        ):
            raise ValueError(f"Rule priority must be an integer in {path}")

    forbidden = [k for k in _FORBIDDEN_FIELDS if k in fm]
    if forbidden:
        logger.error(
            "Rule %s has forbidden fields %s — rules are universal. "
            "Move per-agent guidance to a skill in the agent's core_skills/preferred_skills.",
            path, forbidden,
        )
        return None

    name = fm.get("name")
    if not name:
        logger.error("Rule %s missing required 'name' field — skipped", path)
        return None

    raw_priority = fm.get("priority", 50)
    try:
        priority = int(raw_priority)
    except (TypeError, ValueError):
        # A non-numeric priority (e.g. "high", a list) used to crash the entire
        # enrichment pipeline because this exception bubbled up to every request.
        # Skip the rule instead — one malformed file must not break routing.
        logger.error(
            "Rule %s has non-integer 'priority' value %r — skipped",
            path, raw_priority,
        )
        return None

    return Rule(
        name=str(name),
        description=str(fm.get("description", "")),
        priority=priority,
        category=str(fm.get("category", "general")),
        body=body.strip(),
        filename=os.path.basename(path),
    )


def load_all_rules(*, strict: bool = False) -> List[Rule]:
    """Read every ``rules/rule-*.mdc`` and return a list sorted by priority.

    Reload-safe: call ``invalidate_cache()`` after editing files on disk.
    Rejects rules with ``applies_to`` or ``exclude_agents`` (architectural
    invariant — see module docstring).
    """
    rules: List[Rule] = []

    if not os.path.isdir(RULES_DIR):
        if strict:
            raise FileNotFoundError(f"Rules directory not found: {RULES_DIR}")
        logger.info("Rules directory not found at %s — no rules loaded", RULES_DIR)
        return rules

    for path in sorted(glob.glob(os.path.join(RULES_DIR, "rule-*.mdc"))):
        if strict:
            path = resolve_path(path)
        rule = _parse_rule_file(path, strict=True) if strict else _parse_rule_file(path)
        if strict:
            if rule is None or not rule.body or not re.fullmatch(r"[A-Za-z0-9_-]+", rule.name):
                raise ValueError(f"Invalid mandatory rule: {path}")
            if any(existing.name == rule.name for existing in rules):
                raise ValueError(f"Duplicate rule ID: {rule.name}")
            rule = replace(
                rule,
                body=process_imports(rule.body, {os.path.realpath(path)}, strict=True),
                description=process_imports(
                    rule.description, {os.path.realpath(path)}, strict=True,
                ),
            )
        if rule is not None:
            rules.append(rule)

    if strict and not rules:
        raise ValueError(f"No mandatory rules found in {RULES_DIR}")
    rules.sort(key=lambda r: (r.priority, r.name))
    logger.info("Loaded %d rules from %s", len(rules), RULES_DIR)
    return rules


def get_rules(*, fresh: bool = False, strict: bool = False) -> List[Rule]:
    """Cached entry point used by the enrichment pipeline.

    Returns an empty list when ``RULES_ENABLED=0`` so the layer can be
    disabled for diagnostics without removing files.
    """
    global _cache
    if not RULES_ENABLED:
        return []
    if fresh or strict:
        return load_all_rules(strict=strict)
    if _cache is None:
        _cache = load_all_rules()
    return _cache


def invalidate_cache() -> None:
    """Force ``get_rules()`` to re-read from disk on the next call. Tests use this."""
    global _cache
    _cache = None


def format_rules_for_prompt(rules: List[Rule]) -> str:
    """Render the rules list as a single ``## Rules`` markdown block.

    A leading H1 heading inside a rule body (e.g. ``# No fabrication``) is
    stripped — each rule is already wrapped under ``### Rule: <name>``, so an
    H1 inside would produce mixed heading levels in the rendered prompt.
    """
    if not rules:
        return ""

    lines = [
        "## Rules (always-on)",
        "These apply to every response. Where persona, skill or implant text conflicts "
        "with a rule, the rule wins: those layers are defaults for a domain, the rules "
        "are the floor for honesty and fit.",
        "",
    ]
    for rule in rules:
        lines.append(f"### Rule: {rule.name}")
        if rule.description:
            lines.append(f"_{rule.description}_")
        lines.append("")
        body = _LEADING_H1_RE.sub("", rule.body, count=1).lstrip()
        lines.append(body)
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
