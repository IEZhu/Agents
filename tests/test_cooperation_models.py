"""The public parser rejects malformed input before service side effects."""
import json
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.cooperation.models import (ACTION_MODELS, PROTOCOL, ApplyRequest, ApplyResponse,
                                    Artifact, ErrorResponse, EventEnvelope, InvalidRequest,
                                    ReadResponse, RegisterResponse, json_schemas,
                                    SnapshotCounts, SendResponse, ReceiveResponse, AckResponse,
                                    parse_apply, parse_read, parse_register, parse_send,
                                    parse_receive, parse_ack)


RESOURCE = {"resource_type": "physical_file", "resource_key": "checkout:src/a.py", "access": "write"}
CHECKPOINT = {"trigger": "One file is validated", "max_interval_seconds": 300}
CLAIM = {"claim_id": "claim-1", "expected_revision": 1}
DIGEST = "a" * 64


def proposal():
    return {
        "kind": "propose", "agreement_id": "agreement-1", "proposal_revision": 1,
        "pattern": "partition_join", "required_participants": ["p1", "p2"],
        "task_bindings": [{"participant_id": p, "task_id": f"task-{p}", "attempt_id": "1",
                           "generation": 1} for p in ("p1", "p2")],
        "terms": {"scope": "Two isolated edits", "resources": [{**RESOURCE, "owner": "p1"}],
                  "integrator": "p1", "checkpoint": CHECKPOINT,
                  "completion_conditions": ["Both results validated"]},
    }


def action_fixtures():
    return [
        {"kind": "declare_intent", "checkpoint": CHECKPOINT},
        proposal(),
        {"kind": "acknowledge", "agreement_id": "agreement-1", "proposal_revision": 1, "terms_digest": DIGEST},
        {"kind": "reject", "agreement_id": "agreement-1", "proposal_revision": 1, "reason": "Conflicting scope"},
        {"kind": "acquire", "resources": [RESOURCE], "work_unit_id": "unit-1", "checkpoint": CHECKPOINT},
        {"kind": "heartbeat"},
        {"kind": "checkpoint", "checkpoint_id": "checkpoint-1", "release_ready": True},
        {"kind": "release", "claims": [CLAIM], "reason": "Work unit done"},
        {"kind": "handoff", "claims": [CLAIM], "old_owner": "p1", "new_owner": "p2",
         "old_generation": 1, "new_generation": 1, "checkpoint_id": "checkpoint-1", "agreement_id": "agreement-1"},
        {"kind": "accept_artifact", "artifact_id": "artifact-1", "digest": DIGEST, "head_revision": "abc123",
         "producer_task_id": "task-p1", "producer_attempt_id": "1", "consumer_task_id": "task-p2", "consumer_attempt_id": "1"},
        {"kind": "cancel", "reason": "Owner cancelled"},
        {"kind": "recover", "service_epoch": 2, "previous_generation": 1,
         "execution_evidence": {"kind": "stopped", "reference": "host:execution/1", "execution_id": "exec-1"},
         "reconciliation": {"checkpoint_id": "checkpoint-1", "inspected": True, "summary": "Dirty state reviewed"}},
    ]


def request(action):
    return {"protocol": PROTOCOL, "repository_id": "repo-1", "participant_id": "p1",
            "capability": "private-capability", "operation_id": "op-1", "expected_revision": 0,
            "expected_generation": 1, "action": deepcopy(action)}


@pytest.mark.parametrize("action", action_fixtures(), ids=lambda action: action["kind"])
def test_each_action_round_trips_and_is_closed(action):
    model = parse_apply(request(action))
    assert isinstance(model, ApplyRequest)
    assert parse_apply(model.model_dump_json()) == model
    assert model.action.kind == action["kind"]
    bad = request(action)
    bad["action"]["arbitrary_database_field"] = "no"
    with pytest.raises(ValidationError):
        parse_apply(bad)


def test_exact_action_set_and_exported_schemas():
    assert set(ApplyRequest.model_json_schema()["properties"]["action"]["discriminator"]["mapping"]) == {
        "declare_intent", "propose", "acknowledge", "reject", "acquire", "heartbeat",
        "checkpoint", "release", "handoff", "accept_artifact", "cancel", "recover",
    }
    assert len(ACTION_MODELS) == len(action_fixtures()) == 12
    stored = json.loads((Path(__file__).parents[1] / "docs/agent-cooperation/schemas.json").read_text())
    assert stored == json_schemas()
    for schema in stored["schemas"].values():
        if "anyOf" not in schema:
            assert schema["additionalProperties"] is False
        for nested in schema.get("$defs", {}).values():
            if nested.get("type") == "object":
                assert nested["additionalProperties"] is False


def test_register_and_read_defaults_and_parent_binding():
    registration = {"protocol": PROTOCOL, "checkout_path": "/checkout", "root_task_id": "root",
                    "task_id": "task", "attempt_id": "1", "scope": [RESOURCE]}
    assert parse_register(registration).communication_mode == "mcp"
    for mode in ("mcp", "native", "parent_relay", "checkpoint"):
        assert parse_register({**registration, "communication_mode": mode}).communication_mode == mode
    with pytest.raises(ValidationError):
        parse_register({**registration, "scope": []})
    with pytest.raises(ValidationError):
        parse_register({**registration, "parent_id": "parent"})
    read = parse_read({"protocol": PROTOCOL, "repository_id": "repo"})
    assert read.limit == 50
    assert read.cursor is None
    assert read.offset == 0


def mailbox_request(**fields):
    return {"protocol": PROTOCOL, "repository_id": "repo", "participant_id": "sender",
            "capability": "private-capability", **fields}


def test_mailbox_request_defaults_and_private_representations():
    send = parse_send(mailbox_request(operation_id="send", recipient_id="recipient", body="private body"))
    assert send.kind == "notice" and send.subject_id is None
    assert send.reply_to is None and send.correlation_id is None
    assert "private-capability" not in repr(send) and "private body" not in repr(send)
    receive = parse_receive(mailbox_request())
    assert receive.after_cursor == 0 and receive.limit == 20 and receive.include_acknowledged is False
    ack = parse_ack(mailbox_request(operation_id="ack", message_ids=["m1", "m2"]))
    assert ack.message_ids == ["m1", "m2"]
    for model, parser in ((send, parse_send), (receive, parse_receive), (ack, parse_ack)):
        assert parser(model.model_dump_json()) == model
        for field in ("expected_revision", "expected_generation", "unknown"):
            with pytest.raises(ValidationError):
                parser({**model.model_dump(), field: 1})


@pytest.mark.parametrize("field,value", [
    ("body", ""), ("body", "  "), ("body", "x" * 4001), ("body", 1),
    ("recipient_id", "../recipient"), ("kind", "command"), ("reply_to", True),
    ("correlation_id", ""), ("capability", 42), ("operation_id", None),
])
def test_send_rejects_invalid_fields(field, value):
    with pytest.raises(ValidationError):
        parse_send(mailbox_request(operation_id="send", recipient_id="recipient", body="body") |
                   {field: value})


@pytest.mark.parametrize("fields", [
    {"after_cursor": True}, {"after_cursor": -1}, {"after_cursor": "1"}, {"after_cursor": 2**63},
    {"limit": True}, {"limit": 0}, {"limit": 51}, {"limit": "20"},
    {"include_acknowledged": "true"}, {"include_acknowledged": 1},
])
def test_receive_rejects_coerced_or_unbounded_fields(fields):
    with pytest.raises(ValidationError):
        parse_receive(mailbox_request(**fields))


@pytest.mark.parametrize("ids", [[], ["m1", "m1"], [f"m{i}" for i in range(51)],
                                  [True], "m1", ["invalid/id"]])
def test_ack_requires_unique_bounded_message_ids(ids):
    with pytest.raises(ValidationError):
        parse_ack(mailbox_request(operation_id="ack", message_ids=ids))


@pytest.mark.parametrize("parser,fields", [
    (parse_send, {"operation_id": "send", "recipient_id": "recipient", "body": "body"}),
    (parse_receive, {}),
    (parse_ack, {"operation_id": "ack", "message_ids": ["message"]}),
])
def test_mailbox_parsers_reject_duplicate_keys_versions_and_oversized_requests(parser, fields):
    encoded = json.dumps(mailbox_request(**fields))
    with pytest.raises(InvalidRequest, match="Duplicate"):
        parser(encoded[:-1] + ', "protocol": "' + PROTOCOL + '"}')
    with pytest.raises(InvalidRequest) as error:
        parser({**mailbox_request(**fields), "protocol": "unsupported"})
    assert error.value.code == "unsupported_version"
    with pytest.raises(InvalidRequest, match="bytes"):
        parser({**mailbox_request(**fields), "extra": "😀" * 9000})


def test_mailbox_responses_are_typed_closed_and_bounded():
    message = {"message_id": "m1", "cursor": 1, "repository_id": "repo", "sender_id": "sender",
               "recipient_id": "recipient", "actor_id": "sender", "kind": "request", "body": "private body",
               "reply_to": None, "correlation_id": "discussion", "created_at": 1.0,
               "acknowledged_at": None, "acknowledged_by": None}
    sent = {"protocol": PROTOCOL, "status": "ok", "repository_id": "repo", "message": message}
    received = {"protocol": PROTOCOL, "status": "ok", "repository_id": "repo", "participant_id": "recipient",
                "messages": [message], "next_cursor": 1, "has_more": False}
    acknowledged = {"protocol": PROTOCOL, "status": "ok", "repository_id": "repo", "participant_id": "recipient",
                    "message_ids": ["m1"], "acknowledged_at": 2.0}
    for model, response in ((SendResponse, sent), (ReceiveResponse, received), (AckResponse, acknowledged)):
        assert model.model_validate(response).model_dump(mode="json") == response
        with pytest.raises(ValidationError):
            model.model_validate({**response, "capability": "private-capability"})
        assert model.model_validate({"status": "error", "error": "mailbox_full", "message": "Inbox full"})
    with pytest.raises(ValidationError):
        SendResponse.model_validate({**sent, "message": {**message, "capability": "private-capability"}})
    with pytest.raises(ValidationError):
        ReceiveResponse.model_validate({**received, "messages": [message] * 51})


@pytest.mark.parametrize("offset", [True, -1, 1.0, "1"])
def test_snapshot_offset_is_strict_and_nonnegative(offset):
    with pytest.raises(ValidationError):
        parse_read({"protocol": PROTOCOL, "repository_id": "repo", "offset": offset})


def test_snapshot_pagination_counts_are_compatible_and_explicit():
    assert parse_read({"protocol": PROTOCOL, "repository_id": "repo", "offset": 50}).offset == 50
    counts = SnapshotCounts(participants=2, agreements=1)
    assert counts.model_dump() == {"participants": 2, "agreements": 1, "claims": 0,
                                   "artifacts": 0, "dependencies": 0, "ready": 0}
    assert SnapshotCounts(participants=2, agreements=1, claims=125).claims == 125
    with pytest.raises(ValidationError):
        SnapshotCounts(participants=2, agreements=1, claims=True)


@pytest.mark.parametrize("field,value", [("expected_revision", True), ("expected_revision", "0"),
                                        ("expected_revision", 0.0), ("expected_generation", False),
                                        ("expected_generation", 0), ("capability", 42)])
def test_primitive_types_are_not_coerced(field, value):
    data = request({"kind": "heartbeat"})
    data[field] = value
    with pytest.raises(ValidationError):
        parse_apply(data)


@pytest.mark.parametrize("limit", [True, 101, 0, "50"])
def test_read_limit_is_strict_and_bounded(limit):
    with pytest.raises(ValidationError):
        parse_read({"protocol": PROTOCOL, "repository_id": "repo", "limit": limit})


@pytest.mark.parametrize("bad_json", [
    '{"protocol":"agents-core/cooperation/1","repository_id":"repo","limit":1,"limit":2}',
    '{"protocol":"agents-core/cooperation/1","repository_id":"repo","limit":NaN}',
    '{"protocol":"agents-core/cooperation/1","repository_id":"repo","limit":Infinity}',
    '{"protocol":"agents-core/cooperation/1","repository_id":"repo","limit":1e9999}',
    '{"protocol":"agents-core/cooperation/1","repository_id":"repo","x":{"secret":1,"secret":2}}',
])
def test_duplicate_and_nonfinite_json_is_rejected_without_echoing_values(bad_json):
    with pytest.raises(InvalidRequest) as error:
        parse_read(bad_json)
    assert "secret" not in str(error.value)


def test_depth_bytes_items_and_non_json_values():
    data = request({"kind": "heartbeat"})
    nested = {}
    data["extra"] = nested
    for _ in range(8):
        nested["x"] = {}
        nested = nested["x"]
    with pytest.raises(InvalidRequest, match="depth"):
        parse_apply(data)
    with pytest.raises(InvalidRequest, match="bytes"):
        parse_apply(request({"kind": "cancel", "reason": "😀" * 9000}))
    with pytest.raises(ValidationError):
        parse_apply(request({"kind": "acquire", "resources": [RESOURCE] * 51,
                             "work_unit_id": "unit", "checkpoint": CHECKPOINT}))
    with pytest.raises(InvalidRequest):
        parse_read({"protocol": PROTOCOL, "repository_id": "repo", "cursor": object()})
    with pytest.raises(InvalidRequest):
        parse_read({"protocol": PROTOCOL, "repository_id": "repo", "cursor": float("nan")})


def test_secret_is_not_in_repr_or_validation_error():
    data = request({"kind": "heartbeat"})
    assert "private-capability" not in repr(parse_apply(data))
    data["capability"] = "PRIVATE_SECRET" * 500
    with pytest.raises(ValidationError) as error:
        parse_apply(data)
    assert "PRIVATE_SECRET" not in str(error.value)


def test_unsupported_version_has_typed_error():
    with pytest.raises(InvalidRequest) as error:
        parse_read({"protocol": "agents-core/cooperation/2", "repository_id": "repo"})
    assert error.value.code == "unsupported_version"


@pytest.mark.parametrize("mutate", [
    lambda p: p["required_participants"].append("p1"),
    lambda p: p["terms"].update(integrator="outsider"),
    lambda p: p["task_bindings"].pop(),
    lambda p: p.update(proposal_revision=2),
    lambda p: p["terms"]["resources"][0].update(owner="outsider"),
])
def test_proposal_has_exact_participants_and_prior_revision(mutate):
    data = proposal()
    mutate(data)
    with pytest.raises(ValidationError):
        parse_apply(request(data))


def test_artifact_references_are_closed_but_never_fetched():
    artifact = {"artifact_id": "artifact", "producer_task_id": "producer", "producer_attempt_id": "1",
                "repository_id": "repo", "base_revision": "base", "head_revision": "head",
                "path": "src/not-present.py", "digest": DIGEST, "media_type": "text/plain", "summary": "Pinned output"}
    assert Artifact.model_validate(artifact).path == "src/not-present.py"
    for changed in ({"path": "../outside"}, {"path": "/outside"}, {"uri": "https://example.invalid"}):
        with pytest.raises(ValidationError):
            Artifact.model_validate({**artifact, **changed})


def test_event_has_exact_envelope_and_typed_payload():
    event = {"protocol": PROTOCOL, "event_id": "event", "operation_id": "op", "repository_id": "repo",
             "revision": 1, "actor_id": "p1", "subject_id": None, "task_id": "task", "attempt_id": "1",
             "generation": 1, "kind": "heartbeat", "agreement_id": None, "proposal_revision": None,
             "reply_to": None, "created_at": 1792065600.0, "payload": {"kind": "heartbeat"}}
    assert EventEnvelope.model_validate(event).payload.kind == "heartbeat"
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate({**event, "kind": "cancel"})


@pytest.mark.parametrize("response_type", [RegisterResponse, ReadResponse, ApplyResponse])
def test_closed_response_schemas_include_bounded_declared_diagnostics(response_type):
    schema = response_type.model_json_schema()
    assert schema["type"] == "object"
    for nested in schema["$defs"].values():
        if nested.get("type") == "object":
            assert nested["additionalProperties"] is False
    error = {"protocol": PROTOCOL, "status": "error", "error": "claim_conflict", "message": "Resource held",
             "claim_ids": ["claim-1"], "owner": "other", "service_epoch": 2}
    assert isinstance(response_type.model_validate(error).root, ErrorResponse)
    with pytest.raises(ValidationError):
        response_type.model_validate({**error, "capability": "private"})


def test_apply_receipt_fields_match_the_event_action():
    event = {"protocol": PROTOCOL, "event_id": "event", "operation_id": "op", "repository_id": "repo",
             "revision": 1, "actor_id": "p1", "subject_id": None, "task_id": "task", "attempt_id": "1",
             "generation": 1, "kind": "heartbeat", "agreement_id": None, "proposal_revision": None,
             "reply_to": None, "created_at": 1792065600.0, "payload": {"kind": "heartbeat"}}
    response = {"protocol": PROTOCOL, "status": "ok", "revision": 1, "generation": 1,
                "service_epoch": 1, "cursor": 1, "event": event, "checkpoint_required": []}
    assert ApplyResponse.model_validate(response).root.event.kind == "heartbeat"
    response.pop("checkpoint_required")
    response["accepted"] = True
    with pytest.raises(ValidationError):
        ApplyResponse.model_validate(response)


def test_declared_queue_reports_strict_bounded_truncation_metadata():
    event = {"protocol": PROTOCOL, "event_id": "event", "operation_id": "op", "repository_id": "repo",
             "revision": 1, "actor_id": "p1", "subject_id": None, "task_id": "task", "attempt_id": "1",
             "generation": 1, "kind": "declare_intent", "agreement_id": None, "proposal_revision": None,
             "reply_to": None, "created_at": 1792065600.0,
             "payload": {"kind": "declare_intent", "checkpoint": CHECKPOINT}}
    response = {"protocol": PROTOCOL, "status": "ok", "revision": 1, "generation": 1,
                "service_epoch": 1, "cursor": 1, "event": event, "base_priority": 1,
                "priority_verified": False, "ready": []}
    parsed = ApplyResponse.model_validate(response).root
    assert parsed.ready_count == 0
    assert parsed.ready_truncated is False
    parsed = ApplyResponse.model_validate({**response, "ready_count": 125, "ready_truncated": True}).root
    assert parsed.ready_count == 125
    assert parsed.ready_truncated is True
    for fields in ({"ready_count": True}, {"ready_count": -1}, {"ready_truncated": 1}):
        with pytest.raises(ValidationError):
            ApplyResponse.model_validate({**response, **fields})


def test_read_dependency_cycles_are_closed_bounded_text_lists():
    response = {"protocol": PROTOCOL, "status": "ok", "repository_id": "repo", "revision": 1,
                "service_epoch": 1, "minimum_cursor": 0, "snapshot_cursor": 0, "next_cursor": 0,
                "has_more_events": False, "participants": [], "agreements": [],
                "snapshot_counts": {"participants": 0, "agreements": 0}, "snapshot_truncated": False,
                "claims": [], "artifacts": [], "dependencies": [], "ready": [], "events": [],
                "limitations": "Metadata only"}
    default = ReadResponse.model_validate(response).root
    assert default.dependency_cycles == []
    assert default.dependency_cycles_count == 0
    assert default.dependency_cycles_truncated is False
    cycle = ["producer/1", "consumer/1", "producer/1"]
    assert ReadResponse.model_validate({**response, "dependency_cycles": [cycle]}).root.dependency_cycles == [cycle]
    preview = ReadResponse.model_validate({**response, "dependency_cycles": [cycle],
                                          "dependency_cycles_count": 125,
                                          "dependency_cycles_truncated": True}).root
    assert preview.dependency_cycles_count == 125
    assert preview.dependency_cycles_truncated is True
    for fields in ({"dependency_cycles_count": True}, {"dependency_cycles_count": -1},
                   {"dependency_cycles_count": 1.0}, {"dependency_cycles_truncated": 1}):
        with pytest.raises(ValidationError):
            ReadResponse.model_validate({**response, **fields})
    for cycles in ([cycle] * 101, [["task/1"] * 51], [[True]], [["x" * 4001]]):
        with pytest.raises(ValidationError):
            ReadResponse.model_validate({**response, "dependency_cycles": cycles})


def test_dependency_cycle_error_preview_reports_truncation_without_expanding_bound():
    error = {"protocol": PROTOCOL, "status": "error", "error": "dependency_cycle",
             "message": "Dependency cycle requires reconciliation"}
    assert ErrorResponse.model_validate(error).cycle_truncated is False
    preview = [f"task-{index}/1" for index in range(50)]
    parsed = ErrorResponse.model_validate({**error, "cycle": preview, "cycle_truncated": True})
    assert parsed.cycle == preview
    assert parsed.cycle_truncated is True
    with pytest.raises(ValidationError):
        ErrorResponse.model_validate({**error, "cycle_truncated": 1})
    with pytest.raises(ValidationError):
        ErrorResponse.model_validate({**error, "cycle": preview + ["task-50/1"], "cycle_truncated": True})


def test_owner_cleanup_is_a_typed_offline_event_not_an_apply_action():
    event = {"protocol": PROTOCOL, "event_id": "event", "operation_id": "op", "repository_id": "repo",
             "revision": 1, "actor_id": "installation_owner", "subject_id": "p1", "task_id": "task",
             "attempt_id": "1", "generation": 2, "kind": "owner_cleanup", "agreement_id": None,
             "proposal_revision": None, "reply_to": None, "created_at": 1792065600.0,
             "payload": {"reason": "Owner authorized", "inspection": "Checkpoint inspected",
                         "execution_pid": 42, "checkpoint_id": None, "released_claims": []}}
    assert EventEnvelope.model_validate(event).kind == "owner_cleanup"
    with pytest.raises(ValidationError):
        parse_apply(request({"kind": "owner_cleanup", **event["payload"]}))
    event["payload"]["execution_pid"] = True
    with pytest.raises(ValidationError):
        EventEnvelope.model_validate(event)


def test_real_service_outputs_match_closed_projection_schemas(tmp_path):
    from src.cooperation.identity import CooperationIdentity
    from src.cooperation.service import CooperationService

    service = CooperationService(tmp_path / "state", clock=lambda: 1000.0)
    identity = CooperationIdentity("repo", "workspace", "checkout", str(tmp_path), "base", "dev", str(tmp_path))
    scope = [{"resource_type": "logical_intent", "resource_key": "api/orders", "access": "write"}]
    registration = {"protocol": PROTOCOL, "checkout_path": str(tmp_path), "root_task_id": "root",
                    "task_id": "task", "attempt_id": "1", "scope": scope}
    service.start()
    try:
        registered = service.register(parse_register(registration), identity)
        RegisterResponse.model_validate(registered)
        assert registered["status"] == "ok"
        participant = registered["participant_id"]
        revision = registered["revision"]
        actions = [{"kind": "heartbeat"}, {"kind": "declare_intent", "resources": scope, "checkpoint": CHECKPOINT},
                   {"kind": "propose", "agreement_id": "agreement", "pattern": "single_writer_reviewers",
                    "required_participants": [participant], "task_bindings": [{"participant_id": participant,
                        "task_id": "task", "attempt_id": "1", "generation": 1}],
                    "terms": {"scope": "Bounded work", "resources": [{**scope[0], "owner": participant}],
                              "integrator": participant, "checkpoint": CHECKPOINT,
                              "completion_conditions": ["Validated result"]}}]
        for index, action in enumerate(actions):
            data = {**request(action), "repository_id": "repo", "participant_id": participant,
                    "capability": registered["capability"], "operation_id": f"op-{index}",
                    "expected_revision": revision}
            response = service.apply(parse_apply(data), identity)
            ApplyResponse.model_validate(response)
            assert response["status"] == "ok"
            revision = response["revision"]
        snapshot = service.read(parse_read({"protocol": PROTOCOL, "repository_id": "repo"}), identity)
        ReadResponse.model_validate(snapshot)
        assert snapshot["status"] == "ok"
        assert len(snapshot["events"]) == 3
        assert "capability" not in json.dumps(snapshot)
    finally:
        service.close()
