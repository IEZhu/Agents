"""Durable MCP mail between participants; delivery never changes execution rights.

Messages and receipts are separate from the public cooperation event journal.
Polling is non-destructive. Explicit acknowledgement provides at-least-once
delivery, while operation receipts make uncertain send/ack retries idempotent.
"""
import hashlib
import json
import uuid

from .models import (PROTOCOL, SendResponse, ReceiveResponse, AckResponse,
                     RequestStatusResponse, WaitResponse)
from .store import CooperationError, canonical


MAX_PENDING_MESSAGES = 1000
TERMINAL_REQUEST_STATES = frozenset({"completed", "declined", "failed", "cancelled", "expired"})
PROCESSING_TRANSITIONS = {
    "pending": {"accepted", "input_required", "completed", "declined", "failed", "cancelled"},
    "accepted": {"input_required", "completed", "failed", "cancelled"},
    "input_required": {"accepted", "completed", "declined", "failed", "cancelled"},
}


class Mailbox:
    def __init__(self, service):
        self.service = service

    def _authenticate(self, db, data, identity):
        service = self.service
        service._epoch_check(db)
        repo = data["repository_id"]
        if repo != identity.repository_id:
            raise CooperationError("workspace_invalid")
        actor = service._authenticate(db, repo, data["participant_id"], data["capability"], allow_terminal=True)
        subject = actor
        if data.get("subject_id") and data["subject_id"] != actor["participant_id"]:
            subject = service._participant(db, repo, data["subject_id"])
            if (subject.get("parent_id") != actor["participant_id"]
                    or subject["communication_mode"] != "parent_relay"
                    or actor["status"] == "cancelled"):
                raise CooperationError("unauthorized_actor", "Mailbox relay requires a registered direct child in parent_relay mode")
        return actor, subject

    @staticmethod
    def _error(error):
        return {"protocol": PROTOCOL, "status": "error", "error": error.code, "message": error.message}

    @staticmethod
    def _response(model, response):
        try:
            model.model_validate(response)
        except ValueError as error:
            raise CooperationError("store_unavailable", "Mailbox projection failed; no mutation was committed") from error
        return response

    @staticmethod
    def _receipt(db, data, operation):
        request_digest = hashlib.sha256(canonical({"operation": operation, "request": data}).encode()).hexdigest()
        row = db.execute("SELECT digest,body FROM message_receipts WHERE repository_id=? AND actor_id=? AND operation_id=?",
                         (data["repository_id"], data["participant_id"], data["operation_id"])).fetchone()
        if row:
            if row[0] != request_digest:
                raise CooperationError("idempotency_conflict")
            return request_digest, json.loads(row[1])
        return request_digest, None

    @staticmethod
    def _save_receipt(db, data, request_digest, response):
        db.execute("INSERT INTO message_receipts VALUES (?,?,?,?,?)",
                   (data["repository_id"], data["participant_id"], data["operation_id"], request_digest, canonical(response)))

    @staticmethod
    def _activity(db, *participants):
        for participant in participants:
            db.execute("INSERT OR IGNORE INTO participant_activity VALUES (?)", (participant["participant_id"],))

    @staticmethod
    def _message(row):
        return {**json.loads(row["body"]), "cursor": row["cursor"],
                "acknowledged_at": row["acknowledged_at"], "acknowledged_by": row["acknowledged_by"]}

    def _notify(self, db, repo, participants, kind, message_id, now, record=None):
        event = {"kind": kind, "message_id": message_id, "created_at": now,
                 "request_state": record["state"] if record else None,
                 "request_revision": record["revision"] if record else None}
        for participant in set(participants):
            db.execute("INSERT INTO mailbox_notifications(repository_id,participant_id,body) VALUES (?,?,?)",
                       (repo, participant, canonical(event)))

    def _save_request(self, db, repo, record):
        db.execute("UPDATE mailbox_requests SET body=?,state=?,expires_at=? WHERE repository_id=? AND message_id=?",
                   (canonical(record), record["state"], record["expires_at"], repo, record["message_id"]))

    def _expire(self, db, repo, record, now):
        if (record["state"] not in TERMINAL_REQUEST_STATES and record["expires_at"] is not None
                and now >= record["expires_at"]):
            record.update(state="expired", revision=record["revision"] + 1, updated_at=now,
                          reason="Request deadline elapsed", updated_by=None)
            self._save_request(db, repo, record)
            self._notify(db, repo, [record["sender_id"], record["recipient_id"]],
                         "request_updated", record["message_id"], now, record)
            return True
        return False

    @staticmethod
    def _request(db, repo, message_id, subject):
        row = db.execute("SELECT body FROM mailbox_requests WHERE repository_id=? AND message_id=?",
                         (repo, message_id)).fetchone()
        if not row:
            raise CooperationError("scope_denied", "Request is unavailable to this participant")
        record = json.loads(row[0])
        if subject["participant_id"] not in (record["sender_id"], record["recipient_id"]):
            raise CooperationError("scope_denied", "Request is unavailable to this participant")
        return record

    @staticmethod
    def _request_projection(db, record):
        row = db.execute("SELECT acknowledged_at FROM messages WHERE id=?", (record["message_id"],)).fetchone()
        acknowledged_at = row[0]
        return {**record, "delivery": "acknowledged" if acknowledged_at is not None else "stored",
                "acknowledged_at": acknowledged_at}

    def send(self, request, identity):
        try:
            self.service._available()
            data = request.model_dump(mode="json")
            if data.get("expires_at") is None:
                data.pop("expires_at", None)  # Preserve pre-upgrade send receipt fingerprints.
            with self.service.store.transaction() as db:
                actor, subject = self._authenticate(db, data, identity)
                request_digest, receipt = self._receipt(db, data, "send")
                if receipt is not None:
                    return self._response(SendResponse, receipt)
                if actor["status"] == "cancelled" or subject["status"] == "cancelled":
                    raise CooperationError("unauthorized_actor")
                if not self.service.enabled:
                    raise CooperationError("store_unavailable", "New admission is disabled; existing mail remains readable")
                repo = data["repository_id"]
                now = self.service.clock()
                if data.get("expires_at") is not None and data["expires_at"] <= now:
                    raise CooperationError("invalid_request", "Request deadline must be in the future")
                recipient = self.service._participant(db, repo, data["recipient_id"])
                if recipient["status"] == "cancelled":
                    raise CooperationError("scope_denied", "Recipient task is cancelled; use its newly registered attempt")
                if data["reply_to"] is not None:
                    original = db.execute("SELECT sender_id,recipient_id FROM messages WHERE repository_id=? AND id=?",
                                          (repo, data["reply_to"])).fetchone()
                    if (not original or original["sender_id"] != data["recipient_id"]
                            or original["recipient_id"] != subject["participant_id"]):
                        raise CooperationError("scope_denied", "Reply must reference a message from this recipient to this sender")
                pending = db.execute("SELECT COUNT(*) FROM messages WHERE repository_id=? AND recipient_id=? AND acknowledged_at IS NULL",
                                     (repo, data["recipient_id"])).fetchone()[0]
                if pending >= MAX_PENDING_MESSAGES:
                    raise CooperationError("mailbox_full", "Recipient has 1000 pending messages; wait for explicit acknowledgement")
                message = {"message_id": str(uuid.uuid4()), "repository_id": repo,
                           "sender_id": subject["participant_id"], "recipient_id": data["recipient_id"],
                           "actor_id": actor["participant_id"], "kind": data["kind"], "body": data["body"],
                           "reply_to": data["reply_to"], "correlation_id": data["correlation_id"],
                           "created_at": now}
                cursor = db.execute("INSERT INTO messages(id,repository_id,sender_id,recipient_id,body) VALUES (?,?,?,?,?)",
                                    (message["message_id"], repo, message["sender_id"], message["recipient_id"], canonical(message))).lastrowid
                message.update(cursor=cursor, acknowledged_at=None, acknowledged_by=None)
                self._notify(db, repo, [recipient["participant_id"]], "message_received", message["message_id"], now)
                if data["kind"] == "request":
                    record = {"message_id": message["message_id"], "sender_id": subject["participant_id"],
                              "recipient_id": recipient["participant_id"], "state": "pending", "revision": 1,
                              "created_at": now, "updated_at": now, "expires_at": data.get("expires_at"),
                              "reason": None, "response_message_id": None, "updated_by": actor["participant_id"]}
                    db.execute("INSERT INTO mailbox_requests VALUES (?,?,?,?,?,?,?)",
                               (message["message_id"], repo, subject["participant_id"], recipient["participant_id"],
                                canonical(record), "pending", record["expires_at"]))
                    self._notify(db, repo, [subject["participant_id"]], "request_updated", message["message_id"], now, record)
                response = self._response(SendResponse, {"protocol": PROTOCOL, "status": "ok", "repository_id": repo, "message": message})
                self._save_receipt(db, data, request_digest, response)
                # Incoming mail also makes a registration used: never tombstone
                # a recipient while its durable inbox still awaits inspection.
                self._activity(db, actor, subject, recipient)
            self.service.mailbox_notifier.notify({(repo, subject["participant_id"]), (repo, recipient["participant_id"])})
            return response
        except CooperationError as error:
            return self._error(error)

    def receive(self, request, identity):
        try:
            self.service._available()
            data = request.model_dump(mode="json")
            with self.service.store.transaction() as db:
                actor, subject = self._authenticate(db, data, identity)
                # The optional SQL fragment is an internal constant, never text
                # from the caller. Fetch one extra row for reliable pagination.
                pending = "" if data["include_acknowledged"] else " AND acknowledged_at IS NULL"
                rows = db.execute("SELECT * FROM messages WHERE repository_id=? AND recipient_id=? AND cursor>?"
                                  + pending + " ORDER BY cursor LIMIT ?",
                                  (data["repository_id"], subject["participant_id"], data["after_cursor"], data["limit"] + 1)).fetchall()
                page = rows[:data["limit"]]
                response = self._response(ReceiveResponse, {"protocol": PROTOCOL, "status": "ok", "repository_id": data["repository_id"],
                                      "participant_id": subject["participant_id"], "messages": [self._message(row) for row in page],
                                      "next_cursor": page[-1]["cursor"] if page else data["after_cursor"], "has_more": len(rows) > len(page)})
                # Authentication bookkeeping prevents a proven mailbox listener
                # being retired as a lost registration. This never renews its
                # heartbeat, consumes mail or modifies execution authority.
                self._activity(db, actor, subject)
                return response
        except CooperationError as error:
            return self._error(error)

    def ack(self, request, identity):
        try:
            self.service._available()
            data = request.model_dump(mode="json")
            with self.service.store.transaction() as db:
                actor, subject = self._authenticate(db, data, identity)
                request_digest, receipt = self._receipt(db, data, "ack")
                if receipt is not None:
                    return self._response(AckResponse, receipt)
                now = self.service.clock()
                changed_keys = set()
                for identifier in data["message_ids"]:
                    row = db.execute("SELECT recipient_id,sender_id,acknowledged_at FROM messages WHERE repository_id=? AND id=?",
                                     (data["repository_id"], identifier)).fetchone()
                    if not row or row[0] != subject["participant_id"]:
                        raise CooperationError("scope_denied", "Only the recipient may acknowledge a message")
                    db.execute("UPDATE messages SET acknowledged_at=?,acknowledged_by=? WHERE id=? AND acknowledged_at IS NULL",
                               (now, actor["participant_id"], identifier))
                    if row["acknowledged_at"] is None:
                        self._notify(db, data["repository_id"], [row["sender_id"]], "message_acknowledged", identifier, now)
                        changed_keys.add((data["repository_id"], row["sender_id"]))
                response = self._response(AckResponse, {"protocol": PROTOCOL, "status": "ok", "repository_id": data["repository_id"],
                                         "participant_id": subject["participant_id"], "message_ids": data["message_ids"], "acknowledged_at": now})
                self._save_receipt(db, data, request_digest, response)
                self._activity(db, actor, subject)
            self.service.mailbox_notifier.notify(changed_keys)
            return response
        except CooperationError as error:
            return self._error(error)

    def request_status(self, request, identity):
        try:
            self.service._available()
            data = request.model_dump(mode="json")
            with self.service.store.transaction() as db:
                actor, subject = self._authenticate(db, data, identity)
                repo = data["repository_id"]
                record = self._request(db, repo, data["message_id"], subject)
                expired = self._expire(db, repo, record, self.service.clock())
                response = self._response(RequestStatusResponse, {"protocol": PROTOCOL, "status": "ok",
                    "repository_id": repo, "request": self._request_projection(db, record)})
                self._activity(db, actor, subject)
            if expired:
                self.service.mailbox_notifier.notify({(repo, record["sender_id"]), (repo, record["recipient_id"])})
            return response
        except CooperationError as error:
            return self._error(error)

    def request_update(self, request, identity):
        try:
            self.service._available()
            data = request.model_dump(mode="json")
            with self.service.store.transaction() as db:
                actor, subject = self._authenticate(db, data, identity)
                request_digest, receipt = self._receipt(db, data, "request_update")
                if receipt is not None:
                    return self._response(RequestStatusResponse, receipt)
                if actor["status"] == "cancelled" or subject["status"] == "cancelled":
                    raise CooperationError("unauthorized_actor")
                repo, now = data["repository_id"], self.service.clock()
                record = self._request(db, repo, data["message_id"], subject)
                required = record["sender_id"] if data["state"] == "cancelled" else record["recipient_id"]
                if subject["participant_id"] != required:
                    raise CooperationError("scope_denied", "Only the request sender cancels; only its recipient reports processing")
                self._expire(db, repo, record, now)
                if record["state"] in TERMINAL_REQUEST_STATES or record["revision"] != data["expected_revision"]:
                    response = self._error(CooperationError("stale_revision", "Inspect the current request state and revision"))
                else:
                    if data["state"] not in PROCESSING_TRANSITIONS[record["state"]]:
                        raise CooperationError("invalid_request", "This processing state transition is not allowed")
                    if data["response_message_id"] is not None:
                        row = db.execute("SELECT body FROM messages WHERE repository_id=? AND id=?",
                                         (repo, data["response_message_id"])).fetchone()
                        reply = json.loads(row[0]) if row else {}
                        if (reply.get("kind") != "response" or reply.get("reply_to") != record["message_id"]
                                or reply.get("sender_id") != record["recipient_id"] or reply.get("recipient_id") != record["sender_id"]):
                            raise CooperationError("scope_denied", "Completion must reference this recipient's response to the request")
                    record.update(state=data["state"], revision=record["revision"] + 1, updated_at=now,
                                  reason=data["reason"], response_message_id=data["response_message_id"],
                                  updated_by=actor["participant_id"])
                    self._save_request(db, repo, record)
                    self._notify(db, repo, [record["sender_id"], record["recipient_id"]], "request_updated", record["message_id"], now, record)
                    response = self._response(RequestStatusResponse, {"protocol": PROTOCOL, "status": "ok",
                        "repository_id": repo, "request": self._request_projection(db, record)})
                    self._save_receipt(db, data, request_digest, response)
                    self._activity(db, actor, subject)
            self.service.mailbox_notifier.notify({(repo, record["sender_id"]), (repo, record["recipient_id"])})
            return response
        except CooperationError as error:
            return self._error(error)

    def notifications(self, request, identity):
        try:
            self.service._available()
            data = request.model_dump(mode="json")
            with self.service.store.transaction() as db:
                actor, subject = self._authenticate(db, data, identity)
                repo, participant, now = data["repository_id"], subject["participant_id"], self.service.clock()
                active = "state NOT IN ('completed','declined','failed','cancelled','expired')"
                records = db.execute("SELECT body FROM mailbox_requests WHERE repository_id=? AND (sender_id=? OR recipient_id=?) AND "
                                     + active + " AND expires_at<=? ORDER BY expires_at LIMIT 100",
                                     (repo, participant, participant, now)).fetchall()
                changed_keys = set()
                for row in records:
                    record = json.loads(row[0])
                    if self._expire(db, repo, record, now):
                        changed_keys.update({(repo, record["sender_id"]), (repo, record["recipient_id"])})
                next_expiry = db.execute("SELECT MIN(expires_at) FROM mailbox_requests WHERE repository_id=? AND (sender_id=? OR recipient_id=?) AND "
                                        + active, (repo, participant, participant)).fetchone()[0]
                rows = db.execute("SELECT cursor,body FROM mailbox_notifications WHERE repository_id=? AND participant_id=? AND cursor>? ORDER BY cursor LIMIT ?",
                                  (repo, participant, data["after_cursor"], data["limit"] + 1)).fetchall()
                page = rows[:data["limit"]]
                response = self._response(WaitResponse, {"protocol": PROTOCOL, "status": "ok", "repository_id": repo,
                    "participant_id": participant, "events": [{**json.loads(row["body"]), "cursor": row["cursor"]} for row in page],
                    "next_cursor": page[-1]["cursor"] if page else data["after_cursor"],
                    "has_more": len(rows) > len(page), "timed_out": False, "service_epoch": self.service.epoch})
                self._activity(db, actor, subject)
            # Expiration can also wake the other party, after committing its event.
            if changed_keys:
                self.service.mailbox_notifier.notify(changed_keys)
            return response, next_expiry
        except CooperationError as error:
            return self._error(error), None
