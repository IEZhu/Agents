"""JSON views of work maps for the daemon's web UI: what the Work map tab draws and lists.

The text views (``view.py``) fit a model's context; these carry every node and edge as data, so
the page lays the graph out itself. They only read: nothing here writes to a map. Beside the stored
fields, a node carries the states the store and the text view derive (``settled``, ``ready``,
``stale``, ``attention``), so the page never repeats their rules, and the time of its current status,
so a running stage can show how long it has run without one request per node. The map carries the
verdicts the store recorded, so the page shows them instead of guessing them from findings.

The page polls the open map every few seconds and the list every half minute, so every read goes
through ``FoldCache``: a map file is folded again only when its size or change time moved, and a log
larger than ``WEB_MAX_BYTES`` is not folded for the page at all.
"""
from __future__ import annotations

from bisect import bisect_right
from collections import OrderedDict
from datetime import datetime, timezone
import re
import threading
import time

from src.safe_store import StoreError
from src.workmap.store import (NODE_ID, EdgeIndex, History, WorkMap, WorkmapError, WorkmapStore, _task_id,
                               is_ready, is_stale, map_key, required_level, settled)
from src.workmap.view import needs_attention, ordered

# What a node shows of its stored result's artifacts: the items of its artifacts section, then every
# other reference of the form the result format gives one, artifact://<manifest>/<sha256>/<artifact>.
_ID = r"[A-Za-z0-9_-]+(?:\.[A-Za-z0-9_-]+)*"
ARTIFACT_REF = re.compile(rf"artifact://{_ID}/[0-9a-f]{{64}}/{_ID}")
ARTIFACTS_MAX = 50
ARTIFACT_LINE_MAX = 500
# A result workmap_submit takes is at most 128,000 characters; only a planted one is longer, and the
# artifact scan reads no further than this.
ARTIFACT_SCAN_CHARS = 200_000
# The separators of the result format, as the validator reads them: spaced, and outside code spans.
SEPARATORS = (re.compile(r"\s+—\s+"), re.compile(r"\s+--\s+"))
_BACKTICKS = re.compile(r"`+")
_CITATION = re.compile(r"\[\^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}\]")
# A map log the page folds: the writers' maps stay far below it (a day of work on a release map is
# tens of kilobytes), and a planted log above it costs one stat instead of seconds per poll.
WEB_MAX_BYTES = 2 * 1024 * 1024
# A time later than now by more than this is no time: a log stamped in the future would otherwise
# stay the map that changed last for good.
FUTURE_SKEW = 300


def _time(value: str | None, now: float) -> str | None:
    """``value`` when it is a stamp no later than now (with ``FUTURE_SKEW``), else None."""
    if not value:
        return None
    try:
        at = datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc).timestamp()
    except ValueError:
        return None
    return value if at <= now + FUTURE_SKEW else None


def _counts(workmap: WorkMap) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in workmap.nodes.values():
        counts[node["status"]] = counts.get(node["status"], 0) + 1
    return dict(sorted(counts.items()))


def summary(workmap: WorkMap, now: float | None = None) -> dict:
    """One map's line in the list. ``goal`` is the first goal created, as the text views name it."""
    goals = [node["title"] for node in workmap.nodes.values() if node["kind"] == "goal"]
    return {"task_id": workmap.task_id, "revision": workmap.revision, "closed": workmap.closed,
            "updated": _time(workmap.updated, time.time() if now is None else now),
            "goal": goals[0] if goals else None, "nodes": len(workmap.nodes), "edges": len(workmap.edges),
            "counts": _counts(workmap),
            "running": [node["id"] for node in workmap.nodes.values() if node["status"] == "prepared"]}


def _node(workmap: WorkMap, node: dict, edges: EdgeIndex, steps: list[dict]) -> dict:
    """One node as the page reads it. ``steps`` is its status and level history (``fold_file``):
    the last step dates the current status, and each ``prepared`` step is one attempt."""
    contract = node["refs"].get("contract_revision")
    return {"id": node["id"], "kind": node["kind"], "title": node["title"], "summary": node["summary"],
            "planned_summary": node["planned_summary"],
            "risk": node["risk"], "status": node["status"], "level": node["level"], "reason": node["reason"],
            "needs": required_level(node), "complexity": node["complexity"], "changed": node["changed"],
            "since": steps[-1]["at"] if steps else None,
            "attempts": sum(step["op"] == "prepared" for step in steps),
            "settled": settled(node), "ready": is_ready(workmap, node, edges), "stale": is_stale(workmap, node, edges),
            "attention": needs_attention(node), "result_status": node.get("result_status"),
            "checks": node["checks"], "findings": node["findings"], "limitations": node["limitations"],
            "handback": node["handback"], "question": node.get("question"), "escalation": node.get("escalation"),
            "contract_revision": contract, "attempt_id": node["refs"].get("attempt_id"),
            "result_sha256": node["refs"].get("result_sha256"), "inputs": node["refs"].get("inputs") or {},
            "invalid": workmap.invalid.get(contract, 0) if contract else 0,
            "invalid_codes": list(dict.fromkeys(workmap.invalid_codes.get(contract, []))) if contract else []}


def graph(history: History, now: float | None = None) -> dict:
    """The whole map: nodes in dependency order (``view.ordered``, the page's tie order) and edges;
    ``last_change``, the nodes the last commit touched; ``verdicts``, each verdict the store
    recorded for a verifier's current attempt and the result it judged."""
    workmap = history.workmap
    edges = EdgeIndex(workmap)
    return {**summary(workmap, now),
            "nodes": [_node(workmap, node, edges, history.steps.get(node["id"], [])) for node in ordered(workmap, edges)],
            "edges": [{"from": source, "to": target, "type": kind} for source, target, kind in workmap.edges],
            "last_change": list(history.touched),
            "verdicts": [{"verifier": verifier, "target": target, "verdict": verdict["verdict"],
                          "result_sha256": verdict["result_sha256"]}
                         for (verifier, target), verdict in history.verdicts.items()]}


def _cut(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= ARTIFACT_LINE_MAX else text[:ARTIFACT_LINE_MAX - 1] + "…"


def _heading(line: str) -> str | None:
    """The title of a Markdown heading line (one to six '#', then a space), else None. String
    methods only: a pattern with a lazy group backtracked quadratically on a long run of spaces."""
    text = line.lstrip()
    marks = len(text) - len(text.lstrip("#"))
    if not 1 <= marks <= 6 or not text[marks:marks + 1].isspace():
        return None
    return text[marks:].strip()


def _outside_code(text: str) -> str:
    """``text`` with the characters of every code span masked, lengths kept: a span is a run of
    backticks closed by a run of the same length, as the validator reads a result line."""
    runs = [match.span() for match in _BACKTICKS.finditer(text)]
    if not runs:
        return text
    by_length: dict[int, list[int]] = {}
    for index, (start, end) in enumerate(runs):
        by_length.setdefault(end - start, []).append(index)
    masked, index = list(text), 0
    while index < len(runs):
        start, end = runs[index]
        same = by_length[end - start]
        candidate = bisect_right(same, index)
        if candidate == len(same):
            index += 1
            continue
        closer = same[candidate]
        masked[start:runs[closer][1]] = "\x00" * (runs[closer][1] - start)
        index = closer + 1
    return "".join(masked)


def _parts(item: str) -> tuple[str, str]:
    """An artifacts item split at its separator outside code spans: (reference, purpose)."""
    outside = _outside_code(item)
    for separator in SEPARATORS:
        found = separator.search(outside)
        if found:
            return item[:found.start()], item[found.end():]
    return item, ""


def _uncited(ref: str) -> str:
    """A reference without the footnote citations written right after it, as in ``x[^e1]``."""
    while ref.endswith("]"):
        start = ref.rfind("[^")
        if start < 0 or not _CITATION.fullmatch(ref, start):
            break
        ref = ref[:start].rstrip()
    return ref


def artifacts(text: str | None) -> list[dict]:
    """The artifacts a stored result names: each item of its ``## artifacts`` section as
    {ref, purpose}, then every other artifact:// reference in the text. Plain text for the page
    to show, never markup.

    An item is a line that starts with '- '; an indented line continues the item above it, as in
    the result format, and is never an item of its own. The scan reads the first
    ``ARTIFACT_SCAN_CHARS`` characters."""
    if not text:
        return []
    text = text[:ARTIFACT_SCAN_CHARS]
    items: list[str] = []
    section = False
    for line in text.splitlines():
        heading = _heading(line)
        if heading is not None:
            section = heading.lower() == "artifacts"
            continue
        if not section or not line.strip():
            continue
        if line.startswith("- "):
            items.append(line[2:])
        elif line[:1] in (" ", "\t") and items:
            items[-1] += " " + line.strip()
    found: list[dict] = []
    seen: set[str] = set()
    for item in items:
        ref, purpose = _parts(item)
        ref = _uncited(ref.strip().strip("`").strip())
        if ref and ref not in seen:
            seen.add(ref)
            found.append({"ref": _cut(ref), "purpose": _cut(purpose)})
    for match in ARTIFACT_REF.finditer(text):
        if match.group(0) not in seen:
            seen.add(match.group(0))
            found.append({"ref": match.group(0), "purpose": ""})
    return found[:ARTIFACTS_MAX]


class FoldCache:
    """Each map file's ``History``, kept while the file's stamp (device, inode, size and change time)
    stays the same, so a poll of an unchanged map costs one stat instead of a fold.

    Entries are bounded; the values are shared between the threads that answer requests and only
    read. A file over ``WEB_MAX_BYTES`` is refused with map_too_large before it is read.
    """

    def __init__(self, limit: int = 64):
        self.limit = limit
        self._entries: OrderedDict[tuple[str, str], tuple[tuple, History]] = OrderedDict()
        self._lock = threading.Lock()

    def fold(self, store: WorkmapStore, name: str) -> History:
        stamp = store.stamp(name)
        if stamp is None:
            return History()
        if stamp[2] > WEB_MAX_BYTES:
            raise WorkmapError("map_too_large", f"the map's log is {stamp[2]} bytes; the page draws one of at most "
                                                f"{WEB_MAX_BYTES} bytes (workmap_view still reads it)")
        key = (str(store.root), name)
        with self._lock:
            entry = self._entries.get(key)
            if entry is not None and entry[0] == stamp:
                self._entries.move_to_end(key)
                return entry[1]
        history = store.fold_file(name)  # a write meanwhile only makes the next poll fold again
        with self._lock:
            self._entries[key] = (stamp, history)
            self._entries.move_to_end(key)
            while len(self._entries) > self.limit:
                self._entries.popitem(last=False)
        return history

    def history(self, store: WorkmapStore, task_id: object) -> History:
        task_id = _task_id(task_id)
        history = self.fold(store, f"{map_key(task_id)}.jsonl")
        if history.workmap is not None and history.workmap.task_id != task_id:
            raise WorkmapError("key_collision", "another task's map holds this file; choose another task_id")
        return history


def list_maps(store: WorkmapStore, cache: FoldCache, now: float | None = None) -> dict:
    """A workspace's most recently written maps as list lines, newest first; a map file the store
    refuses or that is too large is counted, not listed, and the others still are."""
    names, total = store.newest()
    maps, unreadable, too_large = [], 0, 0
    for name in names:
        try:
            history = cache.fold(store, name)
        except StoreError as error:
            too_large += error.code == "map_too_large"
            unreadable += error.code != "map_too_large"
            continue
        workmap = history.workmap
        # A file holds the map of the task id it is named after; a foreign log is not listed under it.
        if workmap is not None and f"{map_key(workmap.task_id)}.jsonl" == name:
            maps.append(summary(workmap, now))
    maps.sort(key=lambda item: (item["updated"] or "", item["task_id"]), reverse=True)
    return {"total": total, "maps": maps, "skipped": unreadable, "too_large": too_large}


def _existing(history: History, task_id: str) -> WorkMap:
    if history.workmap is None:
        raise WorkmapError("unknown_map", f"no work map for task {task_id}")
    return history.workmap


def load_graph(store: WorkmapStore, task_id: str, cache: FoldCache | None = None) -> dict:
    history = (cache or FoldCache()).history(store, task_id)
    _existing(history, task_id)
    return graph(history)


def _node_id(node_id: object) -> str:
    if not isinstance(node_id, str) or not NODE_ID.fullmatch(node_id):
        raise WorkmapError("invalid_request", "a node id is 1 to 40 characters of a-z, 0-9 and '-'")
    return node_id


def node_record(store: WorkmapStore, task_id: str, node_id: str, cache: FoldCache | None = None) -> dict:
    """One node's record: what the graph carries, plus its status and level history, its
    relations and the artifacts its stored result names. The result text itself is not included."""
    node_id = _node_id(node_id)
    history = (cache or FoldCache()).history(store, task_id)
    workmap = _existing(history, task_id)
    node = workmap.nodes.get(node_id)
    if node is None:
        raise WorkmapError("unknown_node", f"the map has no node {node_id}")
    edges = EdgeIndex(workmap)
    text = store.stored_result(task_id, node) if node["refs"].get("result_sha256") else None
    relations = {"depends_on": edges.targets(node_id, "depends_on"),
                 "needed_by": edges.sources(node_id, "depends_on"),
                 "verifies": edges.targets(node_id, "verifies"),
                 "verified_by": edges.sources(node_id, "verifies"),
                 "supersedes": edges.targets(node_id, "supersedes"),
                 "superseded_by": edges.sources(node_id, "supersedes"),
                 "contradicts": edges.targets(node_id, "contradicts"),
                 "contradicted_by": edges.sources(node_id, "contradicts")}
    return {"task_id": workmap.task_id, "revision": workmap.revision,
            "node": _node(workmap, node, edges, history.steps.get(node_id, [])),
            "history": history.steps.get(node_id, []), "relations": relations, "artifacts": artifacts(text),
            "has_result": text is not None, "result_chars": len(text) if text is not None else 0}


def result_text(store: WorkmapStore, task_id: str, node_id: str, cache: FoldCache | None = None) -> str | None:
    """The stored text of the result a node holds, or None."""
    node_id = _node_id(node_id)
    workmap = _existing((cache or FoldCache()).history(store, task_id), task_id)
    node = workmap.nodes.get(node_id)
    if node is None:
        raise WorkmapError("unknown_node", f"the map has no node {node_id}")
    return store.stored_result(task_id, node)
