"""The structured record a turn may attach to its journal entry (#584).

``log_interaction(record=...)`` stores one small typed map in the entry's existing
``**Meta:**`` JSON line, under the reserved key ``metadata["record"]``. It is never merged
into ``metadata`` itself, which also holds the server's own keys (``_agents_write``,
``claimed_workspace``) that idempotent replay and the fallback journal trust.
``read_history`` returns ``metadata`` whole in recency and entry mode, so a later run reads
the record back as it was written; semantic mode returns no ``metadata``.

Schema v1 is closed: the version ``v``, then bounded integers, enumerated values, slugs
and UTC times, each under a key this module names. There is no free text and no path,
because a record is read back by machines and a stray path or secret would sit in every
entry that carries one. Unknown keys are refused, a slug that looks like a credential is
refused, and a record over ``MAX_BYTES`` once serialized is refused, so the entry stays
small enough that a ``read_history`` listing never reduces it to a ``stub``.

``validate`` returns the record to store, with times rewritten in UTC, or the problems.
A problem names the place and the rule, never the value or an unknown key's name, so the
answer that reports it cannot echo what the record was refused for.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Mapping, Optional, Tuple

from src.user_sync.scope import SECRET_PATTERNS

RECORD_KEY = "record"
VERSION = 1
MAX_BYTES = 4096
MAX_PROBLEMS = 20

# Lowercase letters and digits, joined by single dots, dashes or underscores: a session id,
# a catalogue slug, a step number such as 5.3, an error code. No slash, no space, no case.
_SLUG = re.compile(r"[a-z0-9]+(?:[._-][a-z0-9]+)*")
SLUG_MAX = 64

KINDS = ("report", "patch-apply", "status")
# caught: a hook blocked or reminded, and the same turn then made the call; never a miss.
FINDING_STATUSES = ("used", "caught", "misused", "missed", "unavailable", "n/a", "uncatalogued", "no-rule")
CAUSES = ("not-in-context", "ignored", "not-loaded", "no-trigger", "failed")
LEVELS = ("V0", "V1", "V2", "V3", "V4", "refuted", "stale", "contested")
LAYERS = ("memory", "user-instruction", "client-instruction", "client-config", "hook", "flow",
          "flow-persona", "flow-library", "agents-core", "issue")
PATCH_STATES = ("proposed", "applied", "verified", "declined", "failed")


class _Rule:
    """One value's rule: ``check`` returns the value to store and appends what is wrong."""

    def check(self, value: Any, where: str, problems: List[str]) -> Any:  # pragma: no cover - abstract
        raise NotImplementedError

    def text(self) -> str:  # pragma: no cover - abstract
        raise NotImplementedError


@dataclass(frozen=True)
class Int(_Rule):
    low: int
    high: int

    def check(self, value, where, problems):
        # bool is an int in Python; true is not a count.
        if isinstance(value, bool) or not isinstance(value, int) or not self.low <= value <= self.high:
            problems.append(f"{where}: {self.text()}")
            return None
        return value

    def text(self):
        return f"an integer from {self.low} to {self.high}"


@dataclass(frozen=True)
class Enum(_Rule):
    values: Tuple[str, ...]

    def check(self, value, where, problems):
        if not isinstance(value, str) or value not in self.values:
            problems.append(f"{where}: {self.text()}")
            return None
        return value

    def text(self):
        return "one of " + ", ".join(self.values)


@dataclass(frozen=True)
class Slug(_Rule):
    def check(self, value, where, problems):
        if not isinstance(value, str) or len(value) > SLUG_MAX or not _SLUG.fullmatch(value):
            problems.append(f"{where}: {self.text()}")
            return None
        if any(pattern.search(value.encode("ascii")) for _, pattern in SECRET_PATTERNS):
            problems.append(f"{where}: looks like a credential; record a reference, not the value")
            return None
        return value

    def text(self):
        return (f"a slug of at most {SLUG_MAX} characters: lowercase letters and digits joined by "
                "single dots, dashes or underscores")


@dataclass(frozen=True)
class Time(_Rule):
    def check(self, value, where, problems):
        moment = None
        if isinstance(value, str) and len(value) <= 40 and "T" in value:
            try:
                moment = _dt.datetime.fromisoformat(value)
            except ValueError:
                moment = None
        if moment is None or moment.tzinfo is None:
            problems.append(f"{where}: {self.text()}")
            return None
        # The journal's own times are UTC with an offset; stored the same way, the two compare as text.
        return moment.astimezone(_dt.timezone.utc).replace(microsecond=0).isoformat()

    def text(self):
        return "an ISO 8601 time with its UTC offset, such as 2026-11-19T13:24:14+00:00"


@dataclass(frozen=True)
class Obj(_Rule):
    fields: Mapping[str, _Rule]
    required: Tuple[str, ...] = ()

    def check(self, value, where, problems):
        if not isinstance(value, dict):
            problems.append(f"{where}: {self.text()}")
            return None
        unknown = [key for key in value if key not in self.fields]
        if unknown:
            # Counted, not named: a key is caller text and may be what the record was refused for.
            problems.append(f"{where}: {len(unknown)} key(s) not in schema v{VERSION}; allowed: "
                            + ", ".join(self.fields))
        for key in self.required:
            if key not in value:
                problems.append(f"{where}.{key}: required, {self.fields[key].text()}")
        out = {}
        for key, rule in self.fields.items():
            if key in value:
                checked = rule.check(value[key], f"{where}.{key}", problems)
                if checked is not None:
                    out[key] = checked
        return out

    def text(self):
        return "an object with " + ", ".join(self.fields)


@dataclass(frozen=True)
class Items(_Rule):
    item: _Rule
    most: int

    def check(self, value, where, problems):
        if not isinstance(value, list) or len(value) > self.most:
            problems.append(f"{where}: {self.text()}")
            return None
        return [self.item.check(entry, f"{where}[{index}]", problems) for index, entry in enumerate(value)]

    def text(self):
        return f"a list of at most {self.most} items, each {self.item.text()}"


COUNT = Int(0, 100_000)

FINDING = Obj({
    "slug": Slug(),
    "status": Enum(FINDING_STATUSES),
    "cause": Enum(CAUSES),
    "miss_at": Time(),
    "level": Enum(LEVELS),
}, required=("slug", "status"))

PATCH = Obj({
    "id": Slug(),
    "layer": Enum(LAYERS),
    "slug": Slug(),
    "state": Enum(PATCH_STATES),
    "applied_at": Time(),
}, required=("layer", "slug", "state"))

VERIFIER = Obj({name: COUNT for name in ("claims", "confirmed", "refuted", "unconfirmed", "invalid_rounds")})

UNEXECUTABLE = Obj({"step": Slug(), "code": Slug()}, required=("step", "code"))

SCHEMA_V1 = Obj({
    "v": Int(VERSION, VERSION),
    "kind": Enum(KINDS),
    "session": Slug(),
    "close_ordinal": Int(1, 10_000),
    "findings": Items(FINDING, 32),
    "patches": Items(PATCH, 32),
    "verifier": VERIFIER,
    "prep_failures": COUNT,
    "unexecutable": Items(UNEXECUTABLE, 16),
    "duration_s": Int(0, 10_000_000),
    "responses": Int(0, 1_000_000),
    "output_tokens": Int(0, 1_000_000_000),
}, required=("v", "kind"))


def size(record: Mapping[str, Any]) -> int:
    """Bytes of ``record`` as compact JSON, the measure ``MAX_BYTES`` caps."""
    return len(json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def validate(value: Any) -> Tuple[Optional[Dict[str, Any]], List[str]]:
    """``(record, [])`` to store, or ``(None, problems)``; ``problems`` holds at most ``MAX_PROBLEMS``."""
    problems: List[str] = []
    if not isinstance(value, dict):
        return None, [f"record: a JSON object of schema v{VERSION}, starting with v and kind"]
    if value.get("v") != VERSION or isinstance(value.get("v"), bool):
        # Under another version every other key means something else: report only this.
        return None, [f"record.v: required, {VERSION} (the only schema version)"]
    record = SCHEMA_V1.check(value, "record", problems)
    if not problems and (measured := size(record)) > MAX_BYTES:
        problems.append(f"record: {measured} bytes once serialized, at most {MAX_BYTES}; "
                        "record fewer findings or patches")
    if problems:
        dropped = len(problems) - MAX_PROBLEMS
        problems = problems[:MAX_PROBLEMS] + ([f"... and {dropped} more"] if dropped > 0 else [])
        return None, problems
    return record, []


def describe() -> str:
    """The schema in one paragraph, for the tool argument's description."""
    return (
        f"Optional structured record of this turn, schema v{VERSION}, stored whole in the journal "
        "entry and returned by read_history in recency and entry mode. A closed JSON object, "
        f"at most {MAX_BYTES} bytes: v={VERSION} and kind ({', '.join(KINDS)}) are required; "
        "optional session (slug), close_ordinal (int), findings [{slug, status, cause, miss_at, level}], "
        "patches [{id, layer, slug, state, applied_at}], verifier {claims, confirmed, refuted, "
        "unconfirmed, invalid_rounds}, prep_failures (int), unexecutable [{step, code}], duration_s, "
        "responses, output_tokens (ints). Slugs are lowercase letters and digits joined by . - or _; "
        "times are ISO 8601 with an offset. No free text and no paths. status: "
        f"{', '.join(FINDING_STATUSES)}; cause: {', '.join(CAUSES)}; level: {', '.join(LEVELS)}; "
        f"layer: {', '.join(LAYERS)}; state: {', '.join(PATCH_STATES)}. An invalid record is "
        "refused and nothing is logged: fix what record_errors names and call once more."
    )


__all__ = [
    "CAUSES", "FINDING_STATUSES", "KINDS", "LAYERS", "LEVELS", "MAX_BYTES", "PATCH_STATES",
    "RECORD_KEY", "SCHEMA_V1", "VERSION", "describe", "size", "validate",
]
