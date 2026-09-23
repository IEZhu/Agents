"""Fresh, all-or-nothing context bundles for persona protocol version 2.

Retrievers choose component IDs using the existing per-agent policies. Their
indexed bodies are never issued here: every selected source and import is read
again so a refresh describes the content actually delivered to the client.
There is no active-persona state or prompt cache in this module.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from dataclasses import dataclass

from src.engine import enrichment
from src.engine.config import IMPLANTS_DEEP_TIER_DEFAULT, MAX_PREFERRED_IMPLANTS
from src.engine.rules import format_rules_for_prompt, get_rules
from src.utils.prompt_loader import process_imports, read_mdc, resolve_path


@dataclass(frozen=True)
class PersonaBundle:
    agent: str
    scope: str
    bundle_revision: str
    persona_block: str
    rules_block: str
    skills_block: str
    implants_block: str
    skills_loaded: list[str]
    implants_loaded: list[str]
    rules_loaded: list[str]
    tier: str


def _component_id(value: str) -> str:
    value = value.removesuffix(".mdc")
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError(f"Invalid component ID: {value!r}")
    return value


def _declared_ids(metadata: dict, key: str) -> list[str]:
    values = metadata.get(key, [])
    if values is None:
        values = []
    if not isinstance(values, list) or any(not isinstance(v, str) for v in values):
        raise ValueError(f"Agent {key} must be a list of component IDs")
    return list(dict.fromkeys(_component_id(value) for value in values))


def _fresh_component(kind: str, component_id: str) -> dict:
    component_id = _component_id(component_id)
    filename = f"{component_id}.mdc"
    path = resolve_path(f"@{kind}/{filename}")
    metadata, body = read_mdc(path, require_frontmatter=True)
    for key in ("description", "compiled", "short_name"):
        if key in metadata and not isinstance(metadata[key], str):
            raise ValueError(f"{key} must be text in {path}")
    if not metadata.get("description", "").strip():
        raise ValueError(f"Missing component description in {path}")
    # These fields can be rendered instead of the body at the standard tier.
    for key in ("description", "compiled"):
        if metadata.get(key):
            metadata[key] = process_imports(metadata[key], {path}, strict=True)
    body = process_imports(body, {path}, strict=True)
    return {
        "filename": filename,
        "content": body,
        "metadata": {**metadata, "filename": filename, "body": body},
    }


def _fresh_components(kind: str, component_ids: list[str]) -> list[dict]:
    return [_fresh_component(kind, value) for value in dict.fromkeys(component_ids)]


async def build_persona_bundle(
    agent_name: str,
    query: str,
    history: list[str] | None = None,
    tier: str | None = None,
) -> PersonaBundle:
    """Assemble separate validated blocks; raise before returning on any error.

    ``history`` is the caller's relevant excerpt, not a server-side transcript.
    No revision includes the query, history or activation ID: equal delivered
    content yields an equal revision across clients and requests.
    """
    if not re.fullmatch(r"[A-Za-z0-9_-]+", agent_name):
        raise ValueError(f"Invalid agent name: {agent_name!r}")
    path = resolve_path(f"@agents/{agent_name}/system_prompt.mdc")
    metadata, body = await asyncio.to_thread(read_mdc, path, require_frontmatter=True)
    identity = metadata.get("identity")
    if not isinstance(identity, dict) or identity.get("name") != agent_name:
        raise ValueError(f"Agent metadata identity does not match {agent_name}")
    scope = identity.get("role")
    if not isinstance(scope, str) or not scope.strip():
        raise ValueError(f"Agent {agent_name} has no competency description")

    core = _declared_ids(metadata, "core_skills")
    preferred = _declared_ids(metadata, "preferred_skills")
    capable = _declared_ids(metadata, "capable_skills")
    preferred_implants = _declared_ids(metadata, "preferred_implants")
    profile = enrichment.resolve_profile(query)
    if tier is None:
        tier = profile.tier if profile is not None else enrichment.infer_tier(query)
        # The promotion is NEVER waived here, unlike src/server.py. The waiver is
        # a per-query decision and this bundle is session-scoped:
        # `persona.load_persona` returns NO_CHANGE for the same agent, so a
        # conversation that opens with "hi" would otherwise run its whole
        # remaining length on a `lite` bundle — no semantic skills, no implants.
        # The v1 path can waive safely because SESSION_CACHE re-derives per query.
        if tier == "lite" and preferred_implants:
            tier = "standard"
    if tier not in ("lite", "standard", "deep"):
        raise ValueError(f"Invalid enrichment tier: {tier!r}")
    # Everything below derives from `tier` alone. The profile contributed the tier
    # above and is deliberately not consulted again.
    #
    # A v2 bundle is a SESSION-scoped artifact: `persona.load_persona` returns
    # NO_CHANGE for the same agent on every later turn, so it is built once and
    # reused. A per-query budget here would let the activating turn fix the skill
    # pool, render mode and implant count for the whole conversation — a turn that
    # happened to be a lookup would leave every later turn with zero semantic
    # skills and zero implants. The same reasoning keeps
    # `suppress_persona_format` out of this bundle.
    #
    # The tier-derived expressions below are written WITHOUT a
    # `profile.X if profile else ...` fallback on purpose. Carrying live-looking
    # branches that are always dead is a trap: relocating one line would silently
    # re-enable per-query budgets in a session-scoped artifact.

    persona_block = await asyncio.to_thread(process_imports, body, {path}, strict=True)
    # NOTE: `profile.suppress_persona_format` is deliberately NOT applied here.
    # A v2 bundle is a SESSION-scoped artifact: `persona.load_persona` returns
    # NO_CHANGE while the same agent stays active, so this block is built once and
    # reused for every later turn. Baking a per-query decision into it means that
    # if the activating turn happens to be a greeting, the persona keeps its
    # `## Output Format` stripped for the rest of the conversation — for
    # code_reviewer or medical_expert that is the whole response contract.
    # The v1 path is safe because SESSION_CACHE is keyed on the query hash, so it
    # re-derives per query; suppression therefore lives only in
    # `enrichment.enrich_agent_prompt`.
    rules = await asyncio.to_thread(get_rules, fresh=True, strict=True)
    rules_block = format_rules_for_prompt(rules)

    selected_skills = await asyncio.to_thread(
        enrichment.skill_retriever.retrieve, query,
        mandatory=core or None, preferred=preferred or None, capable=capable or None,
        n_results=enrichment._n_results_for_tier(tier),
    )
    allowed = set(core + preferred + capable)
    skill_ids = list(core)
    for selected in selected_skills:
        component_id = _component_id(selected["filename"])
        if component_id not in allowed:
            raise ValueError(f"Skill {component_id} is outside {agent_name}'s policy")
        skill_ids.append(component_id)
    skills = await asyncio.to_thread(_fresh_components, "skills", skill_ids)
    skills_block = enrichment.skill_retriever.format_skills_for_prompt(
        skills,
        compiled=tier == "standard",
    )

    implants = []
    # The bundle lives for the whole session, so it must not use the per-query
    # IMPLANT_NEED_GATE: a first message that needs no implant would strip the
    # agent's declared implants from every later turn.
    if tier in ("standard", "deep"):
        default_count = 2 if tier == "standard" else IMPLANTS_DEEP_TIER_DEFAULT
        count = min(max(default_count, len(preferred_implants)), MAX_PREFERRED_IMPLANTS)
        selected_implants = await asyncio.to_thread(
            enrichment.implant_retriever.retrieve, query,
            n_results=count, role=agent_name,
            context={"history_text": "\n".join(history or [])},
            preferred_implants=preferred_implants or None,
        )
        # Declared implants are loaded from source even if an old index does
        # not yet contain them. Missing mandatory source fails the whole bundle.
        implant_ids = list(dict.fromkeys(
            preferred_implants[:count]
            + [_component_id(item["filename"]) for item in selected_implants]
        ))[:count]
        implants = await asyncio.to_thread(_fresh_components, "implants", implant_ids)
    implants_block = enrichment.implant_retriever.format_implants_for_prompt(implants)

    skills_loaded = [item["filename"].removesuffix(".mdc") for item in skills]
    implants_loaded = [
        item["metadata"].get("short_name") or item["filename"].removesuffix(".mdc")
        for item in implants
    ]
    rules_loaded = [rule.name for rule in rules]
    material = {
        "persona_block": persona_block, "rules_block": rules_block,
        "skills_block": skills_block, "implants_block": implants_block,
        "skills_loaded": skills_loaded, "implants_loaded": implants_loaded,
        "rules_loaded": rules_loaded,
    }
    revision_material = {"agent": agent_name, "scope": scope.strip(), **material}
    revision = hashlib.sha256(json.dumps(
        revision_material, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    return PersonaBundle(
        agent=agent_name, scope=scope.strip(), bundle_revision=revision,
        **material, tier=tier,
    )
