"""Byte-level compatibility and per-context snapshot identities."""
from dataclasses import replace

import pytest

from src.local_inference.profile import ModelProfile, canonical_hash, canonical_json, content_fingerprint, snapshot_fingerprint
from src.local_inference.models import CapturedContext, SourceFile
from datetime import datetime, timezone


def profile(**updates):
    return ModelProfile(**{"model_sha256": "a" * 64, "engine_sha256": "b" * 64,
        "tokenizer_identity": "tokenizer", "template_identity": "template", "renderer_version": "v1",
        "context_tokens": 16384, "kv_type_k": "f16", "kv_type_v": "f16", "backend_identity": "cpu", **updates})


def captured():
    now = datetime.now(timezone.utc)
    return CapturedContext("ctx", "workspace", "/workspace", "root", "/worktree", (1, 2),
        "system", "data", (SourceFile("a.py", "a" * 64, "data", 4, 1, 3, 100),),
        "c" * 64, now, now, 100, 10, 1000)


def test_canonical_json_is_finite_sorted_utf8():
    assert canonical_json({"z": 1, "a": "é"}) == b'{"a":"\xc3\xa9","z":1}'
    assert canonical_hash({"a": 1, "b": 2}) == canonical_hash({"b": 2, "a": 1})
    for value in (float("nan"), float("inf"), float("-inf")):
        with pytest.raises(ValueError):
            canonical_json({"number": value})
    with pytest.raises(ValueError):
        canonical_json({1: "ambiguous-key"})


@pytest.mark.parametrize("updates", [{"model_sha256": "d" * 64}, {"engine_sha256": "d" * 64},
    {"template_identity": "new"}, {"renderer_version": "v2"}, {"context_tokens": 8192},
    {"kv_type_k": "q8_0"}, {"backend_identity": "metal"}, {"rope_settings": {"scale": 2.0}},
    {"model_settings": {"token_state_protocol": 2}}])
def test_relevant_settings_change_stable_profile(updates):
    assert profile().stable_hash != profile(**updates).stable_hash


def test_profile_nested_inputs_cannot_change_after_binding():
    original = {"settings": {"scale": 1}}
    immutable = profile(model_settings=original)
    digest = immutable.stable_hash
    original["settings"]["scale"] = 2
    assert immutable.stable_hash == digest
    with pytest.raises(TypeError):
        immutable.model_settings["settings"]["scale"] = 3


def test_worktree_profile_config_and_instance_identity_are_explicit():
    context = captured()
    stable = profile().stable_hash
    for changed in (replace(context, source_root="/other-worktree"), replace(context, workspace_id="other"),
                    replace(context, owner_config_revision="d" * 64)):
        assert content_fingerprint(context, stable) != content_fingerprint(changed, stable)
    other_instance = replace(context, context_id="other-instance")
    assert content_fingerprint(context, stable) == content_fingerprint(other_instance, stable)
    assert snapshot_fingerprint(context, stable) != snapshot_fingerprint(other_instance, stable)
