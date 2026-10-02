"""Always-on universal rules layer.

Rules apply to every agent without exception. Anything per-agent belongs in
``skills/``, listed in the agent's ``core_skills``/``preferred_skills``/
``capable_skills`` frontmatter. The architectural invariant is enforced in ``load_all_rules`` —
any rule with ``applies_to`` or ``exclude_agents`` fields is rejected and logged.

``get_rules()`` returns every rule sorted by ``priority`` (lower first), then
``name``; ``format_rules_for_prompt()`` renders them as one
``## Rules (always-on)`` markdown block. There is no semantic retrieval. Two
paths use it:

- Protocol 2 persona bundles (``persona_bundle.build_persona_bundle``) call
  ``get_rules(fresh=True, strict=True)`` on every bundle build and return the
  block as a separate ``rules_block``. This bypasses the cache and re-reads the
  files, so edited rules reach the next bundle without a restart. An invalid
  rule, a duplicate ``name`` or an empty set raises, and the activation returns
  ``ERROR``. The shared daemon runs the same strict load during warmup.
- The per-query ``server._load_and_enrich``/``enrich_agent_prompt`` path (used
  by the evaluation harnesses) calls the lenient ``get_rules()`` and puts the
  block first in the dynamic context in ``enrichment.py``. It skips an invalid
  file with a logged error and memoizes the loaded set in a process-local cache
  until ``invalidate_cache()`` is called (e.g. by tests).

``RULES_ENABLED=0`` makes ``get_rules()`` return an empty list on both paths.
Persona bundles also honor the per-rule switches of the web UI
(``get_rules(apply_toggles=True)``); the per-query path ignores them.
"""

from __future__ import annotations

import glob
import logging
import os
import re
from dataclasses import dataclass, replace
from typing import List, Optional

import yaml

from src import component_toggles
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
    except (OSError, UnicodeError) as e:
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
            "Move per-agent guidance to a skill in the agent's core_skills/preferred_skills/capable_skills.",
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
    except (TypeError, ValueError, OverflowError):  # OverflowError: priority: .inf
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


def _strict_rule(path: str, rule: Optional[Rule], loaded: List[Rule]) -> Rule:
    """Validate a strictly parsed rule and resolve its imports, or raise."""
    if rule is None or not rule.body or not re.fullmatch(r"[A-Za-z0-9_-]+", rule.name):
        raise ValueError(f"Invalid mandatory rule: {path}")
    if any(existing.name == rule.name for existing in loaded):
        raise ValueError(f"Duplicate rule ID: {rule.name}")
    return replace(
        rule,
        body=process_imports(rule.body, {os.path.realpath(path)}, strict=True),
        description=process_imports(rule.description, {os.path.realpath(path)}, strict=True),
    )


def load_selected_rules(names) -> List[Rule]:
    """Strictly load only the named rules, in priority order; for a flow's exact list.

    Unselected rule files are read leniently to find names, so a broken rule the
    flow does not use cannot block it. A missing or invalid selected rule raises,
    also with ``RULES_ENABLED=0``, which then delivers no rules as ``get_rules()`` does.
    """
    wanted = set(names)
    if not wanted:
        return []
    rules: List[Rule] = []
    for path in sorted(glob.glob(os.path.join(RULES_DIR, "rule-*.mdc"))):
        found = _parse_rule_file(path)
        if found is None or found.name not in wanted:
            continue
        path = resolve_path(path)
        rules.append(_strict_rule(path, _parse_rule_file(path, strict=True), rules))
    missing = wanted - {rule.name for rule in rules}
    if missing:
        raise ValueError(f"Unknown or invalid rules: {', '.join(sorted(missing))}")
    if not RULES_ENABLED:
        return []  # Validated above, so a stale choice is still caught while rules are off.
    rules.sort(key=lambda r: (r.priority, r.name))
    return rules


def load_all_rules(*, strict: bool = False) -> List[Rule]:
    """Read every ``rules/rule-*.mdc`` and return a list sorted by priority.

    Always reads from disk; only the lenient ``get_rules()`` call is memoized.
    With ``strict=True`` an invalid or duplicate rule raises instead of being
    skipped. Rejects rules with ``applies_to`` or ``exclude_agents`` (architectural
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
            rule = _strict_rule(path, rule, rules)
        if rule is not None:
            rules.append(rule)

    if strict and not rules:
        raise ValueError(f"No mandatory rules found in {RULES_DIR}")
    rules.sort(key=lambda r: (r.priority, r.name))
    logger.info("Loaded %d rules from %s", len(rules), RULES_DIR)
    return rules


def get_rules(
    *, fresh: bool = False, strict: bool = False, apply_toggles: bool = False,
) -> List[Rule]:
    """Entry point for persona bundles and per-query enrichment.

    The default lenient call is memoized. ``fresh`` or ``strict`` bypasses the
    cache and reads the files again; persona bundles pass both. Returns an
    empty list when ``RULES_ENABLED=0`` so the layer can be disabled for
    diagnostics without removing files.

    ``apply_toggles=True`` also drops the rules switched off in the web UI
    (``src.component_toggles``), after the files were validated, so switching
    every rule off gives an empty list instead of the strict-load error. Only
    persona bundles ask for it; the per-query evaluation path ignores the
    switches on purpose, so evaluation results do not depend on local settings.
    """
    global _cache
    if not RULES_ENABLED:
        return []
    if fresh or strict:
        rules = load_all_rules(strict=strict)
    else:
        if _cache is None:
            _cache = load_all_rules()
        rules = _cache
    if apply_toggles:
        off = component_toggles.disabled("rules")
        rules = [rule for rule in rules if rule.name not in off]
    return rules


def invalidate_cache() -> None:
    """Force ``get_rules()`` to re-read from disk on the next call. Tests use this."""
    global _cache
    _cache = None


def format_rules_for_prompt(rules: List[Rule]) -> str:
    """Render the rules list as a single ``## Rules (always-on)`` markdown block.

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
