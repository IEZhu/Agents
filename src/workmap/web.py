"""JSON views of work maps for the daemon's web UI: what the Work map tab draws and lists.

The text views (``view.py``) fit a model's context; these carry every node and edge as data, so
the page lays the graph out itself. They only read: nothing here writes to a map.
"""
from __future__ import annotations

import re
from urllib.parse import quote

from src.workmap.store import (WorkMap, WorkmapError, WorkmapStore, EdgeIndex, is_ready, is_stale, map_key,
                               required_level)
from src.workmap.view import needs_attention, ordered

# What a node shows of its stored result's artifacts: references and the items of its artifacts section.
ARTIFACT_REF = re.compile(r"artifact://[A-Za-z0-9._~:/?#\[\]@!$&'()*+,;=%-]+")
ARTIFACTS_MAX = 50
ARTIFACT_LINE_MAX = 500
SEPARATORS = (" — ", " -- ")


def map_fragment(workspace_id: str | None, task_id: str) -> str:
    """The /ui fragment that opens one map: #workmap/<workspace id>/<task id>, or without the
    workspace when it is unknown, which the page resolves among the registered workspaces."""
    task = quote(task_id, safe="")
    return f"#workmap/{quote(workspace_id, safe='')}/{task}" if workspace_id else f"#workmap/{task}"


def map_url(ui_link: str | None, workspace_id: str | None, task_id: str) -> str | None:
    """The link to one map on the daemon's page, or None where the server has no page to link."""
    return f"{ui_link}{map_fragment(workspace_id, task_id)}" if ui_link else None


def _counts(workmap: WorkMap) -> dict[str, int]:
    counts: dict[str, int] = {}
    for node in workmap.nodes.values():
        counts[node["status"]] = counts.get(node["status"], 0) + 1
    return dict(sorted(counts.items()))


def _current(workmap: WorkMap) -> str | None:
    """The stage running now, a prepared node (the most recently prepared one), or else the node
    that changed last; None for an empty map."""
    nodes = list(workmap.nodes.values())
    running = [node for node in nodes if node["status"] == "prepared"]
    pool = running or nodes
    if not pool:
        return None
    return max(pool, key=lambda node: node["changed"])["id"]


def summary(workmap: WorkMap) -> dict:
    """One map's line in the list."""
    goals = [node["title"] for node in workmap.nodes.values() if node["kind"] == "goal"]
    return {"task_id": workmap.task_id, "key": map_key(workmap.task_id), "revision": workmap.revision,
            "closed": workmap.closed, "updated": workmap.updated or None, "goal": goals[0] if goals else None,
            "nodes": len(workmap.nodes), "edges": len(workmap.edges), "counts": _counts(workmap),
            "running": [node["id"] for node in workmap.nodes.values() if node["status"] == "prepared"]}


def _node(workmap: WorkMap, node: dict, edges: EdgeIndex) -> dict:
    contract = node["refs"].get("contract_revision")
    return {"id": node["id"], "kind": node["kind"], "title": node["title"], "summary": node["summary"],
            "risk": node["risk"], "status": node["status"], "level": node["level"], "reason": node["reason"],
            "needs": required_level(node), "complexity": node["complexity"], "changed": node["changed"],
            "ready": is_ready(workmap, node, edges), "stale": is_stale(workmap, node, edges),
            "attention": needs_attention(node), "result_status": node.get("result_status"),
            "checks": node["checks"], "findings": node["findings"], "limitations": node["limitations"],
            "handback": node["handback"], "question": node.get("question"), "escalation": node.get("escalation"),
            "contract_revision": contract, "attempt_id": node["refs"].get("attempt_id"),
            "result_sha256": node["refs"].get("result_sha256"), "inputs": node["refs"].get("inputs") or {},
            "invalid": workmap.invalid.get(contract, 0) if contract else 0,
            "invalid_codes": list(dict.fromkeys(workmap.invalid_codes.get(contract, []))) if contract else []}


def graph(workmap: WorkMap) -> dict:
    """The whole map: nodes in dependency order, edges, and the node to highlight."""
    edges = EdgeIndex(workmap)
    return {**summary(workmap), "current": _current(workmap),
            "nodes": [_node(workmap, node, edges) for node in ordered(workmap, edges)],
            "edges": [{"from": source, "to": target, "type": kind} for source, target, kind in workmap.edges]}


def _cut(text: str) -> str:
    text = " ".join(text.split())
    return text if len(text) <= ARTIFACT_LINE_MAX else text[:ARTIFACT_LINE_MAX - 1] + "…"


def artifacts(text: str | None) -> list[dict]:
    """The artifacts a stored result names: each item of its ``## artifacts`` section as
    {ref, purpose}, then every other artifact:// reference in the text. Plain text for the page
    to show, never markup."""
    if not text:
        return []
    found: list[dict] = []
    seen: set[str] = set()
    section = False
    for line in text.splitlines():
        heading = re.match(r"\s*#{1,6}\s+(.*?)\s*$", line)
        if heading:
            section = heading.group(1).strip().lower() == "artifacts"
            continue
        if not section or not line.lstrip().startswith("- "):
            continue
        item = line.lstrip()[2:].strip()
        ref, purpose = item, ""
        for separator in SEPARATORS:
            if separator in item:
                ref, purpose = item.split(separator, 1)
                break
        ref = ref.strip().strip("`")
        if ref and ref not in seen:
            seen.add(ref)
            found.append({"ref": _cut(ref), "purpose": _cut(purpose)})
    for match in ARTIFACT_REF.finditer(text):
        ref = match.group(0).rstrip(".,;:)]")
        if ref not in seen:
            seen.add(ref)
            found.append({"ref": _cut(ref), "purpose": ""})
    return found[:ARTIFACTS_MAX]


def load_graph(store: WorkmapStore, task_id: str) -> dict:
    workmap = store.load(task_id)
    if workmap is None:
        raise WorkmapError("unknown_map", f"no work map for task {task_id}")
    return graph(workmap)


def node_record(store: WorkmapStore, task_id: str, node_id: str) -> dict:
    """One node's record: what the graph carries, plus its status and level history, its
    relations and the artifacts its stored result names. The result text itself is not included."""
    workmap, steps = store.changes(task_id)
    if workmap is None:
        raise WorkmapError("unknown_map", f"no work map for task {task_id}")
    node = workmap.nodes.get(node_id) if isinstance(node_id, str) else None
    if node is None:
        raise WorkmapError("unknown_node", f"the map has no node {node_id}")
    edges = EdgeIndex(workmap)
    text = store.result_text(task_id, node_id) if node["refs"].get("result_sha256") else None
    relations = {"depends_on": edges.targets(node_id, "depends_on"),
                 "needed_by": edges.sources(node_id, "depends_on"),
                 "verifies": edges.targets(node_id, "verifies"),
                 "verified_by": edges.sources(node_id, "verifies"),
                 "supersedes": edges.targets(node_id, "supersedes"),
                 "superseded_by": edges.sources(node_id, "supersedes"),
                 "contradicts": edges.targets(node_id, "contradicts"),
                 "contradicted_by": edges.sources(node_id, "contradicts")}
    return {"task_id": workmap.task_id, "revision": workmap.revision, "node": _node(workmap, node, edges),
            "history": steps.get(node_id, []), "relations": relations, "artifacts": artifacts(text),
            "has_result": text is not None, "result_chars": len(text) if text is not None else 0}
