"""prompt_ab: arm parsing, resumable answering with reuse, aggregation, reports."""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from evals.scripts import _prompt_builder as builder
from evals.scripts import local_ab as la
from evals.scripts import prompt_ab as pab


def test_parse_arm_reads_label_revision_and_flags():
    arm = pab.parse_arm("gate=HEAD:IMPLANT_NEED_GATE=intent,IMPLANT_GATING=zscore")
    assert (arm.label, arm.rev, arm.implants) == ("gate", "HEAD", "production")
    assert arm.env == {"IMPLANT_NEED_GATE": "intent", "IMPLANT_GATING": "zscore"}
    assert pab.parse_arm("old=3a4fc5f").env == {}
    for bad in ("3a4fc5f", "=HEAD", "x=", "x=HEAD:FLAG"):
        with pytest.raises(ValueError):
            pab.parse_arm(bad)


def test_implant_arms_start_with_the_noise_floor_and_end_with_production():
    arms = pab.implant_arms("HEAD", ["CoV", "StepBack"])
    assert [(a.label, a.implants) for a in arms] == [
        ("none", "none"), ("none_repeat", "none"), ("CoV", "CoV"), ("StepBack", "StepBack"),
        ("production", "production")]


def test_mcnemar_counts_both_directions():
    base = {"a": True, "b": True, "c": False, "d": False}
    other = {"a": False, "b": True, "c": True, "d": False}
    assert pab.mcnemar(base, other, "abcd") == {"fixed": 1, "broken": 1, "p_exact": 1.0}
    assert pab.mcnemar(base, base, "abcd") == {"fixed": 0, "broken": 0, "p_exact": 1.0}
    assert pab.mcnemar({i: True for i in "abcdef"}, {}, "abcdef")["p_exact"] == round(2 / 64, 4)


def _provider(calls):
    async def complete(client, model, query, system_prompt, max_tokens, sample=True):
        calls.append(system_prompt)
        return f"answer to {query} under {system_prompt}", {}, 0
    return SimpleNamespace(name="local", complete=complete)


def test_answers_reuse_identical_prompts_but_regenerate_the_noise_floor(tmp_path):
    cases = [{"id": "c1", "query": "q1"}, {"id": "c2", "query": "q2"}]
    arms = [pab.Arm("none", "H", implants="none"), pab.Arm("none_repeat", "H", implants="none"),
            pab.Arm("CoV", "H", implants="CoV"), pab.Arm("production", "H")]
    prompts = {"none": {"c1": {"system_prompt": "base"}, "c2": {"system_prompt": "base"}},
               "none_repeat": {"c1": {"system_prompt": "base"}, "c2": {"system_prompt": "base"}},
               "CoV": {"c1": {"system_prompt": "base+cov"}, "c2": {"system_prompt": "base+cov"}},
               "production": {"c1": {"system_prompt": "base+cov"}, "c2": {"system_prompt": "base+prod"}}}
    calls: list[str] = []
    path = tmp_path / "answers.jsonl"
    asyncio.run(pab.answer_all(cases, arms, prompts, _provider(calls), None, "m", 1, path))
    # none and none_repeat both generate; production c1 reuses CoV's identical prompt.
    assert calls == ["base", "base", "base", "base", "base+cov", "base+cov", "base+prod"]
    records = pab.read_jsonl(path)
    assert [r["reused_from"] for r in records if r["arm"] == "production"] == ["CoV", None]
    # A rerun resumes: nothing is answered twice.
    calls.clear()
    asyncio.run(pab.answer_all(cases, arms, prompts, _provider(calls), None, "m", 1, path))
    assert calls == [] and len(pab.read_jsonl(path)) == 8


def test_grades_resume_and_a_case_fails_if_any_sample_fails(tmp_path, monkeypatch):
    cases = [{"id": "c1", "category": "fabrication-recall", "query": "q", "reference": "r", "rubric": "r",
              "checks": {"must_not_contain": []}}]
    answers = tmp_path / "answers.jsonl"
    for i, text in enumerate(["good", "bad"]):
        pab.append_jsonl(answers, {"arm": "none", "id": "c1", "sample": i, "answer": text})
    graded = []

    async def fake_grade(provider, client, judge, case, answer):
        graded.append(answer)
        return [], ("FAIL" if answer == "bad" else "PASS"), answer

    monkeypatch.setattr(pab.cr, "grade_sample", fake_grade)
    grades = tmp_path / "grades.jsonl"
    asyncio.run(pab.grade_all(cases, answers, grades, None, None, "j"))
    asyncio.run(pab.grade_all(cases, answers, grades, None, None, "j"))
    assert graded == ["good", "bad"]
    results = pab.arm_results([pab.Arm("none", "H")], pab.read_jsonl(grades))
    assert results["none"].per_case == {"c1": True} and results["none"].reasons == {"c1": "bad"}


def test_implant_report_counts_changed_answers_against_none():
    cases = [{"id": "c1", "category": "fab"}, {"id": "c2", "category": "fab"}]
    arms = [pab.Arm("none", "H", implants="none"), pab.Arm("CoV", "H", implants="CoV")]
    prompts = {"none": {"c1": {"system_prompt": "p"}, "c2": {"system_prompt": "p"}},
               "CoV": {"c1": {"system_prompt": "p+12345"}, "c2": {"system_prompt": "p+12345"}}}
    answers = [{"arm": "none", "id": "c1", "sample": 0, "answer": "x"},
               {"arm": "none", "id": "c2", "sample": 0, "answer": "y"},
               {"arm": "CoV", "id": "c1", "sample": 0, "answer": "x"},
               {"arm": "CoV", "id": "c2", "sample": 0, "answer": "y (not verified)"}]
    results = {"none": pab.cr.ArmResult("none", per_case={"c1": False, "c2": True}),
               "CoV": pab.cr.ArmResult("CoV", per_case={"c1": False, "c2": False})}
    table = pab.implant_table(cases, arms, prompts, answers, results)
    row = next(line for line in table.splitlines() if line.startswith("| CoV"))
    assert "| +6 | 1/2 | 0/2 | 1 / 0 (1.0) | 1 | 0 |" in row


def test_local_ab_runs_prompt_ab_under_its_server():
    args = la.parse_args(["--", "prompt_ab", "implants", "--out-dir", "/abs/out"])
    assert la.ab_command(args)[1:] == ["-m", "evals.scripts.prompt_ab", "implants", "--out-dir", "/abs/out"]
    with pytest.raises(SystemExit):
        la.parse_args(["--", "rm", "-rf", "/"])


def test_builder_records_match_the_preferred_implant_fast_path():
    metas = [{"filename": "implant-chain-of-verification.mdc", "short_name": "CoV", "body": "COV BODY",
              "description": "d"},
             {"filename": "implant-step-back-prompting.mdc", "short_name": "StepBack", "body": "SB", "description": "d"}]

    class Store:
        def get_all_metadatas(self):
            return metas

        def get(self, ids):
            chosen = [m for i in ids for m in metas if m["filename"] == i]
            return SimpleNamespace(ids=[m["filename"] for m in chosen], metadatas=chosen,
                                   documents=["index text"] * len(chosen))

    retriever = SimpleNamespace(store=Store())
    records = builder.implant_records(retriever, ["StepBack", "implant-chain-of-verification"])
    assert [(r["filename"], r["content"]) for r in records] == [
        ("implant-step-back-prompting.mdc", "SB"), ("implant-chain-of-verification.mdc", "COV BODY")]
    with pytest.raises(SystemExit, match="unknown implants"):
        builder.implant_records(retriever, ["Nope"])
