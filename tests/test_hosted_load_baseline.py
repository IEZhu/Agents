"""Lightweight, offline contract tests; runnable with unittest, without the engine."""
from __future__ import annotations

import copy
import json
import os
from pathlib import Path
import random
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from scripts.dev import hosted_load_baseline as baseline


def manifest():
    return {
        "schema_version": 1,
        "pins": {
            "core_sha": "a" * 40,
            "image_digest": "sha256:" + "b" * 64,
            "model_digest": "sha256:" + "c" * 64,
            "config_digest": "sha256:" + "d" * 64,
        },
        "corpus": {"documents": 100, "bytes": 10000, "tokens": 1000},
        "limits": {
            "duration_ms": 1000, "max_active": 1, "max_waiting": 2,
            "max_queued_bytes": 200, "max_generator_lag_ms": 20,
        },
        "resources": {
            "planned_memory_bytes": 10000, "memory_reserve_bytes": 2000,
            "memory_stop_used_bytes": 8000, "planned_disk_bytes": 100000,
            "disk_reserve_bytes": 10000, "disk_stop_free_bytes": 10000,
            "cpu_stop_percent": 80, "pg_latency_increase_stop_percent": 10,
        },
        "classes": [{
            "name": "warm_read", "cache_state": "hit", "arrivals": 5,
            "first_arrival_ms": 0, "interval_ms": 10, "request_bytes": 100,
            "tool_calls_per_action": 1, "deadline_ms": 500, "queue_ttl_ms": 200,
            "slo": {
                "p95_ms": 100, "p99_ms": 200, "max_queue_ms": 100,
                "max_rejection_ppm": 0, "max_noncompletion_ppm": 0,
            },
            "synthetic": {
                "service_ms": 1, "generator_delay_ms": 0,
                "outcome": "completed", "admission": "admitted",
            },
        }],
    }


class BaselineTests(unittest.TestCase):
    def simulate(self, data=None):
        return baseline.simulate(baseline.freeze_manifest(data or manifest()))

    def assert_accounting(self, report):
        for summary in [report["aggregate"], *report["per_class"].values()]:
            counts = summary["counts"]
            self.assertEqual(counts["offered"], sum(counts[key] for key in (
                "completed", "rejected", "unknown", "expired", "dropped")))
            self.assertEqual(counts["dispatched"], sum(counts[key] for key in (
                "completed", "rejected", "unknown")))
            self.assertEqual(counts["known_admitted"], 0)
        total = report["aggregate"]["counts"]
        for key, value in total.items():
            self.assertEqual(value, sum(s["counts"][key] for s in report["per_class"].values()))

    def test_open_schedule_does_not_wait_for_slow_service(self):
        fast = manifest()
        slow = copy.deepcopy(fast)
        slow["classes"][0]["synthetic"]["service_ms"] = 100
        offered = baseline.offered_schedule(baseline.freeze_manifest(slow))
        self.assertEqual([a.scheduled_ms for a in offered], [0, 10, 20, 30, 40])
        fast_report, slow_report = self.simulate(fast), self.simulate(slow)
        self.assertEqual(fast_report["schedule"]["sha256"], slow_report["schedule"]["sha256"])
        self.assertEqual(slow_report["aggregate"]["counts"]["offered"], 5)
        self.assertEqual(slow_report["aggregate"]["counts"]["dropped_queue_full"], 2)
        self.assertEqual(slow_report["occupancy"]["peak_active"], 1)
        self.assertEqual(slow_report["occupancy"]["peak_waiting"], 2)
        self.assertEqual(slow_report["occupancy"]["peak_queued_bytes"], 200)
        self.assert_accounting(slow_report)

    def test_byte_limit_and_zero_waiting_capacity(self):
        data = manifest()
        data["classes"][0]["synthetic"]["service_ms"] = 100
        data["limits"]["max_queued_bytes"] = 99
        report = self.simulate(data)
        self.assertEqual(report["aggregate"]["counts"]["dropped_byte_budget"], 4)
        self.assertEqual(report["occupancy"]["peak_queued_bytes"], 0)
        data["limits"]["max_waiting"] = 0
        report = self.simulate(data)
        self.assertEqual(report["aggregate"]["counts"]["dropped_queue_full"], 4)
        self.assert_accounting(report)

    def test_expiry_removes_waiting_bytes_before_later_arrivals(self):
        data = manifest()
        data["classes"][0].update(arrivals=4, queue_ttl_ms=15)
        data["classes"][0]["synthetic"]["service_ms"] = 100
        data["limits"].update(max_waiting=1, max_queued_bytes=100)
        report = self.simulate(data)
        counts = report["aggregate"]["counts"]
        self.assertEqual((counts["completed"], counts["expired"], counts["dropped"]), (1, 2, 1))
        self.assertEqual(counts["expired_queue_ttl"], 2)
        self.assertEqual(report["aggregate"]["queue_delay_ms"]["max"], 15)
        self.assert_accounting(report)

    def test_deadline_before_dispatch_and_response_at_deadline(self):
        data = manifest()
        data["classes"][0].update(arrivals=2, deadline_ms=50, queue_ttl_ms=200)
        data["classes"][0]["synthetic"]["service_ms"] = 50
        report = self.simulate(data)
        self.assertEqual(report["aggregate"]["counts"]["unknown_deadline"], 2)
        data["classes"][0]["synthetic"]["service_ms"] = 100
        data["classes"][0]["synthetic"]["generator_delay_ms"] = 10
        data["classes"][0]["deadline_ms"] = 5
        report = self.simulate(data)
        self.assertEqual(report["aggregate"]["counts"]["expired_deadline"], 2)
        self.assertEqual(report["aggregate"]["counts"]["dispatched"], 0)
        self.assert_accounting(report)

    def test_waiting_deadline_wins_a_same_time_completion(self):
        data = manifest()
        data["classes"][0].update(arrivals=1, deadline_ms=200)
        data["classes"][0]["synthetic"]["service_ms"] = 50
        short = copy.deepcopy(data["classes"][0])
        short.update(name="write", first_arrival_ms=10, deadline_ms=40)
        data["classes"].append(short)
        report = self.simulate(data)
        self.assertEqual(report["per_class"]["write"]["counts"]["expired_deadline"], 1)
        self.assertEqual(report["per_class"]["write"]["counts"]["dispatched"], 0)
        self.assert_accounting(report)

    def test_caller_timeout_retains_slot_until_native_service_finishes(self):
        data = manifest()
        data["classes"][0].update(arrivals=3, interval_ms=30, deadline_ms=20)
        data["classes"][0]["synthetic"]["service_ms"] = 100
        data["limits"]["max_waiting"] = 0
        report = self.simulate(data)
        counts = report["aggregate"]["counts"]
        self.assertEqual(counts["unknown_deadline"], 1)
        self.assertEqual(counts["dispatched"], 1)
        self.assertEqual(counts["dropped_queue_full"], 2)
        self.assertEqual(report["occupancy"]["peak_retained_after_deadline"], 1)
        self.assertEqual(report["schedule"]["virtual_end_ms"], 100)
        self.assert_accounting(report)

    def test_expired_waiter_never_dispatches_when_active_deadline_elapsed(self):
        data = manifest()
        data["classes"][0].update(arrivals=2, deadline_ms=50, queue_ttl_ms=200)
        data["classes"][0]["synthetic"]["service_ms"] = 100
        report = self.simulate(data)
        counts = report["aggregate"]["counts"]
        self.assertEqual(counts["unknown_deadline"], 1)
        self.assertEqual(counts["expired_deadline"], 1)
        self.assertEqual(counts["dispatched"], 1)
        self.assertEqual(report["schedule"]["virtual_end_ms"], 100)
        self.assert_accounting(report)

    def test_mixed_classes_rejected_unknown_and_completion(self):
        data = manifest()
        data["limits"].update(max_active=3, max_waiting=0)
        data["classes"][0]["arrivals"] = 1
        for name, outcome, admission in [("write", "rejected", "not_admitted"),
                                          ("git_sync", "unknown", "unknown")]:
            other = copy.deepcopy(data["classes"][0])
            other.update(name=name)
            other["synthetic"].update(outcome=outcome, admission=admission)
            data["classes"].append(other)
        report = self.simulate(data)
        self.assertEqual(report["aggregate"]["counts"]["completed"], 1)
        self.assertEqual(report["aggregate"]["counts"]["rejected"], 1)
        self.assertEqual(report["aggregate"]["counts"]["unknown"], 1)
        self.assertEqual(report["aggregate"]["counts"]["simulated_admitted"], 1)
        self.assert_accounting(report)

    def test_late_generator_arrivals_are_visible_and_dropped_when_excessive(self):
        data = manifest()
        data["classes"][0]["synthetic"]["generator_delay_ms"] = 20
        report = self.simulate(data)
        self.assertEqual(report["aggregate"]["counts"]["late"], 5)
        self.assertEqual(report["aggregate"]["generator_delay_ms"]["p99"], 20)
        self.assertEqual(report["aggregate"]["latency_ms"]["p95"], 21)
        data["classes"][0]["synthetic"]["generator_delay_ms"] = 21
        report = self.simulate(data)
        self.assertEqual(report["aggregate"]["counts"]["dropped_generator_late"], 5)
        self.assertIsNone(report["aggregate"]["latency_ms"]["p95"])
        self.assertEqual(report["budget_comparisons"]["warm_read"]["p95_ms"]["status"], "no_samples")
        self.assert_accounting(report)

    def test_drops_are_compared_even_when_completed_latency_is_good(self):
        data = manifest()
        data["classes"][0].update(interval_ms=0, arrivals=10)
        data["limits"]["max_waiting"] = 0
        report = self.simulate(data)
        budgets = report["budget_comparisons"]["warm_read"]
        self.assertEqual(budgets["p95_ms"]["status"], "within_synthetic_budget")
        self.assertEqual(budgets["rejection_ppm"]["status"], "exceeds_synthetic_budget")
        self.assertEqual(budgets["noncompletion_ppm"]["value"], 900000)
        self.assertEqual(report["aggregate"]["latency_ms"]["samples"], 1)
        self.assert_accounting(report)

    def test_virtual_run_is_deterministic_and_never_qualifies_real_capacity(self):
        report = self.simulate()
        self.assertEqual(report, self.simulate())
        self.assertEqual(report["qualification"]["status"], "unmeasured")
        self.assertFalse(report["qualification"]["performance_pass"])
        for evidence in report["qualification"]["evidence"].values():
            self.assertEqual(evidence, "unmeasured")
        self.assertEqual(report["pins"], manifest()["pins"])
        self.assertEqual(report["resource_comparisons"]["status"], "unmeasured")
        self.assert_accounting(report)

    def test_mixed_schedules_preserve_accounting_and_every_capacity_bound(self):
        randomizer = random.Random(90210)
        for scenario in range(30):
            with self.subTest(scenario=scenario):
                data = manifest()
                data["limits"].update(max_active=randomizer.randint(1, 3),
                                      max_waiting=randomizer.randint(0, 5),
                                      max_queued_bytes=randomizer.randint(0, 500))
                template = data["classes"][0]
                data["classes"] = []
                for name in ("warm_read", "write", "git_sync"):
                    item = copy.deepcopy(template)
                    item.update(name=name, arrivals=20, interval_ms=randomizer.randint(0, 10),
                                first_arrival_ms=randomizer.randint(0, 10),
                                deadline_ms=randomizer.randint(1, 200),
                                queue_ttl_ms=randomizer.randint(1, 200),
                                request_bytes=randomizer.randint(1, 200))
                    outcome = randomizer.choice(["completed", "rejected", "unknown"])
                    item["synthetic"].update(service_ms=randomizer.randint(0, 250),
                                             generator_delay_ms=randomizer.randint(0, 30),
                                             outcome=outcome, admission=(
                                                 "not_admitted" if outcome == "rejected" else "unknown"))
                    data["classes"].append(item)
                report = self.simulate(data)
                self.assertEqual(report["aggregate"]["counts"]["offered"], 60)
                for suffix, limit in [("active", "max_active"), ("waiting", "max_waiting"),
                                      ("queued_bytes", "max_queued_bytes")]:
                    self.assertLessEqual(report["occupancy"]["peak_" + suffix], data["limits"][limit])
                    self.assertEqual(report["occupancy"]["final_" + suffix], 0)
                self.assert_accounting(report)

    def test_immutable_snapshot_and_canonical_manifest_digest(self):
        data = manifest()
        frozen = baseline.freeze_manifest(data)
        data["classes"][0]["arrivals"] = 100
        report = baseline.simulate(frozen)
        self.assertEqual(report["aggregate"]["counts"]["offered"], 5)
        self.assertEqual(report["manifest_sha256"], baseline.freeze_manifest(manifest()).sha256)
        with self.assertRaises(AttributeError):
            frozen.canonical_json = "{}"

    def test_strict_validation_and_hard_bounds(self):
        cases = [
            (("schema_version",), 2), (("schema_version",), True),
            (("pins", "core_sha"), "main"), (("pins", "model_digest"), "latest"),
            (("corpus", "tokens"), float("inf")), (("corpus", "tokens"), float("nan")),
            (("corpus", "bytes"), 1.5), (("corpus", "bytes"), -1),
            (("limits", "duration_ms"), 3600001), (("limits", "max_active"), 0),
            (("limits", "max_active"), 1025), (("limits", "max_waiting"), 10001),
            (("limits", "max_queued_bytes"), 64 * 1024**2 + 1),
            (("classes", 0, "arrivals"), 50001), (("classes", 0, "arrivals"), 0),
            (("classes", 0, "interval_ms"), 1000),
            (("classes", 0, "request_bytes"), 1024**2 + 1),
            (("classes", 0, "name"), "arbitrary private payload"),
            (("classes", 0, "slo", "p95_ms"), 201),
            (("classes", 0, "tool_calls_per_action"), 2),
            (("resources", "memory_stop_used_bytes"), 8001),
            (("resources", "disk_stop_free_bytes"), 9999),
            (("classes", 0, "synthetic", "admission"), "not_admitted"),
        ]
        for path, value in cases:
            with self.subTest(path=path, value=value):
                data = manifest()
                item = data
                for part in path[:-1]:
                    item = item[part]
                item[path[-1]] = value
                with self.assertRaises(baseline.ManifestError):
                    baseline.freeze_manifest(data)
        data = manifest()
        data["classes"][0]["payload"] = "private-test-sentinel"
        with self.assertRaises(baseline.ManifestError) as failure:
            baseline.freeze_manifest(data)
        self.assertNotIn("private-test-sentinel", str(failure.exception))
        data = manifest()
        data["classes"].append(copy.deepcopy(data["classes"][0]))
        with self.assertRaises(baseline.ManifestError):
            baseline.freeze_manifest(data)
        data = manifest()
        data["classes"][0].update(arrivals=30000, interval_ms=0)
        other = copy.deepcopy(data["classes"][0])
        other["name"] = "write"
        data["classes"].append(other)
        with self.assertRaises(baseline.ManifestError):
            baseline.freeze_manifest(data)

    def test_documented_example_validates_and_simulates(self):
        documentation = Path(baseline.__file__).parents[2] / "docs/hosted-load-baseline.md"
        example = documentation.read_text().split("```json\n", 1)[1].split("\n```", 1)[0]
        report = self.simulate(json.loads(example))
        self.assertEqual(report["aggregate"]["counts"]["offered"], 5)
        self.assert_accounting(report)

    def test_duplicate_json_nonfinite_and_oversize_files_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "manifest.json"
            for content in ['{"schema_version":1,"schema_version":1}', '{"a":NaN}',
                            '{"a":Infinity}', "[" * 2000, " " * (128 * 1024 + 1)]:
                source.write_text(content)
                with self.assertRaises(baseline.ManifestError):
                    baseline.load_manifest(source)

    def test_private_atomic_replacement_does_not_follow_output_symlink(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            target, output = root / "keep", root / "report.json"
            target.write_text("original")
            target.chmod(0o640)
            output.symlink_to(target)
            baseline.write_report(output, self.simulate())
            self.assertEqual(target.read_text(), "original")
            self.assertEqual(target.stat().st_mode & 0o777, 0o640)
            self.assertFalse(output.is_symlink())
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            output.chmod(0o644)
            baseline.write_report(output, self.simulate())
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)

    def test_failed_replace_preserves_previous_report_and_cleans_tempfile(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "report.json"
            output.write_text("previous")
            with patch.object(os, "replace", side_effect=PermissionError("denied")):
                with self.assertRaises(PermissionError):
                    baseline.write_report(output, self.simulate())
            self.assertEqual(output.read_text(), "previous")
            self.assertEqual(list(Path(directory).iterdir()), [output])

    def test_cli_dry_validation_and_simulation_are_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            source, output = Path(directory) / "input.json", Path(directory) / "output.json"
            source.write_text(json.dumps(manifest()))
            script = Path(baseline.__file__)
            for mode in ("validate", "simulate"):
                result = subprocess.run([sys.executable, "-B", str(script), mode,
                                         "--manifest", str(source), "--output", str(output)],
                                        capture_output=True, text=True, check=False)
                self.assertEqual(result.returncode, 0, result.stderr)
                report = json.loads(output.read_text())
                self.assertEqual(report["mode"], mode)
                self.assertEqual(report["qualification"]["status"], "unmeasured")
                self.assertNotIn(str(source), output.read_text())
                if mode == "validate":
                    self.assertNotIn("aggregate", report)
            result = subprocess.run([sys.executable, "-B", str(script), "simulate",
                                     "--manifest", str(source), "--output", str(source)],
                                    capture_output=True, text=True, check=False)
            self.assertEqual(result.returncode, 2)
            self.assertEqual(json.loads(source.read_text()), manifest())


if __name__ == "__main__":
    unittest.main()
