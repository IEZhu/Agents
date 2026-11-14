"""Bounded structural validation. References are data and are never dereferenced."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from typing import Any

from pydantic import ValidationError

from src.engine import model_tiers
from src.engine.subagent_text import (TextFormatError, dropped_positions, excerpt,
                                      is_text_result, parse_result)
from src.schemas.subagent import (ContractExecution, Diagnostic, Escalation, MAX_ERRORS,
                                  MAX_JSON_DEPTH, MAX_JSON_NODES, MAX_RESULT_CHARS, RESULT_MODELS,
                                  TaskContract, Truncation, ValidationResponse)

# The identity the header carries, checked against the retained contract in every path.
IDENTITY_FIELDS = ("protocol", "task_id", "attempt_id", "bundle_revision", "contract_revision")
# The diagnostics that decide what the parent does next — hand the result back, prepare a new
# attempt, or ask the worker to reformat lines — rather than which line to correct. They are
# reported before the per-line ones so the cap drops a format error instead of one of these (#589).
DECIDING_PATHS = frozenset([f"$.result.{field}" for field in IDENTITY_FIELDS] + ["$.result.status"])
DECIDING_CODES = frozenset({"identity_mismatch", "completed_incomplete", "question_required",
                            "remaining_work_required", "failure_reason_required"})


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


def diagnostics(error: Exception, prefix: str = "$",
                limit: int | None = MAX_ERRORS) -> list[Diagnostic]:
    """Every diagnostic one error carries, capped at `limit`.

    A caller that reports the produced count passes `limit=None` and caps through `capped`
    instead; the list stays bounded by the already bounded size of the input it describes.
    """
    if isinstance(error, ValidationError):
        return [diagnostic(prefix + "".join(f"[{part}]" if isinstance(part, int) else f".{part}"
                                           for part in item["loc"]), item["type"], item["msg"])
                for item in error.errors(include_input=False, include_url=False)[:limit]]
    if isinstance(error, ContractError):
        return [diagnostic(prefix + error.path[1:], error.code, str(error))]
    return [diagnostic(prefix, "invalid_input", "Input could not be validated.")]


def decides_next_action(reported: Diagnostic) -> bool:
    """Whether this diagnostic is an identity or a status one, which outrank the per-line ones."""
    return reported.path in DECIDING_PATHS or reported.code in DECIDING_CODES


def capped(reported: list[Diagnostic]) -> tuple[list[Diagnostic], Truncation | None]:
    """The diagnostics a response carries, identity and status first, and what the cap dropped.

    `sorted` is stable, so everything else keeps the order it was produced in. The marker is
    returned only when the cap actually dropped something, so a response at or under the cap
    carries none and a valid result is unchanged (#589).
    """
    ordered = sorted(reported, key=lambda item: not decides_next_action(item))
    produced = len(ordered)
    return ordered[:MAX_ERRORS], Truncation(produced=produced) if produced > MAX_ERRORS else None


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


def _source_position(position: int, dropped: list[int]) -> int:
    """The position a parsed entry had before the collected errors shortened its list."""
    for index in sorted(dropped):
        if index <= position:
            position += 1
    return position


def _with_line(message: str, line: str) -> str:
    """The diagnostic's own message, followed by the worker's line wherever one is known.

    A rejection over one line of a long result is unusable without it: the parent would see
    only a path into the parsed model and could not tell which line to correct (#526).
    """
    return f"{message} Line: {line}" if line else message


def _source_line(sources: dict[str, list[str]], path: str, position: int) -> str:
    """The line one list position was parsed from, where the parse recorded it."""
    lines = sources.get(path, [])
    return excerpt(lines[position]) if position < len(lines) else ""


def _schema_diagnostics(error: Exception, positions: dict[str, list[int]],
                        sources: dict[str, list[str]]) -> list[Diagnostic]:
    """`diagnostics` with every list position translated back to the line that holds it,
    and that line quoted, so a schema or size error names it too.

    Uncapped: `capped` orders and cuts the collected list once, and counts what it dropped.
    """
    if not isinstance(error, ValidationError):
        return diagnostics(error, "$.result")
    reported = []
    for item in error.errors(include_input=False, include_url=False):
        path, line = "$.result", ""
        for part in item["loc"]:
            if isinstance(part, int):
                position = _source_position(part, positions.get(path, []))
                line = _source_line(sources, path, position)
                path += f"[{position}]"
            else:
                path += f".{part}"
        reported.append(diagnostic(path, item["type"], _with_line(item["msg"], line)))
    return reported


def partial_diagnostics(value: dict, text_errors: list[TextFormatError],
                        expected: TaskContract,
                        sources: dict[str, list[str]]) -> list[Diagnostic]:
    """Everything one validation can say when some lines of a text result did not parse.

    The identity reads the header alone, so it is reported now instead of in the round after
    the format fixes. The status invariants and evidence references are not: they would only
    describe the lines that were skipped (#526).
    """
    reported = [diagnostic(f"$.result.{field}", "identity_mismatch",
                           "Does not match the retained contract.")
                for field in IDENTITY_FIELDS
                if value.get(field) != getattr(expected, field)]
    reported += [diagnostic(error.path, error.code, _with_line(str(error), error.line))
                 for error in text_errors]
    try:
        RESULT_MODELS[expected.result_type].model_validate(
            bounded_object(value, max_chars=MAX_RESULT_CHARS))
    except (ContractError, ValidationError) as error:
        reported += _schema_diagnostics(error, dropped_positions(text_errors), sources)
    return reported


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
        errors, truncated = capped(diagnostics(error, "$.contract", limit=None))
        return ValidationResponse(valid=False, errors=errors, truncated=truncated), None

    digest = None
    sources: dict[str, list[str]] = {}

    def finish(reported: list, status: str | None = None, failed_checks: int = 0):
        errors, truncated = capped(reported)
        valid = not errors
        if expected.execution is None:
            return ValidationResponse(valid=valid, errors=errors, result_sha256=digest,
                                      truncated=truncated), None
        escalation = model_tiers.escalation(expected.execution, valid=valid, status=status,
                                            failed_checks=failed_checks)
        outcome = Outcome(expected.task_id, expected.attempt_id, expected.execution, valid, status,
                          failed_checks, escalation)
        return ValidationResponse(valid=valid, errors=errors, escalation=escalation,
                                  result_sha256=digest, truncated=truncated), outcome

    try:
        if is_text_result(result):
            if len(result) > MAX_RESULT_CHARS:
                raise ContractError("payload_too_large", f"Payload exceeds {MAX_RESULT_CHARS} characters.")
            try:
                digest = hashlib.sha256(result.encode("utf-8")).hexdigest()
            except UnicodeEncodeError:
                raise ContractError("invalid_text", "The result is not valid UTF-8 text.") from None
            try:
                value, text_errors, sources = parse_result(result, expected)
            except TextFormatError as error:
                raise ContractError(error.code, _with_line(str(error), error.line),
                                    "$" + error.path[len("$.result"):]) from None
            if text_errors:
                return finish(partial_diagnostics(value, text_errors, expected, sources))
            value = bounded_object(value, max_chars=MAX_RESULT_CHARS)
        else:
            value = bounded_object(result, max_chars=MAX_RESULT_CHARS, allow_json=True)
            digest = hashlib.sha256((result if isinstance(result, str) else canonical_json(value))
                                    .encode("utf-8")).hexdigest()
        report = RESULT_MODELS[expected.result_type].model_validate(value)
    except (ContractError, ValidationError) as error:
        return finish(_schema_diagnostics(error, {}, sources))

    errors = []

    def reject(path: str, code: str, message: str) -> None:
        """Collect without capping: `finish` orders the list, cuts it and counts the rest."""
        errors.append(diagnostic(path, code, message))

    for field in IDENTITY_FIELDS:
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
