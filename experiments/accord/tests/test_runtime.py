from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import sys
from tempfile import TemporaryDirectory
import threading
import unittest

from experiments.accord.ir import AccordError, Contract
from experiments.accord.runtime import Runtime, Validator
from experiments.accord.sink import SQLiteSink


CONTRACT = Contract("document", ("spec",), 128, (("nonempty", "1"), ("mentions_spec", "1")),
                    ("report", "other"), 3, "p1")


def validators():
    return {"nonempty": Validator("1", lambda text, inputs: bool(text.strip())),
            "mentions_spec": Validator("1", lambda text, inputs: inputs["spec"] in text)}


def make_runtime(directory, *, checks=None, sink=None, **profile):
    directory = Path(directory)
    return Runtime(directory / "host.sqlite", checks or validators(),
                   {"docs": sink or SQLiteSink(directory / "sink.sqlite")}, **profile)


class ContractTests(unittest.TestCase):
    def test_roundtrip_and_canonical_order(self):
        self.assertEqual(Contract.parse(json.dumps(CONTRACT.to_dict())), CONTRACT)
        self.assertEqual(replace(CONTRACT, publish_to=("other", "report")).revision, CONTRACT.revision)
        self.assertNotEqual(replace(CONTRACT, request_budget=2).revision, CONTRACT.revision)

    def test_closed_ir_rejects_unsupported_and_malformed_data(self):
        changes = [{"unknown": True}, {"ir": "accord.contract/2"}, {"request_budget": True},
                   {"request_budget": 1.0}, {"request_budget": -1}, {"request_budget": 10001},
                   {"output_max_bytes": 0}, {"output_max_bytes": 65537}, {"checks": {}},
                   {"inputs": ["spec", "spec"]}, {"inputs": "spec"}, {"checks": []},
                   {"name": "../escape"}, {"name": "\ud800"}, {"checks": {"check": 1}}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(AccordError):
                Contract.parse(CONTRACT.to_dict() | change)
        bad_json = ['{"ir":"one","ir":"two"}', '{"request_budget":NaN}', '{}', '[]', ' ' * 65537]
        for text in bad_json:
            with self.subTest(text=text[:60]), self.assertRaises(AccordError):
                Contract.parse(text)
        with self.assertRaises(AccordError):
            replace(CONTRACT, inputs=["spec"])


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="accord-test-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.runtime = make_runtime(self.directory)
        self.runtime.set_resource("spec", "required")
        self.sink = self.runtime.sinks["docs"]

    def candidate(self, contract=CONTRACT, text="required text", parent=None):
        task = self.runtime.create_task(contract, parent=parent)
        self.runtime.infer(task, "first", lambda _: text)
        return task

    def accepted(self, contract=CONTRACT, text="required text"):
        task = self.candidate(contract, text)
        return task, self.runtime.accept(task, self.runtime.check(task))

    def prepared(self, request="publish", expected=0):
        task, accepted = self.accepted()
        grant = self.runtime.authorize(request, task, accepted, "docs", "report", expected)
        return task, accepted, grant, self.runtime.prepare(task, accepted, grant)

    def race(self, functions):
        barrier = threading.Barrier(len(functions))

        def run(fn):
            barrier.wait(timeout=5)
            try:
                return fn()
            except AccordError as exc:
                return exc

        with ThreadPoolExecutor(max_workers=len(functions)) as pool:
            return list(pool.map(run, functions))

    def crash_process(self, operation, phase):
        script = '''
import os, sys
from experiments.accord.tests.test_runtime import make_runtime
runtime = make_runtime(sys.argv[1])
def crash(phase):
    if phase == sys.argv[3]:
        os._exit(73)
runtime.dispatch(sys.argv[2], crash_hook=crash)
'''
        result = subprocess.run([sys.executable, "-c", script, str(self.directory), operation, phase],
                                cwd=Path(__file__).resolve().parents[3], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 73, result.stderr)

    def test_happy_path_publishes_frozen_bytes_and_replays(self):
        task = self.candidate()
        # Returned values are copies, not mutable capabilities into durable state.
        candidate = self.runtime.infer(task, "first", lambda _: self.fail("unexpected resample"))
        candidate["text"] = "unvalidated replacement"
        accepted = self.runtime.accept(task, self.runtime.check(task))
        grant = self.runtime.authorize("publish", task, accepted, "docs", "report", 0)
        op = self.runtime.prepare(task, accepted, grant)
        self.assertEqual(self.runtime.dispatch(op)["status"], "confirmed")
        self.assertEqual(self.runtime.dispatch(op)["status"], "confirmed")
        self.assertEqual(self.sink.target("report"), {"version": 1, "text": "required text"})
        self.assertEqual(self.runtime.inspect("task", task)["remaining"], 2)
        self.assertEqual(self.runtime.inspect("grant", grant)["state"], "spent")
        reopened = make_runtime(self.directory)
        self.assertEqual(reopened.recover(op)["status"], "confirmed")
        self.assertEqual(reopened.infer(task, "first", lambda _: self.fail("unexpected resample"))["text"], "required text")
        self.assertGreater(len(reopened.events()), 0)
        self.assertEqual(reopened.inspect("profile", reopened.profile_id)["environment"], "e1")

    def test_receipts_missing_forged_failed_and_duplicate(self):
        task = self.candidate()
        receipts = self.runtime.check(task)
        for handles in ([], receipts[:1], ["model_says_passed"], receipts + receipts, [self.runtime.inspect("check", receipts[0])]):
            with self.subTest(handles=handles), self.assertRaises(AccordError):
                self.runtime.accept(task, handles)
        other_receipts = self.runtime.check(task)
        with self.assertRaisesRegex(AccordError, "duplicate check"):
            self.runtime.accept(task, [receipts[0], other_receipts[0]])
        bad = self.candidate(text="schema-valid but requirement absent")
        with self.assertRaises(AccordError):
            self.runtime.accept(bad, self.runtime.check(bad))

    def test_receipts_cannot_cross_task_attempt_or_artifact(self):
        task = self.candidate()
        receipts = self.runtime.check(task)
        other = self.candidate()
        with self.assertRaises(AccordError):
            self.runtime.accept(other, receipts)
        for label, text in (("same_bytes", "required text"), ("new_bytes", "required revision")):
            self.runtime.infer(task, label, lambda _, output=text: output)
            with self.assertRaises(AccordError):
                self.runtime.accept(task, receipts)

    def test_new_attempt_invalidates_prepared_acceptance(self):
        task, _, _, op = self.prepared()
        self.runtime.infer(task, "new", lambda _: "required text")
        with self.assertRaisesRegex(AccordError, "acceptance"):
            self.runtime.dispatch(op)
        self.assertEqual(self.sink.target("report")["version"], 0)

    def test_input_change_and_aba_invalidate_historical_acceptance(self):
        task, accepted, _, op = self.prepared()
        self.runtime.set_resource("spec", "changed")
        self.runtime.set_resource("spec", "required")
        with self.assertRaisesRegex(AccordError, "stale input"):
            self.runtime.dispatch(op)
        self.assertEqual(self.runtime.inspect("acceptance", accepted)["binding"]["task"], task)
        self.assertEqual(self.runtime.inspect("operation", op)["status"], "prepared")
        self.assertEqual(self.sink.target("report")["version"], 0)

    def test_declared_validator_policy_and_environment_changes_block_old_hosts(self):
        for kind in ("validator", "policy", "environment"):
            with self.subTest(kind=kind), TemporaryDirectory() as directory:
                original = make_runtime(directory)
                original.set_resource("spec", "required")
                task = original.create_task(CONTRACT)
                original.infer(task, "first", lambda _: "required")
                receipts = original.check(task)
                checks = validators()
                kwargs = {}
                if kind == "validator":
                    checks["nonempty"] = Validator("2", checks["nonempty"].run)
                elif kind == "policy":
                    kwargs["policy_revision"] = "p2"
                else:
                    kwargs["environment_revision"] = "e2"
                updated = make_runtime(directory, checks=checks, **kwargs)
                updated.activate_profile()
                with self.assertRaisesRegex(AccordError, "stale profile"):
                    original.accept(task, receipts)
                # Reverting registry content does not resurrect old certificates.
                original.activate_profile()
                with self.assertRaisesRegex(AccordError, "stale profile"):
                    original.accept(task, receipts)

    def test_input_change_during_validator_does_not_get_accepted(self):
        def changing_validator(text, inputs):
            self.runtime.set_resource("spec", "new input")
            return True
        checks = validators() | {"nonempty": Validator("1", changing_validator)}
        self.runtime = make_runtime(self.directory, checks=checks)
        task = self.candidate()
        receipts = self.runtime.check(task)
        with self.assertRaisesRegex(AccordError, "stale input"):
            self.runtime.accept(task, receipts)

    def test_validator_exception_and_nonboolean_fail_closed(self):
        for bad in (lambda text, inputs: "passed", lambda text, inputs: 1 / 0):
            checks = validators() | {"nonempty": Validator("1", bad)}
            self.runtime = make_runtime(self.directory, checks=checks)
            task = self.candidate()
            with self.assertRaises(AccordError):
                self.runtime.accept(task, self.runtime.check(task))

    def test_child_cannot_expand_scope_or_overspend(self):
        parent = self.runtime.create_task(replace(CONTRACT, request_budget=1))
        changes = [{"inputs": ("spec", "secret")}, {"publish_to": ("outside",)},
                   {"output_max_bytes": 129}, {"request_budget": 2}]
        for change in changes:
            with self.subTest(change=change), self.assertRaises(AccordError):
                child_contract = replace(replace(CONTRACT, request_budget=1), **change)
                self.runtime.create_task(child_contract, parent=parent)
        self.assertEqual(self.runtime.inspect("task", parent)["remaining"], 1)

    def test_concurrent_child_allocation_and_spending_conserve_budget(self):
        parent = self.runtime.create_task(replace(CONTRACT, request_budget=1))
        one = replace(CONTRACT, request_budget=1)
        allocated = self.race([lambda: make_runtime(self.directory).create_task(one, parent=parent)] * 2)
        children = [r for r in allocated if isinstance(r, str)]
        self.assertEqual(len(children), 1)
        self.assertEqual(self.runtime.inspect("task", parent)["remaining"], 0)
        calls = []
        def model(inputs):
            calls.append(1)
            return "required"
        child = children[0]
        results = self.race([lambda label=label: make_runtime(self.directory).infer(child, label, model)
                             for label in ("one", "two")])
        self.assertEqual(sum(isinstance(r, dict) for r in results), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.runtime.inspect("task", child)["remaining"], 0)

    def test_parent_cancellation_blocks_child_admission(self):
        parent = self.runtime.create_task(CONTRACT)
        child = self.candidate(replace(CONTRACT, request_budget=2), parent=parent)
        accepted = self.runtime.accept(child, self.runtime.check(child))
        grant = self.runtime.authorize("child_write", child, accepted, "docs", "report", 0)
        op = self.runtime.prepare(child, accepted, grant)
        self.runtime.cancel(parent)
        with self.assertRaisesRegex(AccordError, "cancelled"):
            self.runtime.infer(child, "next", lambda _: self.fail("model invoked"))
        with self.assertRaisesRegex(AccordError, "cancelled"):
            self.runtime.dispatch(op)
        self.assertEqual(self.runtime.inspect("task", child)["remaining"], 1)

    def test_failed_and_invalid_model_calls_keep_the_charge(self):
        for output in (123, "x" * 129, "\ud800"):
            task = self.runtime.create_task(replace(CONTRACT, request_budget=1))
            with self.assertRaises((AccordError, UnicodeError)):
                self.runtime.infer(task, "first", lambda _: output)
            self.assertEqual(self.runtime.inspect("task", task)["remaining"], 0)
            with self.assertRaises(AccordError):
                self.runtime.infer(task, "first", lambda _: self.fail("same attempt retried"))
            with self.assertRaisesRegex(AccordError, "budget"):
                self.runtime.infer(task, "retry", lambda _: self.fail("budget bypass"))

    def test_concurrent_same_attempt_never_calls_model_twice(self):
        task = self.runtime.create_task(CONTRACT)
        entered, release = threading.Event(), threading.Event()
        def slow(inputs):
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return "required"
        with ThreadPoolExecutor() as pool:
            first = pool.submit(self.runtime.infer, task, "first", slow)
            try:
                self.assertTrue(entered.wait(timeout=5))
                with self.assertRaisesRegex(AccordError, "replayable"):
                    make_runtime(self.directory).infer(task, "first", lambda _: self.fail("duplicate call"))
            finally:
                release.set()
            first.result(timeout=5)
        self.assertEqual(self.runtime.inspect("task", task)["remaining"], 2)

    def test_late_model_result_does_not_replace_newer_candidate(self):
        task = self.runtime.create_task(CONTRACT)
        entered, release = threading.Event(), threading.Event()
        def slow(inputs):
            entered.set()
            self.assertTrue(release.wait(timeout=5))
            return "required OLD"
        with ThreadPoolExecutor() as pool:
            first = pool.submit(self.runtime.infer, task, "old", slow)
            try:
                self.assertTrue(entered.wait(timeout=5))
                new = self.runtime.infer(task, "new", lambda _: "required NEW")
            finally:
                release.set()
            old = first.result(timeout=5)
        self.assertNotEqual(old["id"], new["id"])
        accepted = self.runtime.accept(task, self.runtime.check(task))
        self.assertEqual(self.runtime.inspect("acceptance", accepted)["binding"]["attempt"], new["id"])

    def test_one_logical_grant_cannot_be_reissued_for_changed_intent(self):
        task, accepted, grant, op = self.prepared()
        self.assertEqual(self.runtime.authorize("publish", task, accepted, "docs", "report", 0), grant)
        with self.assertRaisesRegex(AccordError, "already bound"):
            self.runtime.authorize("publish", task, accepted, "docs", "other", 0)
        self.runtime.dispatch(op)
        self.assertEqual(self.runtime.prepare(task, accepted, grant), op)
        self.assertEqual(make_runtime(self.directory).authorize("publish", task, accepted, "docs", "report", 0), grant)
        self.runtime.revoke(grant)
        self.assertTrue(self.runtime.inspect("grant", grant)["revoked"])
        self.assertEqual(self.runtime.inspect("grant", grant)["state"], "spent")

    def test_two_intents_racing_for_one_request_get_only_one_grant(self):
        first, second = self.accepted(), self.accepted(text="required other bytes")
        outcomes = self.race([lambda pair=pair: make_runtime(self.directory).authorize("same_request", *pair, "docs", "report", 0)
                              for pair in (first, second)])
        self.assertEqual(sum(isinstance(r, str) for r in outcomes), 1)
        self.assertEqual(sum(isinstance(r, AccordError) for r in outcomes), 1)

    def test_concurrent_prepare_and_dispatch_share_one_operation(self):
        task, accepted = self.accepted()
        grant = self.runtime.authorize("publish", task, accepted, "docs", "report", 0)
        operations = self.race([lambda: make_runtime(self.directory).prepare(task, accepted, grant)] * 4)
        self.assertEqual(len(set(operations)), 1)
        op = operations[0]
        self.race([lambda: make_runtime(self.directory).dispatch(op)] * 4)
        self.assertEqual(self.runtime.recover(op)["status"], "confirmed")
        self.assertEqual(self.sink.target("report")["version"], 1)

    def test_scope_and_revocation_are_checked_before_send(self):
        task, accepted, grant, op = self.prepared()
        with self.assertRaisesRegex(AccordError, "scope"):
            self.runtime.authorize("bad_target", task, accepted, "docs", "outside", 0)
        self.runtime.revoke(grant)
        with self.assertRaisesRegex(AccordError, "grant unavailable"):
            self.runtime.dispatch(op)
        self.assertEqual(self.sink.target("report")["version"], 0)

    def test_sink_cas_conflict_is_definite_denial_and_dedup_precedes_cas(self):
        _, _, _, first = self.prepared("first")
        _, _, _, stale = self.prepared("stale")
        self.assertEqual(self.runtime.dispatch(first)["status"], "confirmed")
        self.assertEqual(self.runtime.dispatch(stale)["status"], "denied")
        record = self.runtime.inspect("operation", first)
        payload = self.runtime.inspect("attempt", record["intent"]["attempt"])["text"]
        self.assertEqual(self.sink.apply(first, record["intent"], payload)["status"], "confirmed")
        self.assertEqual(self.sink.target("report")["version"], 1)
        changed = record["intent"] | {"target": "other"}
        with self.assertRaisesRegex(AccordError, "reused"):
            self.sink.apply(first, changed, payload)

    def test_destination_identity_cannot_be_substituted(self):
        _, _, _, op = self.prepared()
        different = SQLiteSink(self.directory / "impostor.sqlite")
        reopened = make_runtime(self.directory, sink=different)
        with self.assertRaisesRegex(AccordError, "identity"):
            reopened.dispatch(op)
        self.assertEqual(different.target("report")["version"], 0)

    def test_cancellation_after_admission_preserves_real_effect(self):
        task, _, grant, op = self.prepared()
        def hook(phase):
            if phase == "before_send":
                self.runtime.cancel(task)
                self.runtime.revoke(grant)
            if phase == "after_send":
                raise ConnectionError("reply lost")
        self.assertEqual(self.runtime.dispatch(op, crash_hook=hook)["status"], "outcome_unknown")
        self.assertEqual(self.runtime.recover(op)["status"], "confirmed")
        self.assertEqual(self.runtime.inspect("task", task)["control"], "cancelled")
        self.assertEqual(self.sink.target("report")["version"], 1)

    def test_process_death_after_sink_commit_reconciles_without_duplicate(self):
        task, _, grant, op = self.prepared()
        self.crash_process(op, "after_send")
        self.assertEqual(self.runtime.inspect("operation", op)["status"], "dispatched")
        self.assertEqual(self.sink.target("report")["version"], 1)
        self.runtime.cancel(task)
        self.runtime.revoke(grant)
        self.runtime.set_resource("spec", "changed after effect")
        reopened = make_runtime(self.directory)
        self.assertEqual(reopened.recover(op)["status"], "confirmed")
        self.assertEqual(reopened.recover(op)["status"], "confirmed")
        self.assertEqual(self.sink.target("report")["version"], 1)

    def test_process_death_before_send_reuses_exact_operation(self):
        _, _, grant, op = self.prepared()
        self.crash_process(op, "before_send")
        self.assertIsNone(self.sink.lookup(op))
        self.assertEqual(self.runtime.inspect("grant", grant)["state"], "spent")
        self.assertEqual(make_runtime(self.directory).recover(op)["status"], "confirmed")
        self.assertEqual(self.sink.target("report")["version"], 1)
        self.assertEqual(self.runtime.inspect("grant", grant)["operation"], op)

    def test_uncertain_dispatch_with_new_guard_failure_stays_unknown(self):
        task, _, grant, op = self.prepared()
        self.crash_process(op, "before_send")
        self.runtime.cancel(task)
        self.runtime.revoke(grant)
        self.assertEqual(self.runtime.recover(op)["status"], "outcome_unknown")
        self.assertEqual(self.sink.target("report")["version"], 0)
        self.assertEqual(self.runtime.inspect("grant", grant)["state"], "spent")

    def test_recovery_can_race_original_sender_without_double_application(self):
        _, _, _, op = self.prepared()
        entered, release = threading.Event(), threading.Event()
        def pause(phase):
            if phase == "before_send":
                entered.set()
                self.assertTrue(release.wait(timeout=5))
        with ThreadPoolExecutor() as pool:
            original = pool.submit(self.runtime.dispatch, op, crash_hook=pause)
            try:
                self.assertTrue(entered.wait(timeout=5))
                self.assertIsNone(self.sink.lookup(op))
                self.assertEqual(make_runtime(self.directory).recover(op)["status"], "confirmed")
            finally:
                release.set()
            self.assertEqual(original.result(timeout=5)["status"], "confirmed")
        self.assertEqual(self.sink.target("report")["version"], 1)

    def test_opaque_sink_is_never_blindly_retried(self):
        backing = self.sink
        class OpaqueSink:
            identity = backing.identity
            idempotent = False
            calls = 0
            def apply(self, op, intent, text):
                self.calls += 1
                backing.apply(op, intent, text)
                raise TimeoutError("outcome inaccessible")
            def lookup(self, op):
                return None
        opaque = OpaqueSink()
        self.runtime = make_runtime(self.directory, sink=opaque)
        _, _, _, op = self.prepared()
        self.assertEqual(self.runtime.dispatch(op)["status"], "outcome_unknown")
        self.assertEqual(self.runtime.dispatch(op)["status"], "outcome_unknown")
        self.assertEqual(self.runtime.recover(op)["status"], "outcome_unknown")
        self.assertEqual(opaque.calls, 1)
        self.assertEqual(backing.target("report")["version"], 1)

    def test_process_death_after_model_leaves_charged_unreplayable_attempt(self):
        task = self.runtime.create_task(CONTRACT)
        script = '''
import os, sys
from experiments.accord.tests.test_runtime import make_runtime
runtime = make_runtime(sys.argv[1])
runtime.infer(sys.argv[2], "interrupted", lambda _: "required", crash_hook=lambda _: os._exit(73))
'''
        result = subprocess.run([sys.executable, "-c", script, str(self.directory), task],
                                cwd=Path(__file__).resolve().parents[3], capture_output=True, text=True, timeout=10)
        self.assertEqual(result.returncode, 73, result.stderr)
        reopened = make_runtime(self.directory)
        self.assertEqual(reopened.inspect("task", task)["remaining"], 2)
        with self.assertRaisesRegex(AccordError, "replayable"):
            reopened.infer(task, "interrupted", lambda _: self.fail("unrecorded response invented"))
        reopened.infer(task, "explicit_retry", lambda _: "required")
        self.assertEqual(reopened.inspect("task", task)["remaining"], 1)


if __name__ == "__main__":
    unittest.main()
