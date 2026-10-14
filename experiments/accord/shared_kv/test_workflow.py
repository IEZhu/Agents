"""Offline checks of the shared host policy on both lifecycle implementations."""
from dataclasses import replace
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
import json
from unittest.mock import patch
from experiments.accord.shared_kv.run import episode
from experiments.accord.inference.client import StreamFailure

from experiments.accord.ir import AccordError
from experiments.accord.runtime import Runtime, Validator
from experiments.accord.evaluation.python_sdk import PythonSDK
from experiments.accord.delegation.run import adopt_checked_child
from experiments.accord.shared_kv.run import declaration, strict_json
from experiments.accord.shared_kv.workload import checker


class WorkflowTests(unittest.TestCase):
    def hosts(self):
        for backend, cls in (("accord", Runtime), ("python", PythonSDK)):
            with TemporaryDirectory() as directory:
                host = cls(Path(directory) / "ledger.sqlite", {"approved": Validator("1", lambda text, inputs: True)}, {})
                host.set_resource("repository", "frozen context")
                parent = host.create_task(declaration(backend, "parent", 3))
                yield backend, host, parent

    def test_child_scope_and_budget_cannot_expand(self):
        for backend, host, parent in self.hosts():
            for changes in ({"inputs": ("repository", "outside")}, {"request_budget": 4},
                            {"output_max_bytes": 16385}, {"publish_to": ("outside",)}):
                with self.subTest(backend=backend, changes=changes), self.assertRaises(AccordError):
                    host.create_task(replace(declaration(backend, "child", 1), **changes), parent=parent)
            self.assertEqual(host.inspect("task", parent)["remaining"], 3)

    def test_cancellation_and_stale_source_block_before_model_dispatch(self):
        for change in ("parent_cancel", "child_cancel", "source"):
            for backend, host, parent in self.hosts():
                child = host.create_task(declaration(backend, "child", 1), parent=parent)
                if change == "source":
                    host.set_resource("repository", "changed")
                else:
                    host.cancel(parent if change == "parent_cancel" else child)
                calls = []
                with self.subTest(backend=backend, change=change), self.assertRaises(AccordError):
                    host.infer(child, "draft", lambda _: calls.append(1) or "answer")
                self.assertEqual(calls, [])

    def test_child_receipts_cannot_accept_parent(self):
        for backend, host, parent in self.hosts():
            child = host.create_task(declaration(backend, "child", 1), parent=parent)
            host.infer(child, "draft", lambda _: "candidate")
            receipts = host.check(child)
            accepted = host.accept(child, receipts)
            self.assertEqual(adopt_checked_child(host, parent, child, accepted), "candidate")
            host.infer(parent, "draft", lambda _: "candidate")
            with self.subTest(backend=backend), self.assertRaises(AccordError):
                host.accept(parent, receipts)

    def test_accepted_child_must_still_be_current(self):
        for change in ("cancel", "source"):
            for backend, host, parent in self.hosts():
                child = host.create_task(declaration(backend, "child", 1), parent=parent)
                host.infer(child, "draft", lambda _: "candidate")
                accepted = host.accept(child, host.check(child))
                if change == "cancel":
                    host.cancel(child)
                else:
                    host.set_resource("repository", "changed")
                with self.subTest(backend=backend, change=change), self.assertRaises(AccordError):
                    adopt_checked_child(host, parent, child, accepted)

    def test_stream_failure_evidence_is_preserved(self):
        class FailingClient:
            schemas = {}
            last_response = None
            def completion(self, prompt, settings, *, cache):
                raise StreamFailure("offline failure", {"content":"partial-output-marker", "tokens":[7], "client_ttft_ms":2})
        for backend in ("accord", "python"):
            with TemporaryDirectory() as directory:
                with patch("experiments.accord.shared_kv.run.render", return_value=("prompt", [1,2], 0)), patch("experiments.accord.shared_kv.run.schema", return_value={}):
                    with self.assertRaises(StreamFailure):
                        episode(1, backend, True, FailingClient(), {"kv-e01-implement":{}},
                                {"context_size":16384}, "context", [1], Path(directory))
                error = json.loads((Path(directory) / "episode-01/implement-error.json").read_text())
                self.assertEqual(error["partial"]["content"], "partial-output-marker")
                self.assertEqual(error["call_id"], "kv-e01-implement")

    def test_closed_json_and_frozen_workload_controls(self):
        for raw in ('{"a":1,"a":2}', '{"a":NaN}', '{} {}'):
            with self.assertRaises(ValueError):
                strict_json(raw)
        report = checker.self_test()
        self.assertEqual(report["patch_rejection_cases"], 33)
        self.assertEqual(len(report["baseline_expected_failures"]), 4)


if __name__ == "__main__":
    unittest.main()
