"""Argument types whose advertised JSON Schema survives client rendering.

Some MCP clients drop every ``anyOf``/``$ref`` property to ``{"default": null}``, so
the model never sees a type or shape. These aliases advertise a plain ``type`` plus a
``description`` (no ``anyOf``, ``$ref``, ``enum``, ``pattern`` or nested ``required``)
and accept the shapes models actually send. Each factory takes the parameter's
description, which is the only place the contract text reaches the model.

FastMCP json-decodes a string argument whose annotation is not exactly ``str``, so a
free-text value that looks like JSON arrives as a dict or list; ``opt_str`` turns it
back into text.
"""

import json
import re
from typing import Annotated, Any

from pydantic import BeforeValidator, WithJsonSchema

_STR = {"type": "string"}
_STRS = {"type": "array", "items": {"type": "string"}}
PERSONA_PROPERTIES = {
    "agent": {"type": "string", "description": "Agent name, lowercase letters, digits, underscore."},
    "activation_id": {"type": "string", "description": "Activation id, up to 128 characters."},
    "bundle_revision": {"type": "string", "description": "64 lowercase hex characters."},
    "scope": {"type": "string", "description": "Scope text of the active persona."},
    "skills_loaded": {**_STRS, "description": "Skill ids from the bundle."},
    "implants_loaded": {**_STRS, "description": "Implant ids from the bundle."},
    "rules_loaded": {**_STRS, "description": "Rule ids from the bundle."},
}
PERSONA_KEYS_TEXT = (
    "agent, activation_id, bundle_revision (64 hex characters), scope, skills_loaded, "
    "implants_loaded, rules_loaded"
)


def _split(pattern: str):
    def convert(value: Any) -> Any:
        if isinstance(value, str):
            return [part.strip() for part in re.split(pattern, value) if part.strip()]
        return value
    return convert


def _as_text(value: Any) -> Any:
    return json.dumps(value, ensure_ascii=False) if isinstance(value, (dict, list)) else value


def opt_str(description: str):
    """Optional free text; JSON-looking input stays text."""
    return Annotated[str | None, BeforeValidator(_as_text),
                     WithJsonSchema({**_STR, "description": description})]


def str_list(description: str, *, separators: str = r"[,\n]"):
    """Optional list of strings; a plain string is split on ``separators``."""
    return Annotated[list[str] | None, BeforeValidator(_split(separators)),
                     WithJsonSchema({**_STRS, "description": description})]


def persona_arg(description: str):
    """A persona descriptor object; the body validates it (strictly or leniently)."""
    return Annotated[dict | str | None, WithJsonSchema({
        "type": "object", "properties": PERSONA_PROPERTIES, "description": description})]


def text_or_lines(description: str):
    """Optional history: a list of strings, or one string (the body normalizes it)."""
    return Annotated[list[str] | str | None, WithJsonSchema({**_STRS, "description": description})]


# Parameter descriptions shared by the persona-carrying tools.
CURRENT_PERSONA_DESC = (
    "The `persona` object of the last SUCCESS/NO_CHANGE, copied verbatim; omit or null "
    f"on first activation. All 7 keys: {PERSONA_KEYS_TEXT}."
)
REFRESH_PERSONA_DESC = (
    "Required: the `persona` object of the active role, copied verbatim from the last "
    f"SUCCESS/NO_CHANGE. All 7 keys: {PERSONA_KEYS_TEXT}."
)
CHAT_HISTORY_DESC = "Optional relevant earlier conversation facts, as a JSON array of strings."
REPO_PATH_DESC = "Optional directory within the caller workspace; defaults to the workspace."

LOG_PERSONA_DESC = (
    "The `persona` object of the last SUCCESS/NO_CHANGE, copied verbatim, with all 7 keys: "
    f"{PERSONA_KEYS_TEXT}. Without a retained descriptor omit both persona and persona_action. "
    "Incomplete values are still logged, marked unverified."
)
LOG_PERSONA_ACTION_DESC = (
    "One of keep, switch, refresh, restore; send it only together with persona."
)
LOG_FILES_DESC = 'Optional files touched, as a JSON array of paths, e.g. ["src/a.py"].'
LOG_TAGS_DESC = 'Optional tags, as a JSON array of strings, e.g. ["bug", "fix"].'
