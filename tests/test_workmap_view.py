"""Work-map views: dependency order, budgets, deltas and node records."""
import pytest

from src.workmap.store import INVALID_LIMIT, WorkmapStore, fold, map_key
from src.workmap.view import render_list, render_map, render_node

TASK = "task-1"


@pytest.fixture
def store(tmp_path):
    store = WorkmapStore(tmp_path, clock=lambda: 1_800_000_000.0)
    store.apply(TASK, [
        {"op": "add_node", "id": "goal", "kind": "goal", "title": "Ship the feature"},
        {"op": "add_node", "id": "write", "title": "Write it", "summary": "The change itself"},
        {"op": "add_node", "id": "map-code", "title": "Map the code", "risk": "routine"},
        {"op": "add_edge", "from": "write", "to": "map-code", "type": "depends_on"},
    ])
    return store


def test_the_view_lists_dependencies_first_and_says_what_is_ready(store):
    lines = render_map(store.load(TASK)).splitlines()
    assert lines[0] == "map task-1 · revision 5"
    assert lines[1:4] == ["[planned V0] goal: Ship the feature",
                          "[planned V0] map-code: Map the code",
                          "[planned V0] write: Write it (after map-code) — The change itself"]
    assert lines[-2:] == ["ready: map-code", "needs attention: none"]


def test_since_shows_only_the_nodes_changed_after_a_revision(store):
    store.apply(TASK, [{"op": "update_node", "id": "write", "summary": "Smaller change"}])
    text = render_map(store.load(TASK), since=5)
    assert "changed after revision 5: 1 of 3 nodes" in text
    assert "write: Write it (after map-code) — Smaller change" in text
    assert "map-code: Map the code" not in text


def test_a_short_budget_drops_summaries_before_nodes(tmp_path):
    store = WorkmapStore(tmp_path)
    store.apply(TASK, [{"op": "add_node", "id": f"n{i}", "title": f"Node {i}", "summary": "s" * 290}
                       for i in range(4)])
    current = store.load(TASK)
    assert len(render_map(current)) > 1000
    shorter = render_map(current, budget=600)
    assert len(shorter) <= 600 and "s" * 290 not in shorter and "n3: Node 3" in shorter


def test_a_budget_too_small_for_every_node_says_how_many_are_left(tmp_path):
    store = WorkmapStore(tmp_path)
    store.apply(TASK, [{"op": "add_node", "id": f"node-{i}", "title": "T" * 100} for i in range(40)])
    text = render_map(store.load(TASK), budget=1000)
    assert len(text) <= 1000
    assert "more nodes: read them with since or node" in text


def test_a_validated_node_below_its_level_needs_attention(store):
    store.record_prepared(TASK, "map-code", contract_revision="a" * 64, attempt_id="1", complexity=None)
    store.record_result(TASK, "map-code", contract_revision="a" * 64, attempt_id="1", text="mapped",
                        sha256="b" * 64, outcome={"status": "validated", "level": "V1", "summary": "Mapped",
                                                  "checks": {"passed": 0, "failed": 0, "not_run": 1},
                                                  "findings": None, "limitations": 0},
                        handback="line")
    # Neither ready nor settled: without this line the loop would stall with nothing to do.
    assert render_map(store.load(TASK)).splitlines()[-2:] == ["ready: none", "needs attention: map-code"]


def test_an_accepted_critical_node_below_v3_needs_attention(store):
    store.apply(TASK, [{"op": "update_node", "id": "map-code", "risk": "critical"}])
    store.record_prepared(TASK, "map-code", contract_revision="a" * 64, attempt_id="1", complexity=None)
    store.record_result(TASK, "map-code", contract_revision="a" * 64, attempt_id="1", text="mapped",
                        sha256="b" * 64, outcome={"status": "validated", "level": "V2", "summary": "Mapped",
                                                  "checks": {"passed": 1, "failed": 0, "not_run": 0},
                                                  "findings": None, "limitations": 0},
                        handback="line")
    # The store now refuses this acceptance; a map written before that rule can still hold it.
    name = f"{map_key(TASK)}.jsonl"
    with store._writing(name) as (handle, records):
        store._commit(handle, name, [{"op": "update_node", "by": "parent", "id": "map-code", "status": "accepted",
                                      "reason": None, "rev": fold(records).revision + 1}])
    # Accepted but still below the V3 a critical node needs: write waits, so the parent must act.
    assert render_map(store.load(TASK)).splitlines()[-2:] == ["ready: none", "needs attention: map-code"]
    store.apply(TASK, [{"op": "update_node", "id": "map-code", "status": "accepted", "level": "V4"}])
    assert render_map(store.load(TASK)).splitlines()[-2:] == ["ready: write", "needs attention: none"]


def listed(line, label):
    """The ids a footer line names and how many more it counts."""
    rest = line.removeprefix(f"{label}: ")
    shown, _, more = rest.rpartition(" and ") if " and " in rest else ("", "", rest)
    if not more.endswith(" more"):
        return rest.split(", "), 0
    return (shown.split(", ") if shown else []), int(more.removesuffix(" more"))


def test_long_ready_and_attention_lists_are_cut_to_the_budget(tmp_path):
    task = "t" * 128
    store = WorkmapStore(tmp_path)
    ids = [f"{index:02d}-" + "n" * 37 for index in range(30)]
    store.apply(task, [{"op": "add_node", "id": node_id, "title": "T"} for node_id in ids])
    for index, node_id in enumerate(ids[:15]):  # validated V1: these need the parent
        revision = f"{index:02d}".ljust(64, "a")
        store.record_prepared(task, node_id, contract_revision=revision, attempt_id="1", complexity=None)
        store.record_result(task, node_id, contract_revision=revision, attempt_id="1", text="t",
                            sha256=revision[::-1], handback="line",
                            outcome={"status": "validated", "level": "V1", "summary": "", "checks": None,
                                     "findings": None, "limitations": 0})
    text = render_map(store.load(task), budget=500)
    assert len(text) <= 500
    ready, attention = text.splitlines()[-2:]
    for line, label, expected in ((ready, "ready", ids[15:]), (attention, "needs attention", ids[:15])):
        shown, more = listed(line, label)
        assert more > 0 and shown == expected[:len(shown)] and len(shown) + more == len(expected), line


def test_a_node_record_shows_its_relations(store):
    text = render_node(store.load(TASK), "map-code")
    assert text.splitlines()[0] == "node map-code (stage, risk routine, needs V2) [planned V0]"
    assert "needed by write" in text


def test_a_node_record_says_why_its_submissions_were_invalid_in_any_status(store):
    store.record_prepared(TASK, "map-code", contract_revision="a" * 64, attempt_id="1", complexity=None)
    store.record_invalid(TASK, "map-code", contract_revision="a" * 64, codes=["string_too_long"])
    store.record_invalid(TASK, "map-code", contract_revision="a" * 64,
                         codes=["separator_required", "separator_required", "string_too_long"])
    assert ("invalid submissions: 2 (codes separator_required, string_too_long)"
            in render_node(store.load(TASK), "map-code").splitlines())
    for _ in range(INVALID_LIMIT - 2):
        store.record_invalid(TASK, "map-code", contract_revision="a" * 64, codes=["schema"])
    text = render_node(store.load(TASK), "map-code")
    assert "[needs_attention V0]" in text.splitlines()[0]
    assert f"invalid submissions: {INVALID_LIMIT} (codes schema)" in text.splitlines()


def test_a_node_record_shows_the_optional_result_fields_when_present(store):
    store.record_prepared(TASK, "map-code", contract_revision="a" * 64, attempt_id="1", complexity=None)
    store.record_result(TASK, "map-code", contract_revision="a" * 64, attempt_id="1", text="mapped",
                        sha256="b" * 64, outcome={"status": "validated", "level": "V2", "summary": "Mapped",
                                                  "checks": {"passed": 1, "failed": 0, "not_run": 0},
                                                  "findings": None, "limitations": 0}, handback="line")
    plain = render_node(store.load(TASK), "map-code")
    assert "result status" not in plain and "question" not in plain and "escalation" not in plain
    name = f"{map_key(TASK)}.jsonl"
    with store._writing(name) as (handle, records):  # the fields a later writer adds to a result event
        event = next(record for record in records if record.get("op") == "result")
        revision = fold(records).revision + 1
        store._commit(handle, name, [{**event, "rev": revision, "contract_revision": "c" * 64,
                                      "result_status": "input_required", "question": "Which\nbranch? " + "q" * 400,
                                      "escalation": {"complexity": "high", "tier": "inherit"}}])
    lines = render_node(store.load(TASK), "map-code").splitlines()
    assert "result status: input_required" in lines
    question = next(line for line in lines if line.startswith("question: "))
    assert question.startswith("question: Which branch? qqq") and len(question) == len("question: ") + 300
    assert "escalation: complexity high, tier inherit" in lines


def test_the_list_names_each_map_with_its_goal(store):
    assert render_list(store.list_maps()) == "task-1 · revision 5 · Ship the feature · planned 3"
