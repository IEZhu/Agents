"""compare.py summarises verdicts per run, per case group and across runs."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[1] / "evals" / "ablation"
_spec = importlib.util.spec_from_file_location("ablation_compare", HARNESS / "compare.py")
compare = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(compare)


def _verdict(winner: str) -> dict:
    return {"winner": winner, "margin": "small", "rubric": [{"item": 1, "A": "met", "B": "met"}],
            "reasons": "r", "factual_errors": {"A": [], "B": []}}


def _run(root: Path, name: str, outcomes: dict[str, tuple[str, str]], missing: tuple[str, ...] = ()) -> Path:
    """A judged run: outcomes maps case id -> winning arm ("with"/"without"/"tie") in orders o1, o2."""
    run = root / name
    (run / "cases").mkdir(parents=True)
    (run / "judge").mkdir()
    groups = {"r1": "reasoning", "r2": "reasoning", "k1": "control"}
    cases = [{"id": c, "group": groups[c], "user_message": "q", "rubric": ["a"]} for c in outcomes]
    (run / "cases" / "rule-x.json").write_text(json.dumps({"component": "rule-x", "cases": cases}))
    plan = {}
    for case, orders in outcomes.items():
        for i, winner_arm in enumerate(orders, 1):
            a, b = ("with", "without") if i == 1 else ("without", "with")
            stem = f"{case}__o{i}"
            plan[stem] = {"component": "rule-x", "case": case, "A": a, "B": b}
            if stem in missing:
                continue
            letter = "tie" if winner_arm == "tie" else ("A" if a == winner_arm else "B")
            (run / "judge" / f"{stem}.verdict.json").write_text(json.dumps(_verdict(letter)))
    (run / "judge_plan.json").write_text(json.dumps(plan))
    return run


def test_sign_test_is_exact_and_two_sided():
    assert compare.sign_test(2, 10) == pytest.approx(158 / 4096)
    assert compare.sign_test(5, 5) == 1.0
    assert compare.sign_test(0, 0) == 1.0


def test_a_run_counts_verdicts_robust_cases_and_missing_ones(tmp_path):
    run = _run(tmp_path, "r", {"r1": ("with", "with"), "r2": ("without", "tie"), "k1": ("tie", "without")},
               missing=("k1__o2",))
    result = compare.compare([run])
    summary = result["runs"][0]
    assert summary["missing"] == 1
    assert summary["all"] == {"cases": 3, "with": 2, "without": 1, "tie": 2, "net": 1,
                              "robust_with": 1, "robust_without": 0}
    assert summary["groups"]["reasoning"]["net"] == 1
    assert result["groups"] == ["reasoning", "control"]  # controls last


def test_cases_of_different_components_with_one_id_stay_apart(tmp_path):
    run = tmp_path / "r"
    (run / "cases").mkdir(parents=True)
    (run / "judge").mkdir()
    plan = {}
    for component, group, winner in (("rule-x", "reasoning", "A"), ("rule-y", "control", "B")):
        cases = [{"id": "c1", "group": group, "user_message": "q", "rubric": ["a"]}]
        (run / "cases" / f"{component}.json").write_text(json.dumps({"component": component, "cases": cases}))
        stem = f"{component}__c1__o1"
        plan[stem] = {"component": component, "case": "c1", "A": "with", "B": "without"}
        (run / "judge" / f"{stem}.verdict.json").write_text(json.dumps(_verdict(winner)))
    (run / "judge_plan.json").write_text(json.dumps(plan))
    result = compare.compare([run])
    assert result["runs"][0]["all"]["cases"] == 2
    assert result["runs"][0]["groups"]["control"]["without"] == 1
    across = result["across"]
    assert (across["change applies"]["better"], across["controls"]["worse"]) == (1, 1)


def test_a_verdict_that_skips_a_rubric_item_counts_as_missing(tmp_path):
    run = _run(tmp_path, "r", {"r1": ("with", "with")})
    cases = [{"id": "r1", "group": "reasoning", "user_message": "q", "rubric": ["a", "b"]}]
    (run / "cases" / "rule-x.json").write_text(json.dumps({"component": "rule-x", "cases": cases}))
    summary = compare.compare([run])["runs"][0]
    assert summary["missing"] == 2 and summary["all"]["cases"] == 0  # both verdicts judge item 1 only


def test_across_runs_scores_cases_and_keeps_controls_apart(tmp_path):
    first = _run(tmp_path, "one", {"r1": ("without", "without"), "r2": ("with", "tie"), "k1": ("with", "with")})
    second = _run(tmp_path, "two", {"r1": ("without", "tie"), "r2": ("with", "with"), "k1": ("without", "tie")})
    across = compare.compare([first, second])["across"]
    assert across["change applies"] == {"cases": 2, "better": 1, "worse": 1, "even": 0, "score": 0, "p": 1.0}
    assert across["controls"]["score"] == 1
    assert "| change applies | 2 | 1 | 1 | 0 | +0 | 1.000 |" in compare.to_markdown(compare.compare([first, second]))
