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
from typing import List, Optional

from src.engine.config import AGENTS_DEBUG, INTENT_CLASSIFIER_ENABLED, get_debug_log_dir
from src.engine.implants import ImplantRetriever
from src.engine.intent import TaskProfile, Tier, classify_intent
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

# ``Tier`` now lives in src.engine.intent (single definition, imported above) and
# is re-exported here because server.py, persona_bundle.py and the eval runners
# import it from this module.

_COMPLEX_SIGNALS = re.compile(
    r"(```|```\w|архитектур|рефактор|оптимиз|debug|анализ|исследу|investigate"
    r"|сравни|compare|план|design|ревью|review|audit|deep dive|/deep)",
    re.IGNORECASE,
)


def _legacy_infer_tier(query: str) -> Tier:
    """The original length+regex rule, kept verbatim and callable.

    Retained for two reasons: it is the control arm of the A/B, and it is the
    fallback when ``INTENT_CLASSIFIER_ENABLED`` is off (the default). Measured on
    evals/datasets/routing.jsonl it scores 67/110 = 60.9%.
    """
    stripped = query.strip()
    if len(stripped) < 50 and not _COMPLEX_SIGNALS.search(stripped):
        return "lite"
    if _COMPLEX_SIGNALS.search(stripped) or len(stripped) > 300:
        return "deep"
    return "standard"


def infer_tier(query: str) -> Tier:
    """Resolve the enrichment tier for *query*.

    Signature, name, module and sync-ness are unchanged so every existing caller
    (server.py:212/372, persona_bundle.py:108, evals/runners/run_tier.py) keeps
    working. When the intent classifier is enabled this is the tier projection of
    :func:`~src.engine.intent.classify_intent`; otherwise it is the legacy rule.

    Callers that also want the mode and the budget should call ``classify_intent``
    directly — this projection deliberately discards everything but the tier.
    """
    if INTENT_CLASSIFIER_ENABLED:
        return classify_intent(query).tier
    return _legacy_infer_tier(query)


def resolve_profile(query: str, *, tier: Optional[Tier] = None) -> Optional[TaskProfile]:
    """Return the :class:`TaskProfile` for *query*, or ``None`` when disabled.

    ``None`` is the signal to every downstream layer that it must keep deriving
    the budget from the tier string exactly as before, which is what keeps the
    flag-off path byte-identical to the previous release.

    *tier* pins the result when an authority outside the classifier has already
    decided the budget (the meta-query ``explicit_tier`` override, or the
    ``preferred_implants`` promotion).
    """
    if not INTENT_CLASSIFIER_ENABLED:
        return None
    profile = classify_intent(query)
    if tier is not None and tier != profile.tier:
        profile = profile.with_tier(tier)
    return profile


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
    profile: Optional[TaskProfile] = None,
) -> EnrichmentResult:
    """Assemble the dynamic context block (rules + skills + implants).

    When *profile* is given it is the source of truth for the enrichment budget
    (pool size, skill render mode, implant count) and *tier* is only carried for
    logging and the wire. When it is ``None`` the budget is derived from *tier*
    exactly as before, so the classifier-off path is unchanged.
    """
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
        n_results = profile.skill_pool_size if profile else _n_results_for_tier(tier)
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
            # Render density is a separate axis from pool size. Legacy behaviour
            # ties it to tier == "standard", which means the lite tier injects
            # FULL skill bodies (median ~2.4 KB each) while standard injects
            # one-liners — the cheapest tier costing more than the middle one.
            use_compiled = (
                profile.skill_render == "compiled" if profile else tier == "standard"
            )
            context_parts.append(
                skill_retriever.format_skills_for_prompt(skills, compiled=use_compiled)
            )
            loaded_skill_names = [
                s.get("filename", "unknown").removesuffix(".mdc") for s in skills
            ]
    except Exception as e:
        logger.error("Failed to retrieve skills: %s", e, exc_info=True)

    # --- Implants layer ---------------------------------------------------
    # Legacy gate is `tier in ("standard", "deep")`; with a profile the gate is
    # the budget itself, so a mode that earns no implants (converse, retrieve)
    # skips the layer the way lite always did.
    implants_enabled = profile.implant_budget > 0 if profile else tier in ("standard", "deep")
    if implants_enabled:
        try:
            from src.engine.config import (
                IMPLANTS_DEEP_TIER_DEFAULT,
                MAX_PREFERRED_IMPLANTS,
            )

            _n_preferred = len(preferred_implants or [])
            if profile is not None:
                base_implants = profile.implant_budget
            else:
                base_implants = 2 if tier == "standard" else IMPLANTS_DEEP_TIER_DEFAULT
            # The agent's declared preferred_implants are a FLOOR, not a
            # suggestion: 43 of 43 agents declare some, so dropping this term
            # would silently starve every persona. The cap stays authoritative.
            n_implants = min(max(base_implants, _n_preferred), MAX_PREFERRED_IMPLANTS)
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


_OUTPUT_FORMAT_HEADING = re.compile(
    r"^##[ \t]+Output Format[ \t]*$.*?(?=^##[ \t]+|\Z)",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)


def strip_output_format(prompt: str) -> str:
    """Remove the persona's ``## Output Format`` section.

    Some task modes are actively harmed by a persona's response template: a
    greeting answered with an "### Analysis / ### Implementation / ###
    Confidence" scaffold, or a piece of prose wrapped in a report skeleton. The
    ``serve-the-request`` rule already says the request outranks the persona's
    Output Format; removing the block gives that rule teeth instead of asking
    the model to disregard text that is still in its context.

    Only a level-2 heading is matched, and only up to the next level-2 heading,
    so nested subsections travel with their parent and the rest of the persona is
    untouched. A persona with no such section is returned unchanged.
    """
    return _OUTPUT_FORMAT_HEADING.sub("", prompt).rstrip() + "\n"


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
    profile: Optional[TaskProfile] = None,
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
    if profile is None:
        profile = resolve_profile(query, tier=tier)

    if profile is not None and profile.suppress_persona_format:
        base_prompt = strip_output_format(base_prompt)

    enrichment = await get_dynamic_context_string(
        agent_name,
        query,
        chat_history,
        core_skills=core_skills,
        preferred_skills=preferred_skills,
        capable_skills=capable_skills,
        tier=tier,
        preferred_implants=preferred_implants,
        profile=profile,
    )
    if enrichment.prompt:
        base_prompt += f"\n\n{enrichment.prompt}"
    return EnrichmentResult(
        prompt=base_prompt,
        skills_loaded=enrichment.skills_loaded,
        implants_loaded=enrichment.implants_loaded,
        rules_loaded=enrichment.rules_loaded,
    )
