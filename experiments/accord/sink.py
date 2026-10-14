"""Independent simulated destination: CAS and durable operation deduplication."""

import uuid

from .ir import digest, require, text_digest
from .store import Store


class SQLiteSink:
    idempotent = True

    def __init__(self, path):
        self.store = Store(path)
        with self.store.transaction() as db:
            identity = self.store.get(db, "meta", "identity", optional=True)
            if identity is None:
                identity = uuid.uuid4().hex
                self.store.put(db, "meta", "identity", identity)
            self.identity = identity

    def target(self, name):
        with self.store.transaction() as db:
            return self.store.get(db, "target", name, optional=True) or {"version": 0, "text": ""}

    def lookup(self, operation):
        with self.store.transaction() as db:
            return self.store.get(db, "result", operation, optional=True)

    def apply(self, operation, intent, text):
        fingerprint = digest("accord.intent/0.1", intent)
        require(intent["sink_identity"] == self.identity, "wrong destination identity")
        require(text_digest(text) == intent["output"], "payload digest mismatch")
        with self.store.transaction() as db:
            prior = self.store.get(db, "result", operation, optional=True)
            if prior:
                require(prior["intent"] == fingerprint, "operation ID reused for another intent")
                return prior
            target = self.store.get(db, "target", intent["target"], optional=True) or {"version": 0, "text": ""}
            status = "denied"
            if target["version"] == intent["expected_version"]:
                status = "confirmed"
                target = {"version": target["version"] + 1, "text": text}
                self.store.put(db, "target", intent["target"], target)
            result = {"operation": operation, "intent": fingerprint, "status": status,
                      "target_version": target["version"]}
            self.store.put(db, "result", operation, result)
            return result
