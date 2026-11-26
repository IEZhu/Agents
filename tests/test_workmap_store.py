"""The work-map store: parent operations, server records, folding and the safe-file pattern."""
import json
import os
import stat
import sys
import threading
from types import SimpleNamespace

import pytest

from src import safe_store
from src.workmap import store as store_module
from src.workmap.store import (INVALID_LIMIT, MAX_OPS, WorkmapError, WorkmapStore, is_ready, is_stale,
                               map_key, required_level, settled)
from src.workmap.view import render_map

TASK = "task-1"
FIRST = "a" * 64
SECOND = "d" * 64
OUTCOME = {"status": "validated", "level": "V2", "summary": "Mapped",
           "checks": {"passed": 1, "failed": 0, "not_run": 0}, "findings": None, "limitations": 0}


@pytest.fixture
def store(tmp_path):
    return WorkmapStore(tmp_path, clock=lambda: 1_800_000_000.0)


def plan(store):
    return store.apply(TASK, [
        {"op": "add_node", "id": "goal", "kind": "goal", "title": "Ship the feature"},
        {"op": "add_node", "id": "map-code", "title": "Map the code", "risk": "routine"},
        {"op": "add_node", "id": "write", "title": "Write it"},
        {"op": "add_edge", "from": "write", "to": "map-code", "type": "depends_on"},
    ])


def prepare(store, node="map-code", revision=FIRST, attempt="1"):
    store.check_preparable(TASK, node, [])
    return store.record_prepared(TASK, node, contract_revision=revision, attempt_id=attempt, complexity=None)


def submit(store, node="map-code", revision=FIRST, sha="b" * 64, text="mapped code"):
    return store.record_result(TASK, node, contract_revision=revision, attempt_id="1", text=text,
                               sha256=sha, outcome=OUTCOME, handback="line")


def map_file(tmp_path):
    return tmp_path / "data" / "workmap" / f"{map_key(TASK)}.jsonl"


def test_a_first_apply_opens_the_map_and_numbers_every_event(store):
    result = plan(store)
    assert result == {"status": "applied", "task_id": TASK, "revision": 5,
                      "changed": ["goal", "map-code", "write"]}
    workmap = store.load(TASK)
    assert list(workmap.nodes) == ["goal", "map-code", "write"]
    assert workmap.nodes["write"]["status"] == "planned" and workmap.nodes["write"]["level"] == "V0"
    assert workmap.targets("write", "depends_on") == ["map-code"]


def test_the_map_is_private_and_append_only(store, tmp_path):
    plan(store)
    store.apply(TASK, [{"op": "update_node", "id": "write", "summary": "Now with a summary"}])
    lines = map_file(tmp_path).read_text(encoding="utf-8").splitlines()
    assert len(lines) == 6 and json.loads(lines[0])["op"] == "open"
    if sys.platform != "win32":
        assert stat.S_IMODE(os.stat(map_file(tmp_path)).st_mode) == 0o600
        assert stat.S_IMODE(os.stat(map_file(tmp_path).parent).st_mode) == 0o700


def test_an_operation_list_is_all_or_nothing(store):
    plan(store)
    with pytest.raises(WorkmapError) as raised:
        store.apply(TASK, [{"op": "add_node", "id": "extra", "title": "Fine"},
                           {"op": "add_node", "id": "extra", "title": "Duplicate"}])
    assert raised.value.code == "invalid_request"
    assert "extra" not in store.load(TASK).nodes


@pytest.mark.parametrize("status", ["prepared", "validated", "needs_attention", "verified", "disputed"])
def test_the_parent_cannot_set_a_server_status(store, status):
    plan(store)
    with pytest.raises(WorkmapError) as raised:
        store.apply(TASK, [{"op": "update_node", "id": "write", "status": status}])
    assert raised.value.code == "status_reserved"


def test_only_a_settled_node_can_be_accepted(store):
    plan(store)
    with pytest.raises(WorkmapError) as raised:
        store.apply(TASK, [{"op": "update_node", "id": "write", "status": "accepted"}])
    assert raised.value.code == "invalid_transition"


def test_a_depends_on_cycle_is_refused(store):
    plan(store)
    with pytest.raises(WorkmapError) as raised:
        store.apply(TASK, [{"op": "add_edge", "from": "map-code", "to": "write", "type": "depends_on"}])
    assert raised.value.code == "cycle"


def test_a_depends_on_edge_to_a_goal_is_refused(store):
    plan(store)
    with pytest.raises(WorkmapError) as raised:
        store.apply(TASK, [{"op": "add_edge", "from": "write", "to": "goal", "type": "depends_on"}])
    assert raised.value.code == "invalid_request" and "goal" in str(raised.value)
    # A goal never settles, so a dependant would never be ready; other edges to it are fine.
    store.apply(TASK, [{"op": "add_edge", "from": "goal", "to": "write", "type": "depends_on"},
                       {"op": "add_edge", "from": "write", "to": "goal", "type": "contradicts"}])


@pytest.mark.parametrize("op", [
    {"op": "add_node", "id": "x", "title": "Lone \ud800 surrogate"},
    {"op": "update_node", "id": "write", "summary": "Lone \udfff surrogate"},
    {"op": "update_node", "id": "write", "status": "blocked", "reason": "Lone \ud83d surrogate"},
])
def test_text_that_utf8_cannot_encode_is_an_invalid_request(store, tmp_path, op):
    plan(store)
    before = map_file(tmp_path).read_bytes()
    with pytest.raises(WorkmapError) as raised:
        store.apply(TASK, [op])
    assert raised.value.code == "invalid_request" and "UTF-8" in str(raised.value)
    assert map_file(tmp_path).read_bytes() == before


def test_a_stale_expected_revision_is_refused(store):
    plan(store)
    with pytest.raises(WorkmapError) as raised:
        store.apply(TASK, [{"op": "close"}], expected_revision=4)
    assert raised.value.code == "revision_conflict"
    assert store.apply(TASK, [{"op": "close"}], expected_revision=5)["revision"] == 6


def test_a_closed_map_refuses_operations(store):
    plan(store)
    store.apply(TASK, [{"op": "close"}])
    with pytest.raises(WorkmapError) as raised:
        store.apply(TASK, [{"op": "add_node", "id": "late", "title": "Too late"}])
    assert raised.value.code == "map_closed"


def test_too_many_operations_are_refused(store):
    with pytest.raises(WorkmapError) as raised:
        store.apply(TASK, [{"op": "add_node", "id": f"n{i}", "title": "Node"} for i in range(MAX_OPS + 1)])
    assert raised.value.code == "invalid_request"


def test_a_torn_last_line_is_skipped_and_never_glued(store, tmp_path):
    plan(store)
    with open(map_file(tmp_path), "ab") as handle:
        handle.write(b'{"op": "add_node", "id": "torn"')
    store.apply(TASK, [{"op": "add_node", "id": "after", "title": "After the tear"}])
    workmap = store.load(TASK)
    assert "after" in workmap.nodes and "torn" not in workmap.nodes


def test_a_torn_commit_applies_none_of_its_events(store, tmp_path):
    plan(store)
    store.apply(TASK, [{"op": "add_node", "id": "lost-a", "title": "Lost"},
                       {"op": "add_node", "id": "lost-b", "title": "Lost too"},
                       {"op": "add_edge", "from": "lost-b", "to": "lost-a", "type": "depends_on"}])
    raw = map_file(tmp_path).read_bytes()
    map_file(tmp_path).write_bytes(raw[:raw.rindex(b"\n", 0, len(raw) - 1) + 20])  # the batch's last line torn
    workmap = store.load(TASK)
    assert workmap.revision == 5 and "lost-a" not in workmap.nodes and "lost-b" not in workmap.nodes
    # The next commit reuses the lost revisions; the torn batch still never applies.
    assert store.apply(TASK, [{"op": "add_node", "id": "kept-a", "title": "Kept"},
                              {"op": "add_node", "id": "kept-b", "title": "Kept too"},
                              {"op": "add_node", "id": "kept-c", "title": "Kept as well"}])["revision"] == 8
    workmap = store.load(TASK)
    assert workmap.revision == 8 and list(workmap.nodes) == ["goal", "map-code", "write", "kept-a", "kept-b",
                                                             "kept-c"]


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX links")
def test_a_linked_map_file_is_refused(store, tmp_path):
    plan(store)
    target = tmp_path / "elsewhere.jsonl"
    map_file(tmp_path).rename(target)
    map_file(tmp_path).symlink_to(target)
    with pytest.raises(WorkmapError) as raised:
        store.apply(TASK, [{"op": "close"}])
    assert raised.value.code == "unsafe_store"


def test_a_valid_result_settles_its_node_and_readies_the_dependant(store):
    plan(store)
    workmap = store.load(TASK)
    assert not is_ready(workmap, workmap.nodes["write"])
    prepare(store)
    receipt = submit(store)
    assert receipt == {"task_id": TASK, "node": "map-code", "status": "validated", "level": "V2",
                       "revision": 7, "duplicate": False}
    workmap = store.load(TASK)
    assert workmap.nodes["map-code"]["summary"] == "Mapped"
    assert is_ready(workmap, workmap.nodes["write"])
    assert store.result_text(TASK, "map-code") == "mapped code"


def test_an_accepted_node_settles_a_routine_or_load_bearing_need_whatever_its_level(store):
    plan(store)
    prepare(store)
    # A completed stage with no passed check is validated V1: below the V2 a dependant needs.
    store.record_result(TASK, "map-code", contract_revision=FIRST, attempt_id="1", text="mapped",
                        sha256="b" * 64, outcome={**OUTCOME, "level": "V1",
                                                  "checks": {"passed": 0, "failed": 0, "not_run": 1}},
                        handback="line")
    workmap = store.load(TASK)
    assert not settled(workmap.nodes["map-code"]) and not is_ready(workmap, workmap.nodes["write"])
    store.apply(TASK, [{"op": "update_node", "id": "map-code", "status": "accepted"}])
    workmap = store.load(TASK)
    assert settled(workmap.nodes["map-code"]) and is_ready(workmap, workmap.nodes["write"])


def test_a_critical_node_below_v3_is_accepted_only_with_the_users_v4(store):
    plan(store)
    store.apply(TASK, [{"op": "update_node", "id": "map-code", "risk": "critical"}])
    prepare(store)
    submit(store)  # validated V2
    # Accepting it without V4 would leave it unsettled, and an accepted node takes no verdict.
    with pytest.raises(WorkmapError) as raised:
        store.apply(TASK, [{"op": "update_node", "id": "map-code", "status": "accepted"}])
    assert raised.value.code == "invalid_transition" and "V4" in str(raised.value)
    assert store.load(TASK).nodes["map-code"]["status"] == "validated"
    store.apply(TASK, [{"op": "update_node", "id": "map-code", "status": "accepted", "level": "V4"}])
    assert settled(store.load(TASK).nodes["map-code"])


def test_a_verified_critical_node_is_accepted_without_v4(store):
    plan(store)
    store.apply(TASK, [{"op": "update_node", "id": "map-code", "risk": "critical"}])
    prepare(store)
    submit(store)
    server_event(store, {"op": "verdict", "id": "map-code", "verifier": "check", "result_sha256": "b" * 64,
                         "status": "verified", "level": "V3"})
    store.apply(TASK, [{"op": "update_node", "id": "map-code", "status": "accepted"}])
    node = store.load(TASK).nodes["map-code"]
    assert node["status"] == "accepted" and node["level"] == "V3" and settled(node)


def test_a_new_attempt_starts_again_at_v0(store):
    plan(store)
    prepare(store)
    submit(store)  # validated V2
    store.apply(TASK, [{"op": "update_node", "id": "map-code", "status": "planned"}])
    assert store.load(TASK).nodes["map-code"]["level"] == "V2"  # kept until the node is prepared again
    prepare(store, revision=SECOND, attempt="2")
    node = store.load(TASK).nodes["map-code"]
    assert node["status"] == "prepared" and node["level"] == "V0"


def test_a_second_different_result_for_one_contract_is_refused(store):
    plan(store)
    prepare(store)
    submit(store)
    assert submit(store)["duplicate"] is True
    with pytest.raises(WorkmapError) as raised:
        submit(store, sha="c" * 64)
    assert raised.value.code == "already_submitted"


def test_a_result_for_an_earlier_attempt_is_refused(store):
    plan(store)
    prepare(store, revision=FIRST)
    prepare(store, revision=SECOND, attempt="2")
    with pytest.raises(WorkmapError) as raised:
        submit(store, revision=FIRST)
    assert raised.value.code == "stale_contract"


def test_the_same_result_of_a_contract_after_a_new_attempt_is_stale(store):
    """A repeated submission of an old contract is refused as stale, not answered as a duplicate."""
    plan(store)
    prepare(store)
    submit(store)
    store.apply(TASK, [{"op": "update_node", "id": "map-code", "status": "planned"}])
    prepare(store, revision=SECOND, attempt="2")
    with pytest.raises(WorkmapError) as raised:
        submit(store)  # the first contract's identical result
    assert raised.value.code == "stale_contract"


def test_repeated_invalid_submissions_send_the_node_to_needs_attention(store):
    plan(store)
    prepare(store)
    for count in range(1, INVALID_LIMIT + 1):
        assert store.record_invalid(TASK, "map-code", contract_revision=FIRST, codes=["string_too_long"]) == count
    assert store.load(TASK).nodes["map-code"]["status"] == "needs_attention"


def test_a_goal_cannot_be_prepared(store):
    plan(store)
    with pytest.raises(WorkmapError) as raised:
        store.check_preparable(TASK, "goal", [])
    assert raised.value.code == "node_not_preparable"


def test_an_input_must_be_settled_and_returns_its_stored_text(store):
    plan(store)
    with pytest.raises(WorkmapError) as raised:
        store.check_preparable(TASK, "write", ["map-code"])
    assert raised.value.code == "input_not_ready"
    prepare(store)
    submit(store)
    assert store.check_preparable(TASK, "write", ["map-code"]) == [("map-code", "mapped code")]


def server_event(store, event):
    """Append one server event as a later PR's writer would (verdicts, extra result fields)."""
    name = f"{map_key(TASK)}.jsonl"
    with store._writing(name) as (handle, records):
        return store._commit(handle, name, [{"by": "server", **event,
                                             "rev": store_module.fold(records).revision + 1}])


def test_a_node_keeps_the_highest_complexity_it_was_prepared_with(store):
    plan(store)
    for revision, complexity in ((FIRST, "high"), (SECOND, "low"), ("e" * 64, None)):
        store.record_prepared(TASK, "map-code", contract_revision=revision, attempt_id="1",
                              complexity=complexity)
        assert required_level(store.load(TASK).nodes["map-code"]) == "V3", complexity


def test_a_disputed_node_can_be_an_input(store):
    plan(store)
    prepare(store)
    submit(store)
    server_event(store, {"op": "verdict", "id": "map-code", "status": "disputed"})
    assert store.load(TASK).nodes["map-code"]["status"] == "disputed"
    # The second verifier of a disputed stage is pinned on the stage's own result.
    assert store.check_preparable(TASK, "write", ["map-code"]) == [("map-code", "mapped code")]


def test_a_verifier_waits_for_a_target_whose_result_needs_attention(store):
    plan(store)
    store.apply(TASK, [{"op": "add_node", "id": "check", "title": "Check the map"},
                       {"op": "add_edge", "from": "check", "to": "map-code", "type": "verifies"}])
    prepare(store)
    store.record_result(TASK, "map-code", contract_revision=FIRST, attempt_id="1", text="partial map",
                        sha256="b" * 64, handback="line",
                        outcome={**OUTCOME, "status": "needs_attention", "level": "V0"})
    workmap = store.load(TASK)
    assert "result_sha256" in workmap.nodes["map-code"]["refs"]
    # workmap_inputs would refuse this target (input_not_ready), so its verifier is not ready either.
    assert not is_ready(workmap, workmap.nodes["check"])
    with pytest.raises(WorkmapError) as raised:
        store.check_preparable(TASK, "check", ["map-code"])
    assert raised.value.code == "input_not_ready"
    prepare(store, revision=SECOND, attempt="2")
    submit(store, revision=SECOND)
    workmap = store.load(TASK)
    assert is_ready(workmap, workmap.nodes["check"])


def test_a_prepared_again_node_drops_the_previous_results_fields(store):
    plan(store)
    prepare(store, node="write")
    store.record_result(TASK, "write", contract_revision=FIRST, attempt_id="1", text="half done",
                        sha256="b" * 64, handback="line",
                        outcome={**OUTCOME, "status": "needs_attention", "level": "V0", "summary": "Half done",
                                 "findings": {"high": 1}, "limitations": 2})
    server_event(store, {**next(record for record in store._read(f"{map_key(TASK)}.jsonl")
                                if record.get("op") == "result"),
                         "contract_revision": "c" * 64, "result_status": "input_required",
                         "question": "Which branch?", "escalation": {"tier": "inherit", "reason": "status_partial"}})
    node = store.load(TASK).nodes["write"]
    assert node["question"] == "Which branch?" and node["checks"] is not None
    prepare(store, node="write", revision=SECOND, attempt="2")
    node = store.load(TASK).nodes["write"]
    assert node["status"] == "prepared"
    assert {key: node[key] for key in ("summary", "checks", "findings", "limitations", "result_status",
                                       "question", "escalation", "handback")} == {
        "summary": "", "checks": None, "findings": None, "limitations": 0, "result_status": None,
        "question": None, "escalation": None, "handback": None}


def test_a_prepared_again_node_shows_the_parents_latest_summary(store):
    plan(store)
    store.apply(TASK, [{"op": "update_node", "id": "write", "summary": "The change itself"}])
    prepare(store, node="write")
    store.record_result(TASK, "write", contract_revision=FIRST, attempt_id="1", text="half done",
                        sha256="b" * 64, handback="line",
                        outcome={**OUTCOME, "status": "needs_attention", "level": "V0", "summary": "Half done"})
    assert store.load(TASK).nodes["write"]["summary"] == "Half done"
    prepare(store, node="write", revision=SECOND, attempt="2")
    assert store.load(TASK).nodes["write"]["summary"] == "The change itself"


def test_a_refuted_dependency_makes_its_dependant_stale(store):
    plan(store)
    prepare(store)
    submit(store)
    store.apply(TASK, [{"op": "update_node", "id": "map-code", "status": "refuted", "reason": "Wrong file"}])
    workmap = store.load(TASK)
    assert is_stale(workmap, workmap.nodes["write"])


def results_file(tmp_path):
    return tmp_path / "data" / "workmap" / f"{map_key(TASK)}.results.jsonl"


def contents(path):
    return path.read_bytes() if path.exists() else None


def test_a_full_map_refuses_an_apply_and_changes_nothing(store, tmp_path, monkeypatch):
    plan(store)
    before = contents(map_file(tmp_path))
    monkeypatch.setattr(store_module, "MAX_STORE_BYTES", len(before) + 50)
    with pytest.raises(WorkmapError) as raised:
        store.apply(TASK, [{"op": "add_node", "id": "late", "title": "One node too many"}])
    assert raised.value.code == "store_full"
    assert contents(map_file(tmp_path)) == before and contents(results_file(tmp_path)) is None


@pytest.mark.parametrize("full", ["map", "results"])
def test_a_full_store_refuses_a_result_and_changes_neither_file(store, tmp_path, monkeypatch, full):
    plan(store)
    prepare(store)
    text = "r" * (100 if full == "map" else 5000)
    submit(store, node="map-code", revision=FIRST, sha="e" * 64, text=text)  # one record in each file
    prepare(store, node="write", revision=SECOND, attempt="2")
    before = (contents(map_file(tmp_path)), contents(results_file(tmp_path)))
    # map: the short text still fits its file, but the event no longer fits the map;
    # results: the long text no longer fits its file, though the event would fit the map.
    limit = len(before[0]) + 50 if full == "map" else len(before[1]) + len(text) // 2
    assert limit > max(len(before[0]), 2 * len(before[1]) if full == "map" else len(before[1]))
    monkeypatch.setattr(store_module, "MAX_STORE_BYTES", limit)
    with pytest.raises(WorkmapError) as raised:
        submit(store, node="write", revision=SECOND, sha="f" * 64, text=text)
    assert raised.value.code == "store_full"
    assert (contents(map_file(tmp_path)), contents(results_file(tmp_path))) == before


def test_keeping_a_result_does_not_parse_the_results_file(store, monkeypatch):
    plan(store)
    prepare(store)
    parsed = []
    real = safe_store.read_records

    def read_records(raw, max_bytes, path, **kwargs):
        parsed.append(path.name)
        return real(raw, max_bytes, path, **kwargs)

    monkeypatch.setattr(safe_store, "read_records", read_records)
    submit(store)
    assert parsed == [f"{map_key(TASK)}.jsonl"]  # the map only, under its lock


def test_an_oversized_map_is_an_unsafe_store(store, tmp_path, monkeypatch):
    plan(store)
    monkeypatch.setattr(store_module, "MAX_STORE_BYTES", len(contents(map_file(tmp_path))) - 1)
    with pytest.raises(WorkmapError) as raised:
        store.load(TASK)
    assert raised.value.code == "unsafe_store"


def test_concurrent_writers_number_every_revision_once(tmp_path):
    WorkmapStore(tmp_path).apply(TASK, [{"op": "add_node", "id": f"s{worker}", "title": "Stage"}
                                        for worker in range(4)])
    errors = []

    def write(worker):
        own = WorkmapStore(tmp_path)  # one store per thread, as the server makes one per call
        try:
            for round_ in range(5):
                own.apply(TASK, [{"op": "add_node", "id": f"n{worker}-{round_}", "title": "Node"},
                                 {"op": "update_node", "id": f"s{worker}", "summary": f"round {round_}"}])
                revision = f"{worker}{round_}".ljust(64, "0")
                own.record_prepared(TASK, f"n{worker}-{round_}", contract_revision=revision,
                                    attempt_id=str(round_), complexity=None)
                own.record_result(TASK, f"n{worker}-{round_}", contract_revision=revision, attempt_id=str(round_),
                                  text=f"result {worker} {round_}", sha256=revision[::-1], outcome=OUTCOME,
                                  handback="line")
        except BaseException as error:  # reported below; a thread's exception is otherwise lost
            errors.append(error)

    threads = [threading.Thread(target=write, args=(worker,)) for worker in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    events = [json.loads(line) for line in map_file(tmp_path).read_text(encoding="utf-8").split("\n") if line]
    revisions = [event["rev"] for event in events]
    # The open event, four nodes, then per worker and round two operations, one preparation, one result.
    assert revisions == list(range(1, 6 + 4 * 5 * 4))
    workmap = WorkmapStore(tmp_path).load(TASK)
    assert workmap.revision == revisions[-1] and len(workmap.nodes) == 4 + 4 * 5
    for worker in range(4):
        for round_ in range(5):
            assert workmap.nodes[f"n{worker}-{round_}"]["status"] == "validated"
            assert WorkmapStore(tmp_path).result_text(TASK, f"n{worker}-{round_}") == f"result {worker} {round_}"


def test_an_unknown_map_is_reported(store):
    with pytest.raises(WorkmapError) as raised:
        store.check_preparable(TASK, "write", [])
    assert raised.value.code == "unknown_map"
    assert store.load(TASK) is None and store.list_maps() == []


# --- the shared safe-file module ----------------------------------------------------


def test_read_records_drops_what_keep_refuses_as_it_parses(tmp_path, monkeypatch):
    raw = b'{"n": 1}\n[2]\nnot json\n{"n": 3}\n'
    path = tmp_path / "store.jsonl"
    assert safe_store.read_records(raw, 100, path) == [{"n": 1}, [2], {"n": 3}]
    calls = []

    def loads(line):
        calls.append("parse")
        return json.loads(line)

    def keep(record):
        calls.append("keep")
        return isinstance(record, dict)

    monkeypatch.setattr(safe_store, "json", SimpleNamespace(loads=loads))
    assert safe_store.read_records(raw, 100, path, keep=keep) == [{"n": 1}, {"n": 3}]
    # Each record is judged right after it is parsed, so a refused one is never kept in the list.
    assert calls == ["parse", "keep", "parse", "keep", "parse", "parse", "keep"]


# json.dumps(ensure_ascii=False) leaves these raw, and str.splitlines() breaks a line at each.
LINE_BREAKS = "    \u0085"


def test_read_records_splits_only_at_newlines(tmp_path):
    raw = (json.dumps({"text": LINE_BREAKS}, ensure_ascii=False) + "\n{\"n\": 2}\n").encode("utf-8")
    assert safe_store.read_records(raw, 1000, tmp_path / "store.jsonl") == [{"text": LINE_BREAKS}, {"n": 2}]


def test_a_result_holding_unicode_line_breaks_reads_back(store):
    plan(store)
    prepare(store)
    text = f"first {LINE_BREAKS} last"
    store.record_result(TASK, "map-code", contract_revision=FIRST, attempt_id="1", text=text,
                        sha256="b" * 64, outcome={**OUTCOME, "summary": f"Mapped {LINE_BREAKS}"},
                        handback="line")
    assert store.result_text(TASK, "map-code") == text
    assert store.load(TASK).nodes["map-code"]["summary"] == f"Mapped {LINE_BREAKS}"
    assert store.check_preparable(TASK, "write", ["map-code"]) == [("map-code", text)]


CREATE = os.O_RDWR | os.O_CREAT | os.O_APPEND


def test_a_fresh_map_directory_ignores_itself_in_git(store, tmp_path):
    plan(store)
    ignore = tmp_path / "data" / "workmap" / ".gitignore"
    assert ignore.read_text(encoding="utf-8") == "*\n"
    assert not (tmp_path / "data" / ".gitignore").exists()  # only the store's own directory


@pytest.mark.parametrize("anchored", [True, False])
def test_a_creating_open_writes_the_ignore_file_only_when_it_is_absent(tmp_path, monkeypatch, anchored):
    if anchored and not safe_store.ANCHORED:
        pytest.skip("POSIX descriptor-relative opens")
    monkeypatch.setattr(safe_store, "ANCHORED", anchored)
    with safe_store.store_directory(tmp_path, ("data", "store"), create=True, private=True):
        pass
    ignore = tmp_path / "data" / "store" / ".gitignore"
    assert ignore.read_text(encoding="utf-8") == "*\n"
    ignore.write_text("mine\n", encoding="utf-8")
    with safe_store.store_directory(tmp_path, ("data", "store"), create=True, private=True):
        pass
    assert ignore.read_text(encoding="utf-8") == "mine\n"  # an existing file is the user's
    with safe_store.store_directory(tmp_path, ("data", "other"), create=False, private=False) as missing:
        assert missing is None
    assert not (tmp_path / "data" / "other").exists()  # a reader creates nothing


def test_a_creating_open_whose_directory_is_gone_is_refused_not_missing(tmp_path):
    gone = tmp_path / "gone"  # the path a Windows store_directory yields
    with pytest.raises(safe_store.UnsafeStore, match="store.jsonl"):
        with safe_store.store_file(gone, "store.jsonl", CREATE, "r+b", private=True):
            pass
    with safe_store.store_file(gone, "store.jsonl", os.O_RDONLY, "rb", private=False) as handle:
        assert handle is None


@pytest.mark.skipif(not safe_store.ANCHORED, reason="POSIX descriptor-relative opens")
def test_a_creating_open_in_a_removed_directory_is_refused_not_missing(tmp_path, monkeypatch):
    monkeypatch.setattr(safe_store, "CREATE_ATTEMPTS", 2)
    with safe_store.store_directory(tmp_path, ("data", "store"), create=True, private=True) as directory:
        (tmp_path / "data" / "store" / ".gitignore").unlink()
        (tmp_path / "data" / "store").rmdir()
        with pytest.raises(safe_store.UnsafeStore, match="store.jsonl"):
            with safe_store.store_file(directory, "store.jsonl", CREATE, "r+b", private=True):
                pass
        with safe_store.store_file(directory, "store.jsonl", os.O_RDONLY, "rb", private=False) as handle:
            assert handle is None


class Junction:
    """What Windows' lstat answers for a directory junction: a directory that is a reparse point."""

    st_file_attributes = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)

    def __init__(self, real):
        self._real = real

    def __getattr__(self, name):
        return getattr(self._real, name)


@pytest.mark.parametrize("create", [True, False])
def test_a_junction_on_the_path_checked_by_name_is_refused(tmp_path, monkeypatch, create):
    (tmp_path / "data" / "store").mkdir(parents=True)
    junction = tmp_path / "data"
    real_lstat = os.lstat

    def lstat(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        return Junction(info) if os.fspath(path) == os.fspath(junction) else info

    monkeypatch.setattr(safe_store, "ANCHORED", False)  # the Windows path: each component by lstat
    monkeypatch.setattr(safe_store.os, "lstat", lstat)
    with pytest.raises(safe_store.UnsafeStore, match="data"):
        with safe_store.store_directory(tmp_path, ("data", "store"), create=create, private=create):
            pass
    assert os.listdir(tmp_path / "data" / "store") == []


NOT_ONE_NAME = ["", ".", "..", f"a{os.sep}b"] + ([f"a{os.altsep}b"] if os.altsep else [])


@pytest.mark.parametrize("part", NOT_ONE_NAME)
def test_store_directory_refuses_a_part_that_is_not_one_name(tmp_path, part):
    with pytest.raises(ValueError) as raised:
        with safe_store.store_directory(tmp_path, ("data", part), create=True, private=True):
            pass
    assert type(raised.value) is ValueError
    assert list(tmp_path.iterdir()) == []  # refused before the first component was opened or made


@pytest.mark.parametrize("name", NOT_ONE_NAME + [f"..{os.sep}escape.jsonl"])
def test_store_file_refuses_a_name_that_is_not_one_name(tmp_path, name):
    store = tmp_path / "data" / "store"
    with safe_store.store_directory(tmp_path, ("data", "store"), create=True, private=True) as directory:
        for where in (directory, store):  # the POSIX descriptor and the Windows path
            with pytest.raises(ValueError) as raised:
                with safe_store.store_file(where, name, CREATE, "r+b", private=True):
                    pass
            assert type(raised.value) is ValueError
    assert os.listdir(tmp_path / "data") == ["store"] and os.listdir(store) == [".gitignore"]


@pytest.mark.parametrize("status", ["refuted", "superseded", "blocked"])
def test_a_result_for_a_node_the_parent_settled_is_refused(store, status):
    plan(store)
    prepare(store)
    store.apply(TASK, [{"op": "update_node", "id": "map-code", "status": status, "reason": "Decided"}])
    with pytest.raises(WorkmapError) as raised:
        submit(store)
    assert raised.value.code == "node_not_open"
    assert store.load(TASK).nodes["map-code"]["status"] == status
    assert store.result_text(TASK, "map-code") is None


def test_a_result_for_a_closed_map_is_refused(store):
    plan(store)
    prepare(store)
    store.apply(TASK, [{"op": "close"}])
    with pytest.raises(WorkmapError) as raised:
        submit(store)
    assert raised.value.code == "node_not_open"


def test_a_worker_may_still_submit_after_its_invalid_results(store):
    plan(store)
    prepare(store)
    for _ in range(INVALID_LIMIT):
        store.record_invalid(TASK, "map-code", contract_revision=FIRST, codes=["string_too_long"])
    assert store.load(TASK).nodes["map-code"]["status"] == "needs_attention"
    assert submit(store)["status"] == "validated"


def test_a_needs_attention_node_refuses_another_contracts_result(store):
    plan(store)
    prepare(store)
    for _ in range(INVALID_LIMIT):
        store.record_invalid(TASK, "map-code", contract_revision=FIRST, codes=["string_too_long"])
    with pytest.raises(WorkmapError) as raised:
        submit(store, revision=SECOND)
    assert raised.value.code == "node_not_open"


def test_a_result_for_an_accepted_node_is_refused(store):
    plan(store)
    prepare(store)
    submit(store)
    store.apply(TASK, [{"op": "update_node", "id": "map-code", "status": "accepted"}])
    with pytest.raises(WorkmapError) as raised:
        submit(store, revision=SECOND)
    assert raised.value.code == "node_not_open"


ATTENTION = {**OUTCOME, "status": "needs_attention", "level": "V0"}


def test_a_contract_that_already_has_a_result_is_not_prepared_again(store):
    plan(store)
    prepare(store)
    store.record_result(TASK, "map-code", contract_revision=FIRST, attempt_id="1", text="partial",
                        sha256="b" * 64, outcome=ATTENTION, handback="line")
    assert store.load(TASK).nodes["map-code"]["status"] == "needs_attention"
    for call in (lambda: store.check_preparable(TASK, "map-code", [], contract_revision=FIRST),
                 lambda: prepare(store)):
        with pytest.raises(WorkmapError) as raised:
            call()
        assert raised.value.code == "attempt_reused" and "new attempt_id" in str(raised.value)
    assert store.load(TASK).nodes["map-code"]["status"] == "needs_attention"
    assert prepare(store, revision=SECOND, attempt="2") > 0  # a new attempt is still welcome


def test_a_contract_that_reached_the_invalid_limit_is_not_prepared_again(store):
    plan(store)
    prepare(store)
    for _ in range(INVALID_LIMIT):
        store.record_invalid(TASK, "map-code", contract_revision=FIRST, codes=["string_too_long"])
    with pytest.raises(WorkmapError) as raised:
        prepare(store)
    assert raised.value.code == "attempt_reused"
    assert store.load(TASK).nodes["map-code"]["status"] == "needs_attention"


def test_preparing_the_same_revision_again_without_a_result_stays_accepted(store):
    plan(store)
    prepare(store)
    store.record_invalid(TASK, "map-code", contract_revision=FIRST, codes=["string_too_long"])
    assert prepare(store) > 0
    assert store.load(TASK).nodes["map-code"]["status"] == "prepared"


@pytest.mark.parametrize("settle, code", [
    ("result", "already_submitted"),        # the parent validates the worker's one-line hand-back
    ("attention_result", "already_submitted"),
    ("reprepared", "stale_contract"),
    ("closed", "node_not_open"),
    ("refuted", "node_not_open"),
])
def test_an_invalid_submission_for_a_node_that_no_longer_takes_one_writes_nothing(store, tmp_path, settle, code):
    plan(store)
    prepare(store)
    if settle == "result":
        submit(store)
    elif settle == "attention_result":
        store.record_result(TASK, "map-code", contract_revision=FIRST, attempt_id="1", text="partial",
                            sha256="b" * 64, outcome=ATTENTION, handback="line")
    elif settle == "reprepared":
        prepare(store, revision=SECOND, attempt="2")
    elif settle == "closed":
        store.apply(TASK, [{"op": "close"}])
    else:
        store.apply(TASK, [{"op": "update_node", "id": "map-code", "status": "refuted", "reason": "Decided"}])
    before = map_file(tmp_path).read_bytes()
    with pytest.raises(WorkmapError) as raised:
        store.record_invalid(TASK, "map-code", contract_revision=FIRST, codes=["format"])
    assert raised.value.code == code
    assert map_file(tmp_path).read_bytes() == before
    assert store.load(TASK).invalid.get(FIRST, 0) == 0


def verify(store, findings, *, verifier="check", pins=None, revision=SECOND):
    """Prepare a verifier of map-code (pinning map-code's first result unless told otherwise); submit its review."""
    store.apply(TASK, [{"op": "add_node", "id": verifier, "title": "Check the map"},
                       {"op": "add_edge", "from": verifier, "to": "map-code", "type": "verifies"}])
    store.record_prepared(TASK, verifier, contract_revision=revision, attempt_id="1", complexity=None,
                          inputs={"map-code": "b" * 64} if pins is None else pins)
    return store.record_result(TASK, verifier, contract_revision=revision, attempt_id="1", text="review",
                               sha256="e" * 64, handback="line",
                               outcome={**OUTCOME, "summary": "Reviewed", "findings": findings})


def test_a_clean_verifier_result_verifies_its_target(store):
    plan(store)
    prepare(store)
    submit(store)
    verify(store, {"low": 2})
    node = store.load(TASK).nodes["map-code"]
    assert node["status"] == "verified" and node["level"] == "V3"


def test_a_high_finding_disputes_the_target_and_staleness_follows(store):
    plan(store)
    prepare(store)
    submit(store)
    verify(store, {"high": 1})
    workmap = store.load(TASK)
    assert workmap.nodes["map-code"]["status"] == "disputed" and workmap.nodes["map-code"]["level"] == "V2"
    assert is_stale(workmap, workmap.nodes["write"])


def test_a_critical_node_readies_its_dependants_only_once_verified(store):
    plan(store)
    store.apply(TASK, [{"op": "update_node", "id": "map-code", "risk": "critical"}])
    prepare(store)
    submit(store)
    assert not is_ready(store.load(TASK), store.load(TASK).nodes["write"])
    verify(store, {})
    assert is_ready(store.load(TASK), store.load(TASK).nodes["write"])


def target(store):
    return store.load(TASK).nodes["map-code"]


def test_a_refuted_target_becomes_disputed_whichever_severity_is_critical_or_high(store):
    plan(store)
    prepare(store)
    submit(store)
    verify(store, {"critical": 1})
    assert target(store)["status"] == "disputed"


def test_an_accepted_target_stays_accepted_at_v4(store):
    plan(store)
    prepare(store)
    submit(store)
    store.apply(TASK, [{"op": "update_node", "id": "map-code", "status": "accepted", "level": "V4"}])
    verify(store, {"high": 1})
    assert target(store)["status"] == "accepted" and target(store)["level"] == "V4"
    verify(store, {}, verifier="clean", revision="9" * 64)
    assert target(store)["status"] == "accepted" and target(store)["level"] == "V4"


def test_a_target_that_needs_attention_gets_no_verdict(store):
    plan(store)
    prepare(store)
    store.record_result(TASK, "map-code", contract_revision=FIRST, attempt_id="1", text="partial",
                        sha256="b" * 64, outcome=ATTENTION, handback="line")
    verify(store, {})
    assert target(store)["status"] == "needs_attention"
    assert store.load(TASK).nodes["check"]["status"] == "validated"


def test_a_target_re_attempted_after_the_verifier_was_prepared_gets_no_verdict(store):
    plan(store)
    prepare(store)
    submit(store)
    store.apply(TASK, [{"op": "add_node", "id": "late", "title": "Late check"},
                       {"op": "add_edge", "from": "late", "to": "map-code", "type": "verifies"}])
    store.record_prepared(TASK, "late", contract_revision="f" * 64, attempt_id="1", complexity=None,
                          inputs={"map-code": "b" * 64})
    verify(store, {"high": 1})  # a first verifier disputes the target, which takes a new attempt
    assert target(store)["status"] == "disputed"
    prepare(store, revision="c" * 64, attempt="2")
    submit(store, revision="c" * 64, sha="c" * 64, text="remapped")
    assert target(store)["status"] == "validated"
    store.record_result(TASK, "late", contract_revision="f" * 64, attempt_id="1", text="review",
                        sha256="9" * 64, handback="line",
                        outcome={**OUTCOME, "summary": "Reviewed", "findings": {"high": 1}})
    assert target(store)["status"] == "validated"  # the dispute would have certified text it never saw
    assert store.load(TASK).nodes["late"]["status"] == "validated"


def test_a_verifier_that_did_not_pin_its_target_gets_no_verdict(store):
    plan(store)
    prepare(store)
    submit(store)
    verify(store, {"high": 1}, pins={})
    assert target(store)["status"] == "validated"
    verify(store, {}, verifier="other", pins={"write": "b" * 64}, revision="9" * 64)
    assert target(store)["status"] == "validated"


def test_a_verifier_pinning_the_current_result_still_verifies_a_validated_target(store):
    plan(store)
    prepare(store)
    submit(store)
    verify(store, {})
    assert target(store)["status"] == "verified"


def test_a_generic_result_gives_no_verdict_through_a_verifies_edge_added_later(store):
    plan(store)
    prepare(store)
    submit(store)
    store.apply(TASK, [{"op": "add_node", "id": "generic", "title": "A generic stage"}])
    store.record_prepared(TASK, "generic", contract_revision=SECOND, attempt_id="1", complexity=None,
                          inputs={"map-code": "b" * 64})
    store.apply(TASK, [{"op": "add_edge", "from": "generic", "to": "map-code", "type": "verifies"}])
    store.record_result(TASK, "generic", contract_revision=SECOND, attempt_id="1", text="generic",
                        sha256="e" * 64, handback="line", outcome=OUTCOME)  # findings None: not a review
    assert target(store)["status"] == "validated" and target(store)["level"] == "V2"
    assert store.load(TASK).nodes["generic"]["status"] == "validated"


REVIEW = {"contract_revision": SECOND, "attempt_id": "1", "text": "review", "sha256": "e" * 64,
          "handback": "line", "outcome": {**OUTCOME, "summary": "Reviewed", "findings": {"low": 1}}}


@pytest.mark.parametrize("after", ["submit_again", "prepare_again", "invalid"])
def test_a_torn_result_and_verdict_commit_leaves_the_contract_open(store, tmp_path, after):
    plan(store)
    prepare(store)
    submit(store)
    store.apply(TASK, [{"op": "add_node", "id": "check", "title": "Check the map"},
                       {"op": "add_edge", "from": "check", "to": "map-code", "type": "verifies"}])
    store.record_prepared(TASK, "check", contract_revision=SECOND, attempt_id="1", complexity=None,
                          inputs={"map-code": "b" * 64})
    store.record_result(TASK, "check", **REVIEW)
    raw = map_file(tmp_path).read_bytes()
    map_file(tmp_path).write_bytes(raw[:raw.rindex(b"\n", 0, len(raw) - 1) + 20])  # the verdict line torn
    assert store.load(TASK).nodes["check"]["status"] == "prepared" and target(store)["status"] == "validated"
    # Neither the result nor the verdict applied, so the contract has no result yet.
    if after == "submit_again":
        receipt = store.record_result(TASK, "check", **REVIEW)
        assert receipt["duplicate"] is False and receipt["status"] == "validated"
        assert store.load(TASK).nodes["check"]["status"] == "validated" and target(store)["status"] == "verified"
    elif after == "prepare_again":
        assert store.check_preparable(TASK, "check", ["map-code"], contract_revision=SECOND) == [
            ("map-code", "mapped code")]
        assert store.record_prepared(TASK, "check", contract_revision=SECOND, attempt_id="1", complexity=None,
                                     inputs={"map-code": "b" * 64}) > 0
    else:
        assert store.record_invalid(TASK, "check", contract_revision=SECOND, codes=["format"]) == 1


@pytest.mark.parametrize("value", [["add_node"], {"op": "add_node"}, 3, None])
def test_an_operation_whose_op_is_not_a_string_is_an_invalid_request(store, value):
    plan(store)
    with pytest.raises(WorkmapError) as raised:
        store.apply(TASK, [{"op": value, "id": "extra", "title": "X"}])
    assert raised.value.code == "invalid_request" and "ops[0]" in str(raised.value)


OPENED = [{"op": "open", "task_id": TASK, "rev": 1},
          {"op": "add_node", "id": "a", "kind": "stage", "title": "A", "summary": "", "risk": "routine", "rev": 2}]


@pytest.mark.parametrize("damaged", [
    {"op": "add_edge", "from": "a", "to": "ghost", "type": "depends_on", "rev": 3},
    {"op": "add_edge", "from": "ghost", "to": "a", "type": "verifies", "rev": 3},
    {"op": "add_edge", "from": ["a"], "to": "a", "type": "depends_on", "rev": 3},
])
def test_a_committed_edge_to_a_missing_node_is_skipped_whole(damaged):
    workmap = store_module.fold(OPENED + [damaged])
    assert workmap.edges == [] and workmap.revision == 2
    assert workmap.nodes["a"]["changed"] == 2
    assert render_map(workmap).splitlines()[1] == "[planned V0] a: A"


NODE_B = {"op": "add_node", "id": "b", "kind": "stage", "title": "B", "summary": "", "risk": "routine"}


def test_a_second_committed_add_node_does_not_replace_the_node():
    prepared = {"op": "prepared", "id": "a", "contract_revision": FIRST, "attempt_id": "1", "complexity": None,
                "rev": 3}
    again = {"op": "add_node", "id": "a", "kind": "stage", "title": "Again", "summary": "", "risk": "routine",
             "rev": 4}
    workmap = store_module.fold(OPENED + [prepared, again])
    assert workmap.revision == 3
    assert workmap.nodes["a"]["title"] == "A" and workmap.nodes["a"]["status"] == "prepared"


@pytest.mark.parametrize("lost", [0, 1, 2])
def test_a_commit_that_lost_a_line_applies_none_of_its_events(lost):
    commit = [{**NODE_B, "id": f"n{rev}", "rev": rev, "commit": 5} for rev in (3, 4, 5)]
    assert set(store_module.fold(OPENED + commit).nodes) == {"a", "n3", "n4", "n5"}
    kept = [event for index, event in enumerate(commit) if index != lost]
    workmap = store_module.fold(OPENED + kept)
    assert workmap.revision == 2 and set(workmap.nodes) == {"a"}


def test_a_commit_with_an_event_that_fails_applies_none_of_its_events():
    commit = [{**NODE_B, "rev": 3, "commit": 4},
              {"op": "add_edge", "from": "b", "to": "ghost", "type": "depends_on", "rev": 4, "commit": 4}]
    workmap = store_module.fold(OPENED + commit)
    assert workmap.revision == 2 and set(workmap.nodes) == {"a"} and workmap.edges == []
    # The writer reuses the discarded revisions, and that commit applies.
    after = store_module.fold(OPENED + commit + [{**NODE_B, "rev": 3}])
    assert after.revision == 3 and set(after.nodes) == {"a", "b"}


def test_an_invalid_count_below_the_limit_marks_its_node_changed():
    prepared = {"op": "prepared", "id": "a", "contract_revision": FIRST, "attempt_id": "1", "complexity": None,
                "rev": 3}
    invalid = {"op": "invalid", "id": "a", "contract_revision": FIRST, "codes": ["x"], "status": None, "rev": 4}
    workmap = store_module.fold(OPENED + [prepared, invalid])
    assert workmap.nodes["a"]["changed"] == 4 and workmap.invalid == {FIRST: 1}


def test_an_invalid_count_for_a_missing_node_is_skipped():
    for status in (None, "needs_attention"):
        workmap = store_module.fold(OPENED + [{"op": "invalid", "id": "ghost", "contract_revision": FIRST,
                                               "codes": ["x"], "status": status, "rev": 3}])
        assert workmap.invalid == {} and workmap.invalid_codes == {} and workmap.revision == 2


def test_a_result_event_without_its_hash_changes_nothing():
    prepared = {"op": "prepared", "id": "a", "contract_revision": FIRST, "attempt_id": "1", "complexity": None,
                "rev": 3}
    damaged = {"op": "result", "id": "a", "status": "validated", "level": "V2", "summary": "Done",
               "checks": None, "findings": None, "limitations": 0, "handback": "line", "rev": 4}
    node = store_module.fold(OPENED + [prepared, damaged]).nodes["a"]
    assert node["status"] == "prepared" and node["summary"] == "" and "result_sha256" not in node["refs"]


def test_the_result_text_is_synced_before_the_map_names_it(store, monkeypatch):
    plan(store)
    prepare(store)
    order = []
    append_line = safe_store.append_line
    fsync = store_module.os.fsync

    def recording_append(handle, line, max_bytes, path):
        order.append(("append", path.name))
        append_line(handle, line, max_bytes, path)

    def recording_fsync(descriptor):
        order.append(("fsync", os.fstat(descriptor).st_size))
        fsync(descriptor)

    monkeypatch.setattr(store_module.safe_store, "append_line", recording_append)
    monkeypatch.setattr(store_module.os, "fsync", recording_fsync)
    submit(store)
    results, events = f"{map_key(TASK)}.results.jsonl", f"{map_key(TASK)}.jsonl"
    assert [step[0] if step[0] == "fsync" else step for step in order] == [
        ("append", results), "fsync", ("append", events)]
    assert order[1][1] == (store.directory / results).stat().st_size


def windows_lstat(**fields):
    """What Windows' lstat answers for a directory, with the reparse fields it sets."""
    return SimpleNamespace(st_mode=stat.S_IFDIR | 0o700, **fields)


REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
SYMLINK_TAG = getattr(stat, "IO_REPARSE_TAG_SYMLINK", 0xA000000C)
MOUNT_POINT_TAG = getattr(stat, "IO_REPARSE_TAG_MOUNT_POINT", 0xA0000003)
# A tag without the name-surrogate bit, like the one a synced folder's placeholder carries.
PLACEHOLDER_TAG = 0x9000001A


@pytest.mark.parametrize("info,plain", [
    (windows_lstat(), True),
    (windows_lstat(st_file_attributes=0x10), True),
    (windows_lstat(st_file_attributes=0x10 | REPARSE, st_reparse_tag=PLACEHOLDER_TAG), True),
    (windows_lstat(st_file_attributes=0x10 | REPARSE, st_reparse_tag=SYMLINK_TAG), False),
    (windows_lstat(st_file_attributes=0x10 | REPARSE, st_reparse_tag=MOUNT_POINT_TAG), False),
    (windows_lstat(st_file_attributes=0x10 | REPARSE, st_reparse_tag=0xA0000099), False),  # another surrogate
    (windows_lstat(st_file_attributes=0x10 | REPARSE), False),  # no tag to tell: refused
    (windows_lstat(st_file_attributes=0x10 | REPARSE, st_reparse_tag=0), False),
    (SimpleNamespace(st_mode=stat.S_IFLNK | 0o777), False),
])
def test_only_a_reparse_point_standing_for_another_path_is_refused(info, plain):
    assert safe_store.plain_directory(info) is plain


def test_a_placeholder_directory_on_the_path_checked_by_name_is_a_store(tmp_path, monkeypatch):
    real_lstat = os.lstat
    placeholder = tmp_path / "data"

    def lstat(path, *args, **kwargs):
        info = real_lstat(path, *args, **kwargs)
        if os.fspath(path) != os.fspath(placeholder):
            return info
        return SimpleNamespace(st_mode=info.st_mode, st_uid=info.st_uid, st_ino=info.st_ino, st_dev=info.st_dev,
                               st_file_attributes=REPARSE, st_reparse_tag=PLACEHOLDER_TAG)

    monkeypatch.setattr(safe_store, "ANCHORED", False)  # the Windows path: each component by lstat
    monkeypatch.setattr(safe_store.os, "lstat", lstat)
    with safe_store.store_directory(tmp_path, ("data", "store"), create=True, private=False) as directory:
        assert directory == tmp_path / "data" / "store"
