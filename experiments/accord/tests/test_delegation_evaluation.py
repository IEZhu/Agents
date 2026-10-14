"""Evidence-bound handoff, strict interchange, and three-file sink regressions."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from experiments.accord.delegation.backends import ARMS, configurations
from experiments.accord.delegation.run import Trial, adopt_checked_child, inspect_controls
from experiments.accord.delegation.workload import PATHS, SOURCE, bundle, parse_bundle, source_snapshot
from experiments.accord.evaluation.git_sink import git
from experiments.accord.ir import AccordError, digest, text_digest


class DelegationEvaluationTests(unittest.TestCase):
    def trial(self, arm):
        directory = TemporaryDirectory(prefix="accord-handoff-test-")
        self.addCleanup(directory.cleanup)
        return Trial(arm, directory.name, source_snapshot())

    def child_result(self, t):
        t.delegate()
        t.propose(t.child, "child", t.text)
        return t.host.accept(t.child, t.host.check(t.child))

    def test_static_consumer_and_export_controls(self):
        result = inspect_controls()
        self.assertEqual(len(result["invalid_rejected"]), 6)
        self.assertEqual(len(result["variants"]), 2)

    def test_unknown_field_rejected_by_both_json_backends(self):
        from experiments.accord.delegation.workload import HERE
        value = json.loads((HERE / "contracts-1.json").read_text())
        value["child"]["description"] = "ignored?"
        for arm in ("accord_json", "sdk_json"):
            with self.subTest(arm=arm), self.assertRaises(AccordError):
                configurations(arm, data=json.dumps(value))

    def test_exact_child_bytes_and_wrong_parent_binding(self):
        for arm in ARMS:
            with self.subTest(arm=arm):
                t = self.trial(arm)
                accepted = self.child_result(t)
                self.assertEqual(adopt_checked_child(t.host, t.parent, t.child, accepted), t.text)
                other = t.host.create_task(t.specs["parent"])
                with self.assertRaisesRegex(AccordError, "wrong child"):
                    adopt_checked_child(t.host, other, t.child, accepted)

    def test_cancelled_or_stale_child_cannot_be_adopted(self):
        for arm in ARMS:
            for fault in ("cancel", "input", "latest"):
                with self.subTest(arm=arm, fault=fault):
                    t = self.trial(arm)
                    accepted = self.child_result(t)
                    if fault == "cancel": t.host.cancel(t.child)
                    elif fault == "input": t.host.set_resource("base_docs", "changed")
                    else:
                        # Trusted-ledger fault injection models a later task attempt.
                        with t.host.store.transaction() as db:
                            task = t.host.store.get(db, "task", t.child)
                            task["latest"] = "different_attempt"
                            t.host.store.put(db, "task", t.child, task)
                    with self.assertRaises(AccordError):
                        adopt_checked_child(t.host, t.parent, t.child, accepted)
                    self.assertEqual(len(t.calls), 1)
                    self.assertEqual(t.sink.publication_count(t.base), 0)

    def test_patch_rejects_extra_path_and_duplicate_key(self):
        t = self.trial("sdk_python")
        files = parse_bundle(t.text)
        files["unrelated.py"] = ""
        with self.assertRaises(AccordError): parse_bundle(bundle(files))
        with self.assertRaises(AccordError): parse_bundle(t.text.replace('"format":', '"format":"bad","format":', 1))

    def test_bundle_publication_is_exact_and_does_not_touch_checkout(self):
        t = self.trial("sdk_python")
        original_index = git(t.repo, "ls-files", "--stage").stdout
        intent = {"sink_identity": t.sink.identity, "target": t.target,
                  "output": text_digest(t.text), "expected_version": t.base}
        operation = digest("test-patch", intent)
        receipt = t.sink.apply(operation, intent, t.text)
        self.assertEqual(receipt["status"], "confirmed")
        self.assertEqual(t.sink.apply(operation, intent, t.text), receipt)
        self.assertEqual(t.sink.publication_count(t.base), 1)
        self.assertEqual(t.sink.target()["text"], t.text)
        self.assertEqual(set(git(t.repo, "diff", "--name-only", t.base, t.sink.tip()).stdout.splitlines()), set(PATHS))
        self.assertEqual(git(t.repo, "rev-parse", "HEAD").stdout.strip(), t.base)
        self.assertEqual(git(t.repo, "ls-files", "--stage").stdout, original_index)
        self.assertEqual((t.repo / SOURCE).read_text(), t.inputs["base_source"])

    def test_stale_bundle_publication_preserves_tree_and_records_denial(self):
        t = self.trial("sdk_python")
        intent = {"sink_identity": t.sink.identity, "target": t.target,
                  "output": text_digest(t.text), "expected_version": t.base}
        first = t.sink.apply(digest("patch", 1), intent, t.text)
        tree = git(t.repo, "rev-parse", t.sink.tip() + "^{tree}").stdout
        second = t.sink.apply(digest("patch", 2), intent, t.text)
        self.assertEqual((first["status"], second["status"]), ("confirmed", "denied"))
        self.assertEqual(git(t.repo, "rev-parse", t.sink.tip() + "^{tree}").stdout, tree)
        self.assertEqual(t.sink.publication_count(t.base), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
