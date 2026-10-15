"""Request processing is durable and independent of mailbox delivery."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import uuid

import pytest

from src.cooperation.models import (RequestStatusResponse, SendResponse,
                                    parse_request_status, parse_request_update, parse_send)
from tests.test_cooperation_mailbox import auth, ack, send, send_request
from tests.test_cooperation_service import Harness


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.service.close()


def status(h, actor, message, **extra):
    result = h.service.request_status(
        parse_request_status(auth(actor, message_id=message["message_id"], **extra)), actor["identity"])
    RequestStatusResponse.model_validate(result)
    return result


def update_request(actor, message, state, revision=1, **extra):
    return parse_request_update(auth(actor, operation_id=str(uuid.uuid4()), message_id=message["message_id"],
                                     expected_revision=revision, state=state, **extra))


def update(h, actor, message, state, revision=1, **extra):
    result = h.service.request_update(update_request(actor, message, state, revision, **extra), actor["identity"])
    RequestStatusResponse.model_validate(result)
    return result


def test_ack_and_response_do_not_complete_processing(h):
    sender, recipient = h.register("sender"), h.register("recipient")
    message = send(h, sender, recipient, kind="request")["message"]
    pending = status(h, sender, message)["request"]
    assert pending == status(h, recipient, message)["request"]
    assert (pending["state"], pending["revision"], pending["delivery"]) == ("pending", 1, "stored")
    assert ack(h, recipient, message)["status"] == "ok"
    acknowledged = status(h, sender, message)["request"]
    assert (acknowledged["state"], acknowledged["revision"], acknowledged["delivery"]) == ("pending", 1, "acknowledged")
    assert acknowledged["acknowledged_at"] == h.now
    response = send(h, recipient, sender, kind="response", reply_to=message["message_id"])["message"]
    assert status(h, sender, message)["request"] == acknowledged
    completed = update(h, recipient, message, "completed", response_message_id=response["message_id"])["request"]
    assert (completed["state"], completed["revision"], completed["response_message_id"]) == ("completed", 2, response["message_id"])


def test_only_recipient_reports_processing_and_only_sender_cancels(h):
    sender, recipient, stranger = [h.register(name) for name in ("sender", "recipient", "stranger")]
    message = send(h, sender, recipient, kind="request")["message"]
    assert update(h, sender, message, "accepted")["error"] == "scope_denied"
    assert update(h, recipient, message, "cancelled", reason="stopping")["error"] == "scope_denied"
    assert status(h, stranger, message)["error"] == "scope_denied"
    assert update(h, stranger, message, "accepted")["error"] == "scope_denied"
    assert update(h, recipient, message, "accepted")["request"]["revision"] == 2
    assert update(h, recipient, message, "input_required", 2, reason="Need artifact reference")["request"]["revision"] == 3
    assert update(h, recipient, message, "accepted", 3)["request"]["revision"] == 4
    cancelled = update(h, sender, message, "cancelled", 4, reason="Superseded")["request"]
    assert (cancelled["state"], cancelled["revision"], cancelled["updated_by"]) == ("cancelled", 5, sender["participant_id"])


def test_processing_state_and_reason_do_not_enter_public_journal_or_grant_execution(h):
    sender, recipient = h.register("sender"), h.register("recipient")
    before = h.read()
    message = send(h, sender, recipient, kind="request")["message"]
    assert update(h, recipient, message, "input_required", reason="Private artifact details")["status"] == "ok"
    after = h.read()
    assert after == before
    assert "Private artifact details" not in json.dumps(after)
    assert after["claims"] == []


@pytest.mark.parametrize("terminal", ["completed", "declined", "failed", "cancelled", "expired"])
def test_terminal_request_outcomes_are_immutable(h, terminal):
    sender, recipient = h.register("sender"), h.register("recipient")
    message = send(h, sender, recipient, kind="request", expires_at=h.now + 1)["message"]
    if terminal == "completed":
        response = send(h, recipient, sender, kind="response", reply_to=message["message_id"])["message"]
        result = update(h, recipient, message, terminal, response_message_id=response["message_id"])
    elif terminal == "expired":
        h.now += 1
        result = status(h, sender, message)
    else:
        actor = sender if terminal == "cancelled" else recipient
        result = update(h, actor, message, terminal, reason="Terminal reason")
    record = result["request"]
    assert (record["state"], record["revision"]) == (terminal, 2)
    assert update(h, recipient, message, "accepted", 2)["error"] == "stale_revision"
    assert update(h, sender, message, "cancelled", 2, reason="Try again")["error"] == "stale_revision"
    h.now += 100
    assert status(h, sender, message)["request"] == record


def test_stale_and_unchanged_updates_preserve_the_current_revision(h):
    sender, recipient = h.register("sender"), h.register("recipient")
    message = send(h, sender, recipient, kind="request")["message"]
    accepted = update(h, recipient, message, "accepted")["request"]
    assert update(h, recipient, message, "failed", reason="Old revision")["error"] == "stale_revision"
    assert update(h, recipient, message, "accepted", 2)["error"] == "invalid_request"
    assert update(h, recipient, message, "declined", 2, reason="Already accepted")["error"] == "invalid_request"
    assert status(h, sender, message)["request"] == accepted


def test_update_retry_is_atomic_and_survives_later_transitions_and_restart(h):
    sender, recipient = h.register("sender"), h.register("recipient")
    message = send(h, sender, recipient, kind="request")["message"]
    request = update_request(recipient, message, "accepted")
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: h.service.request_update(request, h.identity), range(6)))
    assert results[0]["status"] == "ok"
    assert all(result == results[0] for result in results)
    assert status(h, sender, message)["request"]["revision"] == 2
    cancelled = update(h, sender, message, "cancelled", 2, reason="No longer needed")["request"]
    h.service.close()
    h.service.start()
    assert h.service.request_update(request, h.identity) == results[0]
    assert status(h, sender, message)["request"] == cancelled
    with h.service.store.transaction(write=False) as db:
        rows = db.execute("SELECT body FROM mailbox_notifications").fetchall()
    revisions = [json.loads(row[0])["request_revision"] for row in rows]
    assert revisions.count(2) == 2  # One event per party, regardless of concurrent retries.


def test_competing_revisions_have_one_winner(h):
    sender, recipient = h.register("sender"), h.register("recipient")
    message = send(h, sender, recipient, kind="request")["message"]
    requests = [update_request(recipient, message, "accepted"),
                update_request(recipient, message, "input_required", reason="Need details")]
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda request: h.service.request_update(request, h.identity), requests))
    assert sorted(result.get("error", result["status"]) for result in results) == ["ok", "stale_revision"]
    assert status(h, sender, message)["request"]["revision"] == 2


def test_update_receipt_rejects_changed_payload_and_other_operations(h):
    sender, recipient = h.register("sender"), h.register("recipient")
    message = send(h, sender, recipient, kind="request")["message"]
    request = update_request(recipient, message, "accepted")
    assert h.service.request_update(request, h.identity)["status"] == "ok"
    changed = parse_request_update({**request.model_dump(), "state": "failed", "reason": "Changed"})
    assert h.service.request_update(changed, h.identity)["error"] == "idempotency_conflict"
    collision = parse_send(auth(recipient, operation_id=request.operation_id,
                                recipient_id=sender["participant_id"], body="different operation"))
    assert h.service.send(collision, h.identity)["error"] == "idempotency_conflict"


@pytest.mark.parametrize("binding", ["missing", "notice", "unbound", "other_request", "other_parties"])
def test_completion_requires_this_recipients_response_to_this_request(h, binding):
    sender, recipient, other = [h.register(name) for name in ("sender", "recipient", "other")]
    message = send(h, sender, recipient, kind="request")["message"]
    if binding == "missing":
        response_id = "unavailable-message"
    elif binding == "notice":
        response_id = send(h, recipient, sender, kind="notice", reply_to=message["message_id"])["message"]["message_id"]
    elif binding == "unbound":
        response_id = send(h, recipient, sender, kind="response")["message"]["message_id"]
    elif binding == "other_request":
        other_request = send(h, sender, recipient, kind="request")["message"]
        response_id = send(h, recipient, sender, kind="response", reply_to=other_request["message_id"])["message"]["message_id"]
    else:
        other_request = send(h, sender, other, kind="request")["message"]
        response_id = send(h, other, sender, kind="response", reply_to=other_request["message_id"])["message"]["message_id"]
    assert update(h, recipient, message, "completed", response_message_id=response_id)["error"] == "scope_denied"
    assert status(h, sender, message)["request"]["state"] == "pending"


def test_deadline_is_exact_and_expires_only_once(h):
    sender, recipient = h.register("sender"), h.register("recipient")
    deadline = h.now + 10
    message = send(h, sender, recipient, kind="request", expires_at=deadline)["message"]
    assert update(h, recipient, message, "accepted")["request"]["revision"] == 2
    h.now = deadline - 0.001
    assert status(h, sender, message)["request"]["state"] == "accepted"
    h.now = deadline
    expired = status(h, sender, message)["request"]
    assert (expired["state"], expired["revision"], expired["updated_at"]) == ("expired", 3, deadline)
    assert status(h, recipient, message)["request"] == expired
    assert update(h, recipient, message, "failed", 3, reason="Too late")["error"] == "stale_revision"
    with h.service.store.transaction(write=False) as db:
        events = [json.loads(row[0]) for row in db.execute("SELECT body FROM mailbox_notifications")]
    assert sum(event["request_state"] == "expired" for event in events) == 2


def test_update_at_deadline_persists_expiry_without_applying_the_outcome(h):
    sender, recipient = h.register("sender"), h.register("recipient")
    message = send(h, sender, recipient, kind="request", expires_at=h.now + 1)["message"]
    h.now += 1
    assert update(h, recipient, message, "accepted")["error"] == "stale_revision"
    record = status(h, sender, message)["request"]
    assert (record["state"], record["revision"]) == ("expired", 2)


def test_no_deadline_is_not_inferred_from_silence_and_late_send_retry_is_stable(h):
    sender, recipient = h.register("sender"), h.register("recipient")
    timeless = send(h, sender, recipient, kind="request")["message"]
    request = send_request(sender, recipient, kind="request", expires_at=h.now + 1)
    original = h.service.send(request, h.identity)
    h.now += 31 * 86400
    assert status(h, sender, timeless)["request"]["state"] == "pending"
    assert status(h, sender, original["message"])["request"]["state"] == "expired"
    assert h.service.send(request, h.identity) == original
    assert send(h, sender, recipient, kind="request", expires_at=h.now)["error"] == "invalid_request"


def test_status_and_updates_reject_wrong_capabilities_and_foreign_repository(h):
    sender, recipient = h.register("sender"), h.register("recipient")
    message = send(h, sender, recipient, kind="request")["message"]
    assert status(h, recipient, message, capability=sender["capability"])["error"] == "unauthorized_actor"
    assert update(h, recipient, message, "accepted", capability=sender["capability"])["error"] == "unauthorized_actor"
    foreign = replace(h.identity, repository_id="foreign_repository")
    assert h.service.request_status(parse_request_status(auth(sender, message_id=message["message_id"])), foreign)["error"] == "workspace_invalid"
    assert h.service.request_update(update_request(recipient, message, "accepted"), foreign)["error"] == "workspace_invalid"
    outsider = h.register("outsider", identity=foreign)
    assert status(h, outsider, message)["error"] == "scope_denied"
    notice = send(h, sender, recipient)["message"]
    assert status(h, sender, notice)["error"] == "scope_denied"
    assert update(h, recipient, notice, "accepted")["error"] == "scope_denied"


def test_processing_relay_requires_direct_child_and_records_actual_actor(h):
    parent, peer, stranger = [h.register(name) for name in ("parent", "peer", "stranger")]
    child = h.register("child", parent=parent, communication_mode="parent_relay")
    independent = h.register("independent", parent=parent)
    grandchild = h.register("grandchild", parent=child, root_task_id=parent["task"], communication_mode="parent_relay")
    message = send(h, peer, child, kind="request")["message"]
    assert status(h, parent, message, subject_id=child["participant_id"])["request"]["state"] == "pending"
    assert status(h, stranger, message, subject_id=child["participant_id"])["error"] == "unauthorized_actor"
    assert status(h, parent, message, subject_id=independent["participant_id"])["error"] == "unauthorized_actor"
    grandchild_message = send(h, peer, grandchild, kind="request")["message"]
    assert status(h, parent, grandchild_message, subject_id=grandchild["participant_id"])["error"] == "unauthorized_actor"
    record = update(h, parent, message, "accepted", subject_id=child["participant_id"])["request"]
    assert record["updated_by"] == parent["participant_id"]
    assert record["recipient_id"] == child["participant_id"]


def test_failed_processing_projection_rolls_back_state_receipt_and_notifications(h, monkeypatch):
    sender, recipient = h.register("sender"), h.register("recipient")
    message = send(h, sender, recipient, kind="request")["message"]
    request = update_request(recipient, message, "accepted")
    before = status(h, sender, message)["request"]
    with h.service.store.transaction(write=False) as db:
        notification_count = db.execute("SELECT COUNT(*) FROM mailbox_notifications").fetchone()[0]
    def invalid(_):
        raise ValueError("bad projection")
    with monkeypatch.context() as patch:
        patch.setattr(RequestStatusResponse, "model_validate", invalid)
        assert h.service.request_update(request, h.identity)["error"] == "store_unavailable"
    assert status(h, sender, message)["request"] == before
    with h.service.store.transaction(write=False) as db:
        assert db.execute("SELECT COUNT(*) FROM mailbox_notifications").fetchone()[0] == notification_count
        assert db.execute("SELECT COUNT(*) FROM message_receipts WHERE operation_id=?", (request.operation_id,)).fetchone()[0] == 0
    assert h.service.request_update(request, h.identity)["request"]["revision"] == 2


def test_failed_request_send_projection_rolls_back_new_request_and_notifications(h, monkeypatch):
    sender, recipient = h.register("sender"), h.register("recipient")
    request = send_request(sender, recipient, kind="request")
    def invalid(_):
        raise ValueError("bad projection")
    with monkeypatch.context() as patch:
        patch.setattr(SendResponse, "model_validate", invalid)
        assert h.service.send(request, h.identity)["error"] == "store_unavailable"
    with h.service.store.transaction(write=False) as db:
        for table in ("messages", "mailbox_requests", "mailbox_notifications", "message_receipts", "participant_activity"):
            assert db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0
    assert h.service.send(request, h.identity)["status"] == "ok"


@pytest.mark.parametrize("state", ["input_required", "declined", "failed", "cancelled"])
def test_reason_is_required_for_request_outcomes(h, state):
    sender = h.register("sender")
    with pytest.raises(ValueError):
        update_request(sender, {"message_id": "message"}, state)


@pytest.mark.parametrize("extra", [{"state": "completed"}, {"state": "accepted", "response_message_id": "response"},
                                    {"state": "expired"}, {"state": "pending"}, {"state": "accepted", "expected_revision": 0}])
def test_request_update_rejects_invalid_outcome_shapes(h, extra):
    actor = h.register("actor")
    with pytest.raises(ValueError):
        parse_request_update(auth(actor, operation_id="op", message_id="message", expected_revision=1, state="accepted") | extra)
