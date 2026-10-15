"""Pure conflict and scheduling policy; callers supply authorized current facts."""
from __future__ import annotations

import heapq
import math
from collections.abc import Iterable, Mapping
from typing import Any

from pydantic import BaseModel

PRIORITY_RANKS = {"background": 0, "normal": 1, "urgent": 2}
AGING_INTERVAL_SECONDS = 600
_PHYSICAL_TYPES = {"physical_file", "physical_subtree"}


def _mapping(value: Mapping[str, Any] | BaseModel) -> Mapping[str, Any]:
    return value.model_dump() if isinstance(value, BaseModel) else value


def _key_parts(key: str) -> tuple[str, str]:
    domain, separator, path = key.partition(":")
    if not separator:
        return "", domain.rstrip("/")
    return domain, path.rstrip("/")


def resources_overlap(left: Mapping[str, Any] | BaseModel,
                      right: Mapping[str, Any] | BaseModel) -> bool:
    """Compare canonical keys without case folding or filesystem access."""
    left, right = _mapping(left), _mapping(right)
    lt, rt = left["resource_type"], right["resource_type"]
    if lt in _PHYSICAL_TYPES and rt in _PHYSICAL_TYPES:
        ld, lp = _key_parts(left["resource_key"])
        rd, rp = _key_parts(right["resource_key"])
        if ld != rd:
            return False
        if lp == rp:
            return True
        return ((lt == "physical_subtree" and (lp in ("", ".") or rp.startswith(lp + "/")))
                or (rt == "physical_subtree" and (rp in ("", ".") or lp.startswith(rp + "/"))))
    return lt == rt and left["resource_key"] == right["resource_key"]


def logical_overlap(left: Mapping[str, Any] | BaseModel,
                    right: Mapping[str, Any] | BaseModel) -> bool:
    """Semantic overlap is informational and cannot prohibit private edits."""
    left, right = _mapping(left), _mapping(right)
    return left["resource_type"] == "logical_intent" and resources_overlap(left, right)


def resources_conflict(left: Mapping[str, Any] | BaseModel,
                       right: Mapping[str, Any] | BaseModel) -> bool:
    left, right = _mapping(left), _mapping(right)
    if "snapshot_read" in (left["access"], right["access"]):
        return False
    if "logical_intent" in (left["resource_type"], right["resource_type"]):
        return False
    return resources_overlap(left, right)


def _graph(edges: Iterable[tuple[str, str]]) -> dict[str, set[str]]:
    graph: dict[str, set[str]] = {}
    for producer, consumer in edges:
        graph.setdefault(producer, set()).add(consumer)
        graph.setdefault(consumer, set())
    return graph


def find_cycle(edges: Iterable[tuple[str, str]]) -> list[str] | None:
    """Return one deterministic closed cycle path; edges run producer -> consumer."""
    graph = _graph(edges)
    colors: dict[str, int] = {}
    for root in sorted(graph, key=str.encode):
        if colors.get(root):
            continue
        path: list[str] = [root]
        positions = {root: 0}
        colors[root] = 1
        stack = [(root, iter(sorted(graph[root], key=str.encode)))]
        while stack:
            current, children = stack[-1]
            child = next(children, None)
            if child is None:
                colors[current] = 2
                positions.pop(current)
                path.pop()
                stack.pop()
            elif colors.get(child) == 1:
                return path[positions[child]:] + [child]
            elif not colors.get(child):
                colors[child] = 1
                positions[child] = len(path)
                path.append(child)
                stack.append((child, iter(sorted(graph[child], key=str.encode))))
    return None


def cyclic_components(edges: Iterable[tuple[str, str]]) -> list[list[str]]:
    """Return every cyclic strongly connected component without recursion.

    Both each component and the component list use UTF-8 byte ordering. Unlike
    a bounded diagnostic cycle preview, these complete components let callers
    quarantine all nodes in overlapping cycles. A singleton is cyclic only
    when it has a self-loop. Edges run producer -> consumer.
    """
    graph = _graph(edges)
    reverse: dict[str, set[str]] = {node: set() for node in graph}
    for parent, children in graph.items():
        for child in children:
            reverse[child].add(parent)

    # Kosaraju's first pass records finish order using explicit DFS frames.
    seen: set[str] = set()
    finished: list[str] = []
    for root in sorted(graph, key=str.encode):
        if root in seen:
            continue
        seen.add(root)
        stack = [(root, iter(sorted(graph[root], key=str.encode)))]
        while stack:
            current, children = stack[-1]
            child = next(children, None)
            if child is None:
                finished.append(current)
                stack.pop()
            elif child not in seen:
                seen.add(child)
                stack.append((child, iter(sorted(graph[child], key=str.encode))))

    components: list[list[str]] = []
    seen.clear()
    for root in reversed(finished):
        if root in seen:
            continue
        component: list[str] = []
        pending = [root]
        seen.add(root)
        while pending:
            current = pending.pop()
            component.append(current)
            for parent in reverse[current]:
                if parent not in seen:
                    seen.add(parent)
                    pending.append(parent)
        if len(component) > 1 or root in graph[root]:
            components.append(sorted(component, key=str.encode))
    return sorted(components, key=lambda component: tuple(node.encode("utf-8") for node in component))


class DependencyCycle(ValueError):
    def __init__(self, cycle: list[str]):
        super().__init__("Dependency graph contains a cycle")
        self.cycle = cycle


def effective_priorities(tasks: Iterable[Mapping[str, Any]],
                         dependencies: Iterable[tuple[str, str]]) -> dict[str, int]:
    """Recompute aging and reverse-DAG inheritance from active gate edges only.

    ``eligible_wait_seconds`` is already accumulated by the service only while
    ready and waiting (never blocked/running). Previously accumulated eligible
    wait remains valid while blocked. Priority provenance is a service gate.
    """
    task_list = list(tasks)
    indexed = {task["id"]: task for task in task_list}
    if len(indexed) != len(task_list):
        raise ValueError("Task IDs must be unique")
    graph = _graph(dependencies)
    if not graph.keys() <= indexed.keys():
        raise ValueError("Dependency references an unknown task")
    cycle = find_cycle((a, b) for a, children in graph.items() for b in children)
    if cycle:
        raise DependencyCycle(cycle)
    effective: dict[str, int] = {}
    for task_id, task in indexed.items():
        base = task["base_priority"]
        if isinstance(base, str):
            if base not in PRIORITY_RANKS:
                raise ValueError("Unknown priority")
            base = PRIORITY_RANKS[base]
        if type(base) is not int or base not in (0, 1, 2):
            raise ValueError("Priority rank must be 0, 1 or 2")
        wait = task["eligible_wait_seconds"]
        if type(wait) not in (int, float) or not math.isfinite(wait) or wait < 0:
            raise ValueError("Eligible wait must be a finite nonnegative number")
        effective[task_id] = min(2, base + int(wait // AGING_INTERVAL_SECONDS))
        graph.setdefault(task_id, set())
    indegrees = {task_id: 0 for task_id in graph}
    for children in graph.values():
        for child in children:
            indegrees[child] += 1
    queue = [task_id for task_id, count in indegrees.items() if count == 0]
    heapq.heapify(queue)
    ordered = []
    while queue:
        current = heapq.heappop(queue)
        ordered.append(current)
        for child in sorted(graph[current], key=str.encode):
            indegrees[child] -= 1
            if indegrees[child] == 0:
                heapq.heappush(queue, child)
    for task_id in reversed(ordered):
        effective[task_id] = max([effective[task_id], *(effective[child] for child in graph[task_id])])
    return effective


def ordered_ready(tasks: Iterable[Mapping[str, Any]],
                  dependencies: Iterable[tuple[str, str]]) -> list[Mapping[str, Any]]:
    """Ready waiting units ordered by effective rank, ready sequence, UTF-8 ID."""
    tasks, dependencies = list(tasks), list(dependencies)
    effective = effective_priorities(tasks, dependencies)
    blocked = {consumer for _, consumer in dependencies}
    ready = [task for task in tasks if task["ready"] and not task["running"]
             and task["id"] not in blocked]
    return sorted(ready, key=lambda task: (-effective[task["id"]], task["ready_sequence"],
                                           task["id"].encode("utf-8")))
