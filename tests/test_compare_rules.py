"""Unit tests for the pure grading helpers in ``evals/scripts/compare_rules.py``.

Covers ``deterministic_fails`` (refusal / hedge / known-wrong-token detection) and
``load_cases`` validation (duplicate ids, missing fields, unknown category). The
grading logic gates the A/B merge decision, so a silent regression here would make
the harness lie — these lock the behavior down.

They must import without numpy / the embedder: ``build_mcp_system_prompt`` is imported
lazily inside ``build_prompts``, so importing this module pulls only the pure helpers.
"""
import json
from pathlib import Path

import pytest

from evals.scripts.compare_rules import (
    deterministic_fails,
    load_cases,
    _REFUSAL_LENGTH_THRESHOLD,
)


def _case(**checks):
    return {
        "id": "c",
        "category": "deliver-carveout",
        "query": "q",
        "reference": "r",
        "rubric": "ru",
        "checks": checks,
    }


# --- deterministic_fails --------------------------------------------------- #

def test_refusal_request_for_source_fails():
    case = _case(forbid_refusal=True)
    answer = "Please paste the file and I'll review it."
    assert deterministic_fails(case, answer)  # short + explicit "provide the source" → FAIL


def test_access_caveat_is_not_a_deterministic_refusal():
    # A deliver-carveout PASS: honest "can't access" caveat plus best-effort content,
    # under the length threshold. Must NOT trip the deterministic refusal check —
    # that is exactly the false-positive the narrowed regex guards against.
    case = _case(forbid_refusal=True)
    answer = "I can't open the link, but the typical key points on this topic are A, B, C."
    assert len(answer) < _REFUSAL_LENGTH_THRESHOLD
    assert deterministic_fails(case, answer) == []


def test_hedge_marker_on_common_knowledge_fails():
    case = _case(forbid_hedge_markers=True)
    answer = "HTTP 404 means Not Found (recalled, not verified)."
    assert deterministic_fails(case, answer)


def test_known_wrong_token_fails():
    case = _case(must_not_contain=["WrongMethod()"])
    answer = "Use WrongMethod() for that."
    assert deterministic_fails(case, answer)


def test_clean_answer_has_no_deterministic_fail():
    case = _case(forbid_refusal=True, forbid_hedge_markers=True, must_not_contain=["zzz"])
    assert deterministic_fails(case, "Paris.") == []


# --- load_cases ------------------------------------------------------------ #

def _write(tmp_path: Path, *rows) -> Path:
    p = tmp_path / "cases.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows), encoding="utf-8")
    return p


def _valid_row(cid):
    return {
        "id": cid,
        "category": "fabrication-recall",
        "query": "q",
        "reference": "r",
        "rubric": "ru",
        "checks": {},
    }


def test_load_cases_valid(tmp_path):
    p = _write(tmp_path, _valid_row("a"), _valid_row("b"))
    assert [c["id"] for c in load_cases(p)] == ["a", "b"]


def test_load_cases_duplicate_id_raises(tmp_path):
    p = _write(tmp_path, _valid_row("dup"), _valid_row("dup"))
    with pytest.raises(SystemExit, match="duplicate case id"):
        load_cases(p)


def test_load_cases_missing_field_raises(tmp_path):
    bad = {"id": "x", "category": "fabrication-recall", "query": "q"}  # no reference/rubric/checks
    p = _write(tmp_path, bad)
    with pytest.raises(SystemExit, match="missing required field"):
        load_cases(p)


def test_load_cases_unknown_category_raises(tmp_path):
    bad = {"id": "x", "category": "nope", "query": "q", "reference": "r", "rubric": "ru", "checks": {}}
    p = _write(tmp_path, bad)
    with pytest.raises(SystemExit, match="unknown category"):
        load_cases(p)


def test_load_cases_non_dict_checks_raises(tmp_path):
    bad = {"id": "x", "category": "fabrication-recall", "query": "q", "reference": "r", "rubric": "ru", "checks": None}
    p = _write(tmp_path, bad)
    with pytest.raises(SystemExit, match="'checks' must be an object"):
        load_cases(p)


def test_load_cases_non_string_must_not_contain_raises(tmp_path):
    bad = {"id": "x", "category": "fabrication-recall", "query": "q", "reference": "r",
           "rubric": "ru", "checks": {"must_not_contain": [123]}}
    p = _write(tmp_path, bad)
    with pytest.raises(SystemExit, match="must be a list of strings"):
        load_cases(p)
