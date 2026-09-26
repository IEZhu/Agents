"""`openrouter` eval provider: hosted open-weight models behind one OpenAI-compatible API.

No network: the SDK clients are replaced by fakes that record the request.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import openai
import pytest

from evals.judges.pairwise_judge import JudgeValidationError
from evals.runners import _providers as prov

SCHEMA = {"name": "submit_verdict", "input_schema": {"type": "object", "properties": {"winner": {"type": "string"}}}}


def _client(content: str, finish: str = "stop", is_async: bool = False):
    calls = SimpleNamespace(kwargs=None)

    def create(**kwargs):
        calls.kwargs = kwargs
        resp = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=content), finish_reason=finish)],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=3, prompt_tokens_details=None),
        )
        if is_async:
            async def _coro():
                return resp
            return _coro()
        return resp

    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create))), calls


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for key in ("OPENROUTER_PROVIDER", "OPENROUTER_TEMPERATURE", "OPENROUTER_SEED", "OPENROUTER_MODEL",
                "OPENROUTER_JUDGE_MODEL", "OPENROUTER_REASONING", "OPENROUTER_GRADER_TEMPERATURE"):
        monkeypatch.delenv(key, raising=False)


def test_answers_run_at_temperature_zero_with_thinking_off_and_a_fixed_seed():
    client, calls = _client("<think>hidden</think>OK.", is_async=True)
    text, usage, _ = asyncio.run(prov.complete_openrouter(client, "google/gemma-4-31b-it", "hi", "sys", 50))
    assert text == "OK."
    kw = calls.kwargs
    assert kw["max_tokens"] == 50 and kw["temperature"] == 0 and kw["seed"] == 7
    assert kw["messages"] == [{"role": "system", "content": "sys"}, {"role": "user", "content": "hi"}]
    # Unpinned: any host that honours every parameter may serve the call.
    assert kw["extra_body"] == {"reasoning": {"enabled": False}, "provider": {"require_parameters": True}}
    assert usage["input_tokens"] == 10 and usage["output_tokens"] == 3


def test_provider_env_pins_the_listed_endpoints_without_fallback(monkeypatch):
    monkeypatch.setenv("OPENROUTER_PROVIDER", "novita/bf16, deepinfra/bf16")
    assert prov.openrouter_routing() == {"require_parameters": True, "order": ["novita/bf16", "deepinfra/bf16"],
                                         "allow_fallbacks": False}
    client, calls = _client("OK.", is_async=True)
    asyncio.run(prov.complete_openrouter(client, "m", "hi", None, 50))
    assert calls.kwargs["extra_body"]["provider"]["order"] == ["novita/bf16", "deepinfra/bf16"]


def test_sampled_answers_get_a_fresh_seed_but_graders_stay_greedy(monkeypatch):
    monkeypatch.setenv("OPENROUTER_TEMPERATURE", "0.7")
    client, calls = _client("OK.", is_async=True)
    seeds = []
    for _ in range(2):
        asyncio.run(prov.complete_openrouter(client, "m", "hi", None, 50))
        assert calls.kwargs["temperature"] == 0.7
        seeds.append(calls.kwargs["seed"])
    assert seeds[0] != seeds[1]
    asyncio.run(prov.complete_openrouter(client, "m", "hi", None, 300, sample=False))
    assert calls.kwargs["temperature"] == 0 and calls.kwargs["seed"] == 7


def test_sampled_seed_follows_the_caller_identity_across_restarts(monkeypatch):
    monkeypatch.setenv("OPENROUTER_TEMPERATURE", "0.7")
    monkeypatch.setattr(prov, "_seed_attempts", prov.collections.Counter())
    client, calls = _client("OK.", is_async=True)

    def seed(key):
        asyncio.run(prov.complete_openrouter(client, "m", "hi", None, 50, seed_key=key))
        return calls.kwargs["seed"]

    first, retry, other = seed("arm:c1:0"), seed("arm:c1:0"), seed("arm:c1:1")
    assert first != retry and other not in (first, retry)
    # A restarted process (fresh attempt counter) gives each key the seed it had,
    # whatever ran before it.
    monkeypatch.setattr(prov, "_seed_attempts", prov.collections.Counter())
    seed("arm:c9:0")
    assert seed("arm:c1:1") == other and seed("arm:c1:0") == first


def test_request_settings_pin_the_seed_scheme_and_the_local_endpoint(monkeypatch):
    monkeypatch.setenv("LOCAL_LLM_BASE_URL", "http://localhost:1234/v1/")
    local = prov.request_settings("local")
    assert local["base_url"] == "http://localhost:1234/v1" and local["seed_scheme"] == prov.SEED_SCHEME
    assert prov.request_settings("openrouter")["seed_scheme"] == prov.SEED_SCHEME


def test_empty_answer_cut_by_max_tokens_is_an_error():
    client, _ = _client("", finish="length", is_async=True)
    with pytest.raises(RuntimeError, match="max_tokens"):
        asyncio.run(prov.complete_openrouter(client, "m", "hi", None, 50))


def test_judge_asks_for_the_schema_and_reads_the_first_json_object(monkeypatch):
    monkeypatch.setenv("OPENROUTER_TEMPERATURE", "0.7")
    client, calls = _client('Verdict: {"winner": "left"}')
    payload, _ = prov.call_judge_openrouter(client, "q", "l", "r", "m", "sys", 200, SCHEMA)
    assert payload == {"winner": "left"}
    kw = calls.kwargs
    assert kw["temperature"] == 0 and kw["extra_body"]["reasoning"] == {"enabled": False}
    assert kw["response_format"]["json_schema"] == {"name": "submit_verdict", "schema": SCHEMA["input_schema"]}


def test_judge_failures_are_retryable_validation_errors():
    client, _ = _client("no json here")
    with pytest.raises(JudgeValidationError):
        prov.call_judge_openrouter(client, "q", "l", "r", "m", "sys", 200, SCHEMA)
    client, _ = _client("", finish="length")
    with pytest.raises(JudgeValidationError):
        prov.call_judge_openrouter(client, "q", "l", "r", "m", "sys", 200, SCHEMA)


def test_provider_reads_its_key_and_ignores_first_party_judge_defaults(monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key")
    monkeypatch.setenv("OPENROUTER_MODEL", "qwen/qwen3.8-27b")
    monkeypatch.setenv("JUDGE_MODEL", "claude-opus-4-8")
    provider = prov.get_provider("openrouter")
    assert provider.name == "openrouter" and provider.default_model == "qwen/qwen3.8-27b"
    assert provider.default_judge_model == "qwen/qwen3.8-27b"
    assert prov.missing_credentials(provider) is None
    client = provider.make_async_client()
    assert str(client.base_url).rstrip("/") == prov.OPENROUTER_BASE_URL
    assert prov.judge_env_default("JUDGE_MODEL", "openrouter") is None
    monkeypatch.delenv("OPENROUTER_API_KEY")
    assert prov.missing_credentials(prov.get_provider("openrouter")) == "OPENROUTER_API_KEY"


def _rate_limited():
    response = httpx.Response(429, request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions"))
    return openai.RateLimitError("rate-limited upstream", response=response, body=None)


def test_upstream_rate_limits_are_waited_out_within_a_budget(monkeypatch):
    pauses = []

    async def fake_sleep(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(prov.asyncio, "sleep", fake_sleep)
    failures = iter([_rate_limited()] * 5)

    async def create(**kwargs):
        error = next(failures, None)
        if error:
            raise error
        return "ok"

    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    assert asyncio.run(prov._openrouter_create(client, {})) == "ok"
    assert pauses == [5.0, 10.0, 20.0, 40.0, 60.0]

    monkeypatch.setenv("OPENROUTER_RATE_LIMIT_WAIT", "30")
    pauses.clear()
    failures = iter([_rate_limited()] * 5)
    with pytest.raises(openai.RateLimitError):
        asyncio.run(prov._openrouter_create(client, {}))
    # The last pause is cut to what is left of the budget, never past it.
    assert pauses == [5.0, 10.0, 15.0]


def test_thinking_models_get_an_effort_and_unsupported_parameters_can_be_left_out(monkeypatch):
    # claude-opus-5.5 rejects reasoning.enabled=false, and no endpoint of it takes `seed`.
    monkeypatch.setenv("OPENROUTER_REASONING", "low")
    monkeypatch.setenv("OPENROUTER_SEED", "none")
    monkeypatch.setenv("OPENROUTER_TEMPERATURE", "default")
    kw = prov._openrouter_request("anthropic/claude-opus-5.5", [{"role": "user", "content": "hi"}], 1400)
    assert kw["extra_body"]["reasoning"] == {"effort": "low", "exclude": True}
    assert "seed" not in kw and "temperature" not in kw
    monkeypatch.setenv("OPENROUTER_TEMPERATURE", "0")
    kw = prov._openrouter_request("m", [{"role": "user", "content": "hi"}], 10)
    assert kw["temperature"] == 0 and "seed" not in kw
    # A grader call keeps thinking off even when answers think.
    kw = prov._openrouter_request("qwen/qwen3.8-27b", [{"role": "user", "content": "grade"}], 300, sample=False)
    assert kw["extra_body"]["reasoning"] == {"enabled": False}


def test_graders_stay_greedy_when_answers_leave_temperature_to_the_endpoint(monkeypatch):
    # OPENROUTER_TEMPERATURE=default is for answer models whose endpoints reject the
    # parameter; an endpoint default would make grades and router picks sample.
    monkeypatch.setenv("OPENROUTER_TEMPERATURE", "default")
    messages = [{"role": "user", "content": "grade"}]
    assert "temperature" not in prov._openrouter_request("anthropic/claude-opus-5.5", messages, 800)
    assert prov._openrouter_request("qwen/qwen3.8-27b", messages, 300, sample=False)["temperature"] == 0
    # Only a grader whose endpoints reject temperature opts out explicitly.
    monkeypatch.setenv("OPENROUTER_GRADER_TEMPERATURE", "default")
    assert "temperature" not in prov._openrouter_request("m", messages, 300, sample=False)

