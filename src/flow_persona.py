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

import os
import re

from src.flows import FlowError, split_flow_frontmatter
from src.utils.prompt_loader import split_frontmatter

KINDS = ("skills", "implants", "rules")
_AGENT = re.compile(r"[a-z0-9][a-z0-9_]*")
_COMPONENT = re.compile(r"[A-Za-z0-9_-]+")
_MAX_COMPONENTS = 64
_PERSONA_KEY = re.compile(r"""^[ \t]*(?:persona|"persona"|'persona')[ \t]*:""", re.MULTILINE)


def declared(content: str) -> dict | None:
    """The persona a flow's frontmatter declares.

    A block that is not YAML is a Markdown rule, not frontmatter, unless it names
    ``persona:``; then a typo is an error instead of a silently ignored choice.
    """
    meta, _ = split_flow_frontmatter(content)
    if meta is None:
        raw = split_frontmatter(content)[0] if content.startswith("---") else None
        if raw is not None and _PERSONA_KEY.search(raw):
            raise FlowError("flow_invalid: the frontmatter with persona is not a valid YAML mapping")
        return None
    return normalize(meta.get("persona"))


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
        raise FlowError("flow_invalid: unknown persona fields: "
                        + ", ".join(sorted(map(str, unknown))))
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


def check_known(spec: dict | None) -> None:
    """Reject an agent or component this installation does not have.

    Checks only what the persona names, by file, so a broken unrelated agent,
    skill or rule cannot block saving or running a flow that does not use it.
    """
    if spec is None:
        return
    from src.engine.rules import load_selected_rules
    from src.utils.prompt_loader import resolve_path

    def exists(reference: str) -> bool:
        try:
            return os.path.isfile(resolve_path(reference))
        except ValueError:
            return False

    if not exists(f"@agents/{spec['agent']}/system_prompt.mdc"):
        raise FlowError(f"flow_invalid: unknown agent {spec['agent']}")
    for kind in ("skills", "implants"):
        missing = [item for item in spec.get(kind) or () if not exists(f"@{kind}/{item}.mdc")]
        if missing:
            raise FlowError(f"flow_invalid: unknown {kind}: {', '.join(missing)}")
    if spec.get("rules"):
        try:
            load_selected_rules(spec["rules"])
        except (ValueError, OSError) as error:
            raise FlowError(f"flow_invalid: rules: {error}") from None


def selection(spec: dict):
    """The bundle's ``ComponentSelection``; omitted kinds keep the agent's default.

    Always a selection, never None: a flow's activation is compared by bundle
    revision, so the same agent with other components is a new activation, and
    it never trains the shared router cache.
    """
    from src.engine.persona_bundle import ComponentSelection

    return ComponentSelection(**{kind: tuple(spec[kind]) for kind in KINDS if kind in spec})
