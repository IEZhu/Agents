"""Negative controls against overstating offline replay evidence."""
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from .replay import replay, ROOT, sha


class ReplayEvidenceTests(unittest.TestCase):
    def fixture(self, path, *, expected_hash=None, rows=None):
        source = "experiments/accord/ir.py"
        digest = sha((ROOT / source).read_bytes()) if expected_hash is None else expected_hash
        (path / "freeze.json").write_text(json.dumps({
            "source_hashes":{source:digest}, "order":[["accord", 101], ["python", 101]]}))
        (path / "summary.json").write_text(json.dumps({"episodes":[] if rows is None else rows}))

    def test_changed_source_fails_before_any_replay(self):
        with TemporaryDirectory() as temp:
            path = Path(temp)
            self.fixture(path, expected_hash="0" * 64)
            with self.assertRaisesRegex(AssertionError, "source changed"):
                replay(path)

    def test_empty_snapshot_is_not_evidence_of_equivalence(self):
        with TemporaryDirectory() as temp:
            path = Path(temp)
            self.fixture(path)
            result = replay(path)
            self.assertFalse(result["all_stage_outcomes_identical"])
            self.assertFalse(result["complete_primary"])
            self.assertEqual(result["source_episodes"], 0)
            self.assertEqual(result["new_model_calls"], 0)

    def test_missing_captures_are_unreplayable_in_both_frontends(self):
        with TemporaryDirectory() as temp:
            path = Path(temp)
            self.fixture(path, rows=[{"index":1, "backend":"accord"}])
            case = path / "episode-01"
            case.mkdir()
            (case / "episode.json").write_text(json.dumps({"stages":[{"role":"implement", "attempt":1}]}))
            result = replay(path)
            self.assertFalse(result["all_stage_outcomes_identical"])
            self.assertEqual(len(result["rows"]), 2)
            for row in result["rows"]:
                self.assertFalse(row["replayable"])
                self.assertIsNone(row["completed"])
                self.assertEqual(len(row["missing_captures"]), 4)
                self.assertEqual(row["captured_responses_reused"], 0)


if __name__ == "__main__":
    unittest.main()
