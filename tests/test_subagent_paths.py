"""Structural diagnostics identify nested failures without exposing their values."""
import json
import sys

import pytest
from pydantic import ValidationError

from src.engine.subagent_validation import (
    ContractError, bounded_object, contract_fingerprint, diagnostics, validate_result,
)
from src.schemas.subagent import MAX_JSON_DEPTH, MAX_JSON_NODES, TaskContract


def assert_error(value, code, path, *, max_chars=128000, allow_json=False):
    with pytest.raises(ContractError) as caught:
        bounded_object(value, max_chars=max_chars, allow_json=allow_json)
    assert caught.value.code == code
    assert caught.value.path == path
    return caught.value


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), float("-inf"), (1, 2), b"secret", {1}, object()])
def test_nested_invalid_values(bad):
    error = assert_error({"evidence": [{"score": bad}]}, "invalid_json_value", "$.evidence[0].score")
    assert str(error) == "Expected finite JSON values only."


@pytest.mark.parametrize("token", ["NaN", "Infinity", "-Infinity"])
def test_nonfinite_raw_json_has_location_and_preserves_code(token):
    raw = '{"evidence":[{"score":' + token + '}]}'
    assert_error(raw, "non_finite", "$.evidence[0].score", allow_json=True)


def test_numeric_overflow_in_raw_json_preserves_value_error_code():
    assert_error('{"outer":[1e999]}', "invalid_json_value", "$.outer[0]", allow_json=True)


@pytest.mark.parametrize("raw,code", [
    ('{"a":1,"a":2}', "duplicate_key"),
    ('{"a":NaN} trailing', "non_finite"),
    ('{"a":NaN,"a":1}', "non_finite"),
    ('[NaN]', "non_finite"),
    ('{"a":Infinity,"b":[0,]}', "non_finite"),
    ('{} trailing', "invalid_json"),
    ('[]', "object_required"),
])
def test_parser_failures_keep_existing_codes(raw, code):
    assert_error(raw, code, "$", allow_json=True)


@pytest.mark.parametrize("bad,code", [([0] * 101, "array_too_large"), ({str(i): 0 for i in range(101)}, "object_too_large")])
def test_nested_container_bounds(bad, code):
    assert_error({"outer": [bad]}, code, "$.outer[0]")


def test_container_and_depth_boundaries_are_still_accepted():
    value = {"array": [0] * 100, "object": {str(i): 0 for i in range(100)}}
    assert bounded_object(value, max_chars=128000) is value
    value = 0
    for _ in range(MAX_JSON_DEPTH):
        value = {"nested": value}
    assert bounded_object(value, max_chars=128000) is value
    assert_error({"nested": value}, "structure_too_large", "$" + ".nested" * (MAX_JSON_DEPTH + 1))


def test_node_limit_reports_first_node_beyond_the_bound():
    # Root + list + 98 * (list + 100 scalars) + final list + 99 scalars = 10000.
    assert MAX_JSON_NODES == 10000
    value = {"rows": [[0] * 100 for _ in range(98)] + [[0] * 99]}
    assert bounded_object(value, max_chars=128000) is value
    value["rows"][-1].append(0)
    assert_error(value, "structure_too_large", "$.rows[98][99]")


@pytest.mark.parametrize("key", [1, None, ("key",), "x" * 129])
def test_invalid_keys_identify_containing_object_without_rendering_key(key):
    assert_error({"outer": [{key: "hidden"}]}, "invalid_key", "$.outer[0]")


def test_invalid_key_objects_are_not_formatted():
    class UnsafeKey:
        def __repr__(self):
            pytest.fail("Invalid keys must not be formatted")
        def __str__(self):
            pytest.fail("Invalid keys must not be formatted")

    assert_error({"outer": {UnsafeKey(): None}}, "invalid_key", "$.outer")


@pytest.mark.parametrize("key", ["", "a.b", "a[0]", "has space", 'quote"slash\\', "line\n\x1b", "ключ", "😀", "\u202e"])
def test_special_keys_use_escaped_quoted_brackets(key):
    expected = "$.outer[" + json.dumps(key, ensure_ascii=True) + "][0]"
    error = assert_error({"outer": {key: [float("inf")]}}, "invalid_json_value", expected)
    assert error.path.isascii()
    assert all(ord(character) >= 32 for character in error.path)


@pytest.mark.parametrize("value,path", [
    ({"outer": ["\ud800"]}, "$.outer[0]"),
    ({"outer": {"\udfff": 1}}, '$.outer["\\udfff"]'),
])
def test_unencodable_unicode_is_located(value, path):
    assert_error(value, "invalid_json_value", path)


def test_integer_encoding_failure_is_located():
    limit = getattr(sys, "get_int_max_str_digits", lambda: 0)()
    if not limit:
        pytest.skip("Interpreter has no integer conversion limit")
    assert_error({"outer": [10 ** limit]}, "invalid_json_value", "$.outer[0]")


def test_payload_bound_precedes_encoding_of_oversized_strings():
    assert_error({"outer": ["\ud800" * 30]}, "payload_too_large", "$.outer[0]", max_chars=20)


@pytest.mark.parametrize("bad", ["\ud800", 10 ** 5000], ids=["surrogate", "large-integer"])
def test_structural_bound_precedes_encoding_failure(bad):
    assert_error({"a": bad, "b": [0] * 101}, "array_too_large", "$.b")


def test_structural_bound_precedes_key_encoding_failure():
    assert_error({"\ud800": 0, "b": [0] * 101}, "array_too_large", "$.b")


@pytest.mark.parametrize("max_chars", [7, 8])
def test_canonical_size_precedes_utf8_failure(max_chars):
    assert_error({"a": "\ud800"}, "payload_too_large", "$", max_chars=max_chars)


def test_utf8_failure_is_located_at_canonical_size_boundary():
    assert_error({"a": "\ud800"}, "invalid_json_value", "$.a", max_chars=9)


@pytest.mark.parametrize("bad", ["\ud800", 10 ** 5000], ids=["surrogate", "large-integer"])
def test_later_character_count_precedes_encoding_failure(bad):
    assert_error({"a": bad, "b": "x" * 30}, "payload_too_large", "$.b", max_chars=20)


def test_integer_encoding_failure_precedes_utf8_failure():
    limit = getattr(sys, "get_int_max_str_digits", lambda: 0)()
    if not limit:
        pytest.skip("Interpreter has no integer conversion limit")
    assert_error({"a": "\ud800", "b": 10 ** limit}, "invalid_json_value", "$.b")


@pytest.mark.parametrize("kind", ["surrogate", "large-integer"])
def test_encoding_failure_location_follows_canonical_key_order(kind):
    bad = "\ud800"
    if kind == "large-integer":
        limit = getattr(sys, "get_int_max_str_digits", lambda: 0)()
        if not limit:
            pytest.skip("Interpreter has no integer conversion limit")
        bad = 10 ** limit
    assert_error({"z": bad, "a": bad}, "invalid_json_value", "$.a")


def test_payload_count_reports_nested_value_and_canonical_size_keeps_root():
    assert_error({"outer": ["x" * 30]}, "payload_too_large", "$.outer[0]", max_chars=20)
    assert_error({"a": ""}, "payload_too_large", "$", max_chars=5)


def test_paths_are_bounded_and_escape_untrusted_keys():
    value = float("inf")
    for _ in range(10):
        value = {"line\n" * 20: value}
    with pytest.raises(ContractError) as caught:
        bounded_object(value, max_chars=128000)
    error = caught.value
    assert error.code == "invalid_json_value"
    for path in (error.path, diagnostics(error, "$.result")[0].path):
        assert len(path) <= 256
        assert path.isascii()
        assert all(ord(character) >= 32 for character in path)
        assert path.endswith("...")


@pytest.fixture
def retained_contract():
    contract = {
        "protocol": "agents-core/subagent/1", "task_id": "paths", "attempt_id": "1",
        "agent": "software_engineer", "bundle_revision": "0" * 64,
        "task": "Check paths", "context": [], "scope": [], "non_goals": [],
        "acceptance_criteria": [{"id": "ac-1", "description": "Show exact locations"}],
        "result_type": "generic", "budgets": {"max_skills": 3, "max_implants": 0, "max_context_chars": 16000},
    }
    contract["contract_revision"] = contract_fingerprint(contract)
    return contract


def test_result_and_contract_structural_error_prefixes(retained_contract):
    response = validate_result(retained_contract, {"evidence": [{"score": float("inf")}]})
    assert not response.valid and response.verification == "structural_only"
    assert response.errors[0].path == "$.result.evidence[0].score"
    response = validate_result(retained_contract, '{"evidence":[{"score":NaN}]}')
    assert response.errors[0].path == "$.result.evidence[0].score"
    assert response.errors[0].code == "non_finite"
    retained_contract["context"] = [{"score": float("inf")}]
    response = validate_result(retained_contract, {})
    assert response.errors[0].path == "$.contract.context[0].score"


def test_schema_diagnostics_use_same_escaped_key_notation(retained_contract):
    key = 'unknown\n"key'
    retained_contract[key] = "hidden value"
    with pytest.raises(ValidationError) as caught:
        TaskContract.model_validate(retained_contract)
    errors = diagnostics(caught.value, "$.contract")
    assert len(errors) == 1
    assert errors[0].path == "$.contract[" + json.dumps(key) + "]"
    assert "hidden value" not in errors[0].message


def test_valid_unicode_keys_and_values_remain_accepted():
    value = {"ключ": [None, True, 123, 1.25, "😀\n", "NaN", "Infinity"],
             "a.b": {"": "valid"}, "x" * 128: "valid key"}
    assert bounded_object(value, max_chars=128000) is value
    assert bounded_object(json.dumps(value), max_chars=128000, allow_json=True) == value
