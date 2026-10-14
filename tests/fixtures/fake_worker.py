"""Shared by the fake ``claude`` and ``codex`` (#345): a ``subagent/1`` review result for a worker_context.

`build(text, outcome)` reads the assignment contract from the worker_context (the line after
``## Assignment contract``), copies its identity fields and names the reviewed range it finds in
the contract in an evidence item. ``outcome`` changes that: ``status`` (``partial``, ``failed``
and ``input_required`` get the fields `validate_result` needs), ``range`` (another range for the
evidence), ``findings`` (``[{"severity", "location", "explanation", "recommendation"?}]``),
``summary``, ``override`` (top-level fields replaced as given, e.g. a wrong ``attempt_id``).
"""
import json
import re

CONTRACT = "## Assignment contract\n"
RANGE = re.compile(r"[0-9a-f]{40}\.\.[0-9a-f]{40}")


def contract_of(text):
    start = text.index(CONTRACT) + len(CONTRACT)
    return json.loads(text[start:].split("\n", 1)[0])


def build(text, outcome):
    contract = contract_of(text)
    found = RANGE.search(json.dumps(contract))
    reviewed = outcome.get("range") or (found.group(0) if found else "no range")
    status = outcome.get("status", "completed")
    findings = [{"severity": item.get("severity", "medium"), "location": item["location"],
                 "explanation": item["explanation"], "recommendation": item.get("recommendation", "Fix it."),
                 "evidence_ids": ["ev-range"]} for item in outcome.get("findings", [])]
    result = {
        "protocol": "agents-core/subagent/1", "task_id": contract["task_id"], "attempt_id": contract["attempt_id"],
        "bundle_revision": contract["bundle_revision"], "contract_revision": contract["contract_revision"],
        "status": status,
        "evidence": [{"id": "ev-range", "reference": reviewed, "description": "the reviewed range"}],
        "artifacts": [], "checks": [{"name": "read the diff", "status": "passed", "evidence_ids": ["ev-range"]}],
        "limitations": ["could not finish"] if status == "failed" else [],
        "remaining_work": ["more to check"] if status == "partial" else [],
        "question": "Which branch should be reviewed?" if status == "input_required" else None,
        "result": {"summary": outcome.get("summary", "Reviewed the range."), "findings": findings},
    }
    result.update(outcome.get("override", {}))
    return result
