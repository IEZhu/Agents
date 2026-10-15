"""Conflict and fairness examples use facts, never sleeps or a live daemon."""
import pytest

from src.cooperation.policy import (DependencyCycle, cyclic_components, effective_priorities,
                                    find_cycle, logical_overlap, ordered_ready,
                                    resources_conflict)


def resource(kind="physical_file", key="checkout:src/a.py", access="write"):
    return {"resource_type": kind, "resource_key": key, "access": access}


def task(identity, priority="normal", wait=0, sequence=1, ready=True, running=False):
    return {"id": identity, "base_priority": priority, "eligible_wait_seconds": wait,
            "ready_sequence": sequence, "ready": ready, "running": running}


def test_same_file_and_subtree_conflicts_with_component_boundaries():
    assert resources_conflict(resource(), resource())
    assert resources_conflict(resource("physical_subtree", "checkout:src"), resource())
    assert resources_conflict(resource(), resource("physical_subtree", "checkout:src"))
    assert not resources_conflict(resource("physical_subtree", "checkout:src"), resource(key="checkout:src2/a.py"))
    assert not resources_conflict(resource("physical_file", "checkout:src"), resource())
    assert resources_conflict(resource("physical_subtree", "checkout:."), resource())


def test_private_worktrees_and_case_sensitive_names_remain_independent():
    assert not resources_conflict(resource(), resource(key="other:src/a.py"))
    assert not resources_conflict(resource(), resource(key="checkout:src/A.py"))


def test_snapshot_reads_and_logical_overlap_do_not_exclude_writers():
    assert not resources_conflict(resource(), resource(access="snapshot_read"))
    intent = resource("logical_intent", "repo:api/orders")
    assert logical_overlap(intent, intent)
    assert not resources_conflict(intent, intent)
    assert not logical_overlap(resource(), resource())


@pytest.mark.parametrize("kind,key", [("integration_target", "repo:refs/heads/dev"),
                                      ("shared_git", "repo:refs"), ("heavy_execution", "host:heavy")])
def test_exclusive_shared_resources(kind, key):
    assert resources_conflict(resource(kind, key), resource(kind, key, "integrate"))
    assert not resources_conflict(resource(kind, key), resource(kind, key + "2"))


def test_cycle_path_is_closed_and_deterministic():
    edges = [("b", "c"), ("c", "a"), ("a", "b"), ("x", "y")]
    assert find_cycle(edges) == ["a", "b", "c", "a"]
    assert find_cycle(reversed(edges)) == ["a", "b", "c", "a"]
    assert find_cycle([("a", "a")]) == ["a", "a"]
    assert find_cycle([("a", "b"), ("a", "c")]) is None


def test_cyclic_components_cover_overlapping_cycles_without_merging_one_way_links():
    edges = [("a", "b"), ("b", "a"), ("b", "c"), ("c", "b"),
             ("c", "d"), ("d", "e"), ("e", "d"), ("e", "outside"),
             ("self", "self"), ("incoming", "a")]
    expected = [["a", "b", "c"], ["d", "e"], ["self"]]
    assert cyclic_components(edges) == expected
    assert cyclic_components(reversed(edges)) == expected
    assert cyclic_components(iter(edges + edges)) == expected


def test_cyclic_components_exclude_acyclic_nodes_and_empty_graph():
    assert cyclic_components([]) == []
    assert cyclic_components([("a", "b"), ("a", "c"), ("b", "d"), ("c", "d")]) == []
    assert cyclic_components([("b", "a"), ("a", "b"), ("A", "A"), ("z", "z")]) == [
        ["A"], ["a", "b"], ["z"],
    ]


def test_cyclic_components_are_iterative_and_preserve_nodes_beyond_diagnostic_bounds():
    nodes = [f"task-{index:04d}" for index in range(3000)]
    edges = list(zip(nodes, nodes[1:]))
    assert cyclic_components(edges) == []
    assert cyclic_components(edges + [(nodes[-1], nodes[0])]) == [nodes]


def test_priority_inheritance_is_transitive_and_removed_with_accepted_gate():
    tasks = [task("producer", sequence=1), task("middle", ready=False),
             task("consumer", "urgent", ready=False), task("unrelated", sequence=0)]
    edges = [("producer", "middle"), ("middle", "consumer")]
    assert effective_priorities(tasks, edges) == {"producer": 2, "middle": 2, "consumer": 2, "unrelated": 1}
    assert [item["id"] for item in ordered_ready(tasks, edges)] == ["producer", "unrelated"]
    assert effective_priorities(tasks, [])["producer"] == 1


def test_background_aging_uses_effective_rank_then_ready_sequence():
    tasks = [task("old", "background", wait=599, sequence=1), task("new", sequence=2)]
    assert ordered_ready(tasks, [])[0]["id"] == "new"
    tasks[0]["eligible_wait_seconds"] = 600
    assert ordered_ready(tasks, [])[0]["id"] == "old"
    tasks[0]["eligible_wait_seconds"] = 1200
    assert effective_priorities(tasks, [])["old"] == 2


def test_previously_aged_blocked_consumer_transmits_priority_but_never_runs():
    tasks = [task("producer"), task("consumer", "background", wait=1200, ready=False)]
    assert effective_priorities(tasks, [("producer", "consumer")])["producer"] == 2
    assert [item["id"] for item in ordered_ready(tasks, [("producer", "consumer")])] == ["producer"]
    # No wall clock is read: blocked/running time cannot silently grow aging.
    assert effective_priorities(tasks, [("producer", "consumer")])["consumer"] == 2


def test_running_units_are_nonpreemptive_and_dependencies_gate_even_bad_ready_flag():
    tasks = [task("running", running=True), task("consumer", "urgent"), task("ready")]
    assert [item["id"] for item in ordered_ready(tasks, [("running", "consumer")])] == ["ready"]


def test_deterministic_bytewise_final_tie_breaker():
    tasks = [task("z"), task("A"), task("a")]
    assert [item["id"] for item in ordered_ready(tasks, [])] == ["A", "a", "z"]


def test_invalid_graph_and_priority_are_explicit():
    with pytest.raises(DependencyCycle) as error:
        effective_priorities([task("a"), task("b")], [("a", "b"), ("b", "a")])
    assert error.value.cycle == ["a", "b", "a"]
    with pytest.raises(ValueError, match="unknown task"):
        effective_priorities([task("a")], [("a", "b")])
    with pytest.raises(ValueError, match="unique"):
        effective_priorities([task("a"), task("a")], [])
    for bad in (True, float("nan"), -1):
        with pytest.raises(ValueError):
            effective_priorities([task("a", wait=bad)], [])
    with pytest.raises(ValueError):
        effective_priorities([task("a", priority=True)], [])
