"""hosted.py answers and judges an ablation run in the files the cloud steps write.

No network: the steps take the chat function as an argument, and these tests pass
fakes. Nothing here loads the embedding model.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest

HARNESS = Path(__file__).resolve().parents[1] / "evals" / "ablation"
_spec = importlib.util.spec_from_file_location("ablation_hosted", HARNESS / "hosted.py")
hosted = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hosted)

CASE = {"id": "c1", "agent": "software_engineer", "language": "ru",
        "history": [{"role": "user", "content": "Привет"}, {"role": "assistant", "content": "Здравствуйте"}],
        "user_message": "Почему список копится?", "rubric": ["a", "b"]}
VERDICT = {"winner": "A", "margin": "clear",
           "rubric": [{"item": 1, "A": "met", "B": "partial"}, {"item": 2, "A": "met", "B": "met"}],
           "reasons": "A explains item 1.", "factual_errors": {"A": [], "B": []}}


def _context(prompt: str, case: dict) -> str:
    # The layout build_contexts.main writes.
    return f"# Operating context loaded for this conversation\n{prompt}\n\n{hosted.build_contexts.conversation_block(case)}"


def _run(tmp_path: Path) -> Path:
    run = tmp_path / "run"
    (run / "cases").mkdir(parents=True)
    (run / "ctx").mkdir()
    (run / "cases" / "rule-x.json").write_text(json.dumps({"component": "rule-x", "cases": [CASE]}))
    plan = {"t_with": {"component": "rule-x", "case": "c1", "arm": "with"},
            "t_without": {"component": "rule-x", "case": "c1", "arm": "without"}}
    (run / "plan.json").write_text(json.dumps(plan))
    for token, prompt in (("t_with", "SYSTEM WITH"), ("t_without", "SYSTEM WITHOUT")):
        (run / "ctx" / f"{token}.md").write_text(_context(prompt, CASE), encoding="utf-8")
    return run


def test_split_context_returns_the_system_prompt_and_turns():
    system, messages = hosted.split_context(_context("## Persona\nBe precise.", CASE), CASE)
    assert system == "## Persona\nBe precise."
    assert messages == [{"role": "user", "content": "Привет"}, {"role": "assistant", "content": "Здравствуйте"},
                        {"role": "user", "content": "Почему список копится?"}]


def test_split_context_refuses_a_context_of_another_case():
    with pytest.raises(ValueError):
        hosted.split_context(_context("prompt", CASE), {**CASE, "user_message": "Другой вопрос"})


def test_answer_writes_missing_answers_and_keeps_existing(tmp_path):
    run = _run(tmp_path)
    (run / "answers").mkdir()
    (run / "answers" / "t_with.md").write_text("earlier answer\n")
    calls = []

    async def chat(messages, seed_key):
        calls.append((seed_key, messages[0]["content"], messages[-1]["content"]))
        return "Ответ"

    failed = asyncio.run(hosted.answer_run(run, chat, concurrency=2, attempts=1))
    assert failed == 0
    assert calls == [("t_without", "SYSTEM WITHOUT", "Почему список копится?")]
    assert (run / "answers" / "t_with.md").read_text() == "earlier answer\n"
    assert (run / "answers" / "t_without.md").read_text(encoding="utf-8") == "Ответ\n"


def test_judge_keeps_only_verdicts_aggregate_accepts(tmp_path):
    run = tmp_path / "run"
    (run / "cases").mkdir(parents=True)
    (run / "judge").mkdir()
    (run / "cases" / "rule-x.json").write_text(json.dumps({"component": "rule-x", "cases": [CASE]}))
    (run / "judge_plan.json").write_text(json.dumps({
        "s__o1": {"component": "rule-x", "case": "c1", "A": "with", "B": "without"},
        "s__o2": {"component": "rule-x", "case": "c1", "A": "without", "B": "with"}}))
    for stem in ("s__o1", "s__o2"):
        (run / "judge" / f"{stem}.md").write_text(f"# Case c1 {stem}\n")
    replies = {"s__o1": "```json\n" + json.dumps(VERDICT) + "\n```",
               "s__o2": json.dumps({**VERDICT, "rubric": VERDICT["rubric"][:1]})}  # misses item 2
    prompts = []

    async def chat(messages, seed_key):
        prompts.append(messages[0]["content"])
        return replies[seed_key]

    failed = asyncio.run(hosted.judge_run(run, chat, concurrency=2, attempts=2))
    assert failed == 1
    kept, _errors = hosted.aggregate.read_verdict(run / "judge" / "s__o1.verdict.json")
    assert kept == VERDICT
    assert not (run / "judge" / "s__o2.verdict.json").exists()
    assert len(prompts) == 3  # s__o2 was retried once
    assert all("<file>\n# Case c1" in p for p in prompts)


def test_parse_verdict_takes_the_first_object():
    reply = "```json\n" + json.dumps(VERDICT) + "\n```\nNote: {\"winner\": \"B\"}"
    assert hosted.parse_verdict(reply, 2) == VERDICT
    with pytest.raises(ValueError):
        hosted.parse_verdict("no verdict here", 2)


def test_judge_instructions_follow_the_cloud_judge():
    text = hosted.judge_instructions()
    js = (HARNESS / "workflows" / "judges.js").read_text(encoding="utf-8")
    for passage in ("Decide which answer serves the user better", 'Allowed values: "winner" is one of'):
        assert passage in text and passage in js
    assert "WebSearch" not in text and "Write tool" not in text


def test_a_step_with_outputs_from_other_settings_is_refused(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    hosted.record_step(run, "answer", {"model": "a"}, has_outputs=False)
    hosted.record_step(run, "answer", {"model": "a"}, has_outputs=True)
    with pytest.raises(SystemExit):
        hosted.record_step(run, "answer", {"model": "b"}, has_outputs=True)
    with pytest.raises(SystemExit):
        hosted.record_step(run, "judge", {"model": "j"}, has_outputs=True)  # verdicts of unknown origin
