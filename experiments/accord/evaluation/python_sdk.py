"""Ordinary Python baseline for the publication and delegation evaluations.

JobSpec is application configuration, with no contract IR or interpreter. This
SDK implements its own durable lifecycle over the shared ledger and sink wire
protocol, including scoped child jobs with transferred request credits.
"""

from dataclasses import asdict, dataclass
import json
from types import MappingProxyType
import uuid

from ..ir import bounded_int, digest, identifier, require, revision_token, text_digest
from ..store import Store


@dataclass(frozen=True)
class JobSpec:
    name: str
    inputs: tuple[str, ...]
    output_max_bytes: int
    checks: tuple[tuple[str, str], ...]
    publish_to: tuple[str, ...]
    request_budget: int
    policy_revision: str

    def __post_init__(self):
        identifier(self.name)
        identifier(self.policy_revision, revision=True)
        bounded_int(self.output_max_bytes, 1, 65536)
        bounded_int(self.request_budget, 0, 10000)
        for field in ("inputs", "publish_to"):
            values = getattr(self, field)
            require(type(values) is tuple, "resource lists must be immutable tuples")
            for value in values:
                identifier(value)
            require(len(values) == len(set(values)), "duplicate resource")
            object.__setattr__(self, field, tuple(sorted(values)))
        require(type(self.checks) is tuple and len(self.checks) > 0, "checks must be nonempty")
        for check in self.checks:
            require(type(check) is tuple and len(check) == 2, "invalid check")
            identifier(check[0])
            identifier(check[1], revision=True)
        require(len(dict(self.checks)) == len(self.checks), "duplicate check")
        object.__setattr__(self, "checks", tuple(sorted(self.checks)))
        encoded = json.dumps(asdict(self), ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        require(len(encoded) <= 65536, "job specification too large")


class PythonSDK:
    def __init__(self, path, validators, sinks, *, policy_revision="p1", environment_revision="e1"):
        identifier(policy_revision, revision=True)
        identifier(environment_revision, revision=True)
        self.validators = MappingProxyType(dict(validators))
        self.sinks = MappingProxyType(dict(sinks))
        revisions = {}
        for name, validator in self.validators.items():
            identifier(name)
            revisions[name] = identifier(validator.revision, revision=True)
        self.profile = {"policy": policy_revision, "environment": environment_revision,
                        "validators": revisions}
        self.profile_id = digest("python-sdk.profile/1", self.profile)
        self.store = Store(path)
        with self.store.transaction() as db:
            if self.store.get(db, "profile", self.profile_id, optional=True) is None:
                self.store.put(db, "profile", self.profile_id, self.profile)
            if self.store.get(db, "meta", "profile", optional=True) is None:
                self.store.put(db, "meta", "profile", {"id": self.profile_id, "epoch": 1})

    def activate_profile(self):
        with self.store.transaction() as db:
            previous = self.store.get(db, "meta", "profile")
            if previous["id"] != self.profile_id:
                self.store.put(db, "meta", "profile",
                               {"id": self.profile_id, "epoch": previous["epoch"] + 1})

    def set_resource(self, name, text):
        identifier(name)
        require(type(text) is str, "resource must be text")
        fingerprint = text_digest(text)
        with self.store.transaction() as db:
            previous = self.store.get(db, "resource", name, optional=True)
            current = {"text": text, "digest": fingerprint,
                       "revision": previous["revision"] + 1 if previous else 1}
            self.store.put(db, "resource", name, current)
            return current

    def create_task(self, spec, *, parent=None):
        """Create a job, transferring credits from an optional fresh parent."""
        require(isinstance(spec, JobSpec), "expected a JobSpec")
        with self.store.transaction() as db:
            active = self.store.get(db, "meta", "profile")
            require(active["id"] == self.profile_id, "host profile not active")
            require(spec.policy_revision == self.profile["policy"], "policy revision unavailable")
            require(all(self.profile["validators"].get(name) == revision
                        for name, revision in spec.checks), "validator revision unavailable")
            if parent is not None:
                owner = self._current_task(db, parent)
                require(set(spec.inputs) <= set(owner["spec"]["inputs"]), "child expands input scope")
                require(set(spec.publish_to) <= set(owner["spec"]["publish_to"]), "child expands effect scope")
                require(spec.output_max_bytes <= owner["spec"]["output_max_bytes"], "child expands output limit")
                require(spec.request_budget <= owner["remaining"], "insufficient parent budget")
                # Debit and child insertion share the ledger transaction, so
                # allocation races and failed creation cannot mint credits.
                owner["remaining"] -= spec.request_budget
                self.store.put(db, "task", parent, owner)
            settings = asdict(spec)
            task_id = uuid.uuid4().hex
            self.store.put(db, "task", task_id, {
                "id": task_id, "spec": settings, "revision": digest("python-sdk.job/1", settings),
                "inputs": {name: self.store.get(db, "resource", name) for name in spec.inputs},
                "profile": active, "parent": parent, "control": "active", "remaining": spec.request_budget,
                "latest": None, "accepted": None,
            })
            return task_id

    @staticmethod
    def _input_text(task):
        return {name: resource["text"] for name, resource in task["inputs"].items()}

    def _current_task(self, db, task_id):
        task = self.store.get(db, "task", task_id)
        current = task
        while current is not None:
            require(current["control"] == "active", "task or ancestor cancelled")
            # Root jobs persisted by the original SDK have no parent field.
            parent = current.get("parent")
            current = self.store.get(db, "task", parent) if parent is not None else None
        active = self.store.get(db, "meta", "profile")
        require(active == task["profile"] and active["id"] == self.profile_id, "stale profile")
        for name, snapshot in task["inputs"].items():
            resource = self.store.get(db, "resource", name)
            require((resource["revision"], resource["digest"]) ==
                    (snapshot["revision"], snapshot["digest"]), f"stale input: {name}")
        return task

    def infer(self, task_id, attempt_label, model, *, crash_hook=None):
        identifier(attempt_label)
        attempt_id = digest("python-sdk.attempt/1", [task_id, attempt_label])
        with self.store.transaction() as db:
            # Completed outputs are immutable history and can be replayed even
            # after cancellation; any new call requires fresh admission.
            self.store.get(db, "task", task_id)
            existing = self.store.get(db, "attempt", attempt_id, optional=True)
            if existing is not None:
                require(existing["status"] == "complete", "attempt has no replayable output; use an explicit new attempt")
                return existing
            task = self._current_task(db, task_id)
            require(task["remaining"] > 0, "request budget exhausted")
            task.update(remaining=task["remaining"] - 1, latest=attempt_id, accepted=None)
            attempt = {"id": attempt_id, "task": task_id, "label": attempt_label, "status": "started"}
            self.store.put(db, "task", task_id, task)
            self.store.put(db, "attempt", attempt_id, attempt)
        try:
            output = model(self._input_text(task))
            require(type(output) is str, "model output must be text")
            require(len(output.encode("utf-8")) <= task["spec"]["output_max_bytes"], "output too large")
            if crash_hook is not None:
                crash_hook("after_model")
        except Exception as exc:
            attempt.update(status="failed", error=type(exc).__name__)
            with self.store.transaction() as db:
                self.store.put(db, "attempt", attempt_id, attempt)
            raise
        attempt.update(status="complete", text=output, digest=text_digest(output))
        with self.store.transaction() as db:
            # Do not reset task.latest here: a newer call may already own it.
            self.store.put(db, "attempt", attempt_id, attempt)
        return attempt

    def _candidate(self, db, task_id):
        task = self._current_task(db, task_id)
        require(task["latest"] is not None, "task has no candidate")
        attempt = self.store.get(db, "attempt", task["latest"])
        require(attempt["status"] == "complete", "latest attempt has no candidate")
        binding = {
            "task": task_id, "attempt": attempt["id"], "spec": task["revision"],
            "inputs": {name: {"revision": resource["revision"], "digest": resource["digest"]}
                       for name, resource in task["inputs"].items()},
            "output": attempt["digest"], "profile": task["profile"],
        }
        return task, attempt, binding

    def check(self, task_id):
        with self.store.transaction() as db:
            task, attempt, binding = self._candidate(db, task_id)
        results = []
        for name, revision in task["spec"]["checks"]:
            diagnostic = ""
            try:
                passed = self.validators[name].run(attempt["text"], self._input_text(task))
                require(type(passed) is bool, "validator must return bool")
            except Exception as exc:
                passed, diagnostic = False, type(exc).__name__
            results.append({"id": uuid.uuid4().hex, "binding": binding, "check": name,
                            "revision": revision, "passed": passed, "diagnostic": diagnostic})
        with self.store.transaction() as db:
            for result in results:
                self.store.put(db, "check", result["id"], result)
        return [result["id"] for result in results]

    def accept(self, task_id, receipt_ids):
        require(type(receipt_ids) is list and all(type(value) is str for value in receipt_ids),
                "expected receipt handles")
        require(len(receipt_ids) == len(set(receipt_ids)), "duplicate receipts")
        with self.store.transaction() as db:
            task, _, binding = self._candidate(db, task_id)
            coverage = {}
            for handle in receipt_ids:
                result = self.store.get(db, "check", handle)
                require(result["passed"] is True and result["binding"] == binding,
                        "receipt failed or belongs to another candidate")
                require(result["check"] not in coverage, "duplicate check coverage")
                coverage[result["check"]] = result["revision"]
            require(coverage == dict(task["spec"]["checks"]), "missing or unexpected required checks")
            handles = sorted(receipt_ids)
            accepted_id = digest("python-sdk.acceptance/1", [binding, handles])
            if self.store.get(db, "acceptance", accepted_id, optional=True) is None:
                self.store.put(db, "acceptance", accepted_id,
                               {"id": accepted_id, "binding": binding, "receipts": handles})
            task["accepted"] = accepted_id
            self.store.put(db, "task", task_id, task)
            return accepted_id

    def _eligible_task(self, db, task_id, accepted_id):
        task, _, binding = self._candidate(db, task_id)
        accepted = self.store.get(db, "acceptance", accepted_id)
        require(accepted["binding"] == binding, "acceptance no longer eligible")
        return task

    def authorize(self, request_id, task_id, accepted_id, sink_name, target, expected_version):
        identifier(request_id)
        identifier(target)
        revision_token(expected_version)
        require(sink_name in self.sinks, "unregistered sink")
        with self.store.transaction() as db:
            accepted = self.store.get(db, "acceptance", accepted_id)
            require(accepted["binding"]["task"] == task_id, "acceptance belongs to another task")
            intent = {
                "ir": "accord.intent/0.1", "kind": "publish_text", "request": request_id,
                "task": task_id, "attempt": accepted["binding"]["attempt"],
                "output": accepted["binding"]["output"], "sink": sink_name,
                "sink_identity": self.sinks[sink_name].identity, "target": target,
                "expected_version": expected_version,
            }
            existing = self.store.get(db, "grant", request_id, optional=True)
            if existing is not None:
                require(existing["acceptance"] == accepted_id and existing["intent"] == intent,
                        "logical authorization already bound to another intent")
                return request_id
            task = self._eligible_task(db, task_id, accepted_id)
            require(target in task["spec"]["publish_to"], "target outside task scope")
            self.store.put(db, "grant", request_id,
                           {"id": request_id, "acceptance": accepted_id, "intent": intent,
                            "state": "available", "revoked": False, "operation": None})
            return request_id

    def prepare(self, task_id, accepted_id, grant_id):
        with self.store.transaction() as db:
            grant = self.store.get(db, "grant", grant_id)
            require(grant["acceptance"] == accepted_id and grant["intent"]["task"] == task_id,
                    "grant does not cover this acceptance")
            if grant["operation"] is not None:
                return grant["operation"]
            self._eligible_task(db, task_id, accepted_id)
            require(not grant["revoked"] and grant["state"] == "available", "grant unavailable")
            operation_id = digest("python-sdk.operation/1", [grant_id, grant["intent"]])
            grant.update(state="reserved", operation=operation_id)
            self.store.put(db, "grant", grant_id, grant)
            self.store.put(db, "operation", operation_id,
                           {"id": operation_id, "grant": grant_id, "acceptance": accepted_id,
                            "intent": grant["intent"], "status": "prepared", "receipt": None})
            return operation_id

    def _destination(self, operation):
        name = operation["intent"]["sink"]
        require(name in self.sinks, "sink unavailable")
        sink = self.sinks[name]
        require(sink.identity == operation["intent"]["sink_identity"], "destination identity changed")
        return sink

    def _admit_send(self, operation_id, retry=False):
        with self.store.transaction() as db:
            operation = self.store.get(db, "operation", operation_id)
            if operation["status"] in ("confirmed", "denied"):
                return operation, False
            if not retry and operation["status"] != "prepared":
                return operation, False
            require(operation["status"] in ("prepared", "dispatched", "outcome_unknown"),
                    "invalid operation state")
            self._eligible_task(db, operation["intent"]["task"], operation["acceptance"])
            grant = self.store.get(db, "grant", operation["grant"])
            require(not grant["revoked"] and grant["operation"] == operation_id and
                    grant["state"] in ("reserved", "spent"), "grant unavailable")
            sink = self._destination(operation)
            require(not retry or sink.idempotent, "sink cannot safely retry")
            grant["state"] = "spent"
            operation["status"] = "dispatched"
            self.store.put(db, "grant", grant["id"], grant)
            self.store.put(db, "operation", operation_id, operation)
            return operation, True

    def _record_receipt(self, operation_id, receipt):
        with self.store.transaction() as db:
            operation = self.store.get(db, "operation", operation_id)
            require(type(receipt) is dict and receipt.get("operation") == operation_id and
                    receipt.get("intent") == digest("accord.intent/0.1", operation["intent"]) and
                    receipt.get("status") in ("confirmed", "denied"), "invalid sink receipt")
            if operation["status"] in ("confirmed", "denied"):
                require(operation["receipt"] == receipt, "conflicting sink observations")
            else:
                operation.update(status=receipt["status"], receipt=receipt)
                self.store.put(db, "operation", operation_id, operation)
            return operation

    def _mark_unknown(self, operation_id):
        with self.store.transaction() as db:
            operation = self.store.get(db, "operation", operation_id)
            if operation["status"] not in ("confirmed", "denied"):
                operation["status"] = "outcome_unknown"
                self.store.put(db, "operation", operation_id, operation)
            return operation

    def _deliver(self, operation, crash_hook=None):
        try:
            if crash_hook is not None:
                crash_hook("before_send")
            attempt = self.store.inspect("attempt", operation["intent"]["attempt"])
            receipt = self._destination(operation).apply(operation["id"], operation["intent"], attempt["text"])
            if crash_hook is not None:
                crash_hook("after_send")
            return self._record_receipt(operation["id"], receipt)
        except Exception:
            return self._mark_unknown(operation["id"])

    def dispatch(self, operation_id, *, crash_hook=None):
        operation, admitted = self._admit_send(operation_id)
        return self._deliver(operation, crash_hook) if admitted else operation

    def recover(self, operation_id):
        operation = self.store.inspect("operation", operation_id)
        if operation["status"] in ("confirmed", "denied", "prepared"):
            return operation
        operation = self._mark_unknown(operation_id)
        try:
            sink = self._destination(operation)
            receipt = sink.lookup(operation_id)
            if receipt is not None:
                # Reconcile an admitted effect even if future admission is now
                # forbidden by cancellation, revocation, or a stale snapshot.
                return self._record_receipt(operation_id, receipt)
            if not sink.idempotent:
                return self.store.inspect("operation", operation_id)
            operation, admitted = self._admit_send(operation_id, retry=True)
        except Exception:
            return self._mark_unknown(operation_id)
        return self._deliver(operation) if admitted else operation

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
