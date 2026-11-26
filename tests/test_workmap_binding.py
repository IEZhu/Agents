"""Binding packages to work-map nodes, and the submission a worker makes itself."""
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

import src.server as server
from src.engine import config as engine_config
from src.workmap.store import WorkmapStore, map_key
from tests.test_subagent import request, worker_tree  # noqa: F401  (fixture)
from tests.test_subagent_artifacts import artifact_worker_tree  # noqa: F401  (fixture)
from tests.test_subagent_exchange import call, text_result

TASK = "task-1"


@pytest.fixture
def served(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTS_CLIENT_REPO_ROOT", str(tmp_path))
    monkeypatch.delenv("AGENTS_TRANSPORT", raising=False)
    engine_config._reset_client_repo_root_cache()
    WorkmapStore(tmp_path).apply(TASK, [
        {"op": "add_node", "id": "goal", "kind": "goal", "title": "Ship it"},
        {"op": "add_node", "id": "map-code", "title": "Map the code"},
        {"op": "add_node", "id": "write", "title": "Write it"},
        {"op": "add_edge", "from": "write", "to": "map-code", "type": "depends_on"}])
    yield tmp_path
    engine_config._reset_client_repo_root_cache()


@pytest.mark.asyncio
async def test_a_bound_package_marks_its_node_prepared_and_binds_the_workspace(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code"))
    assert receipt["status"] == "prepared"
    node = WorkmapStore(served).load(TASK).nodes["map-code"]
    assert node["status"] == "prepared" and node["refs"]["contract_revision"] == receipt["contract_revision"]
    stored = json.loads((server.subagent_exchange.exchange_dir()
                         / f"{receipt['contract_revision']}.contract.json").read_text())
    assert stored["workmap"] == {"node": "map-code", "root": str(served)}


@pytest.mark.asyncio
async def test_a_goal_node_is_refused_before_any_package_is_written(worker_tree, served):
    refused = await call("get_subagent_context", request(workmap_node="goal"))
    assert refused["status"] == "error" and refused["errors"][0]["code"] == "node_not_preparable"


@pytest.mark.asyncio
async def test_workmap_inputs_need_a_node_and_a_settled_input(worker_tree, served):
    without_node = await call("get_subagent_context", request(workmap_inputs=["map-code"]))
    assert without_node["errors"][0]["code"] == "invalid_request"
    unsettled = await call("get_subagent_context", request(workmap_node="write", workmap_inputs=["map-code"]))
    assert unsettled["errors"][0]["code"] == "input_not_ready"


@pytest.mark.asyncio
async def test_a_settled_input_is_pinned_as_an_artifact(artifact_worker_tree, served):
    store = WorkmapStore(served)
    store.record_prepared(TASK, "map-code", contract_revision="a" * 64, attempt_id="1", complexity=None)
    store.record_result(TASK, "map-code", contract_revision="a" * 64, attempt_id="1", text="mapped code",
                        sha256="b" * 64, handback="line",
                        outcome={"status": "validated", "level": "V2", "summary": "Mapped",
                                 "checks": {"passed": 1, "failed": 0, "not_run": 0}, "findings": None,
                                 "limitations": 0})
    receipt = await call("get_subagent_context", request(workmap_node="write", workmap_inputs=["map-code"]))
    assert receipt["status"] == "prepared"
    package = open(receipt["context_path"], encoding="utf-8").read()
    assert "### Pinned artifacts" in package and "Stored result of work-map node map-code" in package


def text_result_for(receipt):
    """A completed generic text result for a receipt: summary "Checked the log path.", one passed check."""
    package = SimpleNamespace(task_id=receipt["task_id"], attempt_id=receipt["attempt_id"],
                              contract_revision=receipt["contract_revision"],
                              contract=SimpleNamespace(result_type="generic"))
    return text_result(package)


@pytest.mark.asyncio
async def test_a_worker_submission_is_recorded_on_its_node(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code"))
    package_text = open(receipt["context_path"], encoding="utf-8").read()
    assert "submitted task-1/map-code" in package_text
    assert "Submit the result and reply exactly as it says." in receipt["launch"]
    result = text_result_for(receipt)
    validated = await call("workmap_submit", {"contract_revision": receipt["contract_revision"],
                                                        "result": result})
    assert validated["valid"] is True
    assert validated["workmap"] == {"task_id": TASK, "node": "map-code", "status": "validated",
                                    "level": "V2", "revision": 7, "duplicate": False}
    node = WorkmapStore(served).load(TASK).nodes["map-code"]
    assert node["summary"] == "Checked the log path." and node["handback"] == "line"
    assert WorkmapStore(served).result_text(TASK, "map-code") == result


@pytest.mark.asyncio
async def test_a_different_second_submission_is_refused(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code"))
    first = text_result_for(receipt)
    await call("workmap_submit", {"contract_revision": receipt["contract_revision"], "result": first})
    second = first.replace("Checked the log path.", "Checked another path.")
    refused = await call("workmap_submit", {"contract_revision": receipt["contract_revision"],
                                                      "result": second})
    assert refused["valid"] is True and refused["errors"] == []
    assert refused["workmap"]["refused"] == "already_submitted"
    assert refused["workmap"]["task_id"] == TASK and refused["workmap"]["node"] == "map-code"
    assert refused["workmap"]["status"] == "validated"  # what the map holds, not what was refused


@pytest.mark.asyncio
async def test_a_result_for_a_superseded_attempt_is_refused_as_stale_but_stays_valid(worker_tree, served):
    first = await call("get_subagent_context", request(workmap_node="map-code"))
    await call("get_subagent_context", request(workmap_node="map-code", attempt_id="2"))
    stale = await call("workmap_submit", {"contract_revision": first["contract_revision"],
                                                    "result": text_result_for(first)})
    assert stale["valid"] is True and stale["workmap"]["refused"] == "stale_contract"
    assert WorkmapStore(served).result_text(TASK, "map-code") is None


@pytest.mark.asyncio
async def test_a_result_for_a_node_the_parent_settled_is_refused_but_stays_valid(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code"))
    WorkmapStore(served).apply(TASK, [{"op": "update_node", "id": "map-code", "status": "refuted",
                                       "reason": "Decided"}])
    refused = await call("workmap_submit", {"contract_revision": receipt["contract_revision"],
                                                      "result": text_result_for(receipt)})
    assert refused["valid"] is True and refused["workmap"]["refused"] == "node_not_open"
    assert refused["workmap"]["status"] == "refuted"


@pytest.mark.asyncio
async def test_a_recorded_submission_carries_no_refusal(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code"))
    validated = await call("workmap_submit", {"contract_revision": receipt["contract_revision"],
                                                        "result": text_result_for(receipt)})
    assert "refused" not in validated["workmap"]


@pytest.mark.asyncio
async def test_the_package_states_the_submission_rules(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code"))
    package = " ".join(open(receipt["context_path"], encoding="utf-8").read().split())
    revision = receipt["contract_revision"]
    for sentence in ("Submit only your finished result: the first valid submission is final.",
                     f'workmap_submit(contract_revision="{revision}", result=<your complete result text>)',
                     "If workmap.refused is already_submitted, stale_contract or node_not_open, do not "
                     "resubmit; reply with one line: not recorded task-1/map-code <that code>.",
                     "If it is workmap_unavailable, store_full, unsafe_store, unknown_map, key_collision or "
                     "any other code, submit the same result once more; if it is refused again, reply with "
                     "the full result, so the parent can validate it by result_ref.",
                     "If the call cannot reach the server, wait about a minute and try once more; if it "
                     "still fails, reply with the full result."):
        assert sentence in package, sentence
    assert "validate_subagent_result(" not in package
    unbound = await call("get_subagent_context", request())
    text = open(unbound["context_path"], encoding="utf-8").read()
    assert "If it answers contract_unknown, reply with your result unchecked." in text
    assert f'validate_subagent_result(contract_revision="{unbound["contract_revision"]}"' in text
    assert "workmap_submit" not in text


@pytest.mark.asyncio
async def test_an_invalid_submission_is_counted(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code"))
    invalid = await call("workmap_submit", {"contract_revision": receipt["contract_revision"],
                                                      "result": "not a result"})
    assert invalid["valid"] is False and "workmap" not in invalid
    assert WorkmapStore(served).load(TASK).invalid == {receipt["contract_revision"]: 1}


@pytest.mark.asyncio
async def test_validating_the_one_line_hand_back_of_a_recorded_node_leaves_the_node_alone(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code"))
    validated = await call("workmap_submit", {"contract_revision": receipt["contract_revision"],
                                                        "result": text_result_for(receipt)})
    line = f"submitted {TASK}/map-code {validated['result_sha256'][:12]} validated"
    invalid = await call("workmap_submit", {"contract_revision": receipt["contract_revision"],
                                                      "result": line})
    assert invalid["valid"] is False and "workmap" not in invalid
    workmap = WorkmapStore(served).load(TASK)
    assert workmap.invalid == {} and workmap.nodes["map-code"]["status"] == "validated"


@pytest.mark.asyncio
async def test_an_inline_contract_never_records_on_a_map(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code"))
    stored = server.subagent_exchange.load_contract(receipt["contract_revision"])
    validated = await call("validate_subagent_result", {"contract": stored, "result": text_result_for(receipt)})
    assert validated["valid"] is True and "workmap" not in validated
    refused = await call("workmap_submit", {"contract": stored, "result": text_result_for(receipt)})
    assert refused["valid"] is False and refused["errors"][0]["code"] == "extra_forbidden"
    node = WorkmapStore(served).load(TASK).nodes["map-code"]
    assert node["status"] == "prepared"
    assert WorkmapStore(served).result_text(TASK, "map-code") is None


def map_bytes(served):
    return {path.name: path.read_bytes() for path in (served / "data" / "workmap").iterdir()}


@pytest.mark.asyncio
async def test_validating_a_bound_contract_records_nothing(worker_tree, served, tmp_path):
    receipt = await call("get_subagent_context", request(workmap_node="map-code"))
    before = map_bytes(served)
    result_file = tmp_path / "handback.md"
    result_file.write_bytes(text_result_for(receipt).encode("utf-8"))
    for arguments in ({"result": text_result_for(receipt)}, {"result_ref": str(result_file)},
                      {"result": "not a result"}):
        response = await call("validate_subagent_result", {"contract_revision": receipt["contract_revision"],
                                                           **arguments})
        assert "workmap" not in response
    assert map_bytes(served) == before
    workmap = WorkmapStore(served).load(TASK)
    assert workmap.nodes["map-code"]["status"] == "prepared" and workmap.invalid == {}


@pytest.mark.asyncio
async def test_workmap_submit_records_the_first_valid_submission_and_refuses_a_second(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code"))
    first = text_result_for(receipt)
    recorded = await call("workmap_submit", {"contract_revision": receipt["contract_revision"], "result": first})
    assert recorded["valid"] is True and recorded["workmap"]["status"] == "validated"
    assert "refused" not in recorded["workmap"] and recorded["workmap"]["duplicate"] is False
    again = await call("workmap_submit", {"contract_revision": receipt["contract_revision"], "result": first})
    assert again["workmap"]["duplicate"] is True
    second = first.replace("Checked the log path.", "Checked another path.")
    refused = await call("workmap_submit", {"contract_revision": receipt["contract_revision"], "result": second})
    assert refused["valid"] is True and refused["workmap"]["refused"] == "already_submitted"
    assert WorkmapStore(served).result_text(TASK, "map-code") == first


@pytest.mark.asyncio
async def test_workmap_submit_refuses_an_unbound_contract(worker_tree, served):
    receipt = await call("get_subagent_context", request())
    refused = await call("workmap_submit", {"contract_revision": receipt["contract_revision"],
                                            "result": text_result_for(receipt)})
    assert refused["valid"] is False and refused["errors"][0]["code"] == "workmap_unbound"
    unknown = await call("workmap_submit", {"contract_revision": "f" * 64, "result": "x"})
    assert unknown["valid"] is False and unknown["errors"][0]["code"] == "contract_unknown"


@pytest.mark.asyncio
async def test_a_bound_package_keeps_the_callers_own_manifest(artifact_worker_tree, served, monkeypatch):
    from tests.test_subagent_artifacts import manifest
    seen = []

    async def verified(ctx, value):
        seen.append(value)
        return value

    monkeypatch.setattr(server.artifact_tools, "verify_preparation", verified)
    pinned = manifest(task_id=TASK)
    receipt = await call("get_subagent_context", request(workmap_node="map-code", artifact_manifest=pinned))
    assert receipt["status"] == "prepared" and seen == [pinned]
    package = open(receipt["context_path"], encoding="utf-8").read()
    assert "### Pinned artifacts" in package
    stored = server.subagent_exchange.load_contract(receipt["contract_revision"])
    assert stored["artifact_manifest"]["manifest_id"] == pinned["manifest_id"]


@pytest.mark.asyncio
async def test_workmap_inputs_with_a_manifest_are_still_refused(artifact_worker_tree, served):
    from tests.test_subagent_artifacts import manifest
    refused = await call("get_subagent_context", request(workmap_node="write", workmap_inputs=["map-code"],
                                                         artifact_manifest=manifest(task_id=TASK)))
    assert refused["status"] == "error" and refused["errors"][0]["code"] == "invalid_request"


@pytest.mark.asyncio
@pytest.mark.parametrize("delivery", ["file", "inline"])
async def test_a_bound_package_that_cannot_be_stored_is_refused_for_either_delivery(worker_tree, served,
                                                                                    monkeypatch, delivery):
    def unwritable(package):
        raise OSError("disk full")

    monkeypatch.setattr(server.subagent_exchange, "store", unwritable)
    refused = await call("get_subagent_context", request(workmap_node="map-code", delivery=delivery))
    assert refused["status"] == "error" and refused["errors"][0]["code"] == "exchange_unwritable"
    assert WorkmapStore(served).load(TASK).nodes["map-code"]["status"] == "planned"


@pytest.mark.asyncio
async def test_an_unbound_inline_package_still_falls_back_when_the_store_fails(worker_tree, served, monkeypatch):
    def unwritable(package):
        raise OSError("disk full")

    monkeypatch.setattr(server.subagent_exchange, "store", unwritable)
    package = await call("get_subagent_context", request(delivery="inline"))
    assert package["status"] == "prepared" and "contract" in package


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["record_result", "record_invalid"])
async def test_an_unreadable_map_is_reported_not_raised(worker_tree, served, monkeypatch, method):
    receipt = await call("get_subagent_context", request(workmap_node="map-code"))

    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(WorkmapStore, method, broken)
    result = text_result_for(receipt) if method == "record_result" else "not a result"
    response = await call("workmap_submit", {"contract_revision": receipt["contract_revision"],
                                                       "result": result})
    if method == "record_result":
        assert response["valid"] is True and response["errors"] == []
        assert response["workmap"]["refused"] == "workmap_unavailable"
    else:
        assert response["valid"] is False and "workmap" not in response
        assert response["errors"] and "workmap_unavailable" not in {item["code"] for item in response["errors"]}


@pytest.mark.asyncio
async def test_identical_bound_arguments_cannot_reopen_an_attempt_that_has_a_result(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code"))
    partial = text_result_for(receipt).replace("status: completed", "status: partial") + (
        "\n\n## remaining_work\n- Fix the second logger")
    recorded = await call("workmap_submit", {"contract_revision": receipt["contract_revision"],
                                                       "result": partial})
    assert recorded["valid"] is True and recorded["workmap"]["status"] == "needs_attention"
    again = await call("get_subagent_context", request(workmap_node="map-code"))
    assert again["status"] == "error" and again["errors"][0]["code"] == "attempt_reused"
    assert WorkmapStore(served).load(TASK).nodes["map-code"]["status"] == "needs_attention"
    fresh = await call("get_subagent_context", request(workmap_node="map-code", attempt_id="2"))
    assert fresh["status"] == "prepared"


@pytest.mark.asyncio
async def test_identical_bound_arguments_cannot_reopen_an_attempt_at_the_invalid_limit(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code"))
    for _ in range(5):
        await call("workmap_submit", {"contract_revision": receipt["contract_revision"],
                                                "result": "not a result"})
    again = await call("get_subagent_context", request(workmap_node="map-code"))
    assert again["status"] == "error" and again["errors"][0]["code"] == "attempt_reused"


@pytest.mark.asyncio
async def test_identical_bound_arguments_while_prepared_are_still_accepted(worker_tree, served):
    first = await call("get_subagent_context", request(workmap_node="map-code"))
    again = await call("get_subagent_context", request(workmap_node="map-code"))
    assert again["status"] == "prepared" and again["contract_revision"] == first["contract_revision"]


def node_of(served, name="map-code"):
    return WorkmapStore(served).load(TASK).nodes[name]


async def submit_text(receipt, text):
    return await call("workmap_submit", {"contract_revision": receipt["contract_revision"],
                                                   "result": text})


@pytest.mark.asyncio
async def test_a_partial_result_records_its_status_and_the_next_tier(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code", complexity="low",
                                                         client="claude"))
    text = text_result_for(receipt).replace("status: completed", "status: partial") + (
        "\n\n## remaining_work\n- Fix the second logger")
    recorded = await submit_text(receipt, text)
    assert recorded["valid"] is True and recorded["workmap"]["status"] == "needs_attention"
    node = node_of(served)
    assert node["result_status"] == "partial" and node["question"] is None
    assert node["escalation"] == {"complexity": "standard", "tier": "standard", "reason": "status_partial"}


@pytest.mark.asyncio
async def test_a_failed_result_records_its_status_and_the_next_tier(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code", complexity="low",
                                                         client="claude"))
    text = text_result_for(receipt).replace("status: completed", "status: failed") + (
        "\n\n## limitations\n- The file could not be read")
    await submit_text(receipt, text)
    node = node_of(served)
    assert node["status"] == "needs_attention" and node["result_status"] == "failed"
    assert node["escalation"] == {"complexity": "standard", "tier": "standard", "reason": "status_failed"}


@pytest.mark.asyncio
async def test_an_input_required_result_records_its_question_cut_to_300(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code"))
    text = text_result_for(receipt).replace("status: completed", "status: input_required")
    text = text.replace("\n\n## summary", "\nquestion: " + "Which branch? " * 60 + "\n\n## summary", 1)
    await submit_text(receipt, text)
    node = node_of(served)
    assert node["result_status"] == "input_required" and node["status"] == "needs_attention"
    assert node["question"].startswith("Which branch?") and len(node["question"]) == 300
    assert node["question"].endswith("…") and node["escalation"] is None


@pytest.mark.asyncio
async def test_a_failed_check_records_the_next_tier(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code", complexity="low",
                                                         client="claude"))
    text = text_result_for(receipt).replace("- passed: Read the code.", "- failed: Read the code.")
    recorded = await submit_text(receipt, text)
    assert recorded["workmap"]["status"] == "needs_attention"
    node = node_of(served)
    assert node["result_status"] == "completed" and node["checks"]["failed"] == 1
    assert node["escalation"] == {"complexity": "standard", "tier": "standard", "reason": "check_failed"}


@pytest.mark.asyncio
async def test_a_result_without_a_passed_check_is_validated_at_v1(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code"))
    text = text_result_for(receipt).replace("- passed: Read the code.", "- not_run: Read the code.")
    recorded = await submit_text(receipt, text)
    assert recorded["workmap"]["status"] == "validated" and recorded["workmap"]["level"] == "V1"
    node = node_of(served)
    assert node["result_status"] == "completed" and node["escalation"] is None


@pytest.mark.asyncio
async def test_a_result_read_from_a_file_is_recorded_as_a_full_handback(worker_tree, served, tmp_path):
    receipt = await call("get_subagent_context", request(workmap_node="map-code"))
    path = tmp_path / "handback.md"
    path.write_text(text_result_for(receipt), encoding="utf-8")
    recorded = await call("workmap_submit", {"contract_revision": receipt["contract_revision"],
                                                       "result_ref": str(path)})
    assert recorded["valid"] is True and recorded["workmap"]["status"] == "validated"
    node = node_of(served)
    assert node["handback"] == "full" and node["result_status"] == "completed"


@pytest.mark.asyncio
async def test_the_invalid_limit_records_the_next_tier_and_the_last_codes(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code", complexity="low",
                                                         client="claude"))
    for _ in range(5):
        await submit_text(receipt, "not a result")
    node = node_of(served)
    assert node["status"] == "needs_attention"
    assert node["escalation"] == {"complexity": "standard", "tier": "standard", "reason": "invalid_result"}
    events = WorkmapStore(served)._read(f"{map_key(TASK)}.jsonl")
    assert [event["escalation"] for event in events if event["op"] == "invalid"] == (
        [{"complexity": "standard", "tier": "standard", "reason": "invalid_result"}] * 5)


@pytest.mark.asyncio
async def test_the_node_record_shows_the_complexity_a_new_attempt_passes(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code", complexity="standard",
                                                         client="claude"))
    text = text_result_for(receipt).replace("status: completed", "status: partial") + (
        "\n\n## remaining_work\n- Fix the second logger")
    await submit_text(receipt, text)
    assert node_of(served)["escalation"] == {"complexity": "high", "tier": "inherit", "reason": "status_partial"}
    viewed = json.loads(await server.workmap_view(task_id=TASK, node="map-code"))
    # The map stores its events with sorted keys; the record names every key the escalation holds.
    assert "escalation: complexity high, reason status_partial, tier inherit" in viewed["view"].splitlines()


def settle_map_code(served, text="mapped code"):
    store = WorkmapStore(served)
    sha = hashlib.sha256(text.encode()).hexdigest()
    store.record_prepared(TASK, "map-code", contract_revision="a" * 64, attempt_id="1", complexity=None)
    store.record_result(TASK, "map-code", contract_revision="a" * 64, attempt_id="1", text=text, sha256=sha,
                        handback="line", outcome={"status": "validated", "level": "V2", "summary": "Mapped",
                                                  "checks": {"passed": 1, "failed": 0, "not_run": 0},
                                                  "findings": None, "limitations": 0})
    return sha


@pytest.fixture
def nothing_published(monkeypatch):
    async def published(*args, **kwargs):
        raise AssertionError("an input was published before the request was checked")

    monkeypatch.setattr(server.workmap_binding, "_pin", published)


@pytest.mark.asyncio
@pytest.mark.parametrize("inputs", [[], [f"n{index}" for index in range(11)], ["map-code", "map-code"]])
async def test_workmap_inputs_are_checked_before_anything_is_published(worker_tree, served, nothing_published,
                                                                       inputs):
    settle_map_code(served)
    refused = await call("get_subagent_context", request(workmap_node="write", workmap_inputs=inputs))
    assert refused["status"] == "error"
    assert refused["errors"][0]["path"].startswith("$.workmap_inputs")


@pytest.mark.asyncio
async def test_a_request_that_fails_its_own_checks_publishes_nothing(worker_tree, served, nothing_published):
    settle_map_code(served)
    refused = await call("get_subagent_context", request(workmap_node="write", workmap_inputs=["map-code"],
                                                         max_skills=99))
    assert refused["status"] == "error"


@pytest.mark.asyncio
@pytest.mark.parametrize("method", ["check_preparable", "record_prepared"])
async def test_an_unreadable_map_while_preparing_is_a_diagnostic(worker_tree, served, monkeypatch, method):
    def broken(*args, **kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(WorkmapStore, method, broken)
    refused = await call("get_subagent_context", request(workmap_node="map-code"))
    assert refused["status"] == "error" and refused["errors"][0]["code"] == "workmap_unavailable"


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", [OSError("down"), ValueError("bad"), ValidationError.from_exception_data("x", [])])
async def test_a_failing_artifact_service_while_pinning_is_a_diagnostic(artifact_worker_tree, served,
                                                                        monkeypatch, failure):
    settle_map_code(served)

    async def broken(ctx, client=None):
        raise failure

    monkeypatch.setattr(server.artifact_tools, "resolve_service", broken)
    refused = await call("get_subagent_context", request(workmap_node="write", workmap_inputs=["map-code"]))
    assert refused["status"] == "error" and refused["errors"][0]["code"] == "artifact_unavailable"


@pytest.mark.asyncio
async def test_the_prepared_event_records_the_pinned_inputs(artifact_worker_tree, served):
    sha = settle_map_code(served)
    receipt = await call("get_subagent_context", request(workmap_node="write", workmap_inputs=["map-code"]))
    assert receipt["status"] == "prepared"
    events = WorkmapStore(served)._read(f"{map_key(TASK)}.jsonl")
    prepared = [event for event in events if event["op"] == "prepared" and event["id"] == "write"]
    assert prepared[0]["inputs"] == {"map-code": sha}
    plain = [event for event in events if event["op"] == "prepared" and event["id"] == "map-code"]
    assert "inputs" not in plain[0]


def add_verifier(served):
    WorkmapStore(served).apply(TASK, [{"op": "add_node", "id": "check", "title": "Check the map"},
                                      {"op": "add_edge", "from": "check", "to": "map-code", "type": "verifies"}])


@pytest.mark.asyncio
async def test_a_node_that_verifies_others_is_prepared_only_as_a_review(worker_tree, served):
    add_verifier(served)
    refused = await call("get_subagent_context", request(workmap_node="check", result_type="implementation"))
    assert refused["status"] == "error" and refused["errors"][0]["code"] == "invalid_request"
    assert "review" in refused["errors"][0]["message"]
    assert WorkmapStore(served).load(TASK).nodes["check"]["status"] == "planned"
    default = await call("get_subagent_context", request(workmap_node="check"))
    assert default["errors"][0]["code"] == "invalid_request"
    accepted = await call("get_subagent_context", request(workmap_node="check", result_type="review"))
    assert accepted["status"] == "prepared"
    assert WorkmapStore(served).load(TASK).nodes["check"]["status"] == "prepared"


@pytest.mark.asyncio
async def test_a_node_without_verifies_edges_still_takes_any_result_type(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code", result_type="implementation"))
    assert receipt["status"] == "prepared"


REVIEW_FINDING = {"low": "## findings\n- low src/app.py:12 — A nit.[^e1]\n  fix: Rename it.",
                  "high": None}


@pytest.mark.asyncio
@pytest.mark.parametrize("severity, status, level", [("low", "verified", "V3"), ("high", "disputed", "V2")])
async def test_a_pinned_verifier_result_applies_its_verdict_end_to_end(artifact_worker_tree, served,
                                                                       severity, status, level):
    settle_map_code(served)
    add_verifier(served)
    receipt = await call("get_subagent_context", request(workmap_node="check", workmap_inputs=["map-code"],
                                                         result_type="review"))
    assert receipt["status"] == "prepared"
    package = SimpleNamespace(task_id=receipt["task_id"], attempt_id=receipt["attempt_id"],
                              contract_revision=receipt["contract_revision"],
                              contract=SimpleNamespace(result_type="review"))
    text = text_result(package, payload=REVIEW_FINDING[severity])
    validated = await call("workmap_submit", {"contract_revision": receipt["contract_revision"],
                                                        "result": text})
    assert validated["valid"] is True and validated["workmap"]["node"] == "check"
    assert node_of(served)["status"] == status and node_of(served)["level"] == level


@pytest.mark.asyncio
async def test_a_target_changed_after_the_verifier_was_prepared_gets_no_verdict_end_to_end(
        artifact_worker_tree, served):
    settle_map_code(served)
    add_verifier(served)
    receipt = await call("get_subagent_context", request(workmap_node="check", workmap_inputs=["map-code"],
                                                         result_type="review"))
    store = WorkmapStore(served)
    store.apply(TASK, [{"op": "update_node", "id": "map-code", "status": "planned"}])
    store.record_prepared(TASK, "map-code", contract_revision="c" * 64, attempt_id="2", complexity=None)
    store.record_result(TASK, "map-code", contract_revision="c" * 64, attempt_id="2", text="remapped",
                        sha256=hashlib.sha256(b"remapped").hexdigest(), handback="line",
                        outcome={"status": "validated", "level": "V2", "summary": "Remapped",
                                 "checks": {"passed": 1, "failed": 0, "not_run": 0}, "findings": None,
                                 "limitations": 0})
    package = SimpleNamespace(task_id=receipt["task_id"], attempt_id=receipt["attempt_id"],
                              contract_revision=receipt["contract_revision"],
                              contract=SimpleNamespace(result_type="review"))
    validated = await call("workmap_submit", {
        "contract_revision": receipt["contract_revision"], "result": text_result(package)})
    assert validated["valid"] is True
    assert node_of(served)["status"] == "validated"


@pytest.mark.asyncio
async def test_a_clean_review_records_no_findings_and_verifies_its_target_end_to_end(artifact_worker_tree,
                                                                                     served):
    settle_map_code(served)
    add_verifier(served)
    receipt = await call("get_subagent_context", request(workmap_node="check", workmap_inputs=["map-code"],
                                                         result_type="review"))
    package = SimpleNamespace(task_id=receipt["task_id"], attempt_id=receipt["attempt_id"],
                              contract_revision=receipt["contract_revision"],
                              contract=SimpleNamespace(result_type="review"))
    text = text_result(package)
    clean = text.replace(text[text.index("## findings"):text.index("## checks")], "")  # no findings section
    validated = await call("workmap_submit", {"contract_revision": receipt["contract_revision"],
                                                        "result": clean})
    assert validated["valid"] is True and validated["workmap"]["status"] == "validated"
    assert node_of(served, "check")["findings"] == {}  # a review with no findings: {}, not None
    assert node_of(served)["status"] == "verified" and node_of(served)["level"] == "V3"


@pytest.mark.asyncio
async def test_a_generic_result_gives_no_verdict_through_a_verifies_edge_added_later_end_to_end(
        artifact_worker_tree, served):
    settle_map_code(served)
    WorkmapStore(served).apply(TASK, [{"op": "add_node", "id": "check", "title": "Check the map"}])
    receipt = await call("get_subagent_context", request(workmap_node="check", workmap_inputs=["map-code"]))
    assert receipt["status"] == "prepared"
    WorkmapStore(served).apply(TASK, [{"op": "add_edge", "from": "check", "to": "map-code", "type": "verifies"}])
    validated = await call("workmap_submit", {"contract_revision": receipt["contract_revision"],
                                                        "result": text_result_for(receipt)})
    assert validated["valid"] is True and validated["workmap"]["status"] == "validated"
    assert node_of(served, "check")["findings"] is None
    assert node_of(served)["status"] == "validated" and node_of(served)["level"] == "V2"


@pytest.fixture
def two_roots(tmp_path, monkeypatch):
    """A stdio session whose client declares two MCP roots, each its own project."""
    roots = [tmp_path / "project-a", tmp_path / "project-b"]
    for root in roots:
        root.mkdir()
        (root / "CLAUDE.md").write_text("# Project\n", encoding="utf-8")
    for name in ("AGENTS_CLIENT_REPO_ROOT", "CLAUDE_PROJECT_DIR", "AGENTS_TRANSPORT"):
        monkeypatch.delenv(name, raising=False)
    engine_config._reset_client_repo_root_cache()
    ctx = SimpleNamespace(session=SimpleNamespace(
        check_client_capability=lambda _: True,
        list_roots=AsyncMock(return_value=SimpleNamespace(roots=[SimpleNamespace(uri=root.as_uri())
                                                                 for root in roots]))))
    yield ctx, [root.resolve() for root in roots]
    engine_config._reset_client_repo_root_cache()


@pytest.mark.asyncio
async def test_a_session_with_two_roots_binds_the_map_of_the_workspace_it_names(worker_tree, two_roots):
    ctx, (first, second) = two_roots
    applied = json.loads(await server.workmap_update(task_id=TASK, ops=[
        {"op": "add_node", "id": "map-code", "title": "Map the code"}], ctx=ctx, workspace=str(second)))
    assert applied["status"] == "applied" and applied["workspace"]["root"] == str(second)
    unnamed = (await server.get_subagent_context(**request(workmap_node="map-code"), ctx=ctx)).model_dump(
        mode="json")
    assert unnamed["status"] == "error" and unnamed["errors"][0]["code"] == "workspace"
    receipt = (await server.get_subagent_context(**request(workmap_node="map-code"), ctx=ctx,
                                                 workspace=str(second))).model_dump(mode="json")
    assert receipt["status"] == "prepared", receipt
    stored = server.subagent_exchange.load_contract(receipt["contract_revision"])
    assert stored["workmap"] == {"node": "map-code", "root": str(second)}
    assert WorkmapStore(second).load(TASK).nodes["map-code"]["status"] == "prepared"
    assert WorkmapStore(first).load(TASK) is None
    other = (await server.get_subagent_context(**request(workmap_node="map-code", attempt_id="2"), ctx=ctx,
                                               workspace=str(first))).model_dump(mode="json")
    assert other["status"] == "error" and other["errors"][0]["code"] == "unknown_map"


@pytest.mark.asyncio
async def test_the_tool_accepts_a_workspace_argument(worker_tree, served):
    receipt = await call("get_subagent_context", request(workmap_node="map-code", workspace=str(served)))
    assert receipt["status"] == "prepared", receipt
    assert node_of(served)["refs"]["contract_revision"] == receipt["contract_revision"]


@pytest.mark.asyncio
async def test_a_retry_with_the_same_attempt_and_inputs_keeps_its_contract(artifact_worker_tree, served):
    settle_map_code(served)
    first = await call("get_subagent_context", request(workmap_node="write", workmap_inputs=["map-code"]))
    again = await call("get_subagent_context", request(workmap_node="write", workmap_inputs=["map-code"]))
    assert first["status"] == again["status"] == "prepared", (first, again)
    assert again["contract_revision"] == first["contract_revision"]
    assert node_of(served, "write")["refs"]["contract_revision"] == first["contract_revision"]
    renewed = await call("get_subagent_context", request(workmap_node="write", workmap_inputs=["map-code"],
                                                          attempt_id="2"))
    assert renewed["status"] == "prepared" and renewed["contract_revision"] != first["contract_revision"]


@pytest.mark.asyncio
async def test_a_session_with_two_roots_pins_inputs_from_the_workspace_it_names(artifact_worker_tree, two_roots):
    ctx, (_, second) = two_roots
    applied = json.loads(await server.workmap_update(task_id=TASK, ops=[
        {"op": "add_node", "id": "map-code", "title": "Map the code"},
        {"op": "add_node", "id": "write", "title": "Write it"},
        {"op": "add_edge", "from": "write", "to": "map-code", "type": "depends_on"}], ctx=ctx,
        workspace=str(second)))
    assert applied["status"] == "applied", applied
    settle_map_code(second)
    receipt = (await server.get_subagent_context(**request(workmap_node="write", workmap_inputs=["map-code"]),
                                                 ctx=ctx, workspace=str(second))).model_dump(mode="json")
    assert receipt["status"] == "prepared", receipt
    assert WorkmapStore(second).load(TASK).nodes["write"]["refs"]["inputs"]


@pytest.mark.asyncio
async def test_a_bound_package_asks_for_the_receipt_not_also_for_the_full_result(worker_tree, served):
    bound = await call("get_subagent_context", request(workmap_node="map-code"))
    text = open(bound["context_path"], encoding="utf-8").read()
    assert "Reply with only a plain-text result" not in text and "It is the text you submit" in text
    unbound = await call("get_subagent_context", request(task_id="unbound-task"))
    assert "Reply with only a plain-text result" in open(unbound["context_path"], encoding="utf-8").read()
