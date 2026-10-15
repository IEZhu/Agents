"""Small byte-limit regressions; only fake repositories and worker collaborators are used."""
import contextlib
import json
from pathlib import Path
import shutil
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from src.schedules import materials, worker_launch


class RepeatedBlobRepository:
    """Three four-byte paths share one git blob, as cat-file's deduplicated result does."""

    def tree_entries(self, commit):
        return [materials.Entry("100644", "a" * 40, f"file-{index}") for index in range(3)]

    def changes(self, base, head):
        return [materials.Change("000000", "100644", entry.oid, "A", entry.path)
                for entry in self.tree_entries(head)]

    def blobs(self, oids, *, budget):
        found = {oid: b"data" for oid in oids}
        if sum(map(len, found.values())) > budget:
            raise materials.MaterialsRefused("the materials would be too large")
        return found

    def commits(self, base, head):
        return []

    def diff(self, base, head):
        return b"diff\n"

    def log(self, base, head):
        return b"log\n"


class TestMaterialsByteLimits(unittest.TestCase):
    def test_head_materials_charge_each_path_before_creating_the_destination(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(materials, "MAX_HEAD_BYTES", 8):
            destination = Path(temporary) / "materials"
            with self.assertRaisesRegex(materials.MaterialsRefused, "too large"):
                materials.assemble(RepeatedBlobRepository(), "b" * 40, "c" * 40, destination)
            self.assertFalse(destination.exists())

    def test_head_materials_allow_the_exact_per_path_byte_limit(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(materials, "MAX_HEAD_BYTES", 12):
            destination = Path(temporary) / "materials"
            materials.assemble(RepeatedBlobRepository(), "b" * 40, "c" * 40, destination)
            self.assertEqual([path.read_bytes() for path in sorted((destination / "head").iterdir())],
                             [b"data"] * 3)

    def test_tree_export_refuses_repeated_blob_expansion_before_writing_a_batch(self):
        for batch, written_bytes in ((3, 0), (2, 8)):
            with self.subTest(batch=batch), tempfile.TemporaryDirectory() as temporary, \
                    patch.object(materials, "MAX_TREE_BYTES", 8), patch.object(materials, "BATCH", batch):
                destination = Path(temporary) / "tree"
                with self.assertRaisesRegex(materials.MaterialsRefused, "too large"):
                    materials.export_tree(RepeatedBlobRepository(), "c" * 40, destination)
                self.assertEqual(sum(path.stat().st_size for path in destination.iterdir()), written_bytes)
                self.assertFalse((destination / "file-2").exists())

    def test_tree_export_allows_the_exact_limit_across_batches(self):
        with tempfile.TemporaryDirectory() as temporary, patch.object(materials, "MAX_TREE_BYTES", 12), \
                patch.object(materials, "BATCH", 2):
            destination = Path(temporary) / "tree"
            result = materials.export_tree(RepeatedBlobRepository(), "c" * 40, destination)
            self.assertEqual(result["files"], 3)
            self.assertEqual([path.read_bytes() for path in sorted(destination.iterdir())], [b"data"] * 3)


class TestCodexStdoutLimit(unittest.TestCase):
    def launch(self, root, stdout_data):
        tree, review_materials, attempt = (root / name for name in ("tree", "materials", "attempt"))
        tree.mkdir()
        review_materials.mkdir()
        output = attempt / "codex-out" / "result.json"
        schema = {"type": "object", "properties": {"status": {"enum": ["completed"]}},
                  "required": ["status"], "additionalProperties": False}
        result_text = json.dumps({"status": "completed"})
        decision = SimpleNamespace(client_applied=worker_launch.CODEX, reason=None, detail="", known=(),
                                   abort=False, codex=True, launch=object())

        def start(*args, stdout, **kwargs):
            stdout.write(stdout_data)
            output.write_text(result_text)
            return object()

        launcher = worker_launch.Launcher(claude=None, codex="fake-codex", path="/bin", home=root / "home",
                                          state_dir=root / "state", run_id="fake-run", temp_base=root,
                                          codex_decide=lambda *args, **kwargs: decision, codex_start=start)
        with contextlib.ExitStack() as stack:
            for target, value in (
                ("MAX_STDOUT_BYTES", 32),
                ("sandbox.make_temp_root", lambda base: Path(tempfile.mkdtemp(dir=base))),
                ("sandbox.remove_temp_root", lambda path, base: shutil.rmtree(path)),
                ("cs.prepare_run_directories", lambda root: {}),
                ("cs.CodexPaths", lambda **kwargs: SimpleNamespace(output=output)),
                ("cs.describe", lambda launch: {}),
                ("codex_proxy.ConnectProxy", lambda hosts: contextlib.nullcontext(object())),
                ("codex_probes.record_decision", lambda *args, **kwargs: None),
                ("wait", lambda *args: (0, None)),
            ):
                stack.enter_context(patch(f"src.schedules.worker_launch.{target}", value))
            result = launcher.run(client=worker_launch.CODEX, step="code-review-pass", context="test", schema=schema,
                                  tree=tree, materials=review_materials, attempt_dir=attempt, model=None, effort=None,
                                  fallback={})
        self.assertEqual(json.loads(output.read_text()), {"status": "completed"})
        self.assertFalse((attempt / "codex.stdout").exists())
        self.assertFalse((attempt / "codex.stderr").exists())
        return result

    def test_codex_accepts_stdout_at_or_below_the_cap(self):
        for size in (31, 32):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as temporary:
                result = self.launch(Path(temporary), b"x" * size)
                self.assertIsNone(result.failure)
                self.assertEqual(json.loads(result.raw), {"status": "completed"})
                self.assertEqual(result.stdout, "x" * size)

    def test_codex_rejects_stdout_overflow_even_with_a_valid_separate_result(self):
        with tempfile.TemporaryDirectory() as temporary:
            result = self.launch(Path(temporary), b"x" * 32 + b"hidden-tripwire")
            self.assertIsNone(result.raw)
            self.assertIn("standard output exceeds 32 bytes", result.failure)
            self.assertEqual(result.stdout, "x" * 32)
            self.assertEqual(result.cli["exit_code"], 0)


if __name__ == "__main__":
    unittest.main()
