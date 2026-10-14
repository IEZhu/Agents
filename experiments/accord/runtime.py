"""Trusted-host interpreter for Accord IR 0.1 (not a Python sandbox)."""

from dataclasses import dataclass
from types import MappingProxyType
from typing import Callable
import uuid

from .ir import Contract, bounded_int, digest, identifier, require, text_digest
from .store import Store


@dataclass(frozen=True)
class Validator:
    revision: str
    run: Callable[[str, dict[str, str]], bool]


class Runtime:
    def __init__(self, path, validators, sinks, *, policy_revision="p1", environment_revision="e1"):
        self.store = Store(path)
        self.validators = MappingProxyType(dict(validators))
        self.sinks = MappingProxyType(dict(sinks))
        identifier(policy_revision, revision=True)
        identifier(environment_revision, revision=True)
        for name, validator in self.validators.items():
            identifier(name)
            identifier(validator.revision, revision=True)
        self.profile = {"policy": policy_revision, "environment": environment_revision,
                        "validators": {name: v.revision for name, v in self.validators.items()}}
        self.profile_id = digest("accord.profile/0.1", self.profile)
        with self.store.transaction() as db:
            if self.store.get(db, "profile", self.profile_id, optional=True) is None:
                self.store.put(db, "profile", self.profile_id, self.profile)
            if self.store.get(db, "meta", "profile", optional=True) is None:
                self.store.put(db, "meta", "profile", {"id": self.profile_id, "epoch": 1})

    def activate_profile(self):
        """Privileged host action, never inferred from contract/model text."""
        with self.store.transaction() as db:
            old = self.store.get(db, "meta", "profile")
            if old["id"] != self.profile_id:
                self.store.put(db, "meta", "profile", {"id": self.profile_id, "epoch": old["epoch"] + 1})

    def set_resource(self, name, text):
        identifier(name)
        require(type(text) is str, "resource must be text")
        fingerprint = text_digest(text)
        with self.store.transaction() as db:
            old = self.store.get(db, "resource", name, optional=True)
            resource = {"text": text, "digest": fingerprint, "revision": old["revision"] + 1 if old else 1}
            self.store.put(db, "resource", name, resource)
            return resource

    def _live(self, db, task):
        current = task
        while current:
            require(current["control"] == "active", "task or ancestor cancelled")
            current = self.store.get(db, "task", current["parent"]) if current["parent"] else None

    def _fresh(self, db, task):
        self._live(db, task)
        active = self.store.get(db, "meta", "profile")
        require(active["id"] == self.profile_id and active == task["profile"], "stale profile")
        for name, snapshot in task["inputs"].items():
            current = self.store.get(db, "resource", name)
            require(current["revision"] == snapshot["revision"] and current["digest"] == snapshot["digest"],
                    f"stale input: {name}")

    def create_task(self, contract, *, parent=None):
        require(isinstance(contract, Contract), "expected a parsed Contract")
        with self.store.transaction() as db:
            profile = self.store.get(db, "meta", "profile")
            require(profile["id"] == self.profile_id, "host profile not active")
            require(contract.policy_revision == self.profile["policy"], "policy revision unavailable")
            require(all(self.profile["validators"].get(k) == v for k, v in contract.checks),
                    "validator revision unavailable")
            if parent:
                owner = self.store.get(db, "task", parent)
                self._fresh(db, owner)
                outer = Contract.parse(owner["contract"])
                require(set(contract.inputs) <= set(outer.inputs), "child expands input scope")
                require(set(contract.publish_to) <= set(outer.publish_to), "child expands effect scope")
                require(contract.output_max_bytes <= outer.output_max_bytes, "child expands output limit")
                require(contract.request_budget <= owner["remaining"], "insufficient parent budget")
                owner["remaining"] -= contract.request_budget
                self.store.put(db, "task", parent, owner)
            task_id = uuid.uuid4().hex
            task = {"id": task_id, "contract": contract.to_dict(), "revision": contract.revision,
                    "inputs": {name: self.store.get(db, "resource", name) for name in contract.inputs},
                    "profile": profile, "parent": parent, "control": "active",
                    "remaining": contract.request_budget, "latest": None, "accepted": None}
            self.store.put(db, "task", task_id, task)
            return task_id

    @staticmethod
    def _inputs(task):
        return {name: value["text"] for name, value in task["inputs"].items()}

    def infer(self, task_id, attempt_label, model, *, crash_hook=None):
        identifier(attempt_label)
        attempt_id = digest("accord.attempt/0.1", [task_id, attempt_label])
        with self.store.transaction() as db:
            task = self.store.get(db, "task", task_id)
            prior = self.store.get(db, "attempt", attempt_id, optional=True)
            if prior:
                require(prior["status"] == "complete", "attempt has no replayable output; use an explicit new attempt")
                return prior
            self._fresh(db, task)
            require(task["remaining"] > 0, "request budget exhausted")
            task["remaining"] -= 1
            task["latest"], task["accepted"] = attempt_id, None
            attempt = {"id": attempt_id, "task": task_id, "label": attempt_label, "status": "started"}
            self.store.put(db, "task", task_id, task)
            self.store.put(db, "attempt", attempt_id, attempt)
        try:
            text = model(self._inputs(task))
            require(type(text) is str, "model output must be text")
            require(len(text.encode("utf-8")) <= task["contract"]["output_max_bytes"], "output too large")
            if crash_hook:
                crash_hook("after_model")
            attempt.update(status="complete", text=text, digest=text_digest(text))
        except Exception as exc:
            attempt.update(status="failed", error=type(exc).__name__)
            with self.store.transaction() as db:
                self.store.put(db, "attempt", attempt_id, attempt)
            raise
        with self.store.transaction() as db:
            self.store.put(db, "attempt", attempt_id, attempt)
        return attempt

    def _binding(self, db, task):
        self._fresh(db, task)
        require(task["latest"] is not None, "task has no candidate")
        attempt = self.store.get(db, "attempt", task["latest"])
        require(attempt["status"] == "complete", "latest attempt has no candidate")
        return {"task": task["id"], "attempt": attempt["id"], "contract": task["revision"],
                "inputs": {k: {"revision": v["revision"], "digest": v["digest"]}
                           for k, v in task["inputs"].items()},
                "output": attempt["digest"], "profile": task["profile"]}

    def check(self, task_id):
        with self.store.transaction() as db:
            task = self.store.get(db, "task", task_id)
            binding = self._binding(db, task)
            attempt = self.store.get(db, "attempt", binding["attempt"])
        receipts = []
        for name, revision in task["contract"]["checks"].items():
            diagnostic = ""
            try:
                passed = self.validators[name].run(attempt["text"], self._inputs(task))
                require(type(passed) is bool, "validator must return bool")
            except Exception as exc:
                passed, diagnostic = False, type(exc).__name__
            receipts.append({"id": uuid.uuid4().hex, "binding": binding, "check": name,
                             "revision": revision, "passed": passed, "diagnostic": diagnostic})
        with self.store.transaction() as db:
            for receipt in receipts:
                self.store.put(db, "check", receipt["id"], receipt)
        return [r["id"] for r in receipts]

    def accept(self, task_id, receipt_ids):
        require(type(receipt_ids) is list and all(type(r) is str for r in receipt_ids), "expected receipt handles")
        require(len(set(receipt_ids)) == len(receipt_ids), "duplicate receipts")
        with self.store.transaction() as db:
            task = self.store.get(db, "task", task_id)
            binding = self._binding(db, task)
            seen = {}
            for receipt_id in receipt_ids:
                receipt = self.store.get(db, "check", receipt_id)
                require(receipt["binding"] == binding and receipt["passed"] is True, "receipt failed or belongs to another candidate")
                require(receipt["check"] not in seen, "duplicate check coverage")
                seen[receipt["check"]] = receipt["revision"]
            require(seen == task["contract"]["checks"], "missing or unexpected required checks")
            accepted_id = digest("accord.acceptance/0.1", [binding, sorted(receipt_ids)])
            if self.store.get(db, "acceptance", accepted_id, optional=True) is None:
                self.store.put(db, "acceptance", accepted_id,
                               {"id": accepted_id, "binding": binding, "receipts": sorted(receipt_ids)})
            task["accepted"] = accepted_id
            self.store.put(db, "task", task_id, task)
            return accepted_id

    def _eligible(self, db, task_id, accepted_id):
        task = self.store.get(db, "task", task_id)
        accepted = self.store.get(db, "acceptance", accepted_id)
        require(accepted["binding"] == self._binding(db, task), "acceptance no longer eligible")
        return task, accepted

    def authorize(self, request_id, task_id, accepted_id, sink_name, target, expected_version):
        """Privileged host grant; not exposed to model text or inferred from receipts."""
        identifier(request_id)
        identifier(target)
        bounded_int(expected_version, 0, 2**63 - 1)
        require(sink_name in self.sinks, "unregistered sink")
        with self.store.transaction() as db:
            accepted = self.store.get(db, "acceptance", accepted_id)
            require(accepted["binding"]["task"] == task_id, "acceptance belongs to another task")
            intent = {"ir": "accord.intent/0.1", "kind": "publish_text", "request": request_id,
                      "task": task_id, "attempt": accepted["binding"]["attempt"],
                      "output": accepted["binding"]["output"], "sink": sink_name,
                      "sink_identity": self.sinks[sink_name].identity, "target": target,
                      "expected_version": expected_version}
            prior = self.store.get(db, "grant", request_id, optional=True)
            if prior:
                require(prior["intent"] == intent and prior["acceptance"] == accepted_id,
                        "logical authorization already bound to another intent")
                return request_id
            task, _ = self._eligible(db, task_id, accepted_id)
            require(target in task["contract"]["publish_to"], "target outside task scope")
            self.store.put(db, "grant", request_id,
                           {"id": request_id, "acceptance": accepted_id, "intent": intent,
                            "state": "available", "revoked": False, "operation": None})
            return request_id

    def prepare(self, task_id, accepted_id, grant_id):
        with self.store.transaction() as db:
            grant = self.store.get(db, "grant", grant_id)
            require(grant["acceptance"] == accepted_id and grant["intent"]["task"] == task_id,
                    "grant does not cover this acceptance")
            if grant["operation"]:
                return grant["operation"]
            self._eligible(db, task_id, accepted_id)
            require(not grant["revoked"] and grant["state"] == "available", "grant unavailable")
            operation_id = digest("accord.operation/0.1", [grant_id, grant["intent"]])
            operation = {"id": operation_id, "grant": grant_id, "acceptance": accepted_id,
                         "intent": grant["intent"], "status": "prepared", "receipt": None}
            grant.update(state="reserved", operation=operation_id)
            self.store.put(db, "grant", grant_id, grant)
            self.store.put(db, "operation", operation_id, operation)
            return operation_id

    def _sink(self, operation):
        intent = operation["intent"]
        require(intent["sink"] in self.sinks, "sink unavailable")
        sink = self.sinks[intent["sink"]]
        require(sink.identity == intent["sink_identity"], "destination identity changed")
        return sink

    def _admit(self, operation_id, *, retry=False):
        with self.store.transaction() as db:
            operation = self.store.get(db, "operation", operation_id)
            if operation["status"] in ("confirmed", "denied"):
                return operation, False
            if not retry and operation["status"] != "prepared":
                return operation, False
            require(operation["status"] in ("prepared", "dispatched", "outcome_unknown"), "invalid operation state")
            self._eligible(db, operation["intent"]["task"], operation["acceptance"])
            grant = self.store.get(db, "grant", operation["grant"])
            require(not grant["revoked"] and grant["operation"] == operation_id and
                    grant["state"] in ("reserved", "spent"), "grant unavailable")
            sink = self._sink(operation)
            require(not retry or sink.idempotent, "sink cannot safely retry")
            grant["state"] = "spent"
            operation["status"] = "dispatched"
            self.store.put(db, "grant", grant["id"], grant)
            self.store.put(db, "operation", operation_id, operation)
            return operation, True

    def _observe(self, operation_id, receipt):
        with self.store.transaction() as db:
            operation = self.store.get(db, "operation", operation_id)
            require(type(receipt) is dict and receipt.get("operation") == operation_id and
                    receipt.get("intent") == digest("accord.intent/0.1", operation["intent"]) and
                    receipt.get("status") in ("confirmed", "denied"), "invalid sink receipt")
            if operation["status"] in ("confirmed", "denied"):
                require(operation["receipt"] == receipt, "conflicting sink observations")
                return operation
            operation.update(status=receipt["status"], receipt=receipt)
            self.store.put(db, "operation", operation_id, operation)
            return operation

    def _unknown(self, operation_id):
        with self.store.transaction() as db:
            operation = self.store.get(db, "operation", operation_id)
            if operation["status"] not in ("confirmed", "denied"):
                operation["status"] = "outcome_unknown"
                self.store.put(db, "operation", operation_id, operation)
            return operation

    def _send(self, operation, crash_hook=None):
        # Outside the host transaction: these databases intentionally cannot commit together.
        try:
            if crash_hook:
                crash_hook("before_send")
            attempt = self.store.inspect("attempt", operation["intent"]["attempt"])
            receipt = self._sink(operation).apply(operation["id"], operation["intent"], attempt["text"])
            if crash_hook:
                crash_hook("after_send")
            return self._observe(operation["id"], receipt)
        except Exception:
            # Timeout or even malformed adapter output is not proof of non-execution.
            return self._unknown(operation["id"])

    def dispatch(self, operation_id, *, crash_hook=None):
        operation, send = self._admit(operation_id)
        return self._send(operation, crash_hook) if send else operation

    def recover(self, operation_id):
        operation = self.store.inspect("operation", operation_id)
        if operation["status"] in ("confirmed", "denied", "prepared"):
            return operation
        operation = self._unknown(operation_id)
        try:
            sink = self._sink(operation)
            receipt = sink.lookup(operation_id)
            if receipt is not None:
                return self._observe(operation_id, receipt)
            if not sink.idempotent:
                return self.store.inspect("operation", operation_id)
            operation, send = self._admit(operation_id, retry=True)
        except Exception:
            return self._unknown(operation_id)
        return self._send(operation) if send else operation

    def cancel(self, task_id):
        with self.store.transaction() as db:
            task = self.store.get(db, "task", task_id)
            task["control"] = "cancelled"
            self.store.put(db, "task", task_id, task)

    def revoke(self, grant_id):
        with self.store.transaction() as db:
            grant = self.store.get(db, "grant", grant_id)
            grant["revoked"] = True
            self.store.put(db, "grant", grant_id, grant)

    def inspect(self, kind, key):
        return self.store.inspect(kind, key)

    def events(self):
        return self.store.events()
