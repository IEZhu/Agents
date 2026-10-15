"""Durable MCP mail between participants; delivery never changes execution rights.

Messages and receipts are separate from the public cooperation event journal.
Polling is non-destructive. Explicit acknowledgement provides at-least-once
delivery, while operation receipts make uncertain send/ack retries idempotent.
"""
import hashlib
import json
import uuid

from .models import PROTOCOL, SendResponse, ReceiveResponse, AckResponse
from .store import CooperationError, canonical


MAX_PENDING_MESSAGES = 1000


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

    def send(self, request, identity):
        try:
            self.service._available()
            data = request.model_dump(mode="json")
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
                           "created_at": self.service.clock()}
                cursor = db.execute("INSERT INTO messages(id,repository_id,sender_id,recipient_id,body) VALUES (?,?,?,?,?)",
                                    (message["message_id"], repo, message["sender_id"], message["recipient_id"], canonical(message))).lastrowid
                message.update(cursor=cursor, acknowledged_at=None, acknowledged_by=None)
                response = self._response(SendResponse, {"protocol": PROTOCOL, "status": "ok", "repository_id": repo, "message": message})
                self._save_receipt(db, data, request_digest, response)
                # Incoming mail also makes a registration used: never tombstone
                # a recipient while its durable inbox still awaits inspection.
                self._activity(db, actor, subject, recipient)
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
                for identifier in data["message_ids"]:
                    row = db.execute("SELECT recipient_id FROM messages WHERE repository_id=? AND id=?",
                                     (data["repository_id"], identifier)).fetchone()
                    if not row or row[0] != subject["participant_id"]:
                        raise CooperationError("scope_denied", "Only the recipient may acknowledge a message")
                    db.execute("UPDATE messages SET acknowledged_at=?,acknowledged_by=? WHERE id=? AND acknowledged_at IS NULL",
                               (now, actor["participant_id"], identifier))
                response = self._response(AckResponse, {"protocol": PROTOCOL, "status": "ok", "repository_id": data["repository_id"],
                                         "participant_id": subject["participant_id"], "message_ids": data["message_ids"], "acknowledged_at": now})
                self._save_receipt(db, data, request_digest, response)
                self._activity(db, actor, subject)
                return response
        except CooperationError as error:
            return self._error(error)
