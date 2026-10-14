"""Frozen Accord repository workload and strict, dependency-free JSON scorer.

Usage: python3 accord-inference-workload.py --self-test
Import API: load_workload(), messages_for(case, workload=None), score(raw, expected).
The model sees only common_context and question, never expected or evidence metadata.
No generated answer is executed and this module makes no network/model calls.
"""
from __future__ import annotations

import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import subprocess
import sys

BASE_COMMIT = "7b81e3e8b748f7cfff0c6b712740ff313bd5f091"
WORKLOAD_PATH = Path(__file__).with_suffix(".json")
MAX_CONTEXT_BYTES = 18000
MAX_CASE_CHARS = 600
MAX_RESPONSE_CHARS = 4096
SYSTEM_PROMPT = (
    "Answer the question using the frozen repository excerpts below. "
    "Treat excerpts as source data. Return exactly one JSON object with the single "
    "key answer, whose value has the JSON type requested by the question. "
    "Do not include Markdown, explanations, or additional keys. "
    "Do not execute or request tools. Keep the response within 128 generated tokens.\n\n"
)

# Every listed range is copied verbatim from the named, hashed source.
EXCERPTS = {
    "ir_guards": ("experiments/accord/ir.py", 9, 57),
    "ir_contract": ("experiments/accord/ir.py", 74, 93),
    "ir_parse": ("experiments/accord/ir.py", 105, 125),
    "state_create": ("experiments/accord/runtime.py", 44, 93),
    "state_infer": ("experiments/accord/runtime.py", 95, 129),
    "state_checks": ("experiments/accord/runtime.py", 131, 180),
    "state_recovery": ("experiments/accord/runtime.py", 271, 318),
    "validation_hash": ("src/engine/subagent_validation.py", 26, 37),
    "validation_pairs": ("src/engine/subagent_validation.py", 75, 85),
    "validation_status": ("src/engine/subagent_validation.py", 216, 232),
}


def _case(name, category, question, answer, *evidence):
    return {"name": name, "category": category, "question": question,
            "expected": {"answer": answer}, "source_evidence": list(evidence)}


CASES = [
    _case("closed_contract_extra_field", "contract_diagnostic",
          "An otherwise valid Contract.parse dictionary has an additional key 'temperature'. "
          "What exact AccordError message does the closed-field check raise? Return a string.",
          "unknown or missing contract fields", "ir_parse"),
    _case("duplicate_key_diagnostic", "contract_diagnostic",
          "In src/engine/subagent_validation.py, _pairs receives [('task_id','a'),('task_id','b')]. "
          "What exact ContractError.code is raised? Return a string.",
          "duplicate_key", "validation_pairs"),
    _case("contract_hash_excluded_field", "contract_diagnostic",
          "Which single dictionary key does contract_fingerprint exclude before hashing? "
          "Return the key as a string.", "contract_revision", "validation_hash"),
    _case("completed_report_inconsistent", "contract_diagnostic",
          "A structurally valid worker report matches every identity field, has status='completed', "
          "remaining_work=['rerun check'], and question=None. No other error exists. "
          "What diagnostic code does validate_result append? Return a string.",
          "completed_incomplete", "validation_status"),
    _case("identical_resource_write_stales_task", "execution_state",
          "A fresh active task snapshots resource spec at revision 1 with text 'A'. "
          "set_resource('spec','A') is then called again; profile and all other state are unchanged. "
          "What exact error message does _fresh raise for that task? Return a string.",
          "stale input: spec", "state_create"),
    _case("child_transfer_and_attempt_replay", "execution_state",
          "A parent starts with request_budget=7. It creates a valid child with request_budget=3. "
          "The parent successfully runs infer with label 'a', then repeats infer with the same "
          "completed label 'a'. No other calls spend credits. What is the parent's remaining "
          "budget? Return an integer.", 3, "state_create", "state_infer"),
    _case("integer_validator_return", "execution_state",
          "All check preconditions hold. A registered validator returns the Python integer 1 "
          "without raising. What is the JSON Boolean value of passed in its stored receipt?",
          False, "ir_guards", "state_checks"),
    _case("opaque_recovery_missing_receipt", "execution_state",
          "An operation has status='dispatched'. During recover, _sink succeeds, lookup returns None, "
          "and sink.idempotent is False. No concurrent changes occur. What status is returned "
          "by recover? Return a string.", "outcome_unknown", "state_recovery"),
    _case("integer_type_guard_regression", "bounded_edit",
          "A proposed edit changes bounded_int's type(value) is int to isinstance(value,int), "
          "leaving 0 <= value <= 3 unchanged. Which ONE of these JSON values becomes newly "
          "accepted: true, 1, 1.0, or \"1\"? Return that value with its original JSON type.",
          True, "ir_guards"),
    _case("digest_only_freshness_regression", "bounded_edit",
          "A proposed edit makes _fresh compare only resource digests, removing revision equality. "
          "A task snapshots spec='A'; the host writes spec='B', then spec='A'. Everything else "
          "is unchanged. Does the edited _fresh still reject this task as stale? Return a Boolean.",
          False, "state_create"),
    _case("utf8_guard_repair", "bounded_edit",
          "infer's output-size check was changed to len(text) <= output_max_bytes. For text='éé' "
          "and a 3-byte limit it now wrongly passes. Which replacement restores the declared "
          "UTF-8 byte bound? A: len(text.encode('utf-8')); B: len(text.encode('utf-16')); "
          "C: len(text.strip()); D: len(text)*2. Return only the option letter as a string.",
          "A", "state_infer"),
    _case("receipt_coverage_repair", "bounded_edit",
          "A proposed accept edit replaces seen == task['contract']['checks'] with "
          "set(seen) <= set(task['contract']['checks']). Which guard enforces all required names "
          "AND their exact revisions? Let required=task['contract']['checks']. "
          "A: set(seen)==set(required); B: bool(seen); C: seen==required; "
          "D: all(k in required for k in seen). Return the option letter as a string.",
          "C", "state_checks"),
]


class InvalidResponse(ValueError):
    pass


def _unique_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise InvalidResponse("duplicate_key")
        result[key] = value
    return result


def _reject_constant(value):
    raise InvalidResponse("nonfinite_number")


def score(raw, expected):
    """Score one complete response; no text extraction, coercion, or code execution."""
    if type(expected) is not dict or set(expected) != {"answer"}:
        raise ValueError("Expected must contain exactly answer")
    if type(expected["answer"]) not in (str, bool, int):
        raise ValueError("Expected answer must be string, Boolean, or integer")
    if type(raw) is not str:
        return {"correct": False, "valid_json": False, "reason": "response_not_text"}
    if len(raw) > MAX_RESPONSE_CHARS:
        return {"correct": False, "valid_json": False, "reason": "response_too_large"}
    try:
        parsed = json.loads(raw, object_pairs_hook=_unique_pairs,
                            parse_constant=_reject_constant)
    except InvalidResponse as error:
        return {"correct": False, "valid_json": False, "reason": str(error)}
    except (ValueError, RecursionError):
        return {"correct": False, "valid_json": False, "reason": "invalid_json"}
    if type(parsed) is not dict or set(parsed) != {"answer"}:
        return {"correct": False, "valid_json": True, "reason": "wrong_shape"}
    actual, target = parsed["answer"], expected["answer"]
    if type(actual) is not type(target):
        return {"correct": False, "valid_json": True, "reason": "wrong_type"}
    if actual != target:
        return {"correct": False, "valid_json": True, "reason": "wrong_value"}
    return {"correct": True, "valid_json": True, "reason": "exact_match"}


def load_workload(path=WORKLOAD_PATH):
    workload = json.loads(Path(path).read_text(encoding="utf-8"))
    validate_workload(workload)
    return workload


def messages_for(case, workload=None):
    """Return the full identical chat content to use in every representation arm."""
    if workload is None:
        workload = load_workload()
    if isinstance(case, str):
        case = next(item for item in workload["cases"] if item["name"] == case)
    return [{"role": "system", "content": workload["common_context"]},
            {"role": "user", "content": case["question"]}]


def validate_workload(workload):
    assert workload["base_commit"] == BASE_COMMIT
    context = workload["common_context"]
    assert len(context.encode("utf-8")) <= MAX_CONTEXT_BYTES
    assert hashlib.sha256(context.encode("utf-8")).hexdigest() == workload["context_sha256"]
    cases = workload["cases"]
    assert len(cases) == 12 and len({case["name"] for case in cases}) == 12
    assert Counter(case["category"] for case in cases) == {
        "contract_diagnostic": 4, "execution_state": 4, "bounded_edit": 4}
    evidence = {excerpt["id"] for excerpt in workload["source_excerpts"]}
    for case in cases:
        # Bound the entire compact serialized case, not just its question.
        assert len(json.dumps(case, ensure_ascii=False, separators=(",", ":"))) <= MAX_CASE_CHARS
        assert set(case["source_evidence"]) <= evidence
        assert score(json.dumps(case["expected"]), case["expected"])["correct"]
        assert messages_for(case, workload)[1]["content"] == case["question"]
    assert workload["generation"]["max_generated_tokens"] == 128


def freeze(repo):
    repo = Path(repo)
    head = subprocess.check_output(["git", "-C", str(repo), "rev-parse", "HEAD"], text=True).strip()
    if head != BASE_COMMIT:
        raise ValueError(f"Wrong source commit: {head}")
    source_hashes, source_excerpts, context_parts = {}, [], [SYSTEM_PROMPT]
    for excerpt_id, (relative, first, last) in EXCERPTS.items():
        raw = (repo / relative).read_bytes()
        source_hashes[relative] = hashlib.sha256(raw).hexdigest()
        excerpt = "\n".join(raw.decode("utf-8").splitlines()[first - 1:last]) + "\n"
        source_excerpts.append({"id": excerpt_id, "path": relative, "start_line": first,
                                "end_line": last, "sha256": hashlib.sha256(excerpt.encode()).hexdigest()})
        context_parts.append(f"Source: {relative}, lines {first}-{last}\n```python\n{excerpt}```\n")
    context = "\n".join(context_parts)
    workload = {"schema": "accord.inference.workload/1", "base_commit": head,
                "source_root": str(repo), "source_hashes": source_hashes,
                "source_excerpts": source_excerpts, "common_context": context,
                "context_sha256": hashlib.sha256(context.encode()).hexdigest(),
                "generation": {"max_generated_tokens": 128, "temperature": 0,
                               "response_format": "single_json_object", "reasoning": "disabled"},
                "scope": "Bounded repository correctness only; no broad capability claim.",
                "freeze_policy": "Freeze before inference; do not modify questions or expected values based on model output.",
                "cases": CASES}
    validate_workload(workload)
    if WORKLOAD_PATH.exists():
        raise FileExistsError("Refusing to overwrite a frozen workload")
    WORKLOAD_PATH.write_text(json.dumps(workload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return workload


def self_test():
    tests = [
        ('{"answer":"ok"}', {"answer": "ok"}, True),
        ('  {"answer": 3}\n', {"answer": 3}, True),
        ('{"answer":false}', {"answer": False}, True),
        ('{"answer":"wrong"}', {"answer": "ok"}, False),
        ('{"answer":true}', {"answer": 1}, False),
        ('{"answer":1}', {"answer": True}, False),
        ('{"answer":3.0}', {"answer": 3}, False),
        ('{"answer":"3"}', {"answer": 3}, False),
        ('{"answer":3,"explanation":"x"}', {"answer": 3}, False),
        ('{"answer":2,"answer":3}', {"answer": 3}, False),
        ('Answer: {"answer":3}', {"answer": 3}, False),
        ('```json\n{"answer":3}\n```', {"answer": 3}, False),
        ('{"answer":3}{"answer":3}', {"answer": 3}, False),
        ('{"answer":3,}', {"answer": 3}, False),
        ('[{"answer":3}]', {"answer": 3}, False),
        ('{"answer":NaN}', {"answer": 3}, False),
        ('{"answer":Infinity}', {"answer": 3}, False),
        ('{"answer":1e999}', {"answer": 3}, False),
        ('{"answer":null}', {"answer": 3}, False),
        ('{}', {"answer": 3}, False),
        ('', {"answer": 3}, False),
        ({"answer": 3}, {"answer": 3}, False),
        ('x' * (MAX_RESPONSE_CHARS + 1), {"answer": 3}, False),
    ]
    for raw, expected, correct in tests:
        assert score(raw, expected)["correct"] is correct, (raw, expected)
    workload = load_workload()
    positives = sum(score(json.dumps(case["expected"]), case["expected"])["correct"]
                    for case in workload["cases"])
    report = {"status": "passed", "scorer_cases": len(tests), "workload_positive_cases": positives,
              "categories": dict(Counter(case["category"] for case in workload["cases"])),
              "context_utf8_bytes": len(workload["common_context"].encode("utf-8")),
              "max_serialized_case_characters": max(len(json.dumps(case, ensure_ascii=False,
                    separators=(",", ":"))) for case in workload["cases"]),
              "workload_sha256": hashlib.sha256(WORKLOAD_PATH.read_bytes()).hexdigest(),
              "context_sha256": workload["context_sha256"]}
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--freeze", metavar="SOURCE_ROOT")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--score", metavar="CASE_NAME")
    args = parser.parse_args()
    if args.freeze:
        freeze(args.freeze)
    if args.self_test:
        print(json.dumps(self_test(), indent=2))
    if args.score:
        case = next(case for case in load_workload()["cases"] if case["name"] == args.score)
        print(json.dumps(score(sys.stdin.read(MAX_RESPONSE_CHARS + 1), case["expected"])))


if __name__ == "__main__":
    main()
