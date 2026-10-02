"""Stateless version 2 handlers. Activation state belongs to the caller's dialogue."""

import json
import logging
import os
import uuid
from dataclasses import dataclass, field

from src.engine.persona_bundle import ComponentSelection, build_persona_bundle
from src.engine.router import KEYWORD_VETO_ROUTE_REQUIRED
from pydantic import ValidationError

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
    "This is a logical replacement, not deletion of transcript messages. "
    "When a log_interaction or read_history result carries history_last_error, or "
    "returns workspace_required, workspace_unsafe or workspace_invalid, mention it "
    "once in the answer and do not retry logging in a loop."
)


PERSONA_KEYS = tuple(PersonaDescriptor.model_fields)
LOGGED_VALUE_MAX = 128


def parse_persona(value: PersonaDescriptor | dict | str | None) -> PersonaDescriptor | None:
    """Strict parse for the bundle tools; the error names what is wrong with the descriptor."""
    if value is None:
        return None
    try:
        return PersonaDescriptor.model_validate(value)
    except ValidationError as error:
        missing, invalid = [], []
        for item in error.errors():
            key = str(item["loc"][0]) if item["loc"] else "persona"
            (missing if item["type"] == "missing" else invalid).append(key)
        detail = "; ".join(part for part in (
            f"missing: {', '.join(dict.fromkeys(missing))}" if missing else "",
            f"invalid: {', '.join(dict.fromkeys(invalid))}" if invalid else "",
        ) if part)
        raise ValueError(
            "current_persona must be the persona object of the last SUCCESS/NO_CHANGE with all "
            f"{len(PERSONA_KEYS)} keys ({', '.join(PERSONA_KEYS)}); {detail}"
        ) from error




@dataclass
class LoggedPersona:
    """What ``log_interaction`` could read from a client-reported ``persona``.

    ``status`` is None without a persona, else ``client-reported`` (complete and
    valid), ``unverified`` (partial or invalid) or ``mismatch`` (names another
    agent than ``agent_name``). ``descriptor`` is set only for a complete, valid one.
    """

    status: str | None = None
    descriptor: PersonaDescriptor | None = None
    fields: dict = field(default_factory=dict)
    missing: list[str] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _short(value) -> str:
    text = " ".join(str(value).split())
    return text if len(text) <= LOGGED_VALUE_MAX else text[:LOGGED_VALUE_MAX] + "…"


def parse_persona_for_logging(value, agent_name: str) -> LoggedPersona:
    """Never raises: attribution metadata must not cost the logged turn.

    Strict ``parse_persona`` stays authoritative for the bundle tools.
    """
    result = LoggedPersona()
    if value is None:
        return result
    result.status = "unverified"
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed = json.loads(text)
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            value = parsed
        elif text:
            value = {"agent": text}
            result.warnings.append("persona was a bare string; read as the agent name")
        else:
            result.warnings.append("persona was an empty string")
            return result
    if isinstance(value, PersonaDescriptor):
        value = value.model_dump()
    if not isinstance(value, dict):
        result.warnings.append(f"persona must be an object, got {type(value).__name__}")
        return result

    unknown = sorted(str(k) for k in value if k not in PERSONA_KEYS)
    if unknown:
        result.warnings.append(f"dropped unknown persona keys: {', '.join(unknown)}")
    known = {k: v for k, v in value.items() if k in PERSONA_KEYS}
    try:
        result.descriptor = PersonaDescriptor.model_validate(known)
        result.fields = result.descriptor.model_dump()
        result.status = "unverified" if unknown else "client-reported"
    except ValidationError as error:
        for item in error.errors():
            key = str(item["loc"][0]) if item["loc"] else "persona"
            bucket = result.missing if item["type"] == "missing" else result.invalid
            if key not in bucket:
                bucket.append(key)
        result.fields = {k: v for k, v in known.items() if k not in result.invalid}
        if result.missing:
            result.warnings.append(f"persona is missing: {', '.join(result.missing)}")
        if result.invalid:
            result.warnings.append(f"persona has invalid values: {', '.join(result.invalid)}")
    agent = known.get("agent")
    if isinstance(agent, str) and agent and agent != agent_name:
        result.status = "mismatch"
        result.warnings.append("agent_name does not match persona.agent")
    return result


def persona_history_line(logged: LoggedPersona, persona_action) -> str | None:
    """The attribution line appended to a history entry's action; None without persona data."""
    action = _short(persona_action) if persona_action else "unspecified"
    if logged.status is None and persona_action is None:
        return None
    if logged.status == "client-reported":
        d = logged.descriptor
        return (f"Persona (client-reported): {d.agent}; activation={d.activation_id}; "
                f"revision={d.bundle_revision}; action={action}")
    parts = []
    f = logged.fields
    if f.get("agent") is not None:
        parts.append(_short(f["agent"]))
    if f.get("activation_id") is not None:
        parts.append(f"activation={_short(f['activation_id'])}")
    if f.get("bundle_revision") is not None:
        parts.append(f"revision={_short(f['bundle_revision'])}")
    missing = list(logged.missing) if logged.status else ["persona"]
    if missing:
        parts.append(f"missing={','.join(missing)}")
    if logged.invalid:
        parts.append(f"invalid={','.join(logged.invalid)}")
    parts.append(f"action={action}")
    return f"Persona ({logged.status or 'unverified'}): " + "; ".join(parts)


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
