"""Bounded structural validation. References are data and are never dereferenced."""
from __future__ import annotations

import hashlib
import json
import math
from typing import Any

from pydantic import ValidationError

from src.schemas.subagent import (Diagnostic, MAX_ERRORS, MAX_JSON_DEPTH, MAX_JSON_NODES,
                                  MAX_RESULT_CHARS, RESULT_MODELS, TaskContract,
                                  ValidationResponse)


_MAX_PATH_CHARS = 256
_NONFINITE = object()


class ContractError(ValueError):
    def __init__(self, code: str, message: str, path: str = "$"):
        super().__init__(message)
        self.code, self.path = code, path


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                      allow_nan=False)


def fingerprint(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def contract_fingerprint(contract: dict) -> str:
    return fingerprint({key: value for key, value in contract.items()
                        if key != "contract_revision"})


def _bounded_path(path: str) -> str:
    return path if len(path) <= _MAX_PATH_CHARS else path[:_MAX_PATH_CHARS - 3] + "..."


def _child_path(path: str, part: str | int) -> str:
    if path.endswith("..."):
        return path
    if isinstance(part, int):
        suffix = f"[{part}]"
    elif part.isascii() and part.isidentifier():
        suffix = "." + part
    else:
        # JSON escaping makes quotes, controls and Unicode safe in diagnostics.
        suffix = "[" + json.dumps(part, ensure_ascii=True) + "]"
    return _bounded_path(path + suffix)


def diagnostic(path: str, code: str, message: str) -> Diagnostic:
    return Diagnostic(path=_bounded_path(path), code=code[:80], message=message[:240])


def diagnostics(error: Exception, prefix: str = "$") -> list[Diagnostic]:
    if isinstance(error, ValidationError):
        result = []
        for item in error.errors(include_input=False, include_url=False)[:MAX_ERRORS]:
            path = prefix
            for part in item["loc"]:
                path = _child_path(path, part)
            result.append(diagnostic(path, item["type"], item["msg"]))
        return result
    if isinstance(error, ContractError):
        return [diagnostic(prefix + error.path[1:], error.code, str(error))]
    return [diagnostic(prefix, "invalid_input", "Input could not be validated.")]


def _pairs(pairs: list[tuple[str, Any]]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ContractError("duplicate_key", "Duplicate JSON object keys are forbidden.")
        result[key] = value
    return result


def _nonfinite(value: str) -> None:
    raise ContractError("non_finite", "Non-finite numbers are forbidden.")


def _nonfinite_path(raw: str, max_chars: int) -> str:
    """Recover a constant's location only if strict parsing and bounded walking allow it."""
    try:
        decoded = json.loads(raw, object_pairs_hook=_pairs, parse_constant=lambda _: _NONFINITE)
        bounded_object(decoded, max_chars=max_chars)
    except ContractError as error:
        if error.code == "non_finite":
            return error.path
    except (ValueError, RecursionError):
        pass
    # Keep the original parser error when another failure prevents localization.
    return "$"


def _encoding_error_path(value: dict, *, utf8: bool) -> str:
    """Locate an observed encoding failure in an already structurally bounded tree."""
    def visit(item: Any, path: str) -> str | None:
        if type(item) is dict:
            # Follow canonical_json's key order and distinguish its conversion
            # failures from the subsequent whole-string UTF-8 encoding step.
            for key in sorted(item):
                child_path = _child_path(path, key)
                if utf8:
                    failure = visit(key, child_path)
                    if failure is not None:
                        return failure
                failure = visit(item[key], child_path)
                if failure is not None:
                    return failure
        elif type(item) is list:
            for index, child in enumerate(item):
                failure = visit(child, _child_path(path, index))
                if failure is not None:
                    return failure
        else:
            try:
                if utf8 and type(item) is str:
                    item.encode("utf-8")
                elif not utf8 and type(item) is int:
                    str(item)
            except (ValueError, UnicodeError):
                return path
        return None

    return visit(value, "$") or "$"


def bounded_object(value: Any, *, max_chars: int, allow_json: bool = False) -> dict:
    """Validate a JSON tree before serialization/models; cap recursion and work."""
    if isinstance(value, str) and allow_json:
        if len(value) > max_chars:
            raise ContractError("payload_too_large", f"Payload exceeds {max_chars} characters.")
        try:
            value = json.loads(value, object_pairs_hook=_pairs, parse_constant=_nonfinite)
        except (json.JSONDecodeError, RecursionError, ValueError) as error:
            if isinstance(error, ContractError):
                if error.code == "non_finite":
                    error.path = _nonfinite_path(value, max_chars)
                raise
            raise ContractError("invalid_json", "Expected exactly one strict JSON object.") from None
    if type(value) is not dict:
        raise ContractError("object_required", "Expected a JSON object.")
    nodes = 0
    char_count = 0

    def visit(item: Any, depth: int, path: str) -> None:
        nonlocal nodes, char_count
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ContractError("structure_too_large", "JSON exceeds the node or nesting limit.", path)
        if type(item) is dict:
            if len(item) > 100:
                raise ContractError("object_too_large", "JSON objects may have at most 100 keys.", path)
            for key, child in item.items():
                if type(key) is not str or len(key) > 128:
                    raise ContractError("invalid_key", "JSON keys must be strings up to 128 characters.", path)
                child_path = _child_path(path, key)
                char_count += len(key)
                visit(child, depth + 1, child_path)
        elif type(item) is list:
            if len(item) > 100:
                raise ContractError("array_too_large", "JSON arrays may have at most 100 items.", path)
            for index, child in enumerate(item):
                visit(child, depth + 1, _child_path(path, index))
        elif type(item) is str:
            char_count += len(item)
        elif item is None or type(item) in (bool, int):
            pass
        elif type(item) is float and math.isfinite(item):
            pass
        elif item is _NONFINITE:
            raise ContractError("non_finite", "Non-finite numbers are forbidden.", path)
        else:
            raise ContractError("invalid_json_value", "Expected finite JSON values only.", path)
        if char_count > max_chars:
            raise ContractError("payload_too_large", f"Payload exceeds {max_chars} characters.", path)

    visit(value, 0, "$")
    try:
        encoded = canonical_json(value)
        if len(encoded) > max_chars:
            raise ContractError("payload_too_large", f"Payload exceeds {max_chars} characters.")
        encoded.encode("utf-8")
    except (ValueError, UnicodeError) as error:
        if isinstance(error, ContractError):
            raise
        path = _encoding_error_path(value, utf8=isinstance(error, UnicodeError))
        raise ContractError("invalid_json_value", "JSON cannot be canonically encoded as UTF-8.",
                            path) from None
    return value


def validate_result(contract: Any, result: Any) -> ValidationResponse:
    """The parent must supply its retained contract, never a worker replacement."""
    try:
        source = bounded_object(contract, max_chars=64000)
        expected = TaskContract.model_validate(source)
        if contract_fingerprint(source) != expected.contract_revision:
            raise ContractError("contract_revision_mismatch", "Contract fingerprint does not match.",
                                "$.contract_revision")
    except (ContractError, ValidationError) as error:
        return ValidationResponse(valid=False, errors=diagnostics(error, "$.contract"))
    try:
        value = bounded_object(result, max_chars=MAX_RESULT_CHARS, allow_json=True)
        report = RESULT_MODELS[expected.result_type].model_validate(value)
    except (ContractError, ValidationError) as error:
        return ValidationResponse(valid=False, errors=diagnostics(error, "$.result"))

    errors = []

    def reject(path: str, code: str, message: str) -> None:
        if len(errors) < MAX_ERRORS:
            errors.append(diagnostic(path, code, message))

    for field in ("protocol", "task_id", "attempt_id", "bundle_revision", "contract_revision"):
        if getattr(report, field) != getattr(expected, field):
            reject(f"$.result.{field}", "identity_mismatch", "Does not match the retained contract.")
    if report.status == "completed" and (report.remaining_work or report.question is not None):
        reject("$.result.status", "completed_incomplete", "Completed requires no remaining work or question.")
    if report.status == "input_required" and report.question is None:
        reject("$.result.question", "question_required", "Input required needs a concrete question.")
    if report.status == "partial" and not report.remaining_work:
        reject("$.result.remaining_work", "remaining_work_required", "Partial needs remaining work.")
    if report.status == "failed" and not report.limitations:
        reject("$.result.limitations", "failure_reason_required", "Failed needs a limitation explaining failure.")
    known = set()
    for index, evidence in enumerate(report.evidence):
        if evidence.id in known:
            reject(f"$.result.evidence[{index}].id", "duplicate_evidence_id", "Evidence IDs must be unique.")
        known.add(evidence.id)
    references = [(f"$.result.checks[{i}].evidence_ids", check.evidence_ids)
                  for i, check in enumerate(report.checks)]
    for field in ("findings", "conclusions"):
        references.extend((f"$.result.result.{field}[{i}].evidence_ids", item.evidence_ids)
                          for i, item in enumerate(getattr(report.result, field, [])))
    for path, ids in references:
        if len(ids) != len(set(ids)):
            reject(path, "duplicate_evidence_reference", "Evidence references must be unique.")
        if any(evidence_id not in known for evidence_id in ids):
            reject(path, "unknown_evidence_id", "Evidence reference does not identify an evidence item.")
    return ValidationResponse(valid=not errors, errors=errors)
