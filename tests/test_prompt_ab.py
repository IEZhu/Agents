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


def test_implant_arms_bracket_the_implants_with_two_noise_floors():
    arms = pab.implant_arms("HEAD", ["CoV", "StepBack"])
    assert [(a.label, a.implants, a.reverse) for a in arms] == [
        ("none", "none", False), ("none_repeat", "none", False), ("CoV", "CoV", False),
        ("StepBack", "StepBack", False), ("production", "production", False),
        ("none_reversed", "none", True)]


def test_reversed_arm_is_answered_last_and_backwards(tmp_path):
    cases = [{"id": f"c{i}", "query": f"q{i}"} for i in range(3)]
    arms = [pab.Arm("none", "H", implants="none"), pab.Arm("none_reversed", "H", implants="none", reverse=True)]
    prompts = {a.label: {c["id"]: {"system_prompt": "base"} for c in cases} for a in arms}
    order: list[str] = []

    async def complete(client, model, query, system_prompt, max_tokens, sample=True):
        order.append(query)
        return "a", {}, 0

    provider = SimpleNamespace(name="local", complete=complete)
    asyncio.run(pab.answer_all(cases, arms, prompts, provider, None, "m", 1, tmp_path / "a.jsonl"))
    assert order == ["q0", "q1", "q2", "q2", "q1", "q0"]


def _manifest(**over):
    base = {"mode": "implants", "provider": "local", "model": "gemma", "judge_model": "qwen", "temperature": "0",
            "samples": 1, "dataset_sha256": "d", "agents_sha256": "a",
            "arms": [{"label": "none", "rev": "HEAD", "env": {}, "implants": "none", "reverse": False, "sha": "s1"}]}
    return {**base, **over}


def test_manifest_refuses_other_settings_but_accepts_added_arms(tmp_path):
    path = tmp_path / "manifest.json"
    pab.check_manifest(path, _manifest())
    with pytest.raises(SystemExit, match="model"):
        pab.check_manifest(path, _manifest(model="qwen3.8"))
    moved = _manifest()
    moved["arms"][0]["sha"] = "s2"
    with pytest.raises(SystemExit, match="arm 'none' changed"):
        pab.check_manifest(path, moved)
    added = _manifest()
    added["arms"].append({"label": "none_reversed", "rev": "HEAD", "env": {}, "implants": "none", "reverse": True, "sha": "s1"})
    pab.check_manifest(path, added)
    assert [a["label"] for a in pab.read_json(path)["arms"]] == ["none", "none_reversed"]
    # A run from before --max-tokens used the default budget, and resumes only with it.
    pab.check_manifest(path, {**added, "max_tokens": pab.MAX_TOKENS})
    with pytest.raises(SystemExit, match="max_tokens"):
        pab.check_manifest(path, {**added, "max_tokens": 1400})
    # A manifest without the embedding model defers to the prompt files (build_prompts);
    # once recorded, a different model is refused.
    pab.check_manifest(path, {**added, "embedding_model": pab.DEFAULT_EMBEDDING_MODEL})
    with pytest.raises(SystemExit, match="embedding_model"):
        pab.check_manifest(path, {**added, "embedding_model": "sentence-transformers/all-MiniLM-L6-v2"})


def test_manifest_pins_request_settings_and_refuses_runs_that_never_recorded_them(tmp_path):
    path = tmp_path / "manifest.json"
    settings = {"reasoning": "low", "seed": "none", "grader_temperature": "0"}
    pab.check_manifest(path, _manifest())
    # Settings of a run recorded before they were pinned cannot be established.
    with pytest.raises(SystemExit, match="fully recorded"):
        pab.check_manifest(path, {**_manifest(), "request_settings": settings})
    fresh = tmp_path / "fresh" / "manifest.json"
    fresh.parent.mkdir()
    pab.check_manifest(fresh, {**_manifest(), "request_settings": settings})
    with pytest.raises(SystemExit, match="request_settings"):
        pab.check_manifest(fresh, {**_manifest(), "request_settings": {**settings, "grader_temperature": "default"}})


def test_cached_prompts_built_with_another_embedding_model_are_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("EMBEDDING_MODEL", "intfloat/multilingual-e5-large")
    out = tmp_path / "prompts_none.json"
    arm = pab.Arm("none", "HEAD", implants="none")
    for recorded in ("sentence-transformers/all-MiniLM-L6-v2", None):
        pab.write_json_atomic(out, {"embedding_model": recorded, "prompts": {}})
        with pytest.raises(SystemExit, match="embedding model"):
            asyncio.run(pab.build_prompts(tmp_path, tmp_path, tmp_path, out, arm))
    pab.write_json_atomic(out, {"embedding_model": "intfloat/multilingual-e5-large", "prompts": {"c1": {}}})
    assert asyncio.run(pab.build_prompts(tmp_path, tmp_path, tmp_path, out, arm)) == {"c1": {}}


def test_manifest_refuses_reordered_arms_but_accepts_arms_added_in_between(tmp_path):
    path = tmp_path / "manifest.json"

    def arm(label):
        return {"label": label, "rev": "HEAD", "env": {}, "implants": "production", "reverse": False, "sha": "s1"}

    base = {"mode": "revisions", "provider": "local", "model": "m", "judge_model": "j", "temperature": "0",
            "samples": 1, "dataset_sha256": "d", "agents_sha256": None}
    pab.check_manifest(path, {**base, "arms": [arm("old"), arm("new")]})
    # Reports compare every arm with the first: swapping them would change the baseline.
    with pytest.raises(SystemExit, match="reordered"):
        pab.check_manifest(path, {**base, "arms": [arm("new"), arm("old")]})
    pab.check_manifest(path, {**base, "arms": [arm("old"), arm("gate"), arm("new")]})
    # The requested order is stored, so the same command resumes.
    assert [a["label"] for a in pab.read_json(path)["arms"]] == ["old", "gate", "new"]
    pab.check_manifest(path, {**base, "arms": [arm("old"), arm("gate"), arm("new")]})
    # Dropping an arm already run could change the baseline silently.
    with pytest.raises(SystemExit, match="removed or reordered"):
        pab.check_manifest(path, {**base, "arms": [arm("gate"), arm("new")]})


def test_manifest_pins_the_builder_config(tmp_path):
    path = tmp_path / "manifest.json"
    pab.check_manifest(path, {**_manifest(), "builder_config": {"RULES_ENABLED": "0"}})
    with pytest.raises(SystemExit, match="builder_config"):
        pab.check_manifest(path, {**_manifest(), "builder_config": {}})
    legacy = tmp_path / "legacy" / "manifest.json"
    legacy.parent.mkdir()
    pab.check_manifest(legacy, _manifest())
    with pytest.raises(SystemExit, match="fully recorded"):
        pab.check_manifest(legacy, {**_manifest(), "builder_config": {}})


def test_builder_config_records_prompt_settings_the_revision_reads(monkeypatch):
    monkeypatch.setenv("RULES_ENABLED", "0")
    monkeypatch.setenv("IMPLANT_NEED_GATE", "intent")
    monkeypatch.setenv("EMBEDDING_MODEL", "m")
    monkeypatch.setenv("AGENTS_AUTO_UPDATE", "1")
    config = pab.builder_config()
    assert config["RULES_ENABLED"] == "0" and config["IMPLANT_NEED_GATE"] == "intent"
    assert "EMBEDDING_MODEL" not in config and "AGENTS_AUTO_UPDATE" not in config


def test_samples_must_be_positive():
    with pytest.raises(SystemExit):
        pab.parse_args(["implants", "--out-dir", "x", "--samples", "0"])


def test_unreadable_state_files_count_as_missing(tmp_path):
    broken = tmp_path / "catalog.json"
    broken.write_text("")
    assert pab.read_json(broken) is None and pab.read_json(tmp_path / "absent.json") is None
    pab.write_json_atomic(broken, {"ok": 1})
    assert pab.read_json(broken) == {"ok": 1} and not (tmp_path / "catalog.json.tmp").exists()


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


def test_run_end_to_end_with_a_stub_builder(tmp_path, monkeypatch):
    """Orchestration only: manifest, relative paths, async builds, answers, grades, report."""
    stub = tmp_path / "stub_builder.py"
    stub.write_text(
        "import json, os, sys\n"
        "a = dict(zip(sys.argv[1::2], sys.argv[2::2]))\n"
        "rows = [json.loads(l) for l in open(a['--dataset']) if l.strip()]\n"
        "extra = '' if a['--implants'] == 'none' else '+' + a['--implants']\n"
        "json.dump({'embedding_model': os.environ.get('EMBEDDING_MODEL'),"
        " 'prompts': {r['id']: {'system_prompt': 'base' + extra, 'meta': {}} for r in rows}},"
        " open(a['--out'], 'w'))\n")
    monkeypatch.setattr(pab, "BUILDER", stub)
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text("\n".join(json.dumps({"id": f"c{i}", "category": "fabrication-recall", "query": f"q{i}",
                                             "reference": "r", "rubric": "r", "checks": {"must_not_contain": []}})
                                 for i in range(2)))
    agents = tmp_path / "agents.json"
    agents.write_text(json.dumps({"c0": "universal_agent", "c1": "universal_agent"}))

    async def complete(client, model, query, system_prompt, max_tokens, sample=True):
        return f"{query} under {system_prompt}", {}, 0

    async def grade(provider, client, judge, case, answer):
        return [], ("PASS" if "+CoV" in answer else "FAIL"), "stub"

    provider = SimpleNamespace(name="local", default_model="m", default_judge_model="j", complete=complete,
                               make_async_client=lambda: None, env_key="")
    monkeypatch.setattr("evals.runners._providers.get_provider", lambda name: provider)
    monkeypatch.setattr(pab.cr, "grade_sample", grade)
    monkeypatch.setattr("evals.scripts.local_ab.unload", lambda base, model: None)
    monkeypatch.delenv("LOCAL_LLM_BASE_URL", raising=False)
    monkeypatch.setenv("LOCAL_LLM_TEMPERATURE", "0")
    monkeypatch.chdir(tmp_path)
    args = pab.parse_args(["implants", "--out-dir", "out", "--dataset", "cases.jsonl", "--agents", "agents.json",
                           "--implants", "CoV"])
    assert asyncio.run(pab.run(args)) == 0
    summary = json.loads((tmp_path / "out/summary.json").read_text())
    assert summary["fails"]["CoV"] == [] and summary["fails"]["none"] == ["c0", "c1"]
    assert [a["label"] for a in json.loads((tmp_path / "out/manifest.json").read_text())["arms"]] == [
        "none", "none_repeat", "CoV", "production", "none_reversed"]
    assert "| CoV |" in (tmp_path / "out/report.md").read_text()


def test_bounded_caps_concurrency_and_the_first_failure_cancels_the_rest():
    running, peak, finished = 0, 0, []

    def job(i, fail=False):
        async def run():
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.01 if not fail else 0)
            running -= 1
            if fail:
                raise RuntimeError("boom")
            finished.append(i)
        return run

    asyncio.run(pab.bounded([job(i) for i in range(7)], 3))
    assert peak == 3 and sorted(finished) == list(range(7))
    finished.clear()
    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(pab.bounded([job(0, fail=True)] + [job(i) for i in range(1, 7)], 2))
    assert len(finished) < 6


def test_concurrent_answers_keep_twin_reuse_across_arms(tmp_path):
    cases = [{"id": f"c{i}", "query": f"q{i}"} for i in range(5)]
    arms = [pab.Arm("none", "H", implants="none"), pab.Arm("CoV", "H", implants="CoV"),
            pab.Arm("production", "H")]
    prompts = {"none": {c["id"]: {"system_prompt": "base"} for c in cases},
               "CoV": {c["id"]: {"system_prompt": "base+cov"} for c in cases},
               "production": {c["id"]: {"system_prompt": "base+cov"} for c in cases}}
    calls: list[str] = []
    path = tmp_path / "answers.jsonl"
    asyncio.run(pab.answer_all(cases, arms, prompts, _provider(calls), None, "m", 1, path, concurrency=4))
    assert sorted(calls) == ["base"] * 5 + ["base+cov"] * 5
    records = pab.read_jsonl(path)
    assert len(records) == 15
    assert {r["reused_from"] for r in records if r["arm"] == "production"} == {"CoV"}


def _run_args(tmp_path, *extra):
    dataset = tmp_path / "cases.jsonl"
    dataset.write_text(json.dumps({"id": "c0", "category": "fabrication-recall", "query": "q", "reference": "r",
                                   "rubric": "r", "checks": {"must_not_contain": []}}))
    return pab.parse_args(["implants", "--out-dir", str(tmp_path / "out"), "--dataset", str(dataset), *extra])


@pytest.mark.parametrize("name, env, extra, message", [
    ("local", {}, ["--concurrency", "2"], "hosted providers"),
    ("local", {}, ["--samples", "2"], "LOCAL_LLM_TEMPERATURE > 0"),
    ("openrouter", {"OPENROUTER_TEMPERATURE": "0.7"}, [], "OPENROUTER_TEMPERATURE=0"),
    ("openrouter", {"OPENROUTER_API_KEY": None}, [], "needs OPENROUTER_API_KEY"),
])
def test_run_refuses_settings_that_would_spoil_the_measurement(tmp_path, monkeypatch, name, env, extra, message):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("LOCAL_LLM_TEMPERATURE", "0")
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    with pytest.raises(SystemExit, match=message):
        asyncio.run(pab.run(_run_args(tmp_path, "--provider", name, *extra)))


def test_hosted_implant_runs_may_repeat_samples_and_pin_the_answer_budget(tmp_path, monkeypatch):
    """Hosted models vary at temperature 0, so repeated samples measure failure rates."""
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_TEMPERATURE", "default")
    stub = tmp_path / "stub_builder.py"
    stub.write_text(
        "import json, os, sys\n"
        "a = dict(zip(sys.argv[1::2], sys.argv[2::2]))\n"
        "rows = [json.loads(l) for l in open(a['--dataset']) if l.strip()]\n"
        "json.dump({'embedding_model': os.environ.get('EMBEDDING_MODEL'),"
        " 'prompts': {r['id']: {'system_prompt': 'p' + a['--implants'], 'meta': {}} for r in rows}},"
        " open(a['--out'], 'w'))\n")
    monkeypatch.setattr(pab, "BUILDER", stub)
    budgets = []

    async def complete(client, model, query, system_prompt, max_tokens, sample=True):
        budgets.append(max_tokens)
        return "answer", {}, 0

    async def grade(provider, client, judge, case, answer):
        return [], "PASS", "stub"

    provider = SimpleNamespace(name="openrouter", default_model="m", default_judge_model="j", complete=complete,
                               make_async_client=lambda: None, env_key="OPENROUTER_API_KEY")
    monkeypatch.setattr("evals.runners._providers.get_provider", lambda name: provider)
    monkeypatch.setattr(pab.cr, "grade_sample", grade)
    checkouts = []
    real_get = pab.Worktrees.get
    monkeypatch.setattr(pab.Worktrees, "get", lambda self, rev: checkouts.append(rev) or real_get(self, rev))
    agents = tmp_path / "agents.json"
    agents.write_text(json.dumps({"c0": "universal_agent"}))
    args = _run_args(tmp_path, "--provider", "openrouter", "--samples", "2", "--max-tokens", "1400",
                     "--implants", "CoV", "--rev", "HEAD", "--agents", str(agents))
    assert asyncio.run(pab.run(args)) == 0
    assert set(budgets) == {1400} and len(budgets) == 5 * 2
    manifest = json.loads((tmp_path / "out/manifest.json").read_text())
    assert manifest["max_tokens"] == 1400 and manifest["samples"] == 2 and manifest["temperature"] == "default"
    # Prompts are built from the commit the manifest records, not from a name that can move.
    assert checkouts and set(checkouts) == {a["sha"] for a in manifest["arms"]}
    assert all(len(rev) == 40 for rev in checkouts)


def test_every_case_needs_an_agent_before_prompts_are_built(tmp_path, monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_TEMPERATURE", "0")
    provider = SimpleNamespace(name="local", default_model="m", default_judge_model="j", env_key="",
                               make_async_client=lambda: None)
    monkeypatch.setattr("evals.runners._providers.get_provider", lambda name: provider)
    monkeypatch.setattr("evals.scripts.local_ab.unload", lambda base, model: None)
    agents = tmp_path / "agents.json"
    agents.write_text(json.dumps({"other": "universal_agent"}))
    with pytest.raises(SystemExit, match="no agent for 1 cases"):
        asyncio.run(pab.run(_run_args(tmp_path, "--agents", str(agents))))

