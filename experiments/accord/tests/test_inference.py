"""Offline boundary tests for the real-model experiment (no model calls)."""

from collections import Counter, defaultdict
from dataclasses import replace
import io
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from experiments.accord.inference.client import Client, StreamFailure, make_artifact, verify_artifact
from experiments.accord.inference.run import common_prefix, integrity_controls, plan
from experiments.accord.inference.workload import load_workload, messages_for, score, self_test
from experiments.accord.ir import AccordError
from experiments.accord.runtime import Validator
from experiments.accord.tests.test_runtime import CONTRACT, make_runtime


class InferenceTests(unittest.TestCase):
    def test_frozen_scorer_controls_and_no_answer_leak(self):
        report = self_test()
        self.assertEqual(report["scorer_cases"], 23)
        self.assertEqual(report["workload_positive_cases"], 12)
        workload = load_workload()
        for case in workload["cases"]:
            messages = messages_for(case, workload)
            self.assertEqual(messages, [{"role": "system", "content": workload["common_context"]},
                                        {"role": "user", "content": case["question"]}])

    def test_json_validity_is_not_correctness(self):
        result = score('{"answer":true}', {"answer": 1})
        self.assertTrue(result["valid_json"])
        self.assertFalse(result["correct"])
        self.assertFalse(score('{"answer":1,"answer":1}', {"answer": 1})["valid_json"])

    def test_order_has_exact_coverage_and_balanced_positions(self):
        workload = load_workload()
        order = plan(workload["cases"])
        self.assertEqual(len(order), 144)
        self.assertEqual(len({(x["case"], x["repeat"], x["cell"]) for x in order}), 144)
        positions = defaultdict(Counter)
        for i, item in enumerate(order):
            positions[item["cell"]][i % 4] += 1
        for counts in positions.values():
            self.assertEqual(dict(counts), {0:9, 1:9, 2:9, 3:9})

    def test_prefix_is_token_exact_and_not_fragment_concatenation(self):
        self.assertEqual(common_prefix([[1,2,3], [1,2,4], [1,2]]), [1,2])
        self.assertEqual(common_prefix([[1], [2]]), [])

    def test_mismatched_and_tampered_artifacts_rejected(self):
        profile = {"model_sha256": "m", "chat_template_sha256": "t", "context_sha256": "c"}
        artifact = make_artifact(profile, "prompt", [7,8,9])
        checks = integrity_controls(profile, [{"artifact":artifact, "prompt":"prompt"}], [7,8])
        self.assertEqual(len(checks), 6)
        self.assertTrue(all(x["rejected_before_dispatch"] for x in checks))
        for bad_ids in ([True], [-1], [1.0], [], [1]*8193):
            bad = make_artifact(profile, "prompt", bad_ids)
            with self.subTest(ids=bad_ids[:2]), self.assertRaises(ValueError):
                verify_artifact(bad, profile, "prompt")
        with self.assertRaises(ValueError):
            verify_artifact(artifact, profile, "changed source")

    def test_stream_failure_preserves_partial_model_output(self):
        data = b'data: {"tokens":[10],"content":"partial"}\n\ndata: {"error":"failed"}\n\n'
        with patch("urllib.request.urlopen", return_value=io.BytesIO(data)):
            with self.assertRaises(StreamFailure) as caught:
                Client().completion("prompt", {}, cache=False)
        self.assertEqual(caught.exception.partial["content"], "partial")
        self.assertEqual(caught.exception.partial["tokens"], [10])
        self.assertEqual(caught.exception.partial["error_event"], {"error":"failed"})

    def test_incomplete_stream_preserves_partial_model_output(self):
        data = b'data: {"tokens":[10],"content":"partial"}\n\n'
        with patch("urllib.request.urlopen", return_value=io.BytesIO(data)):
            with self.assertRaises(StreamFailure) as caught:
                Client().completion("prompt", {}, cache=False)
        self.assertEqual(caught.exception.partial["tokens"], [10])

    def test_remote_engine_is_rejected(self):
        with self.assertRaises(ValueError):
            Client("https://example.com")

    def test_runtime_goldens_are_observed_not_only_scorer_constants(self):
        expected = {c["name"]:c["expected"]["answer"] for c in load_workload()["cases"]}
        with TemporaryDirectory() as directory:
            runtime = make_runtime(directory)
            runtime.set_resource("spec", "A")
            parent = runtime.create_task(replace(CONTRACT, request_budget=7))
            runtime.create_task(CONTRACT, parent=parent)
            runtime.infer(parent, "a", lambda _: "A")
            runtime.infer(parent, "a", lambda _: self.fail("replay invoked model"))
            self.assertEqual(runtime.inspect("task", parent)["remaining"], expected["child_transfer_and_attempt_replay"])
            runtime.set_resource("spec", "A")
            with self.assertRaises(AccordError) as caught:
                runtime.infer(parent, "b", lambda _: "A")
            self.assertEqual(str(caught.exception), expected["identical_resource_write_stales_task"])
        with TemporaryDirectory() as directory:
            runtime = make_runtime(directory, checks={"nonempty":Validator("1", lambda *_: 1)})
            runtime.set_resource("spec", "A")
            task = runtime.create_task(replace(CONTRACT, checks=(("nonempty", "1"),)))
            runtime.infer(task, "a", lambda _: "A")
            receipt = runtime.inspect("check", runtime.check(task)[0])
            self.assertIs(receipt["passed"], expected["integer_validator_return"])


if __name__ == "__main__":
    unittest.main()
