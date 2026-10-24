"""Bounded structural validation. References are data and are never dereferenced."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any

from pydantic import ValidationError

from src.engine import model_tiers
from src.engine.subagent_text import TextFormatError, is_text_result, parse_result
from src.schemas.subagent import (ContractExecution, Diagnostic, Escalation, MAX_ERRORS,
                                  MAX_JSON_DEPTH, MAX_JSON_NODES, MAX_RESULT_CHARS, RESULT_MODELS,
                                  TaskContract, ValidationResponse)


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


def diagnostic(path: str, code: str, message: str) -> Diagnostic:
    return Diagnostic(path=path[:256], code=code[:80], message=message[:240])


def diagnostics(error: Exception, prefix: str = "$") -> list[Diagnostic]:
    if isinstance(error, ValidationError):
        return [diagnostic(prefix + "".join(f"[{part}]" if isinstance(part, int) else f".{part}"
                                           for part in item["loc"]), item["type"], item["msg"])
                for item in error.errors(include_input=False, include_url=False)[:MAX_ERRORS]]
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


def bounded_object(value: Any, *, max_chars: int, allow_json: bool = False) -> dict:
    """Validate a JSON tree before serialization/models; cap recursion and work."""
    if isinstance(value, str) and allow_json:
        if len(value) > max_chars:
            raise ContractError("payload_too_large", f"Payload exceeds {max_chars} characters.")
        try:
            value = json.loads(value, object_pairs_hook=_pairs, parse_constant=_nonfinite)
        except (json.JSONDecodeError, RecursionError, ValueError) as error:
            if isinstance(error, ContractError):
                raise
            raise ContractError("invalid_json", "Expected exactly one strict JSON object.") from None
    if type(value) is not dict:
        raise ContractError("object_required", "Expected a JSON object.")
    nodes = 0
    char_count = 0

    def visit(item: Any, depth: int) -> None:
        nonlocal nodes, char_count
        nodes += 1
        if nodes > MAX_JSON_NODES or depth > MAX_JSON_DEPTH:
            raise ContractError("structure_too_large", "JSON exceeds the node or nesting limit.")
        if type(item) is dict:
            if len(item) > 100:
                raise ContractError("object_too_large", "JSON objects may have at most 100 keys.")
            for key, child in item.items():
                if type(key) is not str or len(key) > 128:
                    raise ContractError("invalid_key", "JSON keys must be strings up to 128 characters.")
                char_count += len(key)
                visit(child, depth + 1)
        elif type(item) is list:
            if len(item) > 100:
                raise ContractError("array_too_large", "JSON arrays may have at most 100 items.")
            for child in item:
                visit(child, depth + 1)
        elif type(item) is str:
            char_count += len(item)
        elif item is None or type(item) in (bool, int):
            pass
        elif type(item) is float and math.isfinite(item):
            pass
        else:
            raise ContractError("invalid_json_value", "Expected finite JSON values only.")
        if char_count > max_chars:
            raise ContractError("payload_too_large", f"Payload exceeds {max_chars} characters.")

    visit(value, 0)
    try:
        encoded = canonical_json(value)
        if len(encoded) > max_chars:
            raise ContractError("payload_too_large", f"Payload exceeds {max_chars} characters.")
        encoded.encode("utf-8")
    except (ValueError, UnicodeError) as error:
        if isinstance(error, ContractError):
            raise
        raise ContractError("invalid_json_value", "JSON cannot be canonically encoded as UTF-8.") from None
    return value


@dataclass(frozen=True)
class Outcome:
    """What a validation of a tiered contract observed, for the tier ledger."""
    task_id: str
    attempt_id: str
    execution: ContractExecution
    valid: bool
    status: str | None
    failed_checks: int
    escalation: Escalation | None


def validate_result(contract: Any, result: Any) -> ValidationResponse:
    """The parent must supply its retained contract, never a worker replacement."""
    return assess_result(contract, result)[0]


def assess_result(contract: Any, result: Any) -> tuple[ValidationResponse, Outcome | None]:
    """Validate like `validate_result`; also return the observed outcome of a tiered contract."""
    try:
        source = bounded_object(contract, max_chars=64000)
        expected = TaskContract.model_validate(source)
        if contract_fingerprint(source) != expected.contract_revision:
            raise ContractError("contract_revision_mismatch", "Contract fingerprint does not match.",
                                "$.contract_revision")
    except (ContractError, ValidationError) as error:
        return ValidationResponse(valid=False, errors=diagnostics(error, "$.contract")), None

    digest = None

    def finish(errors: list, status: str | None = None, failed_checks: int = 0):
        valid = not errors
        if expected.execution is None:
            return ValidationResponse(valid=valid, errors=errors, result_sha256=digest), None
        escalation = model_tiers.escalation(expected.execution, valid=valid, status=status,
                                            failed_checks=failed_checks)
        outcome = Outcome(expected.task_id, expected.attempt_id, expected.execution, valid, status,
                          failed_checks, escalation)
        return ValidationResponse(valid=valid, errors=errors, escalation=escalation,
                                  result_sha256=digest), outcome

    try:
        if is_text_result(result):
            if len(result) > MAX_RESULT_CHARS:
                raise ContractError("payload_too_large", f"Payload exceeds {MAX_RESULT_CHARS} characters.")
            digest = hashlib.sha256(result.encode("utf-8")).hexdigest()
            try:
                value = parse_result(result, expected)
            except TextFormatError as error:
                raise ContractError(error.code, str(error), "$" + error.path[len("$.result"):]) from None
            value = bounded_object(value, max_chars=MAX_RESULT_CHARS)
        else:
            value = bounded_object(result, max_chars=MAX_RESULT_CHARS, allow_json=True)
            digest = hashlib.sha256((result if isinstance(result, str) else canonical_json(value))
                                    .encode("utf-8")).hexdigest()
        report = RESULT_MODELS[expected.result_type].model_validate(value)
    except (ContractError, ValidationError) as error:
        return finish(diagnostics(error, "$.result"))

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
    return finish(errors, report.status, sum(check.status == "failed" for check in report.checks))
