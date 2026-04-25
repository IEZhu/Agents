import logging
import asyncio
import os
import re
import traceback
from dataclasses import dataclass, field
from typing import List, Literal, Optional

from src.engine.config import AGENTS_DEBUG, get_debug_log_dir
from src.engine.skills import SkillRetriever
from src.engine.implants import ImplantRetriever
from src.engine.capabilities import resolve_capabilities
from src.engine.rules import format_rules_for_prompt, get_rules

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

async def get_dynamic_context_string(
    agent_name: str,
    query: str,
    chat_history: Optional[List[str]] = None,
    preferred_skills: Optional[List[str]] = None,
    tier: Tier = "standard",
    capabilities: Optional[List[str]] = None,
    preferred_implants: Optional[List[str]] = None,
) -> EnrichmentResult:
    """Build the dynamic context block that ``enrich_agent_prompt`` appends to
    an agent's base prompt.

    The block itself is composed of four sub-layers in this fixed internal
    order (tier only affects which sub-layers are populated):

    1. **Rules** — always-on universal directives from ``rules/`` (no
       semantic retrieval, no opt-out). Skipped only when ``RULES_ENABLED=0``.
    2. **Skills** — semantic + capability-resolved (skipped at ``lite`` tier).
    3. **Capability Directives** — terse one-liners from ``agents/capabilities/registry.yaml``.
    4. **Implants** — cognitive reasoning patterns (``standard``/``deep`` tiers only).

    Final concatenation in ``enrich_agent_prompt``: ``base_prompt + "\\n\\n" +
    block``. The agent persona retains primacy; the dynamic block follows.

    The returned ``EnrichmentResult`` carries the joined prompt fragment plus
    the names loaded for each layer, which the server surfaces to clients.
    """
    if chat_history is None:
        chat_history = []
    loop = asyncio.get_running_loop()
    context_parts: list[str] = []
    loaded_skill_names: list[str] = []
    loaded_implant_names: list[str] = []
    loaded_rule_names: list[str] = []

    rules = get_rules()
    if rules:
        rules_block = format_rules_for_prompt(rules)
        if rules_block:
            context_parts.append(rules_block)
            loaded_rule_names = [r.name for r in rules]

    effective_skills = list(preferred_skills or [])
    cap_directive = ""
    if capabilities:
        cap_skills, cap_directive = await loop.run_in_executor(
            None, resolve_capabilities, capabilities
        )
        for s in cap_skills:
            if s not in effective_skills:
                effective_skills.append(s)

    if tier != "lite":
        try:
            n_skills = 2 if tier == "standard" else max(4, len(effective_skills))
            use_compiled = tier == "standard"
            skills = await loop.run_in_executor(
                None,
                lambda: skill_retriever.retrieve(
                    query,
                    n_results=n_skills,
                    preferred_skills=effective_skills if effective_skills else None,
                ),
            )
            if skills:
                context_parts.append(
                    skill_retriever.format_skills_for_prompt(skills, compiled=use_compiled)
                )
                loaded_skill_names = [
                    s.get("filename", "unknown").removesuffix(".mdc")
                    for s in skills
                ]
        except Exception as e:
            logger.error(f"Failed to retrieve skills: {e}")

    if cap_directive:
        context_parts.append(f"## Capability Directives\n{cap_directive}")

    if tier in ("standard", "deep"):
        try:
            from src.engine.config import MAX_PREFERRED_IMPLANTS, IMPLANTS_DEEP_TIER_DEFAULT
            _n_preferred = len(preferred_implants or [])
            if tier == "standard":
                n_implants = min(max(2, _n_preferred), MAX_PREFERRED_IMPLANTS) if _n_preferred else 2
            else:
                n_implants = min(max(IMPLANTS_DEEP_TIER_DEFAULT, _n_preferred), MAX_PREFERRED_IMPLANTS)
            logger.debug("Retrieving implants: tier=%s, n_implants=%d, preferred=%s", tier, n_implants, preferred_implants)
            _preferred = preferred_implants  # capture for closure
            implants = await loop.run_in_executor(
                None,
                lambda: implant_retriever.retrieve(
                    query, n_results=n_implants, role=agent_name,
                    preferred_implants=_preferred if _preferred else None,
                ),
            )
            logger.debug("Implants retrieved: %d results", len(implants))
            if implants:
                context_parts.append(implant_retriever.format_implants_for_prompt(implants))
                loaded_implant_names = [
                    imp.get("metadata", {}).get("short_name") or imp.get("filename", "unknown").removesuffix(".mdc")
                    for imp in implants
                ]
            context_parts.append(
                "**More reasoning implants available** — call `load_implants(query=...)` to load by topic."
            )
        except Exception as e:
            logger.error("Failed to retrieve implants: %s", e, exc_info=True)
            if AGENTS_DEBUG:
                try:
                    import datetime
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
    preferred_skills: Optional[List[str]] = None,
    tier: Optional[Tier] = None,
    capabilities: Optional[List[str]] = None,
    preferred_implants: Optional[List[str]] = None,
) -> EnrichmentResult:
    """Append the dynamic context block (rules, skills, capability directives,
    implants) to the agent's base system prompt and return the combined prompt.

    Concatenation: ``base_prompt + "\\n\\n" + dynamic_block``. Agent persona
    keeps primacy; the dynamic block follows. Any of the four sub-layers may be
    empty depending on tier, ``RULES_ENABLED``, and the agent's frontmatter.

    Returns ``EnrichmentResult`` with the combined prompt plus the names loaded
    for each layer (``rules_loaded``, ``skills_loaded``, ``implants_loaded``).
    """
    if chat_history is None:
        chat_history = []
    if tier is None:
        tier = infer_tier(query)

    enrichment = await get_dynamic_context_string(
        agent_name, query, chat_history, preferred_skills, tier, capabilities, preferred_implants
    )
    if enrichment.prompt:
        base_prompt += f"\n\n{enrichment.prompt}"
    return EnrichmentResult(
        prompt=base_prompt,
        skills_loaded=enrichment.skills_loaded,
        implants_loaded=enrichment.implants_loaded,
        rules_loaded=enrichment.rules_loaded,
    )
