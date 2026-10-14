"""SDK delegation guards, races, replay and reconciliation regressions."""
import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from experiments.accord.evaluation import python_sdk
from experiments.accord.evaluation.python_sdk import JobSpec, PythonSDK

SOURCE = Path(python_sdk.__file__)

from experiments.accord.ir import AccordError
from experiments.accord.sink import SQLiteSink


@dataclass(frozen=True)
class Validator:
    revision: str
    run: object


JOB = JobSpec("document", ("spec",), 128, (("nonempty", "1"),),
              ("report", "other"), 4, "p1")


class DelegationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="accord-sdk-delegation-")
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.sdk = self.open_sdk()
        self.sdk.set_resource("spec", "required")
        self.sink = self.sdk.sinks["docs"]

    def open_sdk(self, **kwargs):
        validators = {"nonempty": Validator("1", lambda text, inputs: bool(text.strip()))}
        return PythonSDK(self.directory / "host.sqlite", validators,
                         {"docs": SQLiteSink(self.directory / "sink.sqlite")}, **kwargs)

    def race(self, calls):
        barrier = threading.Barrier(len(calls))
        def run(call):
            barrier.wait(timeout=5)
            try:
                return call()
            except AccordError as exc:
                return exc
        with ThreadPoolExecutor(max_workers=len(calls)) as pool:
            return list(pool.map(run, calls))

    def prepared_child(self):
        parent = self.sdk.create_task(JOB)
        child = self.sdk.create_task(replace(JOB, request_budget=3), parent=parent)
        completed = self.sdk.infer(child, "first", lambda _: "required")
        receipts = self.sdk.check(child)
        accepted = self.sdk.accept(child, receipts)
        grant = self.sdk.authorize("publish", child, accepted, "docs", "report", 0)
        operation = self.sdk.prepare(child, accepted, grant)
        return parent, child, completed, receipts, accepted, grant, operation

    def test_child_budget_transfer_and_narrowed_snapshot(self):
        self.sdk.set_resource("extra", "secret")
        parent = self.sdk.create_task(replace(JOB, inputs=("spec", "extra")))
        child_spec = replace(JOB, request_budget=2, output_max_bytes=64, publish_to=("report",))
        child = self.sdk.create_task(child_spec, parent=parent)
        self.assertEqual(self.sdk.inspect("task", parent)["remaining"], 2)
        record = self.sdk.inspect("task", child)
        self.assertEqual(record["parent"], parent)
        self.assertEqual(record["remaining"], 2)
        observed = []
        self.sdk.infer(child, "first", lambda inputs: observed.append(inputs) or "required")
        self.assertEqual(observed, [{"spec": "required"}])
        self.assertEqual(self.sdk.inspect("task", parent)["remaining"], 2)
        self.assertEqual(self.sdk.inspect("task", child)["remaining"], 1)

    def test_child_cannot_expand_scope_or_overspend_without_debit(self):
        self.sdk.set_resource("secret", "sensitive")
        parent = self.sdk.create_task(replace(JOB, request_budget=1))
        changes = [({"inputs": ("spec", "secret")}, "input scope"),
                   ({"publish_to": ("outside",)}, "effect scope"),
                   ({"output_max_bytes": 129}, "output limit"),
                   ({"request_budget": 2}, "parent budget")]
        before = self.sdk.events()
        for change, message in changes:
            with self.subTest(change=change), self.assertRaisesRegex(AccordError, message):
                self.sdk.create_task(replace(replace(JOB, request_budget=1), **change), parent=parent)
            self.assertEqual(self.sdk.inspect("task", parent)["remaining"], 1)
            self.assertEqual(self.sdk.events(), before)

    def test_failed_child_insert_rolls_back_parent_debit_and_events(self):
        parent = self.sdk.create_task(JOB)
        original = self.sdk.store.put
        before = self.sdk.events()
        def fail_child(db, kind, key, value):
            if kind == "task" and key != parent:
                raise OSError("injected child insertion failure")
            return original(db, kind, key, value)
        with patch.object(self.sdk.store, "put", side_effect=fail_child):
            with self.assertRaisesRegex(OSError, "injected"):
                self.sdk.create_task(replace(JOB, request_budget=2), parent=parent)
        self.assertEqual(self.sdk.inspect("task", parent)["remaining"], 4)
        self.assertEqual(self.sdk.events(), before)

    def test_concurrent_child_allocations_conserve_budget(self):
        parent = self.sdk.create_task(replace(JOB, request_budget=1))
        spec = replace(JOB, request_budget=1)
        results = self.race([lambda: self.open_sdk().create_task(spec, parent=parent)] * 4)
        children = [result for result in results if isinstance(result, str)]
        self.assertEqual(len(children), 1)
        self.assertEqual(sum(isinstance(result, AccordError) for result in results), 3)
        self.assertEqual(self.sdk.inspect("task", parent)["remaining"], 0)
        self.assertEqual(self.sdk.inspect("task", children[0])["remaining"], 1)

    def test_concurrent_parent_spending_and_child_allocation_share_credit(self):
        parent = self.sdk.create_task(replace(JOB, request_budget=1))
        calls = []
        results = self.race([
            lambda: self.open_sdk().create_task(replace(JOB, request_budget=1), parent=parent),
            lambda: self.open_sdk().infer(parent, "race", lambda _: calls.append(1) or "required"),
        ])
        self.assertEqual(sum(isinstance(result, AccordError) for result in results), 1)
        self.assertEqual(self.sdk.inspect("task", parent)["remaining"], 0)
        children = [result for result in results if isinstance(result, str)]
        self.assertEqual(len(calls) + sum(self.sdk.inspect("task", task)["remaining"] for task in children), 1)

    def test_concurrent_child_spending_respects_transferred_budget(self):
        parent = self.sdk.create_task(replace(JOB, request_budget=1))
        child = self.sdk.create_task(replace(JOB, request_budget=1), parent=parent)
        calls = []
        results = self.race([lambda label=label: self.open_sdk().infer(
            child, label, lambda _: calls.append(1) or "required") for label in ("one", "two")])
        self.assertEqual(sum(isinstance(result, dict) for result in results), 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(self.sdk.inspect("task", parent)["remaining"], 0)
        self.assertEqual(self.sdk.inspect("task", child)["remaining"], 0)

    def test_parent_freshness_checks_all_parent_inputs_before_delegation(self):
        self.sdk.set_resource("extra", "original")
        parent = self.sdk.create_task(replace(JOB, inputs=("spec", "extra")))
        self.sdk.set_resource("extra", "changed")
        self.sdk.set_resource("extra", "original")
        before = self.sdk.events()
        with self.assertRaisesRegex(AccordError, "stale input: extra"):
            self.sdk.create_task(replace(JOB, request_budget=1), parent=parent)
        self.assertEqual(self.sdk.inspect("task", parent)["remaining"], 4)
        self.assertEqual(self.sdk.events(), before)

    def test_profile_aba_prevents_delegation_without_debit(self):
        parent = self.sdk.create_task(JOB)
        self.open_sdk(environment_revision="e2").activate_profile()
        self.sdk.activate_profile()
        with self.assertRaisesRegex(AccordError, "stale profile"):
            self.sdk.create_task(replace(JOB, request_budget=1), parent=parent)
        self.assertEqual(self.sdk.inspect("task", parent)["remaining"], 4)

    def test_ancestor_cancellation_blocks_all_fresh_admission_and_allows_replay(self):
        parent, child, completed, receipts, accepted, grant, operation = self.prepared_child()
        grandchild = self.sdk.create_task(replace(JOB, request_budget=1), parent=child)
        self.sdk.cancel(parent)
        calls = [lambda: self.sdk.create_task(replace(JOB, request_budget=0), parent=child),
                 lambda: self.sdk.infer(child, "new", lambda _: self.fail("model called")),
                 lambda: self.sdk.infer(grandchild, "new", lambda _: self.fail("model called")),
                 lambda: self.sdk.check(child),
                 lambda: self.sdk.accept(child, receipts),
                 lambda: self.sdk.authorize("fresh", child, accepted, "docs", "report", 0),
                 lambda: self.sdk.dispatch(operation)]
        for index, call in enumerate(calls):
            with self.subTest(call=index), self.assertRaisesRegex(AccordError, "cancelled"):
                call()
        self.assertEqual(self.sdk.infer(child, "first", lambda _: self.fail("replayed model")), completed)
        self.assertEqual(self.sdk.prepare(child, accepted, grant), operation)
        self.assertEqual(self.sdk.authorize("publish", child, accepted, "docs", "report", 0), grant)
        self.assertEqual(self.sdk.inspect("task", child)["remaining"], 1)
        self.assertEqual(self.sink.target("report")["version"], 0)

    def test_ancestor_cancellation_blocks_new_preparation(self):
        parent, child, _, _, accepted, _, _ = self.prepared_child()
        grant = self.sdk.authorize("unprepared", child, accepted, "docs", "other", 0)
        self.sdk.cancel(parent)
        with self.assertRaisesRegex(AccordError, "cancelled"):
            self.sdk.prepare(child, accepted, grant)
        self.assertEqual(self.sdk.inspect("grant", grant)["state"], "available")

    def test_already_admitted_effect_reconciles_after_ancestor_cancellation(self):
        parent, child, _, _, _, grant, operation = self.prepared_child()
        def lose_reply(phase):
            if phase == "before_send":
                self.sdk.cancel(parent)
                self.sdk.revoke(grant)
            if phase == "after_send":
                raise ConnectionError("reply lost")
        self.assertEqual(self.sdk.dispatch(operation, crash_hook=lose_reply)["status"], "outcome_unknown")
        self.assertEqual(self.sink.target("report")["version"], 1)
        self.assertEqual(self.open_sdk().recover(operation)["status"], "confirmed")
        self.assertEqual(self.sdk.recover(operation)["status"], "confirmed")
        self.assertEqual(self.sink.target("report")["version"], 1)

    def test_unknown_unapplied_effect_cannot_retry_after_ancestor_cancellation(self):
        parent, _, _, _, _, _, operation = self.prepared_child()
        def fail_before_send(phase):
            if phase == "before_send":
                raise ConnectionError("not sent")
        self.assertEqual(self.sdk.dispatch(operation, crash_hook=fail_before_send)["status"], "outcome_unknown")
        self.sdk.cancel(parent)
        self.assertEqual(self.open_sdk().recover(operation)["status"], "outcome_unknown")
        self.assertEqual(self.sink.target("report")["version"], 0)

    def test_zero_credit_child_and_sibling_cancellation_do_not_affect_other_work(self):
        parent = self.sdk.create_task(JOB)
        first = self.sdk.create_task(replace(JOB, request_budget=0), parent=parent)
        second = self.sdk.create_task(replace(JOB, request_budget=1), parent=parent)
        self.sdk.cancel(first)
        self.sdk.infer(second, "first", lambda _: "required")
        self.sdk.infer(parent, "parent", lambda _: "required")
        self.assertEqual(self.sdk.inspect("task", parent)["remaining"], 2)
        self.assertEqual(self.sdk.inspect("task", first)["remaining"], 0)

    def test_existing_root_without_parent_field_stays_usable(self):
        root = self.sdk.create_task(JOB)
        with self.sdk.store.transaction() as db:
            record = self.sdk.store.get(db, "task", root)
            record.pop("parent", None)
            self.sdk.store.put(db, "task", root, record)
        reopened = self.open_sdk()
        reopened.infer(root, "first", lambda _: "required")
        child = reopened.create_task(replace(JOB, request_budget=1), parent=root)
        reopened.cancel(root)
        with self.assertRaisesRegex(AccordError, "cancelled"):
            reopened.infer(child, "first", lambda _: self.fail("model called"))

    def test_sdk_source_has_no_contract_or_runtime_import(self):
        for node in ast.walk(ast.parse(SOURCE.read_text())):
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn(node.module, ("runtime", "experiments.accord.runtime"))
                self.assertFalse({alias.name for alias in node.names} & {"Contract", "Runtime"})
            if isinstance(node, ast.Import):
                self.assertFalse(any(alias.name.endswith(".runtime") for alias in node.names))


if __name__ == "__main__":
    unittest.main(verbosity=2)
