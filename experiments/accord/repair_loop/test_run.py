"""Runner control-flow tests: simulated model/MCP, real ledger and cache adapter.

No server is contacted and no benchmark result is produced. Successful behavioral
reports are deliberately simulated on a whitespace-only baseline change: these
tests exercise orchestration and never supply a repair solution to model prompts.
The exhaustion test uses the actual frozen behavioral checker.
"""

from contextlib import redirect_stdout
import copy
import io
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from . import run
from .mcp_client import PreparedExchange, TimedResult


IDENTITY = ("protocol", "task_id", "attempt_id", "bundle_revision", "contract_revision")


def fake_schema():
    """Minimal schema structure needed by the real decoder; no MCP substitute."""
    return {
        "properties": {key: {} for key in (*IDENTITY, "checks", "artifacts", "status", "question")}
        | {key: {"items": {}} for key in ("evidence", "limitations", "remaining_work")},
        "$defs": {
            "GenericPayload": {"properties": {"summary": {}, "items": {"items": {}}}},
            "Evidence": {"properties": {"id": {}, "reference": {}, "description": {}}},
        },
    }


class FakeMCP:
    """Simulate preparation and identity gates, retaining exact submitted text."""

    def __init__(self):
        self.prepared = []
        self.validated = []

    def prepare(self, **arguments):
        contract = {
            "protocol": "agents-core/subagent/1",
            "task_id": arguments["task_id"], "attempt_id": arguments["attempt_id"],
            "bundle_revision": "a" * 64,
            "contract_revision": run.sha(json.dumps(arguments, sort_keys=True)),
        }
        exchange = PreparedExchange({"status": "prepared", "contract": contract,
            "worker_context": "Simulated worker context " + json.dumps(contract),
            "result_schema": fake_schema()}, 2.0)
        self.prepared.append((copy.deepcopy(arguments), exchange))
        return exchange

    def validate(self, exchange, raw):
        self.validated.append((exchange, raw))
        try:
            value = run.strict_json(raw)
            valid = all(value.get(key) == exchange.contract[key] for key in IDENTITY)
        except (ValueError, AttributeError):
            valid = False
        return TimedResult({"valid": valid, "errors": [] if valid else ["simulated invalid envelope"]}, 3.0)


class FakeClient:
    """Return bounded engine-shaped responses without network or model calls."""

    def __init__(self, mcp, outputs):
        self.mcp = mcp
        self.outputs = iter(outputs)
        self.schemas = {}
        self.calls = []
        self.rendered = []
        self.last_response = None

    @staticmethod
    def tokenize(text):
        # Stable synthetic IDs, with a one-token common prefix in every request.
        return [1] + [sum(text[index:index + 32].encode()) for index in range(0, len(text), 32)]

    def json(self, route, value):
        if route == "/apply-template":
            prompt = "\n".join(message["content"] for message in value["messages"])
            self.rendered.append(prompt)
            return {"prompt": prompt}, 0.1
        if route == "/tokenize":
            return {"tokens": self.tokenize(value["content"])}, 0.1
        raise AssertionError("Unexpected fake route: " + route)

    def completion(self, prompt, settings, *, cache):
        self.calls.append({"prompt": prompt, "settings": copy.deepcopy(settings), "cache": cache})
        if run.sha(prompt) not in self.schemas:
            raise AssertionError("Decoder was not registered before completion")
        output = next(self.outputs)
        contract = self.mcp.prepared[-1][1].contract
        if isinstance(output, dict) and "raw" in output:
            raw = output["raw"]
        else:
            item = output["item"] if isinstance(output, dict) else output
            value = {**contract, "status": "completed", "result": {"summary": "Simulated output", "items": [item]},
                     "evidence": [{"id": "source", "reference": "supplied source", "description": "Test fixture"}],
                     "artifacts": [], "checks": [], "limitations": [], "remaining_work": [], "question": None}
            if isinstance(output, dict):
                value.update(output.get("override", {}))
            raw = json.dumps(value, ensure_ascii=True, indent=1) + "\n"
        count = len(self.tokenize(prompt))
        response = {"content": raw, "tokens": [7] * 12, "client_total_ms": 5.0, "client_ttft_ms": 1.0,
                    "final": {"timings": {"cache_n": int(cache), "prompt_n": count - int(cache)},
                              "tokens_evaluated": count, "prompt": prompt, "truncated": False, "stop_type": "eos"}}
        self.last_response = response
        return response


class RepairLoopTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        workload = run.checker.load_workload()
        span = workload["span"]
        cls.original = run.checker.baseline()[span["start_byte"]:span["end_byte_exclusive"]].decode()
        cls.changed = cls.original + "\n"  # Whitespace, deliberately not a solution.
        cls.common = workload["common_materials"]

    def exercise(self, backend, outputs, *, behavior=None):
        temp = TemporaryDirectory(prefix="accord-repair-runner-test-")
        self.addCleanup(temp.cleanup)
        directory = Path(temp.name)
        mcp = FakeMCP()
        client = FakeClient(mcp, outputs)
        if behavior is not None:
            behavior_patch = patch.object(run.checker, "run_behavior", return_value=behavior)
            behavior_patch.start()
        try:
            with redirect_stdout(io.StringIO()), patch.object(run, "adopt_checked_child", wraps=run.adopt_checked_child) as adoption:
                result = run.episode(1, backend, 202, client, mcp, {"context_size": 16384}, self.common, [1], directory)
        finally:
            if behavior is not None:
                behavior_patch.stop()
        return result, client, mcp, directory, adoption

    def assert_budget(self, result):
        self.assertEqual(sum(task["remaining"] for task in result["tasks"].values()) + result["request_units_consumed"], 5)
        self.assertLessEqual(result["model_calls"], result["request_units_consumed"])

    def test_failed_proposal_then_success_has_fresh_contract_and_gated_adoption(self):
        settings = []
        for backend in ("accord", "python"):
            with self.subTest(backend=backend):
                result, client, mcp, directory, adoption = self.exercise(
                    backend, ["invalid Python proposal", self.changed, "approve", "approve"],
                    behavior={"passed": True, "changed": True, "simulated": True})
                self.assertTrue(result["completed"])
                self.assertFalse(result["first_attempt_passed"])
                self.assertEqual(result["implementation_attempts"], 2)
                self.assertEqual(result["model_calls"], 4)
                self.assertEqual([stage["accepted"] for stage in result["stages"]], [False, True, True, True])
                self.assertEqual([args["attempt_id"] for args, _ in mcp.prepared], ["1", "2", "1", "1"])
                self.assertEqual(len({exchange.contract["contract_revision"] for _, exchange in mcp.prepared}), 4)
                for index, ((_, prepared), (validated, raw)) in enumerate(zip(mcp.prepared, mcp.validated)):
                    self.assertIs(prepared, validated)
                    stage = result["stages"][index]
                    stored = json.loads((directory / "episode-01" / f"{stage['role']}-{stage['attempt']}" / "response.json").read_text())
                    self.assertEqual(raw, stored["content"])
                self.assertIn("Previous attempt and measured feedback", client.calls[1]["prompt"])
                self.assertIn("invalid Python proposal", client.calls[1]["prompt"])
                self.assertNotIn("invalid Python proposal", client.calls[2]["prompt"])
                self.assertIn('"validated_child_results"', client.calls[2]["prompt"])
                self.assertTrue(adoption.called)
                for call in adoption.call_args_list:
                    host, parent, child, accepted = call.args
                    self.assertEqual(host.inspect("task", child)["accepted"], accepted)
                    self.assertEqual(host.inspect("task", child)["parent"], parent)
                self.assertIsNotNone(result["tasks"]["parent"]["accepted"])
                self.assertEqual([call["cache"] for call in client.calls], [False, True, True, True])
                self.assertEqual(result["prepare_ms"], 8.0)
                self.assertEqual(result["validation_ms"], 12.0)
                self.assert_budget(result)
                settings.append([call["settings"] for call in client.calls])
        self.assertEqual(settings[0], settings[1])
        self.assertTrue(all(value == run.SETTINGS | {"seed": 202} for value in settings[0]))

    def test_real_checker_exhausts_three_unchanged_proposals(self):
        for backend in ("accord", "python"):
            with self.subTest(backend=backend):
                result, client, mcp, _, adoption = self.exercise(backend, [self.original] * 3)
                self.assertFalse(result["completed"])
                self.assertFalse(result["implementation_passed"])
                self.assertEqual(result["model_calls"], 3)
                self.assertEqual(result["implementation_attempts"], 3)
                self.assertEqual(len(mcp.prepared), 3)
                self.assertTrue(all(stage["role"] == "implement" for stage in result["stages"]))
                self.assertTrue(all(not stage["behavior"]["changed"] and not stage["behavior"]["passed"] for stage in result["stages"]))
                self.assertEqual(result["tasks"]["implement"]["remaining"], 0)
                self.assertEqual(result["tasks"]["review"]["remaining"], 1)
                self.assertEqual(result["tasks"]["parent"]["remaining"], 1)
                self.assertIn('"host_report"', client.calls[1]["prompt"])
                adoption.assert_not_called()
                self.assert_budget(result)

    def test_review_rejection_prevents_parent_call_and_acceptance(self):
        for backend in ("accord", "python"):
            with self.subTest(backend=backend):
                result, client, _, _, _ = self.exercise(backend, [self.changed, "reject"],
                    behavior={"passed": True, "changed": True, "simulated": True})
                self.assertFalse(result["completed"])
                self.assertTrue(result["implementation_passed"])
                self.assertEqual(len(client.calls), 2)
                self.assertEqual([stage["role"] for stage in result["stages"]], ["implement", "review"])
                self.assertEqual(result["stages"][1]["decision"], "reject")
                self.assertIsNone(result["tasks"]["parent"]["latest"])
                self.assertIsNone(result["tasks"]["review"]["accepted"])
                self.assert_budget(result)

    def test_invalid_raw_and_wrong_identity_never_reach_checker_or_review(self):
        outputs = [{"raw": "  {broken JSON\n"}, {"item": self.changed, "override": {"attempt_id": "wrong"}},
                   {"raw": '{"task_id":"duplicate","task_id":"again"}'}]
        for backend in ("accord", "python"):
            with self.subTest(backend=backend), patch.object(run.checker, "patch_for_replacement") as decode:
                result, client, mcp, _, adoption = self.exercise(backend, outputs)
                self.assertFalse(result["completed"])
                self.assertEqual(result["model_calls"], 3)
                self.assertEqual(mcp.validated[0][1], outputs[0]["raw"])
                self.assertEqual(mcp.validated[2][1], outputs[2]["raw"])
                self.assertIn("previous_raw_output", client.calls[1]["prompt"])
                self.assertTrue(all(not stage["validation"]["valid"] for stage in result["stages"]))
                decode.assert_not_called()
                adoption.assert_not_called()
                self.assert_budget(result)

    def test_partial_envelope_is_not_accepted_as_success(self):
        outputs = [{"item": self.changed, "override": {"status": "partial", "remaining_work": ["Not solved"]}}] * 3
        for backend in ("accord", "python"):
            with self.subTest(backend=backend), patch.object(run.checker, "patch_for_replacement") as decode:
                result, _, _, _, _ = self.exercise(backend, outputs)
                self.assertFalse(result["completed"])
                self.assertEqual(result["implementation_attempts"], 3)
                self.assertTrue(all("did not complete" in stage["error"] for stage in result["stages"]))
                decode.assert_not_called()
                self.assert_budget(result)

    def test_source_guard_failure_records_consumed_units_without_model_calls(self):
        with TemporaryDirectory(prefix="accord-repair-missing-source-") as source:
            for backend in ("accord", "python"):
                with self.subTest(backend=backend), patch.object(run, "ROOT", Path(source)):
                    result, client, mcp, directory, adoption = self.exercise(backend, [])
                    self.assertFalse(result["completed"])
                    self.assertEqual(result["model_calls"], 0)
                    self.assertEqual(result["request_units_consumed"], 3)
                    self.assertEqual(result["implementation_attempts"], 3)
                    self.assertEqual(client.calls, [])
                    self.assertEqual(mcp.validated, [])
                    self.assertTrue(all(stage["request_units_consumed"] == 1 for stage in result["stages"]))
                    self.assertTrue(all(stage["error_type"] == "FileNotFoundError" for stage in result["stages"]))
                    self.assertEqual(result["tasks"]["implement"]["remaining"], 0)
                    saved = json.loads((directory / "episode-01" / "episode.json").read_text())
                    self.assertEqual(saved["request_units_consumed"], 3)
                    self.assertFalse(saved["completed"])
                    adoption.assert_not_called()
                    self.assert_budget(result)


if __name__ == "__main__":
    unittest.main()
