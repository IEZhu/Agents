"""`local` eval provider: OpenAI-compatible local server (Ollama, LM Studio, llama.cpp, MLX).

No network: the SDK clients are replaced by fakes that record the request.
"""
from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from evals.judges.pairwise_judge import JudgeValidationError
from evals.runners import _providers as prov

SCHEMA = {"name": "submit_verdict", "input_schema": {"type": "object", "properties": {"winner": {"type": "string"}}}}


def _response(content: str, finish: str = "stop"):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=3, prompt_tokens_details=None),
    )


class _FakeCompletions:
    def __init__(self, content: str, finish: str = "stop", is_async: bool = False):
        self.content, self.finish, self.is_async, self.kwargs = content, finish, is_async, None

    def create(self, **kwargs):
        self.kwargs = kwargs
        resp = _response(self.content, self.finish)
        if self.is_async:
            async def _coro():
                return resp
            return _coro()
        return resp


def _client(content: str, finish: str = "stop", is_async: bool = False):
    completions = _FakeCompletions(content, finish, is_async)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


def test_request_uses_max_tokens_and_disables_thinking(monkeypatch):
    monkeypatch.delenv("LOCAL_LLM_THINKING", raising=False)
    monkeypatch.delenv("LOCAL_LLM_TEMPERATURE", raising=False)
    client, calls = _client("OK.", is_async=True)
    text, usage, _ = asyncio.run(prov.complete_local(client, "qwen3:8b", "hi", "sys", 50))
    assert text == "OK."
    kw = calls.kwargs
    assert kw["max_tokens"] == 50 and "max_completion_tokens" not in kw
    assert kw["reasoning_effort"] == "none"
    # Greedy decoding: a seed would change nothing, so none is sent.
    assert kw["temperature"] == 0 and "seed" not in kw
    assert kw["messages"][0] == {"role": "system", "content": "sys"}
    assert usage["input_tokens"] == 10 and usage["output_tokens"] == 3


def test_thinking_applies_to_answer_sized_calls_only(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_THINKING", "1")
    client, calls = _client("OK.", is_async=True)
    asyncio.run(prov.complete_local(client, "m", "hi", None, prov.LOCAL_THINKING_MIN_TOKENS))
    assert "reasoning_effort" not in calls.kwargs
    assert [m["role"] for m in calls.kwargs["messages"]] == ["user"]
    # A 32-token router pick or a 300-token grade would come back empty with
    # thinking on, so small budgets keep it off.
    for budget in (32, 300):
        asyncio.run(prov.complete_local(client, "m", "hi", None, budget))
        assert calls.kwargs["reasoning_effort"] == "none"


def test_sampling_sends_a_fresh_seed_per_call(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_TEMPERATURE", "0.7")
    client, calls = _client("OK.", is_async=True)
    seeds = []
    for _ in range(3):
        asyncio.run(prov.complete_local(client, "m", "hi", None, 50))
        assert calls.kwargs["temperature"] == 0.7
        seeds.append(calls.kwargs["seed"])
    assert len(set(seeds)) == 3


def test_evaluator_calls_stay_greedy_when_answers_sample(monkeypatch):
    """Graders, judges and router picks must not inherit answer sampling."""
    monkeypatch.setenv("LOCAL_LLM_TEMPERATURE", "0.7")
    client, calls = _client("VERDICT: PASS", is_async=True)
    asyncio.run(prov.complete_local(client, "m", "grade this", None, 300, sample=False))
    assert calls.kwargs["temperature"] == 0 and "seed" not in calls.kwargs
    judge_client, judge_calls = _client('{"winner": "left"}')
    prov.call_judge_local(judge_client, "q", "a", "b", "judge", "sys", 300, SCHEMA)
    assert judge_calls.kwargs["temperature"] == 0 and "seed" not in judge_calls.kwargs


def test_rule_ab_grader_asks_for_greedy_decoding():
    from evals.scripts import compare_rules as cr

    seen: dict[str, bool] = {}

    async def complete(client, model, query, system_prompt, max_tokens, sample=True):
        seen[model] = sample
        return ("VERDICT: PASS\nREASON: ok" if model == "judge" else "answer"), {}, 0

    provider = SimpleNamespace(name="local", complete=complete)
    case = {"id": "c0", "category": "fabrication-recall", "query": "q", "reference": "r", "rubric": "r",
            "checks": {"must_not_contain": []}}
    asyncio.run(cr.run_arm([case], {"c0": {"system_prompt": "sp"}}, provider, None, "model", "judge", 1, "arm"))
    assert seen == {"model": True, "judge": False}


def test_inline_think_block_is_stripped():
    client, _ = _client("<think>let me see</think>\nThe answer is 4.", is_async=True)
    text, _, _ = asyncio.run(prov.complete_local(client, "m", "2+2?", None, 50))
    assert text == "The answer is 4."


@pytest.mark.parametrize("content", ["<think>long reasoning</think>", "<think>reasoning cut off by the cap"])
def test_empty_text_at_length_is_an_error_not_an_empty_answer(content):
    client, _ = _client(content, finish="length", is_async=True)
    with pytest.raises(RuntimeError, match="max_tokens"):
        asyncio.run(prov.complete_local(client, "m", "q", None, 20))


def test_judge_parses_json_and_sends_schema():
    client, calls = _client('{"winner": "left"}')
    payload, _ = prov.call_judge_local(client, "q", "a", "b", "judge", "sys", 300, SCHEMA)
    assert payload == {"winner": "left"}
    rf = calls.kwargs["response_format"]
    assert rf["type"] == "json_schema" and rf["json_schema"]["schema"] == SCHEMA["input_schema"]


@pytest.mark.parametrize("content", [
    'Sure! Here is my verdict:\n{"winner": "right"}\nThanks.',
    'Placeholders like {x} aside, the verdict is {"winner": "right"} and {done}.',
])
def test_judge_tolerates_prose_around_json(content):
    client, _ = _client(content)
    payload, _ = prov.call_judge_local(client, "q", "a", "b", "judge", "sys", 300, SCHEMA)
    assert payload == {"winner": "right"}


@pytest.mark.parametrize("content, finish", [("no json here", "stop"), ("", "length"), ("[1, 2]", "stop")])
def test_judge_failures_raise_the_retryable_error(content, finish):
    client, _ = _client(content, finish)
    with pytest.raises(JudgeValidationError):
        prov.call_judge_local(client, "q", "a", "b", "judge", "sys", 300, SCHEMA)


def test_provider_reads_endpoint_and_models_from_env(monkeypatch):
    pytest.importorskip("openai")
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://127.0.0.1:11435/v1")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "gemma3:27b")
    monkeypatch.setenv("LOCAL_LLM_JUDGE_MODEL", "qwen3:32b")
    p = prov.get_provider("local")
    assert (p.default_model, p.default_judge_model) == ("gemma3:27b", "qwen3:32b")
    assert str(p.make_sync_client().base_url).rstrip("/") == "http://127.0.0.1:11435/v1"
    assert prov.missing_credentials(p) is None


def test_judge_model_defaults_to_answer_model(monkeypatch):
    pytest.importorskip("openai")
    monkeypatch.setenv("LOCAL_LLM_MODEL", "gemma3:27b")
    monkeypatch.delenv("LOCAL_LLM_JUDGE_MODEL", raising=False)
    assert prov.get_provider("local").default_judge_model == "gemma3:27b"


def test_cloud_providers_still_require_their_key(monkeypatch):
    pytest.importorskip("anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert prov.missing_credentials(prov.get_provider("anthropic")) == "ANTHROPIC_API_KEY"


def test_local_models_cost_nothing():
    assert prov.get_pricing("qwen3:8b", "local") == prov.LOCAL_PRICING
    assert all(v == 0 for v in prov.LOCAL_PRICING.values())
    # Without the provider hint an unknown tag still gets the conservative fallback.
    assert prov.get_pricing("qwen3:8b")["output"] > 0


def test_cloud_judge_env_does_not_leak_into_local_runs(monkeypatch):
    monkeypatch.setenv("JUDGE_MODEL", "claude-opus-4-8")
    monkeypatch.setenv("JUDGE_PROVIDER", "anthropic")
    assert prov.judge_env_default("JUDGE_MODEL", "local") is None
    assert prov.judge_env_default("JUDGE_PROVIDER", "local") is None
    assert prov.judge_env_default("JUDGE_MODEL", "anthropic") == "claude-opus-4-8"


def test_rule_ab_arm_answers_everything_before_grading(monkeypatch):
    """A local server that holds one model at a time must see all answer calls
    (model) before any grade call (judge), not an alternation per case."""
    from evals.scripts import compare_rules as cr

    calls: list[str] = []

    async def complete(client, model, query, system_prompt, max_tokens, sample=True):
        calls.append(model)
        return ("VERDICT: PASS\nREASON: ok" if model == "judge" else f"answer to {query}"), {}, 0

    provider = SimpleNamespace(name="local", complete=complete)
    cases = [
        {"id": f"c{i}", "category": "fabrication-recall", "query": f"q{i}", "reference": "r", "rubric": "r",
         "checks": {"must_not_contain": []}}
        for i in range(3)
    ]
    prompts = {c["id"]: {"system_prompt": "sp"} for c in cases}
    res = asyncio.run(cr.run_arm(cases, prompts, provider, None, "model", "judge", 2, "arm"))
    assert calls == ["model"] * 6 + ["judge"] * 6
    assert res.per_case == {"c0": False, "c1": False, "c2": False}


def test_rule_ab_arm_keeps_interleaving_and_early_break_for_cloud():
    """Cloud runs grade each sample as it arrives and stop a failed case early."""
    from evals.scripts import compare_rules as cr

    calls: list[str] = []

    async def complete(client, model, query, system_prompt, max_tokens, sample=True):
        calls.append(model)
        return ("VERDICT: FAIL\nREASON: wrong" if model == "judge" else "answer"), {}, 0

    provider = SimpleNamespace(name="anthropic", complete=complete)
    case = {"id": "c0", "category": "fabrication-recall", "query": "q", "reference": "r", "rubric": "r",
            "checks": {"must_not_contain": []}}
    res = asyncio.run(cr.run_arm([case], {"c0": {"system_prompt": "sp"}}, provider, None, "model", "judge", 3, "arm"))
    assert calls == ["model", "judge"]  # first sample failed; samples 2-3 never generated
    assert res.per_case == {"c0": True}


def test_rule_ab_records_answers_and_verdicts(tmp_path):
    from evals.scripts import compare_rules as cr

    async def complete(client, model, query, system_prompt, max_tokens, sample=True):
        return ("VERDICT: FAIL\nREASON: invented port" if model == "judge" else "port 6379"), {}, 0

    provider = SimpleNamespace(name="local", complete=complete)
    case = {"id": "c0", "category": "fabrication-recall", "query": "q", "reference": "r", "rubric": "r",
            "checks": {"must_not_contain": []}}
    path = tmp_path / "ab.answers.jsonl"
    asyncio.run(cr.run_arm([case], {"c0": {"system_prompt": "sp"}}, provider, None, "model", "judge", 1, "baseline", path))
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert rows == [{"arm": "baseline", "id": "c0", "sample": 0, "answer": "port 6379",
                     "deterministic": [], "verdict": "FAIL", "reason": "invented port"}]


def test_transcript_keeps_samples_graded_before_a_mid_arm_failure(tmp_path):
    """Each graded sample is written as it happens, so a crash later in the arm keeps it."""
    from evals.scripts import compare_rules as cr

    grades = iter(["VERDICT: PASS\nREASON: ok"])

    async def complete(client, model, query, system_prompt, max_tokens, sample=True):
        if model == "judge":
            try:
                return next(grades), {}, 0
            except StopIteration:
                raise RuntimeError("judge died") from None
        return f"answer {query}", {}, 0

    provider = SimpleNamespace(name="anthropic", complete=complete)
    cases = [{"id": f"c{i}", "category": "fabrication-recall", "query": f"q{i}", "reference": "r",
              "rubric": "r", "checks": {"must_not_contain": []}} for i in range(2)]
    path = tmp_path / "ab.answers.jsonl"
    with pytest.raises(RuntimeError, match="judge died"):
        asyncio.run(cr.run_arm(cases, {c["id"]: {"system_prompt": "sp"} for c in cases}, provider, None,
                               "model", "judge", 1, "baseline", path))
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [(r["id"], r["verdict"]) for r in rows] == [("c0", "PASS")]


def test_judge_keeps_thinking_off_at_any_budget(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_THINKING", "1")
    client, calls = _client('{"winner": "left"}')
    for budget in (prov.LOCAL_THINKING_MIN_TOKENS, 4096):
        prov.call_judge_local(client, "q", "a", "b", "judge", "sys", budget, SCHEMA)
        assert calls.kwargs["reasoning_effort"] == "none"


_VERDICT = json.dumps({
    "winner": "right",
    "reasoning": "ok",
    "criterion_scores": {f"{s}_{c}": 5 for s in ("left", "right")
                         for c in ("helpfulness", "correctness", "depth", "structure", "intent_fit")},
})


def test_main_async_runs_end_to_end_on_local_provider(monkeypatch, tmp_path):
    """`--provider local` through run_mcp_vs_vanilla.main_async with fake clients:
    no key needed, cloud JUDGE_* env ignored, the judge never thinks, cost is zero."""
    pytest.importorskip("openai")
    pytest.importorskip("jinja2")
    import dataclasses

    from evals.runners import run_mcp_vs_vanilla as rmv

    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "LOCAL_LLM_JUDGE_MODEL", "LOCAL_LLM_TEMPERATURE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LOCAL_LLM_MODEL", "qwen3:8b")
    monkeypatch.setenv("LOCAL_LLM_THINKING", "1")
    monkeypatch.setenv("JUDGE_PROVIDER", "anthropic")
    monkeypatch.setenv("JUDGE_MODEL", "claude-opus-4-8")

    answers, _ = _client("An answer.", is_async=True)
    judge, judge_calls = _client(_VERDICT)
    real = prov.get_provider("local")
    fake = dataclasses.replace(real, make_async_client=lambda: answers, make_sync_client=lambda: judge)
    monkeypatch.setattr(rmv, "get_provider", lambda name: fake if name == "local" else prov.get_provider(name))
    monkeypatch.setattr(rmv, "sample_queries", lambda dataset, n, seed: [(0, "What is 2+2?")])
    monkeypatch.setattr(rmv, "_get_router", lambda: None)

    async def _no_route(query, pick_agent=None):
        return "sys", {"agent": "universal_agent", "tier": "lite", "routing_path": "meta_query",
                       "skills_loaded": [], "implants_loaded": [], "rules_loaded": []}

    monkeypatch.setattr(rmv, "build_mcp_system_prompt", _no_route)

    out = tmp_path / "report.html"
    args = rmv.parse_args(["--provider", "local", "--n", "1", "--concurrency", "1",
                           "--judge-max-tokens", "1024", "--out", str(out), "--save-json"])
    assert asyncio.run(rmv.main_async(args)) == 0

    data = json.loads(out.with_suffix(".json").read_text())
    assert (data["config"]["judge_provider"], data["config"]["judge_model"]) == ("local", "qwen3:8b")
    assert data["arm_pricing"] == data["judge_pricing"] == prov.LOCAL_PRICING
    assert data["runs"][0]["verdict"]["pos1"]["winner"] == "right"
    kw = judge_calls.kwargs
    assert kw["max_tokens"] == 1024 and kw["reasoning_effort"] == "none"


def test_main_async_answers_everything_before_judging_with_a_second_local_model(monkeypatch, tmp_path):
    """With a separate local judge model, a one-model server must not swap models per query."""
    pytest.importorskip("openai")
    pytest.importorskip("jinja2")
    import dataclasses

    from evals.runners import run_mcp_vs_vanilla as rmv

    for var in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY", "JUDGE_PROVIDER", "JUDGE_MODEL", "LOCAL_LLM_TEMPERATURE"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("LOCAL_LLM_MODEL", "answer:1")
    monkeypatch.setenv("LOCAL_LLM_JUDGE_MODEL", "judge:1")
    order: list[str] = []

    class _Recorder(_FakeCompletions):
        def create(self, **kwargs):
            order.append(kwargs["model"])
            return super().create(**kwargs)

    answers = SimpleNamespace(chat=SimpleNamespace(completions=_Recorder("An answer.", is_async=True)))
    judge = SimpleNamespace(chat=SimpleNamespace(completions=_Recorder(_VERDICT)))
    real = prov.get_provider("local")
    fake = dataclasses.replace(real, make_async_client=lambda: answers, make_sync_client=lambda: judge)
    monkeypatch.setattr(rmv, "get_provider", lambda name: fake)
    monkeypatch.setattr(rmv, "sample_queries", lambda dataset, n, seed: [(0, "q1"), (1, "q2"), (2, "q3")])
    monkeypatch.setattr(rmv, "_get_router", lambda: None)

    async def _no_route(query, pick_agent=None):
        return "sys", {"agent": "universal_agent", "tier": "lite", "routing_path": "meta_query",
                       "skills_loaded": [], "implants_loaded": [], "rules_loaded": []}

    monkeypatch.setattr(rmv, "build_mcp_system_prompt", _no_route)
    out = tmp_path / "report.html"
    args = rmv.parse_args(["--provider", "local", "--n", "3", "--concurrency", "2", "--out", str(out), "--save-json"])
    assert asyncio.run(rmv.main_async(args)) == 0
    assert order == ["answer:1"] * 6 + ["judge:1"] * 6  # 2 arms per query, then 2 judge passes per query
    data = json.loads(out.with_suffix(".json").read_text())
    assert [run["query"] for run in data["runs"]] == ["q1", "q2", "q3"]
