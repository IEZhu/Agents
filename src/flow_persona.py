"""The agent and exact components a flow runs with.

A flow declares a default in its YAML frontmatter::

    ---
    persona:
      agent: code_reviewer
      skills: [skill-dev-clean-code]   # optional: exactly these
      implants: []                     # optional: none
      rules: [no-fabrication]          # optional: exactly these
    ---
    # Review a pull request

An omitted list keeps the agent's own selection for that kind. A personal
overlay (``FlowLibrary.set_persona``) replaces the whole declaration for one flow,
built-in or not, without copying its text; ``persona: null`` in an overlay runs
the flow without a persona even when its frontmatter declares one.
"""
from __future__ import annotations

import re

import yaml

from src.flows import FlowError
from src.utils.prompt_loader import split_frontmatter

KINDS = ("skills", "implants", "rules")
_AGENT = re.compile(r"[a-z0-9][a-z0-9_]*")
_COMPONENT = re.compile(r"[A-Za-z0-9_-]+")
_MAX_COMPONENTS = 64


def frontmatter(content: str) -> tuple[dict, str]:
    """The flow's YAML mapping (empty without one) and the Markdown after it."""
    if not content.startswith("---"):
        return {}, content
    raw, body = split_frontmatter(content)
    if raw is None:
        raise FlowError("flow_invalid: the frontmatter has no closing ---")
    try:
        meta = yaml.safe_load(raw)
    except yaml.YAMLError as error:
        raise FlowError(f"flow_invalid: the frontmatter is not valid YAML ({error})") from None
    if meta is None:
        meta = {}
    if not isinstance(meta, dict):
        raise FlowError("flow_invalid: the frontmatter must be a mapping")
    return meta, body


def normalize(value) -> dict | None:
    """A validated ``{agent, skills?, implants?, rules?}``, or None for no persona.

    Checks the shape only; whether the components exist is checked against the
    installation by ``check_known`` and again when the bundle is built.
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise FlowError("flow_invalid: persona must be a mapping with an agent")
    unknown = set(value) - {"agent", *KINDS}
    if unknown:
        raise FlowError(f"flow_invalid: unknown persona fields: {', '.join(sorted(unknown))}")
    agent = value.get("agent")
    if not isinstance(agent, str) or not _AGENT.fullmatch(agent):
        raise FlowError("flow_invalid: persona.agent must be an agent name such as code_reviewer")
    spec = {"agent": agent}
    for kind in KINDS:
        if kind not in value or value[kind] is None:
            continue
        items = value[kind]
        if not isinstance(items, list) or len(items) > _MAX_COMPONENTS or any(
                not isinstance(item, str) or not _COMPONENT.fullmatch(item.removesuffix(".mdc"))
                for item in items):
            raise FlowError(f"flow_invalid: persona.{kind} must be a list of component IDs")
        spec[kind] = list(dict.fromkeys(item.removesuffix(".mdc") for item in items))
    return spec


def declared(content: str) -> dict | None:
    """The persona a flow's frontmatter declares."""
    return normalize(frontmatter(content)[0].get("persona"))


def check_known(spec: dict | None) -> None:
    """Reject an agent or component this installation does not have."""
    if spec is None:
        return
    from src.component_catalog import known_ids, known_agents

    if spec["agent"] not in known_agents():
        raise FlowError(f"flow_invalid: unknown agent {spec['agent']}")
    for kind in KINDS:
        missing = [item for item in spec.get(kind, ()) if item not in known_ids(kind)]
        if missing:
            raise FlowError(f"flow_invalid: unknown {kind}: {', '.join(missing)}")


def selection(spec: dict):
    """The bundle's ``ComponentSelection``; None when every kind keeps the agent's default."""
    from src.engine.persona_bundle import ComponentSelection

    chosen = {kind: tuple(spec[kind]) for kind in KINDS if kind in spec}
    return ComponentSelection(**chosen) if chosen else None
