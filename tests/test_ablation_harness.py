"""The ablation harness refuses gaps instead of reporting a partial run as complete.

``evals/ablation`` is a script directory, not a package, so the modules are loaded
from their paths. Nothing here loads the embedding model: ``build_contexts`` is only
driven down the paths that stop or return before its heavy imports.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[1] / "evals" / "ablation"


def _module(name: str):
    spec = importlib.util.spec_from_file_location(f"ablation_{name}", HARNESS / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


aggregate = _module("aggregate")
build_judges = _module("build_judges")
build_contexts = _module("build_contexts")

VERDICT = {"winner": "B", "margin": "small",
           "rubric": [{"item": 1, "A": "met", "B": "met"}, {"item": 2, "A": "partial", "B": "met"}],
           "reasons": "B covers item 2.", "factual_errors": {"A": [], "B": []}}


def _write(path: Path, data) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path


def _judged_run(tmp_path: Path) -> Path:
    """One component, one case with a two-item rubric, judged in both orders."""
    run = tmp_path / "run"
    _write(run / "cases" / "skill-x.json",
           {"component": "skill-x", "cases": [{"id": "c1", "user_message": "q", "rubric": ["a", "b"]}]})
    _write(run / "judge_plan.json", {"s__o1": {"component": "skill-x", "case": "c1", "A": "with", "B": "without"},
                                     "s__o2": {"component": "skill-x", "case": "c1", "A": "without", "B": "with"}})
    for stem in ("s__o1", "s__o2"):
        _write(run / "judge" / f"{stem}.verdict.json", VERDICT)
    return run


@pytest.mark.parametrize("change", [
    {"margin": None},
    {"winner": "C"},
    {"rubric": []},
    {"rubric": [{"item": 1, "A": "met", "B": "maybe"}]},
    {"rubric": [{"item": True, "A": "met", "B": "met"}]},
    {"reasons": 3},
    {"factual_errors": {"A": [1], "B": []}},
    {"factual_errors": {"A": []}},
    {"factual_errors": []},
    {"factual_errors": ""},
])
def test_a_malformed_verdict_is_rejected(tmp_path, change):
    path = _write(tmp_path / "v.json", {**VERDICT, **change})
    with pytest.raises(ValueError):
        aggregate.read_verdict(path)


def test_a_verdict_that_is_not_an_object_is_rejected_and_null_errors_mean_none(tmp_path):
    with pytest.raises(ValueError):
        aggregate.read_verdict(_write(tmp_path / "v.json", []))
    for absent in ({**VERDICT, "factual_errors": None}, {k: v for k, v in VERDICT.items() if k != "factual_errors"}):
        _v, errors = aggregate.read_verdict(_write(tmp_path / "v.json", absent))
        assert errors == {"A": [], "B": []}


def test_a_complete_run_aggregates_and_exits_zero(tmp_path, capsys):
    run = _judged_run(tmp_path)
    assert aggregate.main([run]) == 0
    result = json.loads((run / "results.json").read_text())
    assert len(result["verdicts"]) == 2 and result["missing"] == []


@pytest.mark.parametrize("gap", ["verdict", "rubric", "unknown case", "skipped", "build"])
def test_any_gap_is_listed_as_missing_and_fails_unless_partial_is_allowed(tmp_path, capsys, gap):
    run = _judged_run(tmp_path)
    if gap == "verdict":
        (run / "judge" / "s__o2.verdict.json").unlink()
    elif gap == "rubric":  # only one of the case's two rubric items is graded
        _write(run / "judge" / "s__o2.verdict.json", {**VERDICT, "rubric": VERDICT["rubric"][:1]})
    elif gap == "unknown case":  # the verdict stays, the case it was judged on is gone
        _write(run / "cases" / "skill-x.json", {"component": "skill-x", "cases": []})
    elif gap == "skipped":
        _write(run / "judge_skipped.json", ["skill-x/c2"])
    else:
        _write(run / "build_errors.json", [{"component": "skill-x", "case": "c3", "error": "arms identical"}])
    assert aggregate.main([run]) == 1
    assert len(json.loads((run / "results.json").read_text())["missing"]) == (2 if gap == "unknown case" else 1)
    assert aggregate.main([run], allow_partial=True) == 0


def test_build_judges_records_a_pair_with_a_missing_answer(tmp_path, capsys):
    run = tmp_path / "run"
    _write(run / "cases" / "skill-x.json", {"component": "skill-x", "cases": [
        {"id": c, "user_message": "q", "rubric": ["a"]} for c in ("c1", "c2")]})
    plan = {f"{c}-{arm}": {"component": "skill-x", "case": c, "arm": arm}
            for c in ("c1", "c2") for arm in ("with", "without")}
    _write(run / "plan.json", plan)
    for token in plan:
        (run / "answers").mkdir(exist_ok=True)
        (run / "answers" / f"{token}.md").write_text("answer")
    assert build_judges.main(run) == 0
    assert len(json.loads((run / "judge_plan.json").read_text())) == 4
    (run / "answers" / "c2-without.md").unlink()
    assert build_judges.main(run) == 1
    assert json.loads((run / "judge_skipped.json").read_text()) == ["skill-x/c2"]
    assert len(json.loads((run / "judge_plan.json").read_text())) == 2
    assert build_judges.main(run, allow_partial=True) == 0


@pytest.fixture
def snapshot(tmp_path, monkeypatch):
    """A repository root whose components.json lists one present and one removed skill."""
    root = tmp_path / "repo"
    (root / "skills").mkdir(parents=True)
    (root / "skills" / "skill-kept.mdc").write_text("---\n---\n")
    _write(root / "evals" / "ablation" / "components.json", [
        {"id": "skill-kept", "file": "skills/skill-kept.mdc"},
        {"id": "skill-gone", "file": "skills/skill-gone.mdc"}])
    monkeypatch.setattr(build_contexts, "ROOT", root)
    return root


def test_build_contexts_refuses_a_run_with_no_cases(tmp_path, snapshot):
    (tmp_path / "run" / "cases").mkdir(parents=True)
    with pytest.raises(SystemExit, match="no case files"):
        asyncio.run(build_contexts.main(tmp_path / "run"))


def test_build_contexts_refuses_a_listed_component_without_cases(tmp_path, snapshot):
    run = tmp_path / "run"
    (run / "cases").mkdir(parents=True)
    (run / "ids.txt").write_text("skill-kept\nskill-gone\n")
    with pytest.raises(SystemExit, match=r"no cases file for \['skill-kept'\]"):
        asyncio.run(build_contexts.main(run))


def test_build_contexts_refuses_case_files_it_was_not_asked_to_build(tmp_path, snapshot):
    run = tmp_path / "run"
    _write(run / "cases" / "skill-kept.json", {"component": "skill-kept", "cases": []})
    (run / "ids.txt").write_text("skill-gone\n")
    with pytest.raises(SystemExit, match="not in ids.txt"):
        asyncio.run(build_contexts.main(run))


def test_build_contexts_refuses_a_case_file_naming_another_component(tmp_path, snapshot):
    run = tmp_path / "run"
    _write(run / "cases" / "skill-kept.json", {"component": "skill-gone", "cases": []})
    with pytest.raises(SystemExit, match="does not match the file name"):
        asyncio.run(build_contexts.main(run))


def test_build_contexts_refuses_a_repeated_case_id(tmp_path, snapshot):
    run = tmp_path / "run"
    case = {"id": "c1", "user_message": "q", "rubric": ["a"]}
    _write(run / "cases" / "skill-kept.json", {"component": "skill-kept", "cases": [case, {**case, "user_message": "r"}]})
    with pytest.raises(SystemExit, match="repeated case id"):
        asyncio.run(build_contexts.main(run))


def test_build_contexts_records_a_removed_component_without_loading_the_model(tmp_path, snapshot, capsys):
    run = tmp_path / "run"
    (run / "cases").mkdir(parents=True)
    (run / "ids.txt").write_text("skill-gone\n")
    asyncio.run(build_contexts.main(run))
    errors = json.loads((run / "build_errors.json").read_text())
    assert [(e["component"], e["error"].split(":")[0]) for e in errors] == [("skill-gone", "not in store")]
    assert json.loads((run / "plan.json").read_text()) == {}
    assert set(json.loads((run / "build_meta.json").read_text())) == {"commit", "dirty", "embedding_model"}
