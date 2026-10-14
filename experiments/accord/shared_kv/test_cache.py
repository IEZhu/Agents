"""Offline adapter contract checks. No server, sockets, or model calls."""

import ast
import copy
import importlib.util
import inspect
import json
import os
from pathlib import Path
import sys
import threading
import unittest
from unittest.mock import patch

from experiments.accord.shared_kv import cache as shared
MODULE_PATH = Path(shared.__file__)

from experiments.accord.inference.client import Client, StreamFailure

SETTINGS = {"n_predict": 4, "id_slot": 0, "temperature": 0, "seed": 42}
PROFILE = {"engine_commit": "frozen-engine", "model_sha256": "frozen-model",
           "chat_template_sha256": "frozen-template", "context_sha256": "frozen-source",
           "context_size": 32, "settings": SETTINGS}
PREFIX = [10, 20]
PROMPT = "trusted rendered prompt"
IDS = [10, 20, 30, 40]


class FakeClient:
    def __init__(self):
        self.calls = []
        self.failure = None
        self.reused = 2
        self.change = None
        self.started = threading.Event()
        self.second_entered = threading.Event()
        self.release = None
        self.active = 0
        self.max_active = 0

    def completion(self, prompt, settings, *, cache):
        self.calls.append((prompt, copy.deepcopy(settings), cache))
        self.active += 1
        self.max_active = max(self.max_active, self.active)
        index = len(self.calls)
        try:
            self.started.set()
            if index > 1:
                self.second_entered.set()
            if self.release is not None and index == 1:
                if not self.release.wait(2):
                    raise TimeoutError("offline test release was not signalled")
            if self.failure:
                failure, self.failure = self.failure, None
                raise failure
            reused = self.reused if cache else 0
            response = {"content": str(index), "tokens": [71], "client_ttft_ms": 0.1,
                        "client_total_ms": 0.2, "request_body_bytes": 100,
                        "final": {"timings": {"cache_n": reused, "prompt_n": len(IDS) - reused},
                                  "tokens_evaluated": len(IDS), "truncated": False,
                                  "prompt": prompt, "stop": True}}
            if self.change:
                self.change(response)
            return response
        finally:
            self.active -= 1


class SharedKVTests(unittest.TestCase):
    def setUp(self):
        self.client = FakeClient()
        self.service = shared.SharedKV(self.client, PROFILE, SETTINGS)
        self.handle = self.service.register("case-a", "parent", PREFIX)

    def call(self, handle=None, actor="parent", namespace="case-a", profile=None,
             prompt_text=PROMPT, prompt_ids=None):
        return self.service.complete(self.handle if handle is None else handle, actor,
                                     namespace, PROFILE if profile is None else profile,
                                     prompt_text, IDS if prompt_ids is None else prompt_ids)

    def test_parent_child_share_resident_handle_and_preserve_response(self):
        self.service.grant(self.handle, "parent", "child")
        first = self.call()
        second = self.call(actor="child")
        self.assertEqual([c[2] for c in self.client.calls], [False, True])
        self.assertEqual(first["shared_kv"]["actual_reused_tokens"], 0)
        self.assertEqual(second["shared_kv"]["actual_reused_tokens"], 2)
        self.assertEqual(second["shared_kv"]["actual_evaluated_tokens"], 2)
        self.assertEqual(second["shared_kv"]["cache_state"], "hit")
        self.assertEqual(second["content"], "2")
        self.assertEqual(second["tokens"], [71])
        for key in ("lock_wait_ms", "validation_ms", "dispatch_ms", "lock_hold_ms",
                    "service_total_ms", "service_ttft_ms"):
            self.assertGreaterEqual(second["shared_kv"][key], 0)
        self.assertTrue(all(type(c[0]) is str for c in self.client.calls))

    def test_disabled_baseline_is_always_cold_with_identical_text(self):
        self.service = shared.SharedKV(self.client, PROFILE, SETTINGS, enabled=False)
        self.handle = self.service.register("case-a", "parent", PREFIX)
        self.call()
        response = self.call()
        self.assertEqual([c[2] for c in self.client.calls], [False, False])
        self.assertEqual([c[0] for c in self.client.calls], [PROMPT, PROMPT])
        self.assertEqual(response["shared_kv"]["prior_cache_state"], "disabled")

    def test_reject_profile_source_namespace_prefix_actor_before_dispatch(self):
        for field in ("engine_commit", "model_sha256", "chat_template_sha256", "context_sha256"):
            wrong = copy.deepcopy(PROFILE)
            wrong[field] = "changed"
            with self.subTest(field=field), self.assertRaises(shared.CacheValidationError):
                self.call(profile=wrong)
        for arguments, error in (({"namespace": "case-b"}, ValueError),
                                 ({"prompt_ids": [10, 21, 30, 40]}, ValueError),
                                 ({"prompt_ids": [10]}, ValueError),
                                 ({"actor": "ungranted-child"}, PermissionError)):
            with self.subTest(arguments=arguments), self.assertRaises(error):
                self.call(**arguments)
        self.assertEqual(self.client.calls, [])

    def test_grants_require_owner_and_are_revocable(self):
        with self.assertRaises(PermissionError):
            self.service.grant(self.handle, "outsider", "child")
        self.service.grant(self.handle, "parent", "child")
        self.call(actor="child")
        with self.assertRaises(PermissionError):
            self.service.revoke(self.handle, "child", "parent")
        self.service.revoke(self.handle, "parent", "child")
        with self.assertRaises(PermissionError):
            self.call(actor="child")
        self.assertEqual(len(self.client.calls), 1)
        self.call()
        self.assertTrue(self.client.calls[-1][2])

    def test_invalidation_is_owner_only_and_permanent(self):
        self.call()
        with self.assertRaises(PermissionError):
            self.service.invalidate(self.handle, "outsider")
        self.service.invalidate(self.handle, "parent")
        with self.assertRaises(ValueError):
            self.call()
        with self.assertRaises(ValueError):
            self.service.grant(self.handle, "parent", "child")
        self.handle = self.service.register("case-a", "parent", PREFIX)
        self.call()
        self.assertEqual([c[2] for c in self.client.calls], [False, False])

    def test_cross_namespace_residency_misses_are_cold(self):
        other = self.service.register("case-b", "parent", PREFIX)
        self.call()
        self.call(handle=other, namespace="case-b")
        returned = self.call()
        self.assertEqual([c[2] for c in self.client.calls], [False, False, False])
        self.assertEqual(returned["shared_kv"]["prior_cache_state"], "residency_miss")
        self.call()
        self.assertTrue(self.client.calls[-1][2])

    def test_distinct_handle_same_namespace_is_still_cold(self):
        other = self.service.register("case-a", "parent", PREFIX)
        self.call()
        self.call(handle=other)
        self.assertEqual([c[2] for c in self.client.calls], [False, False])

    def test_failed_stream_clears_residency_but_does_not_retry(self):
        self.call()
        failure = StreamFailure("incomplete stream", {"content": "partial"})
        self.client.failure = failure
        with self.assertRaises(StreamFailure) as caught:
            self.call()
        self.assertIs(caught.exception, failure)
        self.assertEqual(len(self.client.calls), 2)
        self.call()
        self.assertEqual([c[2] for c in self.client.calls], [False, True, False])

    def test_observed_cache_misses_partial_and_larger_hits_are_reported(self):
        self.call()
        for count, state in ((0, "miss"), (1, "partial_hit"), (3, "hit")):
            self.client.reused = count
            response = self.call()
            self.assertTrue(response["shared_kv"]["requested_reuse"])
            self.assertEqual(response["shared_kv"]["actual_reused_tokens"], count)
            self.assertEqual(response["shared_kv"]["cache_state"], state)

    def test_bounded_lru_eviction_rejects_stale_handles_before_dispatch(self):
        self.service = shared.SharedKV(self.client, PROFILE, SETTINGS, max_entries=2)
        first = self.service.register("case-a", "parent", PREFIX)
        second = self.service.register("case-a", "parent", PREFIX)
        self.call(handle=first)
        third = self.service.register("case-a", "parent", PREFIX)
        with self.assertRaises(ValueError):
            self.call(handle=second)
        self.assertEqual(len(self.client.calls), 1)
        self.call(handle=first)
        self.assertTrue(self.client.calls[-1][2])
        self.call(handle=third)
        self.assertFalse(self.client.calls[-1][2])
        one = shared.SharedKV(self.client, PROFILE, SETTINGS, max_entries=1)
        first = one.register("case-a", "parent", PREFIX)
        one.complete(first, "parent", "case-a", PROFILE, PROMPT, IDS)
        next_handle = one.register("case-a", "parent", PREFIX)
        result = one.complete(next_handle, "parent", "case-a", PROFILE, PROMPT, IDS)
        self.assertEqual(result["shared_kv"]["prior_cache_state"], "empty")

    def test_whole_requests_are_serialized_including_wait_timing(self):
        self.service.grant(self.handle, "parent", "child")
        self.client.release = threading.Event()
        second_started = threading.Event()
        results, errors = [], []

        def run(actor):
            try:
                if actor == "child":
                    second_started.set()
                results.append(self.call(actor=actor))
            except BaseException as error:
                errors.append(error)

        first = threading.Thread(target=run, args=("parent",))
        second = threading.Thread(target=run, args=("child",))
        first.start()
        try:
            self.assertTrue(self.client.started.wait(1))
            second.start()
            self.assertTrue(second_started.wait(1))
            self.assertFalse(self.client.second_entered.wait(0.05))
        finally:
            self.client.release.set()
            first.join(2)
            if second.ident is not None:
                second.join(2)
        self.assertFalse(first.is_alive() or second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(self.client.max_active, 1)
        self.assertEqual([c[2] for c in self.client.calls], [False, True])
        self.assertEqual(len(results), 2)
        self.assertGreater(results[1]["shared_kv"]["lock_wait_ms"], 0)

    def test_strict_tokens_and_context_output_limits(self):
        for ids in ([True, 20, 30, 40], [10, 20, -1, 40], [10, 20, 3.0, 40],
                    "10203040", [], [10, 20] + [30] * 27):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.call(prompt_ids=ids)
        for prefix in ([], [True], [-1], [1] * 29):
            with self.subTest(prefix=prefix), self.assertRaises(ValueError):
                self.service.register("case-a", "parent", prefix)
        with self.assertRaises(ValueError):
            self.call(prompt_text=IDS)
        self.assertEqual(self.client.calls, [])

    def test_constructor_bounds_and_pinned_settings(self):
        for delta in ({"n_predict": 0}, {"n_predict": -1}, {"n_predict": True},
                      {"n_predict": 32}, {"id_slot": True}, {"id_slot": 1}):
            with self.subTest(delta=delta), self.assertRaises(ValueError):
                shared.SharedKV(self.client, PROFILE, {**SETTINGS, **delta})
        for value in (0, -1, True, 1.5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                shared.SharedKV(self.client, PROFILE, SETTINGS, max_entries=value)
        with self.assertRaises(ValueError):
            shared.SharedKV(self.client, PROFILE, SETTINGS, enabled=1)

    def test_input_mutation_cannot_change_pinned_profile_settings_or_prefix(self):
        profile, settings, prefix = copy.deepcopy(PROFILE), copy.deepcopy(SETTINGS), PREFIX[:]
        self.service = shared.SharedKV(self.client, profile, settings)
        self.handle = self.service.register("case-a", "parent", prefix)
        profile["context_sha256"] = "changed"
        settings["n_predict"] = 900
        prefix[0] = 999
        self.call()
        self.assertEqual(self.client.calls[-1][1], SETTINGS)
        with self.assertRaises(ValueError):
            self.call(profile=profile)

    def test_invalid_engine_response_clears_residency(self):
        changes = (
            lambda r: r["final"].update(prompt="different"),
            lambda r: r["final"].update(truncated=True),
            lambda r: r["final"].update(tokens_evaluated=3),
            lambda r: r["final"]["timings"].update(prompt_n=9),
            lambda r: r["final"]["timings"].update(cache_n=True),
            lambda r: r.update(tokens=[1] * 5),
            lambda r: r.update(client_ttft_ms=float("nan")),
        )
        for change in changes:
            with self.subTest(change=change):
                self.client.change = None
                self.call()
                self.client.change = change
                with self.assertRaises(ValueError):
                    self.call()
                self.client.change = None
                self.call()
                self.assertFalse(self.client.calls[-1][2])

    def test_unexpected_cold_reuse_is_rejected(self):
        def change(response):
            response["final"]["timings"].update(cache_n=1, prompt_n=3)
        self.client.change = change
        with self.assertRaises(ValueError):
            self.call()
        self.client.change = None
        self.call()
        self.assertFalse(self.client.calls[-1][2])

    def test_handle_is_instance_and_process_local(self):
        other = shared.SharedKV(self.client, PROFILE, SETTINGS)
        foreign = other.register("case-a", "parent", PREFIX)
        for invalid in (foreign, object(), [], "copied-handle"):
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.call(handle=invalid)
        with patch.object(shared.os, "getpid", return_value=os.getpid() + 1):
            with self.assertRaises(ValueError):
                self.call()
        self.assertEqual(self.client.calls, [])

    def test_exact_client_source_contract_and_existing_native_response(self):
        self.assertEqual(str(inspect.signature(Client.completion)), "(self, prompt, settings, *, cache)")
        source = inspect.getsource(Client.completion)
        self.assertIn('"cache_prompt": cache', source)
        self.assertIn('"stream": True', source)
        self.assertIn('"return_tokens": True', source)
        client_file = Path(inspect.getfile(Client))
        fixture = client_file.parent / "results/qwen3-8b-m4max-20261014/trials.jsonl"
        seen = set()
        with fixture.open() as records:
            for line in records:
                row = json.loads(line)
                if row["cell"] in ("text_cold", "text_warm") and row["cell"] not in seen:
                    check = shared.SharedKV(self.client, {"context_size": 8192},
                                            {"n_predict": 128, "id_slot": 0})
                    observed = check._check_response(row, row["final"]["prompt"],
                                                    row["input_tokens"], row["cell"] == "text_warm")
                    self.assertEqual(observed, (row["cache_n"], row["prompt_n"]))
                    seen.add(row["cell"])
                if len(seen) == 2:
                    break
        self.assertEqual(seen, {"text_cold", "text_warm"})

    def test_proposal_uses_only_completion_engine_call_without_disk_io(self):
        tree = ast.parse(MODULE_PATH.read_text())
        calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
        client_calls = [node.func.attr for node in calls if isinstance(node.func, ast.Attribute)
                        and isinstance(node.func.value, ast.Attribute)
                        and node.func.value.attr == "_client"]
        self.assertEqual(client_calls, ["completion"])
        self.assertFalse(any(isinstance(node.func, ast.Name) and node.func.id == "open" for node in calls))
        self.assertFalse(any(isinstance(node.func, ast.Attribute) and node.func.attr in
                             ("write_text", "write_bytes", "save", "restore", "json") for node in calls))


if __name__ == "__main__":
    unittest.main(verbosity=2)
