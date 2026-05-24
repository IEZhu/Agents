"""Enrichment pipeline: assemble the dynamic context block for an agent.

The block is appended to the agent's base system prompt. Composition order
(top-to-bottom of the final prompt):

    1. base agent prompt (from ``agents/<name>/system_prompt.mdc`` body)
    2. **Rules** — always-on universal directives from ``rules/`` (no retrieval,
       no opt-out). Skipped only when ``RULES_ENABLED=0``.
    3. **Skills** — 3-tier per-agent model:
        - core (mandatory)     loaded unconditionally
        - preferred (boost)    in semantic pool with distance × boost_factor
        - capable (base)       in semantic pool with base distance
       Skills outside the three lists are excluded for this agent.
    4. **Implants** — cognitive reasoning patterns (standard/deep tiers only).

The previous global ``core_skills.yaml`` and ``agents/capabilities/registry.yaml``
mechanisms are removed: universals belong in ``rules/``, and per-agent skill
selection is fully explicit through the agent's frontmatter.
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import os
import re
import traceback
from dataclasses import dataclass, field
from typing import List, Literal, Optional

from src.engine.config import AGENTS_DEBUG, get_debug_log_dir
from src.engine.implants import ImplantRetriever
from src.engine.rules import format_rules_for_prompt, get_rules
from src.engine.skills import SkillRetriever

logger = logging.getLogger(__name__)


@dataclass
class EnrichmentResult:
    prompt: str
    skills_loaded: list[str] = field(default_factory=list)
    implants_loaded: list[str] = field(default_factory=list)
    rules_loaded: list[str] = field(default_factory=list)


skill_retriever = SkillRetriever()
implant_retriever = ImplantRetriever()

Tier = Literal["lite", "standard", "deep"]

_COMPLEX_SIGNALS = re.compile(
    r"(```|```\w|архитектур|рефактор|оптимиз|debug|анализ|исследу|investigate"
    r"|сравни|compare|план|design|ревью|review|audit|deep dive|/deep)",
    re.IGNORECASE,
)


def infer_tier(query: str) -> Tier:
    stripped = query.strip()
    if len(stripped) < 50 and not _COMPLEX_SIGNALS.search(stripped):
        return "lite"
    if _COMPLEX_SIGNALS.search(stripped) or len(stripped) > 300:
        return "deep"
    return "standard"


def _n_results_for_tier(tier: Tier) -> int:
    """How many skills to draw from the semantic pool, on top of mandatory.

    Mandatory skills are loaded regardless of tier (core is always-on).
    """
    if tier == "lite":
        return 0
    if tier == "standard":
        return 2
    return 4  # deep


async def get_dynamic_context_string(
    agent_name: str,
    query: str,
    chat_history: Optional[List[str]] = None,
    *,
    core_skills: Optional[List[str]] = None,
    preferred_skills: Optional[List[str]] = None,
    capable_skills: Optional[List[str]] = None,
    tier: Tier = "standard",
    preferred_implants: Optional[List[str]] = None,
) -> EnrichmentResult:
    """Assemble the dynamic context block (rules + skills + implants)."""
    if chat_history is None:
        chat_history = []
    loop = asyncio.get_running_loop()
    context_parts: list[str] = []
    loaded_skill_names: list[str] = []
    loaded_implant_names: list[str] = []
    loaded_rule_names: list[str] = []

    # --- Rules layer ------------------------------------------------------
    try:
        rules = get_rules()
        if rules:
            rules_block = format_rules_for_prompt(rules)
            if rules_block:
                context_parts.append(rules_block)
                loaded_rule_names = [r.name for r in rules]
    except Exception as e:
        logger.error("Failed to load rules layer: %s", e, exc_info=True)

    # --- Skills layer (3-tier per-agent model) ----------------------------
    # Core skills load on every tier (including lite) — they're the agent's
    # mandatory baseline. Preferred + capable participate in semantic search
    # only when tier permits (n_results > 0).
    try:
        n_results = _n_results_for_tier(tier)
        skills = await loop.run_in_executor(
            None,
            lambda: skill_retriever.retrieve(
                query,
                mandatory=core_skills or None,
                preferred=preferred_skills or None,
                capable=capable_skills or None,
                n_results=n_results,
            ),
        )
        if skills:
            use_compiled = tier == "standard"
            context_parts.append(
                skill_retriever.format_skills_for_prompt(skills, compiled=use_compiled)
            )
            loaded_skill_names = [
                s.get("filename", "unknown").removesuffix(".mdc") for s in skills
            ]
    except Exception as e:
        logger.error("Failed to retrieve skills: %s", e, exc_info=True)

    # --- Implants layer (standard/deep only) ------------------------------
    if tier in ("standard", "deep"):
        try:
            from src.engine.config import (
                IMPLANTS_DEEP_TIER_DEFAULT,
                MAX_PREFERRED_IMPLANTS,
            )

            _n_preferred = len(preferred_implants or [])
            if tier == "standard":
                n_implants = (
                    min(max(2, _n_preferred), MAX_PREFERRED_IMPLANTS)
                    if _n_preferred
                    else 2
                )
            else:
                n_implants = min(
                    max(IMPLANTS_DEEP_TIER_DEFAULT, _n_preferred),
                    MAX_PREFERRED_IMPLANTS,
                )
            logger.debug(
                "Retrieving implants: tier=%s, n_implants=%d, preferred=%s",
                tier, n_implants, preferred_implants,
            )
            _preferred = preferred_implants  # capture for closure
            implants = await loop.run_in_executor(
                None,
                lambda: implant_retriever.retrieve(
                    query,
                    n_results=n_implants,
                    role=agent_name,
                    preferred_implants=_preferred if _preferred else None,
                ),
            )
            logger.debug("Implants retrieved: %d results", len(implants))
            if implants:
                context_parts.append(implant_retriever.format_implants_for_prompt(implants))
                loaded_implant_names = [
                    imp.get("metadata", {}).get("short_name")
                    or imp.get("filename", "unknown").removesuffix(".mdc")
                    for imp in implants
                ]
            context_parts.append(
                "**More reasoning implants available** — call `load_implants(query=...)` to load by topic."
            )
        except Exception as e:
            logger.error("Failed to retrieve implants: %s", e, exc_info=True)
            if AGENTS_DEBUG:
                try:
                    debug_dir = get_debug_log_dir()
                    os.makedirs(debug_dir, exist_ok=True)
                    with open(os.path.join(debug_dir, "implant_enrichment_error.log"), "a") as f:
                        f.write(f"\n--- {datetime.datetime.now().isoformat()} ---\n")
                        f.write(traceback.format_exc())
                except Exception:
                    pass

    return EnrichmentResult(
        prompt="\n\n".join(context_parts),
        skills_loaded=loaded_skill_names,
        implants_loaded=loaded_implant_names,
        rules_loaded=loaded_rule_names,
    )


async def enrich_agent_prompt(
    agent_name: str,
    base_prompt: str,
    query: str,
    chat_history: Optional[List[str]] = None,
    *,
    core_skills: Optional[List[str]] = None,
    preferred_skills: Optional[List[str]] = None,
    capable_skills: Optional[List[str]] = None,
    tier: Optional[Tier] = None,
    preferred_implants: Optional[List[str]] = None,
) -> EnrichmentResult:
    """Append the dynamic context block (rules, skills, implants) to the
    agent's base system prompt and return the combined prompt.

    Concatenation: ``base_prompt + "\n\n" + dynamic_block``. Agent persona
    keeps primacy; the dynamic block follows. Any sub-layer may be empty
    depending on tier and the agent's frontmatter.
    """
    if chat_history is None:
        chat_history = []
    if tier is None:
        tier = infer_tier(query)

    enrichment = await get_dynamic_context_string(
        agent_name,
        query,
        chat_history,
        core_skills=core_skills,
        preferred_skills=preferred_skills,
        capable_skills=capable_skills,
        tier=tier,
        preferred_implants=preferred_implants,
    )
    if enrichment.prompt:
        base_prompt += f"\n\n{enrichment.prompt}"
    return EnrichmentResult(
        prompt=base_prompt,
        skills_loaded=enrichment.skills_loaded,
        implants_loaded=enrichment.implants_loaded,
        rules_loaded=enrichment.rules_loaded,
    )
