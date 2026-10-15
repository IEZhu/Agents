"""Persistent, provider-neutral message delivery and its authorization boundary."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
import sqlite3
import uuid

import pytest

from src.cooperation import mailbox
from src.cooperation.models import PROTOCOL, SendResponse, ReceiveResponse, AckResponse
from src.cooperation.models import parse_send, parse_receive, parse_ack
from tests.test_cooperation_service import Harness


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.service.close()


def auth(actor, **extra):
    return {"protocol": PROTOCOL, "repository_id": actor["repository_id"],
            "participant_id": actor["participant_id"], "capability": actor["capability"], **extra}


def send_request(sender, recipient, **extra):
    return parse_send(auth(sender, operation_id=str(uuid.uuid4()), recipient_id=recipient["participant_id"],
                           body="Please inspect artifact A before integration.", **extra))


def send(h, sender, recipient, **extra):
    request = send_request(sender, recipient, **extra)
    result = h.service.send(request, sender["identity"])
    SendResponse.model_validate(result)
    return result


def receive(h, actor, **extra):
    result = h.service.receive(parse_receive(auth(actor, **extra)), actor["identity"])
    ReceiveResponse.model_validate(result)
    return result


def ack_request(actor, *messages, **extra):
    return parse_ack(auth(actor, operation_id=str(uuid.uuid4()), message_ids=[m["message_id"] for m in messages], **extra))


def ack(h, actor, *messages, **extra):
    result = h.service.ack(ack_request(actor, *messages, **extra), actor["identity"])
    AckResponse.model_validate(result)
    return result


def test_request_reply_ack_without_public_journal_or_permission_side_effect(h):
    a, b = h.register("provider-a"), h.register("provider-b")
    assert a["communication_mode"] == "mcp"
    before = h.read()
    request = send(h, a, b, kind="request", correlation_id="review-1")["message"]
    first = receive(h, b)
    assert first == receive(h, b)  # Reading does not consume mail.
    assert first["messages"] == [request]
    reply = send(h, b, a, kind="response", reply_to=request["message_id"], correlation_id="review-1")["message"]
    assert receive(h, a)["messages"] == [reply]
    assert ack(h, b, request)["status"] == "ok"
    assert receive(h, b)["messages"] == []
    assert receive(h, b, include_acknowledged=True)["messages"][0]["acknowledged_by"] == b["participant_id"]
    after = h.read()
    assert before == after
    assert request["body"] not in json.dumps(after)
    with h.service.store.transaction(write=False) as db:
        stored = json.dumps([tuple(row) for row in db.execute("SELECT * FROM messages")])
        stored += json.dumps([tuple(row) for row in db.execute("SELECT * FROM message_receipts")])
        assert a["capability"] not in stored and b["capability"] not in stored


def test_send_ack_receipts_survive_restart_without_claim_recovery(h):
    a, b = h.register("a"), h.register("b")
    assert h.acquire(a)["status"] == "ok"
    request = send_request(a, b)
    sent = h.service.send(request, h.identity)
    acknowledged = ack_request(b, sent["message"])
    receipt = h.service.ack(acknowledged, h.identity)
    h.service.close()
    h.service.start()
    assert h.service.send(request, h.identity) == sent
    assert h.service.ack(acknowledged, h.identity) == receipt
    assert receive(h, b)["messages"] == []
    assert len(receive(h, b, include_acknowledged=True)["messages"]) == 1
    assert send(h, b, a, kind="response", reply_to=sent["message"]["message_id"])["status"] == "ok"
    snapshot = h.read()
    assert all(p["status"] == "suspect" and p["generation"] == 1 for p in snapshot["participants"])
    assert snapshot["claims"][0]["quarantined"]
    assert h.acquire(b)["error"] == "stale_generation"


def test_operation_collision_and_concurrent_retry_are_atomic(h):
    a, b = h.register("a"), h.register("b")
    request = send_request(a, b)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(pool.map(lambda _: h.service.send(request, h.identity), range(6)))
    assert all(result == results[0] for result in results)
    assert len(receive(h, b)["messages"]) == 1
    changed = parse_send({**request.model_dump(), "body": "different"})
    assert h.service.send(changed, h.identity)["error"] == "idempotency_conflict"
    other_operation = parse_ack(auth(a, operation_id=request.operation_id, message_ids=[results[0]["message"]["message_id"]]))
    assert h.service.ack(other_operation, h.identity)["error"] == "idempotency_conflict"


def test_inbox_and_ack_require_recipient_auth_and_atomic_batch(h):
    a, b, c = [h.register(name) for name in ("a", "b", "c")]
    for_b = send(h, a, b)["message"]
    for_c = send(h, a, c)["message"]
    assert receive(h, a)["messages"] == []
    assert receive(h, b, capability=a["capability"])["error"] == "unauthorized_actor"
    assert receive(h, a, subject_id=b["participant_id"])["error"] == "unauthorized_actor"
    assert ack(h, a, for_b)["error"] == "scope_denied"
    assert ack(h, b, for_b, for_c)["error"] == "scope_denied"
    assert receive(h, b)["messages"] == [for_b]  # Valid first item was rolled back.
    assert send(h, c, a, reply_to=for_b["message_id"])["error"] == "scope_denied"
    assert send(h, b, c, reply_to=for_b["message_id"])["error"] == "scope_denied"


def test_foreign_repository_cannot_read_send_or_ack(h):
    a, b = h.register("a"), h.register("b")
    sent = send(h, a, b)["message"]
    foreign = replace(h.identity, repository_id="different_repository")
    assert h.service.send(send_request(a, b), foreign)["error"] == "workspace_invalid"
    assert h.service.receive(parse_receive(auth(b)), foreign)["error"] == "workspace_invalid"
    assert h.service.ack(ack_request(b, sent), foreign)["error"] == "workspace_invalid"
    external = h.register("other", identity=foreign)
    assert send(h, a, external)["error"] == "unauthorized_actor"
    assert receive(h, external)["messages"] == []


def test_parent_relay_requires_direct_child_and_explicit_mode(h):
    parent, peer, other = [h.register(name) for name in ("parent", "peer", "other")]
    child = h.register("child", parent=parent, communication_mode="parent_relay")
    independent = h.register("independent", parent=parent)
    sent = send(h, peer, child)["message"]
    assert receive(h, parent, subject_id=child["participant_id"])["messages"] == [sent]
    assert receive(h, other, subject_id=child["participant_id"])["error"] == "unauthorized_actor"
    assert receive(h, parent, subject_id=independent["participant_id"])["error"] == "unauthorized_actor"
    response = send(h, parent, peer, subject_id=child["participant_id"], reply_to=sent["message_id"])["message"]
    assert response["sender_id"] == child["participant_id"] and response["actor_id"] == parent["participant_id"]
    assert ack(h, parent, sent, subject_id=child["participant_id"])["status"] == "ok"
    record = receive(h, child, include_acknowledged=True)["messages"][0]
    assert record["acknowledged_by"] == parent["participant_id"]
    assert h.apply(parent, {"kind": "cancel", "reason": "done"})["status"] == "ok"
    assert receive(h, parent, subject_id=child["participant_id"])["error"] == "unauthorized_actor"


def test_pagination_retries_only_pending_messages_without_skipping(h):
    a, b, c = [h.register(name) for name in ("a", "b", "c")]
    first = send(h, a, b)["message"]
    send(h, a, c)
    second = send(h, a, b)["message"]
    third = send(h, a, b)["message"]
    page = receive(h, b, limit=1)
    assert page["messages"] == [first] and page["has_more"]
    assert ack(h, b, second)["status"] == "ok"
    next_page = receive(h, b, limit=1, after_cursor=page["next_cursor"])
    assert next_page["messages"] == [third] and not next_page["has_more"]
    assert receive(h, b, after_cursor=next_page["next_cursor"])["next_cursor"] == third["cursor"]
    assert [m["message_id"] for m in receive(h, b)["messages"]] == [first["message_id"], third["message_id"]]
    assert len(receive(h, b, include_acknowledged=True)["messages"]) == 3


def test_pending_backpressure_is_atomic_and_ack_reopens_capacity(h, monkeypatch):
    monkeypatch.setattr(mailbox, "MAX_PENDING_MESSAGES", 2)
    a, b = h.register("a"), h.register("b")
    first = send(h, a, b)["message"]
    send(h, a, b)
    rejected = send_request(a, b)
    assert h.service.send(rejected, h.identity)["error"] == "mailbox_full"
    assert ack(h, b, first)["status"] == "ok"
    assert h.service.send(rejected, h.identity)["status"] == "ok"
    assert len(receive(h, b)["messages"]) == 2


def test_cancelled_identity_cannot_send_but_can_read_ack_and_retry(h):
    a, b = h.register("a"), h.register("b")
    request = send_request(a, b)
    original = h.service.send(request, h.identity)
    answer = send(h, b, a)["message"]
    assert h.apply(a, {"kind": "cancel", "reason": "done"})["status"] == "ok"
    assert h.service.send(request, h.identity) == original
    assert send(h, a, b)["error"] == "unauthorized_actor"
    assert send(h, b, a)["error"] == "scope_denied"
    assert receive(h, a)["messages"] == [answer]
    assert ack(h, a, answer)["status"] == "ok"
    with h.service.store.transaction() as db:
        db.execute("UPDATE participants SET verifier='' WHERE id=?", (a["participant_id"],))
    assert receive(h, a)["error"] == "unauthorized_actor"
    assert h.service.send(request, h.identity)["error"] == "unauthorized_actor"


def test_mail_protects_used_registrations_and_is_not_retention_collected(h):
    a, b, unused = [h.register(name) for name in ("a", "b", "unused")]
    request = send_request(a, b)
    sent = h.service.send(request, h.identity)
    h.now += 31 * 86400
    h.service.collect_terminal()
    assert receive(h, b)["messages"] == [sent["message"]]
    assert h.service.send(request, h.identity) == sent
    assert receive(h, unused)["error"] == "unauthorized_actor"
    assert ack(h, b, sent["message"])["status"] == "ok"
    h.now += 31 * 86400
    h.service.collect_terminal()
    assert len(receive(h, b, include_acknowledged=True)["messages"]) == 1


def test_mail_does_not_renew_heartbeat_or_clear_quarantine(h):
    a, b = h.register("a"), h.register("b")
    assert h.acquire(a)["status"] == "ok"
    h.now += 121
    sent = send(h, a, b)["message"]
    assert ack(h, b, sent)["status"] == "ok"
    snapshot = h.read()
    assert all(p["last_seen"] == 1000.0 and p["status"] == "suspect" for p in snapshot["participants"])
    assert snapshot["claims"][0]["quarantined"]


def test_successful_empty_poll_protects_listener_without_renewing_heartbeat(h):
    listener, unused = h.register("listener"), h.register("unused")
    assert receive(h, listener)["messages"] == []
    assert receive(h, unused, capability="wrong")["error"] == "unauthorized_actor"
    h.now += 31 * 86400
    assert receive(h, listener)["messages"] == []
    h.service.collect_terminal()
    assert receive(h, listener)["messages"] == []
    assert receive(h, unused)["error"] == "unauthorized_actor"
    states = {p["task_id"]: p for p in h.read()["participants"]}
    assert states["listener"]["status"] == "suspect"
    assert states["listener"]["last_seen"] == 1000.0
    assert states["listener"]["generation"] == 1


def test_largest_supported_inbox_cursor_has_empty_page(h):
    listener = h.register("listener")
    response = receive(h, listener, after_cursor=2**63 - 1)
    assert response["messages"] == []
    assert response["next_cursor"] == 2**63 - 1
    assert not response["has_more"]


def test_failed_poll_projection_does_not_record_authenticated_activity(h, monkeypatch):
    listener = h.register("listener")
    def invalid(_):
        raise ValueError("bad projection")
    monkeypatch.setattr(ReceiveResponse, "model_validate", invalid)
    response = h.service.receive(parse_receive(auth(listener)), h.identity)
    assert response["error"] == "store_unavailable"
    with h.service.store.transaction(write=False) as db:
        assert db.execute("SELECT COUNT(*) FROM participant_activity").fetchone()[0] == 0


def test_projection_failure_rolls_back_message_and_operation_receipt(h, monkeypatch):
    a, b = h.register("a"), h.register("b")
    def invalid(_):
        raise ValueError("bad projection")
    monkeypatch.setattr(SendResponse, "model_validate", invalid)
    result = h.service.send(send_request(a, b), h.identity)
    assert result["error"] == "store_unavailable"
    with h.service.store.transaction(write=False) as db:
        assert db.execute("SELECT COUNT(*) FROM messages").fetchone()[0] == 0
        assert db.execute("SELECT COUNT(*) FROM message_receipts").fetchone()[0] == 0


def test_v2_migration_preserves_participants_claims_receipts_and_adds_mail(h):
    a, b = h.register("a"), h.register("b")
    acquired = h.acquire(a)
    h.service.close()
    with sqlite3.connect(h.service.store.path) as db:
        db.execute("DROP TABLE message_receipts")
        db.execute("DROP TABLE messages")
        db.execute("PRAGMA user_version=2")
    h.service.start()
    assert send(h, a, b)["status"] == "ok"
    snapshot = h.read()
    assert snapshot["claims"][0]["claim_id"] == acquired["claims"][0]["claim_id"]
    assert snapshot["claims"][0]["quarantined"]
    with h.service.store.transaction(write=False) as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
        assert db.execute("SELECT COUNT(*) FROM participants").fetchone()[0] == 2
        assert db.execute("SELECT COUNT(*) FROM receipts").fetchone()[0] == 1


def test_disabled_admission_preserves_read_ack_and_receipts(h):
    a, b = h.register("a"), h.register("b")
    request = send_request(a, b)
    sent = h.service.send(request, h.identity)
    h.service.enabled = False
    assert h.service.send(request, h.identity) == sent
    assert send(h, a, b)["error"] == "store_unavailable"
    assert receive(h, b)["messages"] == [sent["message"]]
    assert ack(h, b, sent["message"])["status"] == "ok"
