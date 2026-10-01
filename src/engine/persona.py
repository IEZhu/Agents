"""Stateless version 2 handlers. Activation state belongs to the caller's dialogue."""

import logging
import os
import uuid

from src.engine.persona_bundle import ComponentSelection, build_persona_bundle
from src.engine.router import KEYWORD_VETO_ROUTE_REQUIRED
from src.schemas.protocol import PersonaDescriptor, PersonaResponse
from src.version import agents_core_version

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


_ui_port: int | None = None


def configure_ui_port(port: int | None) -> None:
    """Record the port of the daemon's web UI; only the daemon calls this."""
    global _ui_port
    _ui_port = port


def ui_link() -> str | None:
    """The bare UI link, only under the daemon. Never carries a code or token."""
    if os.environ.get("AGENTS_TRANSPORT") != "http" or not _ui_port:
        return None
    return f"http://127.0.0.1:{_ui_port}/ui"


def persona_footer(persona: PersonaDescriptor) -> str:
    # The version segment depends on the installation, not the bundle: it stays out
    # of PersonaDescriptor and bundle_revision. Plain Markdown, no HTML.
    version = f"Agents-Core {agents_core_version()}"
    link = ui_link()
    segment = f"[{version}]({link})" if link else version
    return (
        f"**Agent**: {persona.agent} · **Skills**: {', '.join(persona.skills_loaded) or '—'}"
        f" · **Implants**: {', '.join(persona.implants_loaded) or '—'}"
        f" · **Rules**: {', '.join(persona.rules_loaded) or '—'}"
        f" · {segment}"
    )


BUNDLE_NOT_APPLIED = "Keep the existing activation. The requested bundle was not applied."


def error_response(error: Exception | str, request_id: str | None = None, *,
                   instruction: str = BUNDLE_NOT_APPLIED) -> str:
    return PersonaResponse(
        status="ERROR", request_id=request_id or str(uuid.uuid4()), message=str(error),
        instruction=instruction,
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
    selection: ComponentSelection | None = None,
) -> str:
    """With ``selection`` (a flow's persona) bundles are compared by revision, not
    agent name, and the choice does not train the shared router cache."""
    request_id = request_id or str(uuid.uuid4())
    try:
        current = parse_persona(current_persona)
        if refresh and (current is None or current.agent != agent_name):
            raise ValueError("Refresh requires the descriptor of the same agent")
        if (current and current.agent == agent_name and not force_reload and not refresh
                and selection is None):
            return unchanged(current, request_id)

        bundle = await build_persona_bundle(agent_name, query, history, selection=selection)
        if ((refresh or selection is not None) and current
                and current.agent == agent_name
                and current.bundle_revision == bundle.bundle_revision):
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
        if not refresh and not force_reload and selection is None:
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
        cached = await router.lookup_cache(query, {"history_text": "\n".join(history)})
        agent_name = None
        if cached:
            veto = router.keyword_veto(query, cached.target_agent)
            if veto != KEYWORD_VETO_ROUTE_REQUIRED:
                agent_name = veto or cached.target_agent
        elif is_meta(query):
            # The bundle infers the tier and promotes lite to standard: NO_CHANGE
            # keeps this session-scoped bundle for the whole conversation.
            agent_name = "universal_agent"
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
            router, agent_name, query, history, current, request_id=request_id,
            reasoning="Semantic routing with keyword validation",
        )
    except Exception as error:
        return error_response(error, request_id)
