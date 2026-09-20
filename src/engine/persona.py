"""Stateless version 2 handlers. Activation state belongs to the caller's dialogue."""

import logging
import uuid

from src.engine.persona_bundle import build_persona_bundle
from src.engine.router import KEYWORD_VETO_ROUTE_REQUIRED
from src.schemas.protocol import PersonaDescriptor, PersonaResponse

logger = logging.getLogger(__name__)

APPLY_INSTRUCTION = (
    "Apply this complete persona bundle only if replaces_activation_id matches your "
    "current activation (null for initial load). Ignore replayed or stale activations. "
    "On SUCCESS, replace all four previous blocks with persona_block, rules_block, "
    "skills_block, and implants_block, including empty blocks. Changed or removed "
    "rules supersede previous rules on switches, restores, and refreshes. Preserve "
    "higher-priority instructions, conversation, facts, goals, constraints, user "
    "permissions, and tool results. "
    "This is a logical replacement, not deletion of transcript messages."
)


def parse_persona(value: PersonaDescriptor | dict | None) -> PersonaDescriptor | None:
    return PersonaDescriptor.model_validate(value) if value is not None else None


def persona_footer(persona: PersonaDescriptor) -> str:
    return (
        f"**Agent**: {persona.agent} · **Skills**: {', '.join(persona.skills_loaded) or '—'}"
        f" · **Implants**: {', '.join(persona.implants_loaded) or '—'}"
        f" · **Rules**: {', '.join(persona.rules_loaded) or '—'}"
    )


def error_response(error: Exception | str, request_id: str | None = None) -> str:
    return PersonaResponse(
        status="ERROR", request_id=request_id or str(uuid.uuid4()), message=str(error),
        instruction="Keep the existing activation. The requested bundle was not applied.",
    ).to_json()


def unchanged(persona: PersonaDescriptor, request_id: str) -> str:
    return PersonaResponse(
        status="NO_CHANGE", request_id=request_id, persona=persona,
        replaces_activation_id=persona.activation_id, footer=persona_footer(persona),
        instruction="Keep the existing activation and its instructions.",
    ).to_json()


async def load_persona(
    router, agent_name: str, query: str, history: list[str],
    current_persona: PersonaDescriptor | dict | None = None, *,
    force_reload: bool = False, refresh: bool = False,
    reasoning: str = "Explicit persona selection", request_id: str | None = None,
    tier: str | None = None,
) -> str:
    request_id = request_id or str(uuid.uuid4())
    try:
        current = parse_persona(current_persona)
        if refresh and (current is None or current.agent != agent_name):
            raise ValueError("Refresh requires the descriptor of the same agent")
        if current and current.agent == agent_name and not force_reload and not refresh:
            return unchanged(current, request_id)

        bundle = await build_persona_bundle(agent_name, query, history, tier=tier)
        if refresh and current.bundle_revision == bundle.bundle_revision:
            return unchanged(current, request_id)

        persona = PersonaDescriptor(
            agent=bundle.agent, activation_id=str(uuid.uuid4()),
            bundle_revision=bundle.bundle_revision, scope=bundle.scope,
            skills_loaded=bundle.skills_loaded, implants_loaded=bundle.implants_loaded,
            rules_loaded=bundle.rules_loaded,
        )
        result = PersonaResponse(
            status="SUCCESS", request_id=request_id, persona=persona,
            replaces_activation_id=current.activation_id if current else None,
            footer=persona_footer(persona), instruction=APPLY_INSTRUCTION,
            persona_block=bundle.persona_block, rules_block=bundle.rules_block,
            skills_block=bundle.skills_block, implants_block=bundle.implants_block,
        )
        if not refresh and not force_reload:
            # Learning failure cannot invalidate an already assembled bundle.
            try:
                await router.update_cache(query, agent_name, reasoning, request_id)
            except Exception:
                logger.warning("Could not cache persona selection", exc_info=True)
        return result.to_json()
    except Exception as error:
        logger.warning("Persona bundle not activated: %s", error)
        return error_response(error, request_id)


async def route_persona(router, query: str, history: list[str], current_persona, is_meta) -> str:
    request_id = str(uuid.uuid4())
    try:
        current = parse_persona(current_persona)
        # An explicit route request never inherits v1's sticky binding.
        tier = None
        cached = await router.lookup_cache(query, {"history_text": "\n".join(history)})
        agent_name = None
        if cached:
            veto = router.keyword_veto(query, cached.target_agent)
            if veto != KEYWORD_VETO_ROUTE_REQUIRED:
                agent_name = veto or cached.target_agent
        elif is_meta(query):
            agent_name = "universal_agent"
            tier = "lite"
        if agent_name is None:
            return PersonaResponse(
                status="ROUTE_REQUIRED", request_id=request_id,
                replaces_activation_id=current.activation_id if current else None,
                candidates=router.get_agent_catalog(),
                instruction=("Choose the best agent for the request and relevant conversation, "
                             "then call get_agent_context with protocol_version=2 and the same "
                             "current_persona. Keep the existing activation until SUCCESS."),
            ).to_json()
        return await load_persona(
            router, agent_name, query, history, current, request_id=request_id, tier=tier,
            reasoning="Semantic routing with keyword validation",
        )
    except Exception as error:
        return error_response(error, request_id)
