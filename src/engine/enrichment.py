import logging
import asyncio
import re
from dataclasses import dataclass, field
from typing import List, Literal, Optional

from src.engine.skills import SkillRetriever
from src.engine.implants import ImplantRetriever
from src.engine.capabilities import resolve_capabilities

logger = logging.getLogger(__name__)


@dataclass
class EnrichmentResult:
    prompt: str
    skills_loaded: list[str] = field(default_factory=list)
    implants_loaded: list[str] = field(default_factory=list)

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
    """Retrieve and format dynamic context (Skills + Implants) based on tier."""
    if chat_history is None:
        chat_history = []
    loop = asyncio.get_running_loop()
    context_parts: list[str] = []
    loaded_skill_names: list[str] = []
    loaded_implant_names: list[str] = []

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
            logger.debug(f"Retrieving implants: tier={tier}, n_implants={n_implants}, preferred={preferred_implants}")
            _preferred = preferred_implants  # capture for closure
            implants = await loop.run_in_executor(
                None,
                lambda: implant_retriever.retrieve(
                    query, n_results=n_implants, role=agent_name,
                    preferred_implants=_preferred if _preferred else None,
                ),
            )
            logger.debug(f"Implants retrieved: {len(implants)} results")
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
            logger.error(f"Failed to retrieve implants: {e}", exc_info=True)

    return EnrichmentResult(
        prompt="\n\n".join(context_parts),
        skills_loaded=loaded_skill_names,
        implants_loaded=loaded_implant_names,
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
    """Enrich the base system prompt with dynamic skills and implants."""
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
    )
