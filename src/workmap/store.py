"""Append-only per-task work maps: the parent's top-down view of delegated stages.

One JSON line per event in ``<workspace>/data/workmap/<key>.jsonl``, where ``key`` is the
first 16 hexadecimal characters of the SHA-256 of the task ID, and the exact text of each
submitted result in ``<key>.results.jsonl`` beside it. The map's state is never stored: it
is folded from the events on every read. The events of one change are written together and
each carries the change's last revision (``commit``), so a write torn part-way applies none
of them. The parent writes structure and its own statuses through ``apply``; only the server
records preparation, results and verdicts.
"""
from __future__ import annotations

import contextlib
import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import time
from typing import Callable

from src import safe_store
from src.safe_store import StoreError

FORMAT_VERSION = 1
MAX_STORE_BYTES = 20 * 1024 * 1024
DIRECTORY = ("data", "workmap")
MAX_OPS = 50
LIST_LIMIT = 20  # maps the list view reads, the most recently written first
TITLE_MAX = 120
SUMMARY_MAX = 300
QUESTION_MAX = 300
REASON_MAX = 300
INVALID_LIMIT = 5
NODE_ID = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?")
TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}")
MAP_FILE = re.compile(r"[0-9a-f]{16}\.jsonl")
NODE_KINDS = ("goal", "stage", "decision", "question")
RISKS = ("routine", "load_bearing", "critical")
EDGE_TYPES = ("depends_on", "verifies", "supersedes", "contradicts")
PARENT_STATUSES = ("planned", "accepted", "refuted", "superseded", "blocked")
SERVER_STATUSES = ("prepared", "validated", "needs_attention", "verified", "disputed")
REASONED = ("refuted", "superseded", "blocked")
PREPARABLE = ("planned", "prepared", "needs_attention", "disputed")
SETTLED = ("validated", "verified", "accepted")
# What a package's input may be: a settled node, or a disputed one, whose second verifier
# needs the disputed result itself.
INPUT_STATUSES = SETTLED + ("disputed",)
VERDICT_TARGETS = ("validated", "verified", "disputed")
UNSETTLING = ("refuted", "superseded", "disputed")
LEVELS = ("V0", "V1", "V2", "V3", "V4")
COMPLEXITIES = ("low", "standard", "high")
OPERATIONS = {
    "add_node": {"op", "id", "kind", "title", "summary", "risk"},
    "update_node": {"op", "id", "title", "summary", "risk", "status", "reason", "level"},
    "add_edge": {"op", "from", "to", "type"},
    "remove_edge": {"op", "from", "to", "type"},
    "close": {"op"},
}

# One error type for the tools and the binding to catch; codes tell the cases apart.
WorkmapError = StoreError


def map_key(task_id: str) -> str:
    """The file name stem: a hash, so a task ID's dots and colons never reach the file system."""
    return hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:16]


def _iso(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def _task_id(value: object) -> str:
    if not isinstance(value, str) or not TASK_ID.fullmatch(value):
        raise WorkmapError("invalid_request", "task_id is 1 to 128 letters, digits, '_', '.', ':' or '-', "
                                              "starting with a letter or digit")
    return value


def _node_id(value: object, where: str) -> str:
    if not isinstance(value, str) or not NODE_ID.fullmatch(value):
        raise WorkmapError("invalid_request", f"{where}: a node id is 1 to 40 characters of a-z, 0-9 and "
                                              "'-', starting and ending with a letter or digit")
    return value


def _text(where: str, value: object, limit: int, *, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or (not allow_empty and not value.strip()):
        raise WorkmapError("invalid_request", f"{where} is required text")
    value = " ".join(value.split())
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:  # a lone surrogate: the record could not be written
        raise WorkmapError("invalid_request", f"{where} holds a character UTF-8 cannot encode") from None
    if len(value) > limit:
        raise WorkmapError("invalid_request", f"{where} is {len(value)} characters; at most {limit}")
    return value


def _choice(where: str, value: object, allowed: tuple[str, ...]) -> str:
    if value not in allowed:
        raise WorkmapError("invalid_request", f"{where} must be one of {', '.join(allowed)}; got {value!r}")
    return value


@dataclass
class WorkMap:
    """A map's state, folded from its events; ``nodes`` keeps creation order."""

    task_id: str
    revision: int = 0
    closed: bool = False
    nodes: dict[str, dict] = field(default_factory=dict)
    edges: list[tuple[str, str, str]] = field(default_factory=list)
    invalid: dict[str, int] = field(default_factory=dict)
    # The codes of each contract's last invalid submission.
    invalid_codes: dict[str, list[str]] = field(default_factory=dict)
    # The contracts that have a result: neither a second result nor an invalid count follows one.
    resulted: set[str] = field(default_factory=set)
    updated: str = ""  # when the last applied event was written, as the store stamps it

    def targets(self, node_id: str, edge_type: str) -> list[str]:
        return [target for source, target, kind in self.edges if source == node_id and kind == edge_type]

    def sources(self, node_id: str, edge_type: str) -> list[str]:
        return [source for source, target, kind in self.edges if target == node_id and kind == edge_type]


class EdgeIndex:
    """A map's edges by node and type, built in one pass: what a view asks of every node.

    ``WorkMap.targets`` scans every edge, which a whole-map view would repeat once per node; the
    index answers the same lists, in edge order, from the snapshot it was built from.
    """

    def __init__(self, workmap: WorkMap):
        self._targets: dict[tuple[str, str], list[str]] = {}
        self._sources: dict[tuple[str, str], list[str]] = {}
        for source, target, kind in workmap.edges:
            self._targets.setdefault((source, kind), []).append(target)
            self._sources.setdefault((target, kind), []).append(source)

    def targets(self, node_id: str, edge_type: str) -> list[str]:
        return self._targets.get((node_id, edge_type), [])

    def sources(self, node_id: str, edge_type: str) -> list[str]:
        return self._sources.get((node_id, edge_type), [])


# What a node holds of its attempt's result; a new preparation sets them back to these values.
NO_RESULT = {"checks": None, "findings": None, "limitations": 0, "handback": None, "result_status": None,
             "question": None, "escalation": None}


def _new_node(event: dict) -> dict:
    # planned_summary is the parent's own summary: a result replaces summary, a new attempt restores it.
    return {"id": event["id"], "kind": event["kind"], "title": event["title"], "summary": event["summary"],
            "planned_summary": event["summary"], "risk": event["risk"], "status": "planned", "level": "V0",
            "reason": None, "complexity": None, "refs": {}, **NO_RESULT, "changed": event["rev"]}


def _higher(kept: str | None, prepared: object) -> str | None:
    """The higher of two complexities: a node once prepared as high keeps needing V3."""
    def rank(value: object) -> int:
        return COMPLEXITIES.index(value) if value in COMPLEXITIES else -1
    return prepared if rank(prepared) > rank(kept) else kept


_VALUES = {"kind": NODE_KINDS, "risk": RISKS, "type": EDGE_TYPES, "level": LEVELS,
           "status": PARENT_STATUSES + SERVER_STATUSES}
# The only enum fields an event may carry as null: an invalid count that changes no status, a
# verdict that leaves the level alone, a package prepared without a complexity.
_NULLABLE = {("invalid", "status"), ("verdict", "level"), ("prepared", "complexity")}


def _is_text(value: object, *, optional: bool = False) -> bool:
    return isinstance(value, str) or (optional and value is None)


def _is_count(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _is_line(value: object, limit: int, *, allow_empty: bool = False) -> bool:
    """Text as the writers leave it (_text, and the binding's _cut): one line of single spaces, at
    most ``limit`` characters, encodable as UTF-8, and not empty unless the field may be. A view
    renders each such field on one line, so anything else would break or forge the lines around it."""
    if not isinstance(value, str) or value != " ".join(value.split()) or len(value) > limit:
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return allow_empty or bool(value)


# The status and level a result gives its node, as outcome_of chooses them.
_RESULT_OUTCOMES = {("validated", "V1"), ("validated", "V2"), ("needs_attention", "V0")}
# What workmap_submit records as the way a result came back: the full hand-back or the one line.
HANDBACKS = ("full", "line")
# An invalid submission keeps at most this many validation codes, each as long as
# ValidationError.code allows (src/schemas/subagent.py); the node view joins them on one line.
CODES_KEPT = 20
CODE_MAX = 80


# A contract revision and a result hash are 64 lowercase hex digits (Revision in
# src/schemas/subagent.py); an attempt id has a task id's form; a review's findings count the
# severities a finding may carry.
REVISION = re.compile(r"[a-f0-9]{64}")
SEVERITIES = ("critical", "high", "medium", "low", "info")


def _is_revision(value: object) -> bool:
    return isinstance(value, str) and REVISION.fullmatch(value) is not None


def _is_identifier(value: object) -> bool:
    return isinstance(value, str) and TASK_ID.fullmatch(value) is not None


def _package_problem(contract_revision: object, attempt_id: object, inputs: object) -> str | None:
    """Why a prepared package's references are not what get_subagent_context gives, or None: a
    contract revision of 64 hex digits, an attempt id of a task id's form, and inputs that pin
    node ids to result hashes."""
    if not _is_revision(contract_revision):
        return "a contract revision is 64 lowercase hex digits"
    if not _is_identifier(attempt_id):
        return "an attempt id has a task id's form"
    if inputs is not None and not (isinstance(inputs, dict) and all(
            isinstance(key, str) and NODE_ID.fullmatch(key) and _is_revision(value) for key, value in inputs.items())):
        return "inputs pin node ids to 64-hex result hashes"
    return None


def _text_sha256(text: object) -> str | None:
    """The SHA-256 the validator gives a result text (its UTF-8 bytes), or None for no text."""
    if not isinstance(text, str):
        return None
    try:
        return hashlib.sha256(text.encode("utf-8")).hexdigest()
    except UnicodeEncodeError:
        return None


def _codes_ok(codes: object) -> bool:
    return (isinstance(codes, list) and len(codes) <= CODES_KEPT
            and all(_is_line(code, CODE_MAX) for code in codes))


# The worker's own status a result keeps (Report.status in src/schemas/subagent.py), and the keys
# of the escalation the validation names (the binding's _escalation).
RESULT_STATUSES = ("completed", "input_required", "partial", "failed")
ESCALATION_KEYS = ("complexity", "tier", "reason")


def _escalation_ok(value: object) -> bool:
    return value is None or (isinstance(value, dict) and set(value) <= set(ESCALATION_KEYS)
                             and all(item is None or _is_line(item, CODE_MAX) for item in value.values()))


def _result_fields(summary: object, question: object, handback: object, result_status: object,
                   escalation: object) -> str | None:
    """Why a result's fields are not the writers' own, or None: a summary of up to SUMMARY_MAX and a
    question of up to QUESTION_MAX, both cut to one line, one of HANDBACKS, the worker's status as
    one of RESULT_STATUSES, and an escalation of ESCALATION_KEYS with one-line values."""
    if not _is_line(summary, SUMMARY_MAX, allow_empty=True):
        return f"a result summary is one line of at most {SUMMARY_MAX} characters"
    if question is not None and not _is_line(question, QUESTION_MAX):
        return f"a result question is one line of at most {QUESTION_MAX} characters"
    if handback not in HANDBACKS:
        return f"a result hand-back is one of {', '.join(HANDBACKS)}"
    if result_status is not None and result_status not in RESULT_STATUSES:
        return f"a result's worker status is one of {', '.join(RESULT_STATUSES)}"
    if not _escalation_ok(escalation):
        return f"an escalation has only {', '.join(ESCALATION_KEYS)}, each one line"
    return None


def _takes_submission(workmap: WorkMap, node: dict, contract_revision: object) -> bool:
    """What _require_open and the writers' duplicate checks let through: the node's current
    contract, while the node is prepared or needs_attention, before that contract has a result."""
    return (_is_text(contract_revision) and node["refs"].get("contract_revision") == contract_revision
            and node["status"] in ("prepared", "needs_attention") and contract_revision not in workmap.resulted)


def _check_update(workmap: WorkMap, event: dict) -> None:
    """The rules of workmap_update's update_node: text as _text leaves it, parent statuses only,
    accepted only from a settled node, a critical node accepted below V3 only with V4, a reason
    exactly with the statuses that need one, and level V4 only together with accepted."""
    node = workmap.nodes[event["id"]]
    if ("title" in event and not _is_line(event["title"], TITLE_MAX)) or (
            "summary" in event and not _is_line(event["summary"], SUMMARY_MAX, allow_empty=True)):
        raise ValueError("update_node text fields")
    if "status" in event:
        status = event["status"]
        if status not in PARENT_STATUSES:
            raise ValueError(f"update_node status {status!r} is the server's")
        if status == "accepted" and node["status"] not in SETTLED:
            raise ValueError("update_node accepts a node that is not settled")
        if (status == "accepted" and event.get("risk", node["risk"]) == "critical" and event.get("level") != "V4"
                and LEVELS.index(node["level"]) < LEVELS.index("V3")):
            raise ValueError("update_node accepts a critical node below V3")
        reason = event.get("reason")
        if not (_is_line(reason, REASON_MAX) if status in REASONED else reason is None):
            raise ValueError("update_node reason")
    elif "reason" in event:
        raise ValueError("update_node reason without a status")
    if "level" in event and (event["level"] != "V4" or event.get("status") != "accepted"):
        raise ValueError("update_node level")


def _check_event(workmap: WorkMap, event: dict) -> None:
    """Raise ValueError for an event the writers would never commit, before it changes anything.

    A damaged or foreign line must not reach the state the views and the readiness rules read, so
    a persisted event is held to what workmap_update and the server's own writes enforce: allowed
    values (null only where the event may leave a field unset), the shapes of a result, the edge
    rules (no duplicate, no self or goal dependency, no cycle), the parent's status rules, the
    statuses and levels each server event writes, and the node states it needs. Nothing follows a
    close. fold then skips the event with the rest of its commit."""
    op = event["op"]
    if workmap.closed:
        raise ValueError(f"{op} after close")
    for key, allowed in _VALUES.items():
        if key in event and not (event[key] is None and (op, key) in _NULLABLE) and event[key] not in allowed:
            raise ValueError(f"{op} {key} {event[key]!r}")
    if op == "add_node":
        # _parent_event's rules: an id of NODE_ID's form, a title and a summary as _text leaves them.
        if not (isinstance(event.get("id"), str) and NODE_ID.fullmatch(event["id"])
                and _is_line(event.get("title"), TITLE_MAX)
                and _is_line(event.get("summary"), SUMMARY_MAX, allow_empty=True)):
            raise ValueError("add_node id, title or summary the writer refuses")
    elif op == "update_node":
        _check_update(workmap, event)
    elif op in ("add_edge", "remove_edge"):
        source, target, kind = event["from"], event["to"], event["type"]
        if not (_is_text(source) and _is_text(target)) or source not in workmap.nodes or target not in workmap.nodes:
            raise ValueError(f"{op} between unknown nodes")
        edge = (source, target, kind)
        if op == "remove_edge":
            if edge not in workmap.edges:
                raise ValueError("remove_edge of a missing edge")
        # _parent_event's rules for add_edge: no edge from a node to itself, of any type; no
        # duplicate; and a depends_on edge neither onto a goal nor closing a cycle.
        elif source == target:
            raise ValueError(f"{kind} edge from a node to itself")
        elif edge in workmap.edges:
            raise ValueError("duplicate edge")
        elif kind == "depends_on" and (workmap.nodes[target]["kind"] == "goal" or _reaches(workmap, target, source)):
            raise ValueError("depends_on edge the writer refuses")
    elif op == "prepared":
        node = workmap.nodes[event["id"]]
        if node["kind"] == "goal" or node["status"] not in PREPARABLE:
            raise ValueError("prepared event for a node that takes no package")
        contract, attempt = event.get("contract_revision"), event.get("attempt_id")
        problem = _package_problem(contract, attempt, event.get("inputs"))
        if problem is not None:
            raise ValueError(problem)
        # record_prepared's refusals: a spent contract, another contract for the attempt the node holds.
        if contract in workmap.resulted or workmap.invalid.get(contract, 0) >= INVALID_LIMIT:
            raise ValueError("prepared event for a spent contract")
        if node["refs"].get("attempt_id") == attempt and node["refs"].get("contract_revision") not in (None, contract):
            raise ValueError("prepared event reusing an attempt")
        if event.get("complexity") not in (None, *COMPLEXITIES):
            raise ValueError(f"complexity {event.get('complexity')!r}")
    elif op == "result":
        if not _takes_submission(workmap, workmap.nodes[event["id"]], event.get("contract_revision")):
            raise ValueError("result event for a node that takes no result")
        if (event.get("status"), event.get("level")) not in _RESULT_OUTCOMES:
            raise ValueError("result status and level")
        checks, findings = event.get("checks"), event.get("findings")
        problem = _result_fields(event.get("summary"), event.get("question"), event.get("handback"),
                                 event.get("result_status"), event.get("escalation"))
        if problem is not None:
            raise ValueError(problem)
        # The result belongs to the node's current attempt, under the hash of its stored text.
        if event.get("attempt_id") != workmap.nodes[event["id"]]["refs"].get("attempt_id"):
            raise ValueError("result event for another attempt")
        if not (_is_revision(event.get("result_sha256"))
                and (event.get("limitations") is None or _is_count(event.get("limitations")))):
            raise ValueError("result event text and counts")
        if checks is not None and not (isinstance(checks, dict) and set(checks) == {"passed", "failed", "not_run"}
                                       and all(_is_count(value) for value in checks.values())):
            raise ValueError("result checks")
        if findings is not None and not (isinstance(findings, dict)
                                         and all(key in SEVERITIES and _is_count(value) for key, value in findings.items())):
            raise ValueError("result findings")
    elif op == "invalid":
        node, contract = workmap.nodes[event["id"]], event.get("contract_revision")
        if (not _codes_ok(event.get("codes", [])) or not _escalation_ok(event.get("escalation"))
                or not _takes_submission(workmap, node, contract)):
            raise ValueError("invalid event for a node that takes no submission")
        # record_invalid sends the node to needs_attention exactly when this count reaches the limit.
        limit_reached = node["status"] == "prepared" and workmap.invalid.get(contract, 0) + 1 >= INVALID_LIMIT
        if event.get("status") != ("needs_attention" if limit_reached else None):
            raise ValueError("invalid event status")
    elif op == "verdict":
        target = workmap.nodes[event["id"]]
        verifier = workmap.nodes.get(event.get("verifier")) if _is_text(event.get("verifier")) else None
        if target["status"] not in VERDICT_TARGETS or verifier is None or verifier["status"] != "validated":
            raise ValueError("verdict for a node that takes none")
        # record_result's rule: a validated review verifies a target that still holds the result it was given.
        findings = verifier["findings"]
        if (not isinstance(findings, dict) or event["id"] not in workmap.targets(verifier["id"], "verifies")
                or target["refs"].get("result_sha256") != event.get("result_sha256")
                or (verifier["refs"].get("inputs") or {}).get(event["id"]) != event.get("result_sha256")):
            raise ValueError("verdict without its review")
        severe = sum(findings.get(level, 0) for level in ("critical", "high"))
        if (event.get("status"), event.get("level")) != (("disputed", None) if severe else ("verified", "V3")):
            raise ValueError("verdict status and level")


def _would_apply(workmap: WorkMap, events: list[dict]) -> None:
    """Refuse a server write the fold would skip: apply its events to a copy of the map, so every
    rule replay holds an event to is also a rule its writer holds it to before committing."""
    trial = copy.deepcopy(workmap)
    try:
        for event in events:
            _apply_event(trial, event)
    except (KeyError, TypeError, ValueError) as error:
        raise WorkmapError("invalid_request", f"the map would skip this write: {error}") from None


def _apply_event(workmap: WorkMap, event: dict) -> None:
    """Apply one event, or raise before changing anything: fold skips an event that raises, so a
    damaged one must not leave half its change behind (an edge to a missing node, a count)."""
    op, rev = event["op"], event["rev"]
    _check_event(workmap, event)
    if op == "add_node":
        if event["id"] in workmap.nodes:  # a second add_node would erase the node and its work
            raise KeyError(event["id"])
        workmap.nodes[event["id"]] = _new_node(event)
        return
    if op in ("add_edge", "remove_edge"):
        edge = (event["from"], event["to"], event["type"])
        ends = [workmap.nodes[node_id] for node_id in edge[:2]]
        if op == "add_edge":
            workmap.edges.append(edge)
        else:
            workmap.edges.remove(edge)
        for node in ends:
            node["changed"] = rev
        return
    if op == "close":
        workmap.closed = True
        return
    if op == "invalid":
        if event["id"] not in workmap.nodes:  # a count for a missing node is a damaged event
            raise KeyError(event["id"])
        revision = event["contract_revision"]
        codes = event.get("codes")
        workmap.invalid[revision] = workmap.invalid.get(revision, 0) + 1
        workmap.invalid_codes[revision] = ([code for code in codes if isinstance(code, str)]
                                           if isinstance(codes, list) else [])
        if event.get("status") is None:
            # The count and codes changed: a delta view since the last revision must show the node.
            workmap.nodes[event["id"]]["changed"] = rev
            return
    node = workmap.nodes[event["id"]]
    if op == "update_node":
        for key in ("title", "summary", "risk", "status", "reason", "level"):
            if key in event:
                node[key] = event[key]
        if "summary" in event:
            node["planned_summary"] = event["summary"]
    elif op == "prepared":
        # A new attempt shows nothing of the previous attempt's result.
        node.update(NO_RESULT, status="prepared", level="V0", reason=None, summary=node["planned_summary"],
                    complexity=_higher(node["complexity"], event["complexity"]),
                    refs={"contract_revision": event["contract_revision"], "attempt_id": event["attempt_id"],
                          # Optional: the {node: result sha256} the worker was given; none for older events.
                          **({"inputs": dict(event["inputs"])} if isinstance(event.get("inputs"), dict) else {})})
    elif op == "result":
        sha256 = event["result_sha256"]  # read before the node changes
        node.update(status=event["status"], level=event["level"], summary=event["summary"],
                    checks=event["checks"], findings=event["findings"], limitations=event["limitations"],
                    handback=event["handback"],
                    # Optional: older result events do not carry them.
                    **{key: event.get(key) for key in ("result_status", "question", "escalation")})
        node["refs"] = {**node["refs"], "result_sha256": sha256}
        workmap.resulted.add(event["contract_revision"])
    elif op == "invalid":
        node["status"] = event["status"]
        node["escalation"] = event.get("escalation")
    elif op == "verdict":
        node["status"] = event["status"]
        if event.get("level") is not None:
            node["level"] = event["level"]
    else:
        raise ValueError(op)
    node["changed"] = rev


def _revision(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _commits(events: list):
    """The complete commits, each as the list of its well-formed events, in order.

    A commit is one write of consecutive revisions, each event stamped with the last of them.
    A torn write leaves a run that stops short of that revision, and none of the run applies;
    the next commit reuses its revisions, so a run also ends where the revisions do not follow
    on. An event without a stamp is a commit of its own. A run that reaches its stamp can still
    have lost its first lines; ``_applied`` refuses it by where it starts.
    """
    run: list[dict] = []
    for event in events:
        if not isinstance(event, dict) or not _revision(event.get("rev")):
            continue
        last = event.get("commit", event["rev"])
        if run and (last != run[-1].get("commit", run[-1]["rev"]) or event["rev"] != run[-1]["rev"] + 1):
            run = []
        run.append(event)
        if event["rev"] == last:
            yield run
            run = []


def _apply_commit(workmap: WorkMap, events: list) -> WorkMap | None:
    """The map after every event of one commit, or None when any of them fails to apply.

    A single event raises before it changes anything, so it applies in place; a longer commit
    is applied to a copy, so that its failing event leaves none of the others behind.
    """
    trial = copy.deepcopy(workmap) if len(events) > 1 else workmap
    try:
        for event in events:
            _apply_event(trial, event)
    except (KeyError, TypeError, ValueError):
        return None
    if events:
        trial.revision = events[-1]["rev"]
        stamps = [event["at"] for event in events if isinstance(event.get("at"), str)]
        if stamps:
            trial.updated = stamps[-1]
    return trial


def _applied(events: list):
    """The map after each commit that applies, with that commit's events.

    A commit applies whole or not at all. It must start at the revision after the one already
    applied, so the tail of a commit whose first or middle lines were lost does not apply on its
    own, and an event that fails to apply discards every other event of its commit.
    """
    workmap = None
    for commit in _commits(events):
        if workmap is None:
            head = commit[0]
            # A map opens at revision 1 with a task id of TASK_ID's form, as apply writes it.
            if head.get("op") != "open" or head.get("rev") != 1 or not _is_identifier(head.get("task_id")):
                continue
            opened = WorkMap(task_id=head["task_id"], revision=head["rev"])
            opened.updated = head["at"] if isinstance(head.get("at"), str) else ""
            trial = _apply_commit(opened, commit[1:])
        elif commit[0]["rev"] != workmap.revision + 1:
            continue
        else:
            trial = _apply_commit(workmap, commit)
        if trial is None:
            continue
        workmap = trial
        yield workmap, commit


def fold(events: list) -> WorkMap | None:
    """The state after every committed event, in order; a damaged or foreign line is skipped,
    and with it the rest of its commit."""
    workmap = None
    for workmap, _ in _applied(events):
        pass
    return workmap


def required_level(node: dict) -> str:
    """What a dependant needs from this node: V3 when it is critical or was ever prepared as high complexity."""
    return "V3" if node["risk"] == "critical" or node["complexity"] == "high" else "V2"


def settled(node: dict) -> bool:
    """Whether a dependant may build on this node.

    The parent's acceptance settles a routine or load_bearing node whatever its level, since a
    completed stage without a passed check stays V1; a critical node still needs V3 or V4.
    """
    if node["status"] not in SETTLED:
        return False
    if node["status"] == "accepted" and node["risk"] != "critical":
        return True
    return LEVELS.index(node["level"]) >= LEVELS.index(required_level(node))


def is_ready(workmap: WorkMap, node: dict, edges: EdgeIndex | None = None) -> bool:
    """A planned non-goal node whose dependencies are settled and whose verified targets each hold a
    result that workmap_inputs would take: one in INPUT_STATUSES. ``edges`` is an index a caller
    asking about every node built once; without it the map's own edges are scanned."""
    edges = workmap if edges is None else edges
    if node["kind"] == "goal" or node["status"] != "planned":
        return False
    return (all(settled(workmap.nodes[target]) for target in edges.targets(node["id"], "depends_on"))
            and all(workmap.nodes[target]["status"] in INPUT_STATUSES
                    and "result_sha256" in workmap.nodes[target]["refs"]
                    for target in edges.targets(node["id"], "verifies")))


def is_stale(workmap: WorkMap, node: dict, edges: EdgeIndex | None = None) -> bool:
    edges = workmap if edges is None else edges
    return any(workmap.nodes[target]["status"] in UNSETTLING
               for target in edges.targets(node["id"], "depends_on"))


def _existing(workmap: WorkMap, node_id: object, where: str) -> dict:
    node = workmap.nodes.get(_node_id(node_id, where))
    if node is None:
        raise WorkmapError("unknown_node", f"{where}: the map has no node {node_id}")
    return node


def _reaches(workmap: WorkMap, start: str, goal: str) -> bool:
    """Whether ``start`` depends on ``goal``, directly or through other nodes."""
    seen, stack = set(), [start]
    while stack:
        node_id = stack.pop()
        if node_id == goal:
            return True
        if node_id not in seen:
            seen.add(node_id)
            stack.extend(workmap.targets(node_id, "depends_on"))
    return False


def _parent_event(workmap: WorkMap, op: object, where: str) -> dict:
    """One parent operation checked against the draft state, as the event that records it."""
    if not isinstance(op, dict) or not isinstance(op.get("op"), str) or op["op"] not in OPERATIONS:
        raise WorkmapError("invalid_request", f"{where} is an object whose op is one of {', '.join(OPERATIONS)}")
    if workmap.closed:
        raise WorkmapError("map_closed", "the map is closed")
    kind = op["op"]
    unknown = set(op) - OPERATIONS[kind]
    if unknown:
        raise WorkmapError("invalid_request", f"{where} has unknown fields: {', '.join(sorted(unknown))}")
    if kind == "add_node":
        node_id = _node_id(op.get("id"), f"{where}.id")
        if node_id in workmap.nodes:
            raise WorkmapError("invalid_request", f"{where}: node {node_id} exists")
        return {"op": kind, "by": "parent", "id": node_id,
                "kind": _choice(f"{where}.kind", op.get("kind", "stage"), NODE_KINDS),
                "title": _text(f"{where}.title", op.get("title"), TITLE_MAX),
                "summary": _text(f"{where}.summary", op.get("summary", ""), SUMMARY_MAX, allow_empty=True),
                "risk": _choice(f"{where}.risk", op.get("risk", "load_bearing"), RISKS)}
    if kind == "update_node":
        node = _existing(workmap, op.get("id"), f"{where}.id")
        event = {"op": kind, "by": "parent", "id": node["id"]}
        if "title" in op:
            event["title"] = _text(f"{where}.title", op["title"], TITLE_MAX)
        if "summary" in op:
            event["summary"] = _text(f"{where}.summary", op["summary"], SUMMARY_MAX, allow_empty=True)
        if "risk" in op:
            event["risk"] = _choice(f"{where}.risk", op["risk"], RISKS)
        if "status" in op:
            if op["status"] in SERVER_STATUSES:
                raise WorkmapError("status_reserved", f"{where}: only the server sets {op['status']}")
            status = _choice(f"{where}.status", op["status"], PARENT_STATUSES)
            if status == "accepted" and node["status"] not in SETTLED:
                raise WorkmapError("invalid_transition",
                                   f"{where}: only a validated, verified or accepted node can be accepted")
            # An accepted node takes no verdict, so a critical one accepted below V3 would never settle.
            if (status == "accepted" and event.get("risk", node["risk"]) == "critical"
                    and op.get("level") != "V4" and LEVELS.index(node["level"]) < LEVELS.index("V3")):
                raise WorkmapError("invalid_transition",
                                   f"{where}: a critical node is accepted at V3, after a verifier, or with level "
                                   "V4 when the user accepted it")
            event["status"] = status
            event["reason"] = (_text(f"{where}.reason", op.get("reason"), REASON_MAX)
                               if status in REASONED else None)
        elif "reason" in op:
            raise WorkmapError("invalid_request", f"{where}: a reason goes with a status")
        if "level" in op:
            if op["level"] != "V4" or event.get("status") != "accepted":
                raise WorkmapError("invalid_request",
                                   f"{where}: the parent sets only level V4, together with status accepted")
            event["level"] = "V4"
        if len(event) == 3:
            raise WorkmapError("invalid_request", f"{where} changes nothing")
        return event
    if kind in ("add_edge", "remove_edge"):
        source = _existing(workmap, op.get("from"), f"{where}.from")["id"]
        target = _existing(workmap, op.get("to"), f"{where}.to")["id"]
        edge_type = _choice(f"{where}.type", op.get("type"), EDGE_TYPES)
        edge = (source, target, edge_type)
        if kind == "add_edge":
            if source == target:
                raise WorkmapError("invalid_request", f"{where}: a node cannot point to itself")
            if edge in workmap.edges:
                raise WorkmapError("invalid_request", f"{where}: the edge exists")
            if edge_type == "depends_on" and workmap.nodes[target]["kind"] == "goal":
                raise WorkmapError("invalid_request", f"{where}: {target} is a goal, which never settles; "
                                                      "nothing can depend_on it")
            if edge_type == "depends_on" and _reaches(workmap, target, source):
                raise WorkmapError("cycle", f"{where}: {source} depends_on {target} would close a cycle")
        elif edge not in workmap.edges:
            raise WorkmapError("invalid_request", f"{where}: no such edge")
        return {"op": kind, "by": "parent", "from": source, "to": target, "type": edge_type}
    return {"op": "close", "by": "parent"}


def _result_of(records: list, contract_revision: str) -> dict | None:
    """The result event a contract already has on the map, if any.

    Only a committed event counts, as in the fold: a result whose commit was torn never applied,
    so the node still waits for it and its contract stays open.
    """
    found = None
    for _, commit in _applied(records):
        for event in commit:
            if event.get("op") == "result" and event.get("contract_revision") == contract_revision:
                found = event
    return found


def _require_open(workmap: WorkMap, node: dict, contract_revision: str) -> None:
    """Refuse a submission the node no longer takes: a prepared node, or one sent to
    needs_attention by this contract's own invalid results, and only from its current contract."""
    current = node["refs"].get("contract_revision") == contract_revision
    if workmap.closed or not (node["status"] == "prepared" or (node["status"] == "needs_attention" and current)):
        raise WorkmapError("node_not_open", f"node {node['id']} no longer takes this result: the map "
                                            "is closed or the node was settled or set aside")
    if not current:
        raise WorkmapError("stale_contract", f"node {node['id']} was prepared again; this contract is "
                                             "no longer its current attempt")


class WorkmapStore:
    """The work maps of one workspace."""

    def __init__(self, root: Path | str, *, clock: Callable[[], float] = time.time):
        self.root = Path(root)
        self.directory = self.root.joinpath(*DIRECTORY)
        self.clock = clock

    # --- files ---------------------------------------------------------------------

    def _read(self, name: str) -> list:
        with safe_store.store_directory(self.root, DIRECTORY, create=False, private=False) as directory:
            if directory is None:
                return []
            with safe_store.store_file(directory, name, os.O_RDONLY, "rb", private=False) as handle:
                if handle is None:
                    return []
                return safe_store.read_records(handle.read(MAX_STORE_BYTES + 1), MAX_STORE_BYTES,
                                               self.directory / name)

    @contextlib.contextmanager
    def _locked(self, name: str):
        """The open file under its exclusive lock."""
        with safe_store.store_directory(self.root, DIRECTORY, create=True, private=True) as directory, \
                safe_store.store_file(directory, name, os.O_RDWR | os.O_CREAT | os.O_APPEND, "r+b",
                                      private=True) as handle, \
                safe_store.exclusive(handle, self.directory / name):
            yield handle

    @contextlib.contextmanager
    def _writing(self, name: str):
        """The open file under its exclusive lock, and the records it held when the lock was taken."""
        with self._locked(name) as handle:
            handle.seek(0)
            yield handle, safe_store.read_records(handle.read(MAX_STORE_BYTES + 1), MAX_STORE_BYTES,
                                                  self.directory / name)

    @staticmethod
    def _encode(records: list[dict]) -> bytes:
        return "".join(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                       for record in records).encode("utf-8")

    def _append(self, handle, name: str, records: list[dict]) -> None:
        safe_store.append_line(handle, self._encode(records), MAX_STORE_BYTES, self.directory / name)

    def _commit(self, handle, name: str, events: list[dict], *, first: Callable[[], None] | None = None) -> int:
        """Stamp numbered events and append them in one write; returns the last revision.

        ``first`` runs once the map is known to have room for the events, so what it writes
        elsewhere is never left without the events that refer to it.
        """
        now = _iso(self.clock())
        for event in events:
            event.update(v=FORMAT_VERSION, at=now, commit=events[-1]["rev"])
        data = self._encode(events)
        if first is not None:
            safe_store.require_room(handle, len(data), MAX_STORE_BYTES, self.directory / name)
            first()
        safe_store.append_line(handle, data, MAX_STORE_BYTES, self.directory / name)
        return events[-1]["rev"]

    @staticmethod
    def _state(records: list, task_id: str) -> WorkMap | None:
        workmap = fold(records)
        if workmap is not None and workmap.task_id != task_id:
            raise WorkmapError("key_collision", "another task's map holds this file; choose another task_id")
        return workmap

    def _existing_map(self, records: list, task_id: str) -> WorkMap:
        workmap = self._state(records, task_id)
        if workmap is None:
            raise WorkmapError("unknown_map", f"no work map for task {task_id}; create it with workmap_update")
        return workmap

    # --- reading -------------------------------------------------------------------

    def load(self, task_id: object) -> WorkMap | None:
        task_id = _task_id(task_id)
        return self._state(self._read(f"{map_key(task_id)}.jsonl"), task_id)

    def list_maps(self, limit: int = LIST_LIMIT) -> tuple[list[WorkMap], int]:
        """The ``limit`` most recently written maps, folded, and how many map files there are.

        Each log may hold up to the store's size cap and closed maps stay, so the list reads only
        the newest files instead of folding every log of the workspace for a few lines of view."""
        with safe_store.store_directory(self.root, DIRECTORY, create=False, private=False) as directory:
            if directory is None:
                return [], 0
            names = [name for name in os.listdir(directory) if MAP_FILE.fullmatch(name)]

            def written(name: str) -> float:
                try:
                    if isinstance(directory, int):
                        return os.stat(name, dir_fd=directory, follow_symlinks=False).st_mtime
                    return os.lstat(Path(directory) / name).st_mtime
                except OSError:
                    return 0.0
            newest = sorted(names, key=lambda name: (written(name), name), reverse=True)[:limit]
        folded = [(name, fold(self._read(name))) for name in newest]
        # A file holds the map of the task id it is named after; a foreign log is not listed under it.
        return [workmap for name, workmap in folded
                if workmap is not None and f"{map_key(workmap.task_id)}.jsonl" == name], len(names)

    def _result(self, task_id: str, node: dict) -> str | None:
        sha256 = node["refs"].get("result_sha256")
        if sha256 is None:
            return None
        for record in reversed(self._read(f"{map_key(task_id)}.results.jsonl")):
            if (isinstance(record, dict) and record.get("sha256") == sha256
                    and record.get("node") == node["id"] and _text_sha256(record.get("text")) == sha256):
                return record["text"]  # only a text that still hashes to what the map recorded
        return None

    def result_text(self, task_id: object, node_id: object) -> str | None:
        task_id = _task_id(task_id)
        workmap = self._existing_map(self._read(f"{map_key(task_id)}.jsonl"), task_id)
        return self._result(task_id, _existing(workmap, node_id, "node"))

    # --- parent writes -------------------------------------------------------------

    def apply(self, task_id: object, ops: object, expected_revision: object = None) -> dict:
        """Apply parent operations together, or none of them; the first call creates the map."""
        task_id = _task_id(task_id)
        if not isinstance(ops, list) or not 1 <= len(ops) <= MAX_OPS:
            raise WorkmapError("invalid_request", f"ops is a list of 1 to {MAX_OPS} operations")
        if expected_revision is not None and (isinstance(expected_revision, bool)
                                              or not isinstance(expected_revision, int)):
            raise WorkmapError("invalid_request", "expected_revision is an integer")
        name = f"{map_key(task_id)}.jsonl"
        with self._writing(name) as (handle, records):
            current = self._state(records, task_id)
            revision = current.revision if current is not None else 0
            if expected_revision is not None and expected_revision != revision:
                raise WorkmapError("revision_conflict", f"the map is at revision {revision}, not {expected_revision}")
            draft = copy.deepcopy(current) if current is not None else WorkMap(task_id=task_id)
            events = []
            if current is None:
                revision += 1
                events.append({"op": "open", "by": "parent", "task_id": task_id, "rev": revision})
            for index, op in enumerate(ops):
                event = _parent_event(draft, op, f"ops[{index}]")
                revision += 1
                event["rev"] = revision
                _apply_event(draft, event)
                events.append(event)
            self._commit(handle, name, events)
        changed = sorted({value for event in events for key in ("id", "from", "to")
                          if isinstance(value := event.get(key), str)})
        return {"status": "applied", "task_id": task_id, "revision": revision, "changed": changed}

    # --- server writes -------------------------------------------------------------

    @staticmethod
    def _refuse_spent(workmap: WorkMap, records: list, node: dict, contract_revision: str | None) -> None:
        """A contract that has a result, or reached the invalid limit, is not prepared again.

        An identical request yields the same revision, so re-preparing it would reopen a finished
        attempt; the worker's next submission would then be taken or refused as the first one was.
        """
        if contract_revision is None:
            return
        spent = (workmap.invalid.get(contract_revision, 0) >= INVALID_LIMIT
                 or _result_of(records, contract_revision) is not None)
        if spent:
            raise WorkmapError("attempt_reused", f"this contract already has a result or reached the limit of "
                                                 f"invalid results on node {node['id']}; prepare a new attempt "
                                                 "with a new attempt_id")

    def check_preparable(self, task_id: object, node_id: object, inputs: list, *,
                         contract_revision: str | None = None,
                         result_type: str | None = None) -> list[tuple[str, str]]:
        """Refuse a node that cannot take a package; return the stored text of each input node.

        With the contract's revision, also refuse one that was already used up: the server knows
        the revision only once the package is assembled, so record_prepared checks it again. With the
        request's result_type, also refuse a node that verifies others unless it is a review.
        """
        task_id = _task_id(task_id)
        records = self._read(f"{map_key(task_id)}.jsonl")
        workmap = self._existing_map(records, task_id)
        if workmap.closed:
            raise WorkmapError("map_closed", "the map is closed")
        node = _existing(workmap, node_id, "workmap_node")
        if result_type is not None and result_type != "review" and workmap.targets(node["id"], "verifies"):
            raise WorkmapError("invalid_request", f"node {node['id']} verifies other nodes, so it is prepared "
                                                  f"only with result_type review, not {result_type}")
        if node["kind"] == "goal" or node["status"] not in PREPARABLE:
            raise WorkmapError("node_not_preparable",
                               f"node {node['id']} is a {node['kind']} in status {node['status']}; only a "
                               "planned, prepared, needs_attention or disputed non-goal node takes a package")
        self._refuse_spent(workmap, records, node, contract_revision)
        texts = []
        for index, input_id in enumerate(inputs):
            source = _existing(workmap, input_id, f"workmap_inputs[{index}]")
            text = self._result(task_id, source) if source["status"] in INPUT_STATUSES else None
            if text is None:
                raise WorkmapError("input_not_ready", f"node {source['id']} has no validated result yet")
            texts.append((source["id"], text))
        return texts

    def record_prepared(self, task_id: object, node_id: object, *, contract_revision: str,
                        attempt_id: str, complexity: str | None, inputs: dict[str, str] | None = None) -> int:
        """Mark the node prepared; ``inputs`` are the pinned {node: result sha256} the worker was given."""
        task_id = _task_id(task_id)
        problem = _package_problem(contract_revision, attempt_id, inputs or None)
        if problem is not None:  # the fold would skip such an event, so it is never written
            raise WorkmapError("invalid_request", problem)
        name = f"{map_key(task_id)}.jsonl"
        with self._writing(name) as (handle, records):
            workmap = self._existing_map(records, task_id)
            node = _existing(workmap, node_id, "workmap_node")
            if workmap.closed or node["kind"] == "goal" or node["status"] not in PREPARABLE:
                raise WorkmapError("node_not_preparable", f"node {node['id']} no longer takes a package")
            self._refuse_spent(workmap, records, node, contract_revision)
            current = node["refs"]
            if current.get("attempt_id") == attempt_id and current.get("contract_revision") not in (
                    None, contract_revision):
                raise WorkmapError("attempt_reused", f"node {node['id']} already holds another contract for "
                                                     f"attempt {attempt_id}; a changed package needs a new attempt_id")
            event = {"op": "prepared", "by": "server", "id": node["id"], "rev": workmap.revision + 1,
                     "contract_revision": contract_revision, "attempt_id": attempt_id,
                     "complexity": complexity}
            if inputs:
                event["inputs"] = dict(inputs)
            _would_apply(workmap, [event])
            return self._commit(handle, name, [event])

    def _keep_text(self, task_id: str, node_id: str, contract_revision: str, sha256: str, text: str) -> None:
        name = f"{map_key(task_id)}.results.jsonl"
        with self._locked(name) as handle:  # appending needs no parse of what the file holds
            self._append(handle, name, [{"v": FORMAT_VERSION, "task_id": task_id, "node": node_id,
                                         "contract_revision": contract_revision, "sha256": sha256,
                                         "text": text}])
            # On disk before the map's event names it: a crash between the two writes must not
            # leave a result event whose text was still only in the page cache.
            os.fsync(handle.fileno())

    def record_result(self, task_id: object, node_id: object, *, contract_revision: str, attempt_id: str,
                      text: str, sha256: str, outcome: dict, handback: str) -> dict:
        """Keep a valid result and record it on its node; the first result of a contract is final.

        Only a prepared node takes a result, or one sent to needs_attention by this contract's own
        invalid results: a worker may still submit a valid one after them.
        """
        task_id = _task_id(task_id)
        # The fold would skip such an event, so it is never written.
        if (outcome.get("status"), outcome.get("level")) not in _RESULT_OUTCOMES:
            raise WorkmapError("invalid_request", "a result is validated at V1 or V2, or needs_attention at V0")
        problem = _result_fields(outcome.get("summary"), outcome.get("question"), handback,
                                 outcome.get("result_status"), outcome.get("escalation"))
        findings = outcome.get("findings")
        if problem is None and not _is_revision(sha256):
            problem = "a result hash is 64 lowercase hex digits"
        if problem is None and _text_sha256(text) != sha256:
            # The text is served only under its own hash, so a node validated under another hash
            # would hold a result that neither workmap_view nor a dependant's inputs could read.
            problem = "a result hash is the SHA-256 of the result text's UTF-8 bytes"
        if problem is None and isinstance(findings, dict) and any(key not in SEVERITIES for key in findings):
            problem = f"findings count the severities {', '.join(SEVERITIES)}"
        if problem is not None:
            raise WorkmapError("invalid_request", problem)
        name = f"{map_key(task_id)}.jsonl"
        with self._writing(name) as (handle, records):
            workmap = self._existing_map(records, task_id)
            node = _existing(workmap, node_id, "workmap.node")
            earlier = _result_of(records, contract_revision)
            if earlier is not None:
                if node["refs"].get("contract_revision") != contract_revision:
                    # The node was prepared again: the old contract's result, even repeated, is stale.
                    raise WorkmapError("stale_contract", f"node {node['id']} was prepared again; this "
                                                         "contract is no longer its current attempt")
                if earlier.get("result_sha256") != sha256:
                    raise WorkmapError("already_submitted", "a different result is already recorded for this "
                                                            "contract; a new result needs a new attempt")
                return {"task_id": task_id, "node": node["id"], "status": node["status"],
                        "level": node["level"], "revision": workmap.revision, "duplicate": True}
            _require_open(workmap, node, contract_revision)
            if node["refs"].get("attempt_id") != attempt_id:
                raise WorkmapError("stale_contract", f"node {node['id']} holds attempt "
                                                     f"{node['refs'].get('attempt_id')}, not {attempt_id}")
            event = {"op": "result", "by": "server", "id": node["id"], "rev": workmap.revision + 1,
                     "contract_revision": contract_revision, "attempt_id": attempt_id,
                     "result_sha256": sha256, "handback": handback,
                     **{key: outcome.get(key) for key in ("status", "level", "summary", "checks", "findings",
                                                          "limitations", "result_status", "question",
                                                          "escalation")}}
            events = [event]
            # Only a review gives a verdict: its outcome carries findings, {} when it found none. Any
            # other result's findings are None, even when a verifies edge was added after its node
            # was prepared.
            if event["status"] == "validated" and event["findings"] is not None:
                severe = sum(event["findings"].get(level, 0) for level in ("critical", "high"))
                # A verdict certifies the text the verifier was given: only a target that still holds
                # the result pinned in the verifier's own prepared event takes one, and accepted
                # (the user's V4 decision), needs_attention and planned targets never do.
                pinned = node["refs"].get("inputs") or {}
                for target in workmap.targets(node["id"], "verifies"):
                    held = workmap.nodes[target]
                    if (held["status"] not in VERDICT_TARGETS or pinned.get(target) is None
                            or held["refs"].get("result_sha256") != pinned[target]):
                        continue
                    events.append({"op": "verdict", "by": "server", "id": target, "verifier": node["id"],
                                   "rev": events[-1]["rev"] + 1, "result_sha256": pinned[target],
                                   "status": "disputed" if severe else "verified",
                                   "level": None if severe else "V3"})
            _would_apply(workmap, events)  # before the text is kept: a refused write leaves no orphan
            # The text is kept first, so the event never names a missing text, and only once the
            # map has room for the events, so a full map leaves no orphan text either.
            revision = self._commit(handle, name, events, first=lambda: self._keep_text(
                task_id, node["id"], contract_revision, sha256, text))
        return {"task_id": task_id, "node": node["id"], "status": event["status"], "level": event["level"],
                "revision": revision, "duplicate": False}

    def record_invalid(self, task_id: object, node_id: object, *, contract_revision: str,
                       codes: list[str], escalation: dict | None = None) -> int:
        """Count an invalid submission; the limit sends the node's current attempt to needs_attention.

        Only a submission the node could still take is counted: once the contract has a result, or
        the node was prepared again, settled or set aside, an invalid one (such as the worker's own
        one-line hand-back, validated by mistake) is refused and the map is left alone.
        """
        task_id = _task_id(task_id)
        if not _escalation_ok(escalation):  # the fold would skip such an event, so it is never written
            raise WorkmapError("invalid_request", f"an escalation has only {', '.join(ESCALATION_KEYS)}, each one line")
        name = f"{map_key(task_id)}.jsonl"
        with self._writing(name) as (handle, records):
            workmap = self._existing_map(records, task_id)
            node = _existing(workmap, node_id, "workmap.node")
            if _result_of(records, contract_revision) is not None:
                raise WorkmapError("already_submitted", "this contract already has a result; an invalid "
                                                        "submission is not counted")
            _require_open(workmap, node, contract_revision)
            count = workmap.invalid.get(contract_revision, 0) + 1
            # Each code as one line of at most CODE_MAX characters, which is what the fold accepts.
            kept = [" ".join(code.split())[:CODE_MAX].rstrip() for code in codes if isinstance(code, str)]
            kept = [code for code in kept if _is_line(code, CODE_MAX)][:CODES_KEPT]
            # At the limit a prepared node goes to needs_attention; a later invalid submission only
            # counts, with no status, and the fold leaves the node's status as it is.
            event = {"op": "invalid", "by": "server", "id": node["id"], "rev": workmap.revision + 1,
                     "contract_revision": contract_revision, "codes": kept, "escalation": escalation,
                     "status": "needs_attention" if node["status"] == "prepared" and count >= INVALID_LIMIT
                     else None}
            _would_apply(workmap, [event])
            self._commit(handle, name, [event])
        return count
