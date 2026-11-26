"""Text views of a work map: what the parent reads instead of the event log or the results."""
from __future__ import annotations

from src.workmap.store import SETTLED, WorkMap, WorkmapError, is_ready, is_stale, required_level, settled

DEFAULT_BUDGET = 3_000
MIN_BUDGET = 500
MAX_BUDGET = 8_000
NODE_BUDGET = 2_000
QUESTION_MAX = 300
LIST_MAX = 10
ATTENTION = ("needs_attention", "disputed")
MORE = "more nodes: read them with since or node"
MORE_MAPS = "more maps: name a task_id to read one"


def _ids(values: list[str]) -> str:
    shown = ", ".join(values[:LIST_MAX])
    more = f" and {len(values) - LIST_MAX} more" if len(values) > LIST_MAX else ""
    return shown + more if shown else "none"


def _listing(label: str, ids: list[str], room: int) -> str:
    """``label: a, b and N more``, naming as many of the first ten ids as fit in ``room``."""
    if not ids:
        return f"{label}: none"
    for count in range(min(len(ids), LIST_MAX), 0, -1):
        more = len(ids) - count
        line = f"{label}: {', '.join(ids[:count])}" + (f" and {more} more" if more else "")
        if len(line) <= room:
            return line
    return f"{label}: {len(ids)} more"


def _footer(ready: list[str], attention: list[str], room: int) -> list[str]:
    """The ready and needs-attention lines within ``room`` characters, newline included:
    each list gets half, and what one leaves the other may use."""
    room -= 1
    first = _listing("ready", ready, room // 2)
    second = _listing("needs attention", attention, room - len(first))
    return [_listing("ready", ready, room - len(second)), second]


def needs_attention(node: dict) -> bool:
    """A node the parent must act on: needs_attention, disputed, or validated, verified or
    accepted below the level its dependants need (an accepted critical node below V3 without
    V4), which neither readies them nor changes on its own."""
    return node["status"] in ATTENTION or (node["status"] in SETTLED and not settled(node))


def ordered(workmap: WorkMap) -> list[dict]:
    """Nodes with every depends_on target before its dependant; ties keep creation order."""
    placed: set[str] = set()
    order: list[dict] = []
    remaining = list(workmap.nodes)
    while remaining:
        ready = [node_id for node_id in remaining
                 if all(target in placed for target in workmap.targets(node_id, "depends_on"))]
        if not ready:  # a cycle cannot be written, but a damaged log must still render
            ready = remaining[:]
        for node_id in ready:
            order.append(workmap.nodes[node_id])
            placed.add(node_id)
            remaining.remove(node_id)
    return order


def _line(workmap: WorkMap, node: dict, *, summary: bool) -> str:
    marks = f"{node['status']} {node['level']}" + (" stale" if is_stale(workmap, node) else "")
    line = f"[{marks}] {node['id']}: {node['title']}"
    after = workmap.targets(node["id"], "depends_on")
    if after:
        line += f" (after {', '.join(after)})"
    checked = workmap.targets(node["id"], "verifies")
    if checked:
        line += f" (verifies {', '.join(checked)})"
    if summary and node["summary"]:
        line += f" — {node['summary']}"
    return line


def render_map(workmap: WorkMap, *, budget: int = DEFAULT_BUDGET, since: int | None = None) -> str:
    """One line per node in dependency order, then what is ready and what needs the parent.

    Summaries go first when the budget runs short, then the last nodes. The ready and
    attention lists name at most ten nodes each, fewer when the budget leaves no room for
    them beside the header, and count the rest ("and N more"), so the whole text stays
    within the budget.
    """
    budget = max(MIN_BUDGET, min(budget, MAX_BUDGET))
    everything = ordered(workmap)
    nodes = everything if since is None else [node for node in everything if node["changed"] > since]
    header = [f"map {workmap.task_id} · revision {workmap.revision}" + (" · closed" if workmap.closed else "")]
    if since is not None:
        header.append(f"changed after revision {since}: {len(nodes)} of {len(everything)} nodes")
    # Room for the two lists beside the header and the longest note on nodes left out.
    room = budget - len("\n".join(header + [f"… {len(nodes)} {MORE}"])) - 1
    footer = _footer([node["id"] for node in everything if is_ready(workmap, node)],
                     [node["id"] for node in everything if needs_attention(node)], room)
    lines: list[str] = []
    for summary in (True, False):
        lines = [_line(workmap, node, summary=summary) for node in nodes]
        text = "\n".join(header + lines + footer)
        if len(text) <= budget:
            return text
    kept: list[str] = []
    for line in lines:
        rest = len(lines) - len(kept) - 1
        note = [f"… {rest} {MORE}"] if rest else []
        if len("\n".join(header + kept + [line] + note + footer)) > budget:
            break
        kept.append(line)
    return "\n".join(header + kept + [f"… {len(lines) - len(kept)} {MORE}"] + footer)


def _cut(text: object, limit: int) -> str:
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


def _escalation(value: object) -> str:
    if isinstance(value, dict):
        return ", ".join(f"{key} {item}" for key, item in value.items())
    return str(value)


def render_node(workmap: WorkMap, node_id: str) -> str:
    node = workmap.nodes.get(node_id)
    if node is None:
        raise WorkmapError("unknown_node", f"the map has no node {node_id}")
    lines = [f"node {node['id']} ({node['kind']}, risk {node['risk']}, needs {required_level(node)}) "
             f"[{node['status']} {node['level']}]",
             f"title: {node['title']}"]
    if node["summary"]:
        lines.append(f"summary: {node['summary']}")
    if node["reason"]:
        lines.append(f"reason: {node['reason']}")
    relations = [("after", workmap.targets(node_id, "depends_on")),
                 ("needed by", workmap.sources(node_id, "depends_on")),
                 ("verifies", workmap.targets(node_id, "verifies")),
                 ("verified by", workmap.sources(node_id, "verifies")),
                 ("supersedes", workmap.targets(node_id, "supersedes")),
                 ("contradicts", workmap.targets(node_id, "contradicts"))]
    shown = [f"{label} {_ids(ids)}" for label, ids in relations if ids]
    if shown:
        lines.append("; ".join(shown))
    if node["refs"]:
        lines.append(" · ".join(f"{key} {', '.join(value) if isinstance(value, dict) else value}"
                                for key, value in node["refs"].items()))
    if node["checks"] is not None:
        checks = node["checks"]
        lines.append(f"checks: passed {checks['passed']}, failed {checks['failed']}, not run "
                     f"{checks['not_run']} · limitations {node['limitations']}")
    if node["findings"]:
        lines.append("findings: " + ", ".join(f"{severity} {count}"
                                              for severity, count in sorted(node["findings"].items())))
    if node["handback"]:
        lines.append(f"hand-back: {node['handback']}")
    if node.get("result_status"):
        lines.append(f"result status: {node['result_status']}")
    if node.get("question"):
        lines.append(f"question: {_cut(node['question'], QUESTION_MAX)}")
    if node.get("escalation"):
        lines.append(f"escalation: {_cut(_escalation(node['escalation']), QUESTION_MAX)}")
    contract = node["refs"].get("contract_revision", "")
    invalid = workmap.invalid.get(contract, 0)
    if invalid:  # in any status: it says why a node went to needs_attention
        codes = list(dict.fromkeys(workmap.invalid_codes.get(contract, [])))
        lines.append(f"invalid submissions: {invalid}" + (f" (codes {', '.join(codes)})" if codes else ""))
    text = "\n".join(lines)
    return text if len(text) <= NODE_BUDGET else text[:NODE_BUDGET - 1] + "…"


def render_list(maps: list[WorkMap], *, budget: int = DEFAULT_BUDGET) -> str:
    """One line per map, the most recently changed first, as many as fit the budget, then how
    many more there are."""
    if not maps:
        return "no work maps in this workspace"
    lines = []
    for workmap in sorted(maps, key=lambda workmap: (workmap.updated, workmap.task_id), reverse=True):
        goals = [node["title"] for node in workmap.nodes.values() if node["kind"] == "goal"]
        counts: dict[str, int] = {}
        for node in workmap.nodes.values():
            counts[node["status"]] = counts.get(node["status"], 0) + 1
        tally = ", ".join(f"{status} {count}" for status, count in sorted(counts.items())) or "empty"
        lines.append(f"{workmap.task_id} · revision {workmap.revision}" + (" · closed" if workmap.closed else "")
                     + f" · {goals[0] if goals else 'no goal'} · {tally}")
    if len("\n".join(lines)) <= budget:
        return "\n".join(lines)
    kept: list[str] = []
    for line in lines:
        note = f"… {len(lines) - len(kept) - 1} {MORE_MAPS}"
        if len("\n".join(kept + [line, note])) > budget:
            break
        kept.append(line)
    return "\n".join(kept + [f"… {len(lines) - len(kept)} {MORE_MAPS}"])
