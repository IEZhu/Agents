"""Regression checks for the real Git adapter and controlled comparison."""

import ast
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
from pathlib import Path
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch
import subprocess

from experiments.accord.evaluation.clients import json_contract, python_contract, sdk_specification
from experiments.accord.evaluation.git_sink import GitSink, git, seed_repository
from experiments.accord.evaluation.workload import PATHS, candidate, checks, source_snapshot
from experiments.accord.ir import AccordError, digest, revision_token, text_digest


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.temp = TemporaryDirectory(prefix="accord-git-test-")
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.inputs = source_snapshot()
        self.base = seed_repository(self.repo, self.inputs, PATHS)
        self.sink = GitSink(self.repo, "test", initial=self.base)
        self.text = candidate(self.inputs)

    def intent(self, text=None, version=None):
        return {"sink_identity": self.sink.identity, "target": "test_guide",
                "output": text_digest(self.text if text is None else text),
                "expected_version": self.base if version is None else version}

    def test_json_and_python_task_declarations_are_equivalent(self):
        self.assertEqual(json_contract(), python_contract())
        self.assertEqual(asdict(sdk_specification()), asdict(python_contract()))

    def test_baseline_has_no_runtime_or_contract_dependency(self):
        path = Path(__file__).parents[1] / "evaluation/python_sdk.py"
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                self.assertNotIn(node.module, ("runtime", "experiments.accord.runtime"))
                self.assertFalse({alias.name for alias in node.names} & {"Runtime", "Contract"})

    def test_version_tokens_allow_git_ids_but_reject_unsafe_types(self):
        self.assertEqual(revision_token(self.base), self.base)
        self.assertEqual(revision_token(0), 0)
        for value in (True, -1, 2**63, "", "a b", "line\nbreak", "x" * 129, None):
            with self.subTest(value=value), self.assertRaises(AccordError):
                revision_token(value)

    def test_domain_predicates_check_real_sources_and_reject_negative_controls(self):
        registry = checks()
        self.assertTrue(all(check.run(self.text, self.inputs) for check in registry.values()))
        self.assertFalse(registry["bounded_edit"].run(self.text.replace("# Tests", "# Unrelated", 1), self.inputs))
        self.assertFalse(registry["runnable_command"].run(self.text.replace("tests/test_subagent_mcp.py -q", "fake.py -q", 1), self.inputs))
        from experiments.accord.evaluation.workload import COMMAND
        for bad in ("# " + COMMAND, 'echo "' + COMMAND + '"'):
            self.assertFalse(registry["runnable_command"].run(self.text.replace(COMMAND, bad, 1), self.inputs))
        changed = self.inputs | {"core_tests": "def unrelated(): pass"}
        self.assertFalse(registry["source_alignment"].run(self.text, changed))
        self.assertFalse(registry["focused_tests"].run(self.text, changed))

    def test_concurrent_publication_has_one_winner_and_keeps_index_and_head(self):
        before_index = git(self.repo, "ls-files", "--stage").stdout
        barrier = threading.Barrier(2)
        def send(label):
            text = self.text + label
            intent = self.intent(text)
            operation = digest("git-race", label)
            barrier.wait(timeout=5)
            return operation, self.sink.apply(operation, intent, text)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(send, ("one", "two")))
        self.assertEqual(sorted(result["status"] for _, result in results), ["confirmed", "denied"])
        self.assertEqual(self.sink.publication_count(self.base), 1)
        for operation, result in results:
            self.assertEqual(self.sink.lookup(operation), result)
        self.assertEqual(git(self.repo, "rev-parse", "HEAD").stdout.strip(), self.base)
        self.assertEqual(git(self.repo, "ls-files", "--stage").stdout, before_index)
        self.assertEqual((self.repo / "tests/README.md").read_text(), self.inputs["test_guide"])

    def test_receipts_survive_later_commits_and_denials_replay(self):
        first = digest("operation", 1)
        confirmed = self.sink.apply(first, self.intent(), self.text)
        stale = digest("operation", 2)
        denied = self.sink.apply(stale, self.intent(), self.text)
        next_intent = self.intent(version=self.sink.tip())
        self.sink.apply(digest("operation", 3), next_intent, self.text)
        self.assertEqual(self.sink.apply(first, self.intent(), self.text), confirmed)
        self.assertEqual(self.sink.apply(stale, self.intent(), self.text), denied)
        self.assertEqual(self.sink.publication_count(self.base), 2)

    def test_committed_ref_with_lost_ack_has_no_separate_receipt_gap(self):
        from experiments.accord.evaluation import git_sink
        original, injected = git_sink.git, []
        def fault(repo, *args, **kwargs):
            response = original(repo, *args, **kwargs)
            if args[0] == "update-ref" and response.returncode == 0 and not injected:
                self.assertNotIn("--stdin", args)
                injected.append(True)
                return subprocess.CompletedProcess(response.args, 128, "", "injected loss after CAS")
            return response
        operation = digest("regression", "receipt-gap")
        with patch.object(git_sink, "git", fault):
            result = self.sink.apply(operation, self.intent(), self.text)
        self.assertTrue(injected)
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(self.sink.lookup(operation), result)
        self.assertEqual(self.sink.publication_count(self.base), 1)

    def test_same_operation_winning_after_lookup_keeps_its_confirmed_outcome(self):
        operation = digest("regression", "same-operation-interleaving")
        other = GitSink(self.repo, "test")
        original, competing = self.sink._lookup_at, []
        def lookup_at(op, tip):
            result = original(op, tip)
            if result is None and not competing:
                competing.append(other.apply(operation, self.intent(), self.text))
            return result
        with patch.object(self.sink, "_lookup_at", lookup_at):
            result = self.sink.apply(operation, self.intent(), self.text)
        self.assertEqual(result, competing[0])
        self.assertEqual(result["status"], "confirmed")
        self.assertEqual(self.sink.lookup(operation), result)
        self.assertEqual(self.sink.publication_count(self.base), 1)
        self.assertEqual(git(self.repo, "rev-list", "--count", f"{self.base}..{self.sink.tip()}").stdout.strip(), "1")

    def test_sink_rejects_scope_id_payload_and_version_substitution(self):
        intent = self.intent()
        operation = digest("operation", "binding")
        self.sink.apply(operation, intent, self.text)
        for changed in (intent | {"target": "outside"}, intent | {"expected_version": self.sink.tip()},
                        intent | {"expected_version": "HEAD"}, intent | {"sink_identity": "elsewhere"}):
            with self.subTest(changed=changed), self.assertRaises(AccordError):
                self.sink.apply(operation, changed, self.text)
        with self.assertRaises(AccordError):
            self.sink.apply(operation, intent, "unvalidated text")
        self.assertEqual(self.sink.publication_count(self.base), 1)


if __name__ == "__main__":
    unittest.main()
