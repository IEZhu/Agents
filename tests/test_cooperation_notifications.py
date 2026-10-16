"""Private durable notifications and bounded, cancellable asynchronous waits."""
import asyncio
from dataclasses import replace
import json
import time

import pytest

from src.cooperation.mailbox import Mailbox
from src.cooperation.models import WaitResponse, parse_wait
from tests.test_cooperation_mailbox import auth, ack, send, send_request
from tests.test_cooperation_requests import status, update, update_request
from tests.test_cooperation_service import Harness


@pytest.fixture
def h(tmp_path):
    harness = Harness(tmp_path)
    yield harness
    harness.service.close()


def notifications(h, actor, **extra):
    result, _ = Mailbox(h.service).notifications(parse_wait(auth(actor, timeout_seconds=0, **extra)), actor["identity"])
    WaitResponse.model_validate(result)
    return result


def wait_request(actor, **extra):
    return parse_wait(auth(actor, timeout_seconds=2, **extra))


async def listening(h, count=1):
    async def registered():
        while sum(map(len, h.service.mailbox_notifier._listeners.values())) != count:
            await asyncio.sleep(0.001)
    await asyncio.wait_for(registered(), 1)


def test_notification_pages_are_private_and_non_destructive(h):
    sender, recipient, other = [h.register(name) for name in ("sender", "recipient", "other")]
    first = send(h, sender, recipient)["message"]
    unrelated = send(h, sender, other)["message"]
    second = send(h, sender, recipient)["message"]
    third = send(h, sender, recipient)["message"]
    page = notifications(h, recipient, limit=1)
    assert page == notifications(h, recipient, limit=1)
    assert [event["message_id"] for event in page["events"]] == [first["message_id"]]
    assert page["has_more"] and not page["timed_out"]
    page2 = notifications(h, recipient, after_cursor=page["next_cursor"], limit=1)
    assert [event["message_id"] for event in page2["events"]] == [second["message_id"]]
    assert page2["has_more"]
    page3 = notifications(h, recipient, after_cursor=page2["next_cursor"], limit=1)
    assert [event["message_id"] for event in page3["events"]] == [third["message_id"]]
    assert not page3["has_more"]
    empty = notifications(h, recipient, after_cursor=page3["next_cursor"])
    assert empty["events"] == [] and empty["next_cursor"] == page3["next_cursor"]
    assert notifications(h, sender)["events"] == []
    assert [event["message_id"] for event in notifications(h, other)["events"]] == [unrelated["message_id"]]
    encoded = json.dumps(page)
    assert unrelated["message_id"] not in encoded and first["body"] not in encoded
    assert sender["capability"] not in encoded and recipient["capability"] not in encoded
    assert h.read()["events"] == []


def test_send_ack_and_update_replays_do_not_duplicate_notification_events(h):
    sender, recipient = h.register("sender"), h.register("recipient")
    request = send_request(sender, recipient, kind="request")
    sent = h.service.send(request, h.identity)
    assert h.service.send(request, h.identity) == sent
    assert len(notifications(h, recipient)["events"]) == 1
    assert len(notifications(h, sender)["events"]) == 1
    message = sent["message"]
    assert ack(h, recipient, message)["status"] == "ok"
    assert ack(h, recipient, message)["status"] == "ok"  # A different ack operation still has no new effect.
    request_update = update_request(recipient, message, "accepted")
    updated = h.service.request_update(request_update, h.identity)
    assert updated["status"] == "ok"
    assert h.service.request_update(request_update, h.identity) == updated
    sender_events = notifications(h, sender)["events"]
    assert [event["kind"] for event in sender_events] == ["request_updated", "message_acknowledged", "request_updated"]
    assert [event["request_revision"] for event in sender_events] == [1, None, 2]
    assert [event["kind"] for event in notifications(h, recipient)["events"]] == ["message_received", "request_updated"]


def test_notification_authority_checks_capability_repository_and_direct_child_relay(h):
    parent, peer, stranger = [h.register(name) for name in ("parent", "peer", "stranger")]
    child = h.register("child", parent=parent, communication_mode="parent_relay")
    independent = h.register("independent", parent=parent)
    grandchild = h.register("grandchild", parent=child, root_task_id=parent["task"], communication_mode="parent_relay")
    message = send(h, peer, child)["message"]
    relayed = notifications(h, parent, subject_id=child["participant_id"])
    assert relayed["participant_id"] == child["participant_id"]
    assert [event["message_id"] for event in relayed["events"]] == [message["message_id"]]
    assert notifications(h, parent)["events"] == []
    assert notifications(h, stranger, subject_id=child["participant_id"])["error"] == "unauthorized_actor"
    assert notifications(h, parent, subject_id=independent["participant_id"])["error"] == "unauthorized_actor"
    assert notifications(h, parent, subject_id=grandchild["participant_id"])["error"] == "unauthorized_actor"
    assert notifications(h, child, capability=parent["capability"])["error"] == "unauthorized_actor"
    foreign = replace(h.identity, repository_id="foreign_repository")
    response, _ = Mailbox(h.service).notifications(parse_wait(auth(child)), foreign)
    assert response["error"] == "workspace_invalid"
    assert h.apply(parent, {"kind": "cancel", "reason": "finished"})["status"] == "ok"
    assert notifications(h, parent, subject_id=child["participant_id"])["error"] == "unauthorized_actor"


@pytest.mark.asyncio
async def test_wait_returns_existing_events_immediately_and_zero_timeout_reports_empty(h):
    sender, recipient = h.register("sender"), h.register("recipient")
    sent = send(h, sender, recipient)["message"]
    response = await asyncio.wait_for(h.service.wait(wait_request(recipient), h.identity), 1)
    assert [event["message_id"] for event in response["events"]] == [sent["message_id"]]
    assert not response["timed_out"]
    empty_request = parse_wait(auth(recipient, after_cursor=response["next_cursor"], timeout_seconds=0))
    empty = await h.service.wait(empty_request, h.identity)
    assert empty["status"] == "ok" and empty["events"] == [] and empty["timed_out"]
    assert empty["next_cursor"] == response["next_cursor"]
    assert not h.service.mailbox_notifier._listeners


@pytest.mark.asyncio
async def test_wait_wakes_for_new_message_and_all_concurrent_listeners(h):
    sender, recipient = h.register("sender"), h.register("recipient")
    waits = [asyncio.create_task(h.service.wait(wait_request(recipient), h.identity)) for _ in range(3)]
    await listening(h, 3)
    message = send(h, sender, recipient)["message"]
    results = await asyncio.wait_for(asyncio.gather(*waits), 1)
    assert all(result == results[0] for result in results)
    assert [event["message_id"] for event in results[0]["events"]] == [message["message_id"]]
    assert not results[0]["timed_out"]
    assert not h.service.mailbox_notifier._listeners


@pytest.mark.asyncio
async def test_message_arriving_after_empty_read_does_not_lose_wakeup(h, monkeypatch):
    sender, recipient = h.register("sender"), h.register("recipient")
    original = Mailbox.notifications
    sent = []
    def send_between_read_and_wait(mailbox, request, identity):
        response = original(mailbox, request, identity)
        if not sent:
            sent.append(send(h, sender, recipient)["message"])
        return response
    monkeypatch.setattr(Mailbox, "notifications", send_between_read_and_wait)
    response = await asyncio.wait_for(h.service.wait(wait_request(recipient), h.identity), 1)
    assert len(sent) == 1
    assert [event["message_id"] for event in response["events"]] == [sent[0]["message_id"]]
    assert not response["timed_out"]


@pytest.mark.asyncio
async def test_failed_wait_authentication_cleans_up_listener(h):
    sender, recipient = h.register("sender"), h.register("recipient")
    request = wait_request(recipient, capability=sender["capability"])
    response = await asyncio.wait_for(h.service.wait(request, h.identity), 1)
    assert response["error"] == "unauthorized_actor"
    assert not h.service.mailbox_notifier._listeners


@pytest.mark.asyncio
async def test_wait_does_not_return_another_participants_message(h):
    sender, recipient, other = [h.register(name) for name in ("sender", "recipient", "other")]
    pending = asyncio.create_task(h.service.wait(wait_request(recipient), h.identity))
    await listening(h)
    hidden = send(h, sender, other)["message"]
    await asyncio.sleep(0.02)
    assert not pending.done()
    visible = send(h, sender, recipient)["message"]
    response = await asyncio.wait_for(pending, 1)
    assert [event["message_id"] for event in response["events"]] == [visible["message_id"]]
    assert hidden["message_id"] not in json.dumps(response)


@pytest.mark.asyncio
async def test_wait_wakes_sender_for_delivery_ack(h):
    sender, recipient = h.register("sender"), h.register("recipient")
    message = send(h, sender, recipient)["message"]
    pending = asyncio.create_task(h.service.wait(wait_request(sender), h.identity))
    await listening(h)
    assert ack(h, recipient, message)["status"] == "ok"
    response = await asyncio.wait_for(pending, 1)
    assert [event["kind"] for event in response["events"]] == ["message_acknowledged"]
    assert response["events"][0]["message_id"] == message["message_id"]


@pytest.mark.asyncio
async def test_processing_change_wakes_both_parties_with_revision(h):
    sender, recipient = h.register("sender"), h.register("recipient")
    message = send(h, sender, recipient, kind="request")["message"]
    cursors = {actor["participant_id"]: notifications(h, actor)["next_cursor"] for actor in (sender, recipient)}
    pending = [asyncio.create_task(h.service.wait(wait_request(actor, after_cursor=cursors[actor["participant_id"]]), h.identity))
               for actor in (sender, recipient)]
    await listening(h, 2)
    assert update(h, recipient, message, "input_required", reason="Need the artifact")["status"] == "ok"
    responses = await asyncio.wait_for(asyncio.gather(*pending), 1)
    for response in responses:
        assert len(response["events"]) == 1
        event = response["events"][0]
        assert (event["kind"], event["request_state"], event["request_revision"]) == ("request_updated", "input_required", 2)
        assert event["message_id"] == message["message_id"]
        assert "Need the artifact" not in json.dumps(response)


@pytest.mark.asyncio
async def test_deadline_alone_wakes_wait_without_any_message_or_state_operation(h):
    sender, recipient = h.register("sender"), h.register("recipient")
    started = time.monotonic()
    h.service.clock = lambda: h.now + time.monotonic() - started
    message = send(h, sender, recipient, kind="request", expires_at=h.now + 0.15)["message"]
    cursor = notifications(h, sender)["next_cursor"]
    response = await asyncio.wait_for(h.service.wait(wait_request(sender, after_cursor=cursor), h.identity), 1)
    assert not response["timed_out"]
    assert len(response["events"]) == 1
    event = response["events"][0]
    assert (event["kind"], event["request_state"], event["request_revision"]) == ("request_updated", "expired", 2)
    assert event["message_id"] == message["message_id"]
    assert status(h, recipient, message)["request"]["state"] == "expired"


@pytest.mark.asyncio
async def test_wait_timeout_preserves_cursor_and_unregisters_listener(h):
    actor = h.register("actor")
    request = parse_wait(auth(actor, after_cursor=17, timeout_seconds=1))
    response = await asyncio.wait_for(h.service.wait(request, h.identity), 2)
    assert response["events"] == [] and response["timed_out"]
    assert response["next_cursor"] == 17 and not response["has_more"]
    assert not h.service.mailbox_notifier._listeners


@pytest.mark.asyncio
async def test_cancelled_wait_unregisters_listener_and_later_wait_still_works(h):
    sender, recipient = h.register("sender"), h.register("recipient")
    pending = asyncio.create_task(h.service.wait(wait_request(recipient), h.identity))
    await listening(h)
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending
    assert not h.service.mailbox_notifier._listeners
    sent = send(h, sender, recipient)["message"]
    response = await asyncio.wait_for(h.service.wait(wait_request(recipient), h.identity), 1)
    assert response["events"][0]["message_id"] == sent["message_id"]
    assert not h.service.mailbox_notifier._listeners


@pytest.mark.asyncio
async def test_shutdown_wakes_pending_wait_with_unavailable_and_cleans_listener(h):
    actor = h.register("actor")
    pending = asyncio.create_task(h.service.wait(wait_request(actor), h.identity))
    await listening(h)
    h.service.close()
    response = await asyncio.wait_for(pending, 1)
    assert response["error"] == "store_unavailable"
    assert not h.service.mailbox_notifier._listeners


@pytest.mark.asyncio
async def test_notification_cursors_and_request_states_survive_coordinator_restart(h):
    sender, recipient = h.register("sender"), h.register("recipient")
    message = send(h, sender, recipient, kind="request")["message"]
    first = notifications(h, sender)
    assert update(h, recipient, message, "accepted")["status"] == "ok"
    h.service.close()
    h.service.start()
    request = parse_wait(auth(sender, after_cursor=first["next_cursor"], timeout_seconds=0))
    result = await h.service.wait(request, h.identity)
    assert result["service_epoch"] == first["service_epoch"] + 1
    assert [(event["request_state"], event["request_revision"]) for event in result["events"]] == [("accepted", 2)]
    assert result["next_cursor"] > first["next_cursor"]
    assert status(h, sender, message)["request"]["state"] == "accepted"
    assert all(participant["status"] == "suspect" for participant in h.read()["participants"])


@pytest.mark.asyncio
@pytest.mark.parametrize("with_event", [False, True])
async def test_restart_while_scan_is_dispatched_ends_old_wait(h, monkeypatch, with_event):
    from src.cooperation import notifications as notification_module

    sender, recipient = h.register("sender"), h.register("recipient")
    if with_event:
        send(h, sender, recipient)
    dispatched, release = asyncio.Event(), asyncio.Event()
    original = asyncio.to_thread

    async def delayed_scan(function, *args, **kwargs):
        dispatched.set()
        await release.wait()
        return await original(function, *args, **kwargs)

    monkeypatch.setattr(notification_module.asyncio, "to_thread", delayed_scan)
    pending = asyncio.create_task(h.service.wait(parse_wait(auth(recipient, timeout_seconds=0)), h.identity))
    await asyncio.wait_for(dispatched.wait(), 1)
    h.service.close()
    h.service.start()
    release.set()
    response = await asyncio.wait_for(pending, 1)
    assert response["error"] == "store_unavailable"
    assert not h.service.mailbox_notifier._listeners


def test_failed_notification_projection_rolls_back_deadline_and_activity(h, monkeypatch):
    sender, recipient = h.register("sender"), h.register("recipient")
    message = send(h, sender, recipient, kind="request", expires_at=h.now + 1)["message"]
    with h.service.store.transaction(write=False) as db:
        count = db.execute("SELECT COUNT(*) FROM mailbox_notifications").fetchone()[0]
    h.now += 1
    def invalid(_):
        raise ValueError("bad projection")
    with monkeypatch.context() as patch:
        patch.setattr(WaitResponse, "model_validate", invalid)
        result, _ = Mailbox(h.service).notifications(parse_wait(auth(sender)), h.identity)
        assert result["error"] == "store_unavailable"
    with h.service.store.transaction(write=False) as db:
        assert db.execute("SELECT state FROM mailbox_requests WHERE message_id=?", (message["message_id"],)).fetchone()[0] == "pending"
        assert db.execute("SELECT COUNT(*) FROM mailbox_notifications").fetchone()[0] == count
    assert notifications(h, sender)["events"][-1]["request_state"] == "expired"


def test_failed_empty_notification_projection_does_not_record_listener_activity(h, monkeypatch):
    actor = h.register("actor")
    def invalid(_):
        raise ValueError("bad projection")
    monkeypatch.setattr(WaitResponse, "model_validate", invalid)
    result, _ = Mailbox(h.service).notifications(parse_wait(auth(actor)), h.identity)
    assert result["error"] == "store_unavailable"
    with h.service.store.transaction(write=False) as db:
        assert db.execute("SELECT COUNT(*) FROM participant_activity").fetchone()[0] == 0


@pytest.mark.parametrize("timeout", [-1, 31, True, 1.0, "1", None])
def test_wait_timeout_is_a_bounded_integer(h, timeout):
    actor = h.register("actor")
    with pytest.raises(ValueError):
        parse_wait(auth(actor, timeout_seconds=timeout))


@pytest.mark.parametrize("fields", [{"limit": 0}, {"limit": 51}, {"after_cursor": -1}, {"after_cursor": 2**63}])
def test_wait_rejects_invalid_page_boundaries(h, fields):
    actor = h.register("actor")
    with pytest.raises(ValueError):
        parse_wait(auth(actor, **fields))
