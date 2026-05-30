"""
Tests for the MCP-vs-vanilla benchmark harness.

Covers pure-Python logic — no API calls, no HF dataset fetches:
- swap-aggregation logic (consistency, contradiction, tie handling)
- token-usage normalisation for both providers (OpenAI, Anthropic)
- HTML escaping of user-supplied content (XSS-prevention)
- provider dispatch
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.judges.pairwise_judge import JudgeCall, aggregate_with_swap


def _jc(winner: str, *, in_=10, out=20, cc=0, cr=0) -> JudgeCall:
    return JudgeCall(
        winner=winner,  # type: ignore[arg-type]
        reasoning="x",
        criterion_scores={},
        usage={"input_tokens": in_, "output_tokens": out, "cache_creation_input_tokens": cc, "cache_read_input_tokens": cr},
    )


# --------------------------------------------------------------------------- #
# aggregate_with_swap
# --------------------------------------------------------------------------- #


class TestAggregateWithSwap:
    """pos1: LEFT=vanilla, RIGHT=mcp. pos2 is the swap: LEFT=mcp, RIGHT=vanilla."""

    def test_consistent_mcp_wins(self):
        v = aggregate_with_swap(pos1=_jc("right"), pos2=_jc("left"), pos1_left_is="vanilla")
        assert v.final == "mcp"
        assert v.contradicted is False

    def test_consistent_vanilla_wins(self):
        v = aggregate_with_swap(pos1=_jc("left"), pos2=_jc("right"), pos1_left_is="vanilla")
        assert v.final == "vanilla"
        assert v.contradicted is False

    def test_contradiction_resolves_to_tie(self):
        v = aggregate_with_swap(pos1=_jc("left"), pos2=_jc("left"), pos1_left_is="vanilla")
        assert v.final == "tie"
        assert v.contradicted is True

    def test_both_ties(self):
        v = aggregate_with_swap(pos1=_jc("tie"), pos2=_jc("tie"), pos1_left_is="vanilla")
        assert v.final == "tie"
        assert v.contradicted is False

    def test_one_tie_one_winner_falls_to_winner(self):
        v = aggregate_with_swap(pos1=_jc("tie"), pos2=_jc("left"), pos1_left_is="vanilla")
        assert v.final == "mcp"
        assert v.contradicted is False

    def test_one_winner_one_tie_falls_to_winner(self):
        v = aggregate_with_swap(pos1=_jc("right"), pos2=_jc("tie"), pos1_left_is="vanilla")
        assert v.final == "mcp"

    def test_total_usage_is_summed(self):
        v = aggregate_with_swap(
            pos1=_jc("left", in_=100, out=50, cc=5, cr=10),
            pos2=_jc("right", in_=200, out=80, cc=0, cr=30),
            pos1_left_is="vanilla",
        )
        assert v.total_usage["input_tokens"] == 300
        assert v.total_usage["output_tokens"] == 130
        assert v.total_usage["cache_creation_input_tokens"] == 5
        assert v.total_usage["cache_read_input_tokens"] == 40


# --------------------------------------------------------------------------- #
# Token-usage normalisation per provider
# --------------------------------------------------------------------------- #


class FakeAnthropicUsage:
    def __init__(self, **kwargs):
        for k in ("input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"):
            setattr(self, k, kwargs.get(k, 0))


class FakeOpenAIPromptDetails:
    def __init__(self, cached_tokens: int):
        self.cached_tokens = cached_tokens


class FakeOpenAIUsage:
    def __init__(self, prompt_tokens: int, completion_tokens: int, cached_tokens: int | None = None):
        self.prompt_tokens = prompt_tokens
        self.completion_tokens = completion_tokens
        self.total_tokens = prompt_tokens + completion_tokens
        if cached_tokens is not None:
            self.prompt_tokens_details = FakeOpenAIPromptDetails(cached_tokens)


class TestUsageNormalisation:
    def test_anthropic_extracts_all_fields(self):
        from evals.runners._providers import normalise_usage_anthropic

        d = normalise_usage_anthropic(
            FakeAnthropicUsage(input_tokens=500, output_tokens=120, cache_creation_input_tokens=2000, cache_read_input_tokens=1500)
        )
        assert d == {
            "input_tokens": 500,
            "output_tokens": 120,
            "cache_creation_input_tokens": 2000,
            "cache_read_input_tokens": 1500,
        }

    def test_anthropic_handles_none_cache_fields(self):
        from evals.runners._providers import normalise_usage_anthropic

        class PartialUsage:
            input_tokens = 100
            output_tokens = 50
            cache_creation_input_tokens = None
            cache_read_input_tokens = None

        d = normalise_usage_anthropic(PartialUsage())
        assert d["cache_creation_input_tokens"] == 0
        assert d["cache_read_input_tokens"] == 0

    def test_openai_without_cache_details(self):
        from evals.runners._providers import normalise_usage_openai

        d = normalise_usage_openai(FakeOpenAIUsage(prompt_tokens=500, completion_tokens=120))
        assert d == {
            "input_tokens": 500,
            "output_tokens": 120,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        }

    def test_openai_with_cached_tokens(self):
        """For OpenAI, prompt_tokens is the TOTAL including cached; we subtract cached
        from input_tokens so the four fields sum cleanly when costed against pricing."""
        from evals.runners._providers import normalise_usage_openai

        d = normalise_usage_openai(FakeOpenAIUsage(prompt_tokens=1000, completion_tokens=200, cached_tokens=600))
        assert d == {
            "input_tokens": 400,  # 1000 total - 600 cached
            "output_tokens": 200,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 600,
        }


# --------------------------------------------------------------------------- #
# Provider dispatch
# --------------------------------------------------------------------------- #


class TestCostSplit:
    """Verify per-component cost split correctly separates input/output/cache spend."""

    def test_split_uses_correct_prices(self):
        from evals.runners.run_mcp_vs_vanilla import _cost_split

        usage = {
            "input_tokens": 1_000_000,
            "output_tokens": 500_000,
            "cache_read_input_tokens": 200_000,
            "cache_creation_input_tokens": 0,
        }
        pricing = {"input": 5.0, "output": 30.0, "cache_read": 0.5, "cache_creation": 0.0}
        split = _cost_split(usage, pricing)
        assert split["input_usd"] == 5.0   # 1M * $5
        assert split["output_usd"] == 15.0  # 0.5M * $30
        assert split["cache_read_usd"] == 0.1  # 0.2M * $0.5

    def test_split_zero_usage(self):
        from evals.runners.run_mcp_vs_vanilla import _cost_split

        zero = {"input_tokens": 0, "output_tokens": 0, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0}
        split = _cost_split(zero, {"input": 5.0, "output": 30.0, "cache_read": 0.5, "cache_creation": 0.0})
        assert all(v == 0.0 for v in split.values())

    def test_sum_usages_merges_correctly(self):
        from evals.runners.run_mcp_vs_vanilla import _sum_usages

        u1 = {"input_tokens": 100, "output_tokens": 50, "cache_read_input_tokens": 10, "cache_creation_input_tokens": 0}
        u2 = {"input_tokens": 200, "output_tokens": 80, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 3}
        out = _sum_usages([u1, u2])
        assert out == {"input_tokens": 300, "output_tokens": 130, "cache_read_input_tokens": 15, "cache_creation_input_tokens": 3}


class TestProviderDispatch:
    def test_openai_provider_resolves(self):
        # `get_provider("openai")` imports the openai SDK eagerly. Skip the
        # test cleanly when running outside the `[evals]` extra rather than
        # erroring on a missing optional dependency.
        pytest.importorskip("openai")
        from evals.runners._providers import get_provider

        p = get_provider("openai")
        assert p.name == "openai"
        assert p.env_key == "OPENAI_API_KEY"
        assert p.default_model.startswith("gpt-")
        assert "input" in p.pricing and "output" in p.pricing

    def test_anthropic_provider_resolves(self):
        # Same rationale as the openai test — anthropic SDK is also an
        # `[evals]`-extra dependency.
        pytest.importorskip("anthropic")
        from evals.runners._providers import get_provider

        p = get_provider("anthropic")
        assert p.name == "anthropic"
        assert p.env_key == "ANTHROPIC_API_KEY"
        assert p.default_model.startswith("claude-")

    def test_unknown_provider_raises(self):
        from evals.runners._providers import get_provider

        with pytest.raises(ValueError, match="unknown provider"):
            get_provider("ollama")


# --------------------------------------------------------------------------- #
# OpenAI API parameter-name regression
#
# gpt-5.5 (and other newer OpenAI models) reject `max_tokens` with HTTP 400.
# These tests pin the forward-compatible `max_completion_tokens` to prevent
# the bug from recurring on a careless edit.
# --------------------------------------------------------------------------- #


class TestOpenAIMaxCompletionTokensRegression:
    def _fake_chat_response(self, text: str = "ok"):
        from unittest.mock import MagicMock

        message = MagicMock()
        message.content = text
        message.tool_calls = None
        choice = MagicMock()
        choice.message = message
        choice.finish_reason = "stop"
        response = MagicMock()
        response.choices = [choice]
        response.usage = FakeOpenAIUsage(prompt_tokens=20, completion_tokens=10)
        return response

    def test_complete_openai_uses_max_completion_tokens(self):
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from evals.runners._providers import complete_openai

        client = MagicMock()
        client.chat.completions.create = AsyncMock(return_value=self._fake_chat_response("hi"))

        asyncio.run(complete_openai(client, "gpt-5.5", "hello", None, max_tokens=500))

        kwargs = client.chat.completions.create.call_args.kwargs
        assert "max_completion_tokens" in kwargs
        assert kwargs["max_completion_tokens"] == 500
        assert "max_tokens" not in kwargs, "max_tokens is rejected by gpt-5.x — must use max_completion_tokens"

    def test_complete_openai_omits_temperature(self):
        """gpt-5.x rejects temperature=0; we omit the param so the model uses its default."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from evals.runners._providers import complete_openai

        client = MagicMock()
        client.chat.completions.create = AsyncMock(return_value=self._fake_chat_response("hi"))

        asyncio.run(complete_openai(client, "gpt-5.5", "hello", None, max_tokens=100))

        kwargs = client.chat.completions.create.call_args.kwargs
        assert "temperature" not in kwargs, "temperature must be omitted for gpt-5.x compatibility"

    def test_complete_openai_disables_reasoning_for_gpt5(self):
        """Without reasoning_effort='none', gpt-5.x reasoning tokens consume
        the entire max_completion_tokens budget and visible output is empty."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from evals.runners._providers import complete_openai

        client = MagicMock()
        client.chat.completions.create = AsyncMock(return_value=self._fake_chat_response("hi"))

        asyncio.run(complete_openai(client, "gpt-5.5", "hello", None, max_tokens=100))

        kwargs = client.chat.completions.create.call_args.kwargs
        assert kwargs.get("reasoning_effort") == "none", (
            "reasoning_effort must be 'none' on gpt-5.x or the model burns the token budget on hidden reasoning"
        )

    def test_complete_openai_no_reasoning_effort_for_gpt4o(self):
        """gpt-4o is non-reasoning; sending reasoning_effort returns HTTP 400."""
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from evals.runners._providers import complete_openai

        client = MagicMock()
        client.chat.completions.create = AsyncMock(return_value=self._fake_chat_response("hi"))

        asyncio.run(complete_openai(client, "gpt-4o", "hello", None, max_tokens=100))

        kwargs = client.chat.completions.create.call_args.kwargs
        assert "reasoning_effort" not in kwargs, (
            "gpt-4o rejects reasoning_effort — must only be sent for gpt-5.x/o-series models"
        )

    def test_is_reasoning_openai_model_classifier(self):
        from evals.runners._providers import _is_reasoning_openai_model

        assert _is_reasoning_openai_model("gpt-5.5") is True
        assert _is_reasoning_openai_model("gpt-5.5-pro") is True
        assert _is_reasoning_openai_model("gpt-5.4-mini") is True
        assert _is_reasoning_openai_model("o1-preview") is True
        assert _is_reasoning_openai_model("o3-mini") is True
        assert _is_reasoning_openai_model("gpt-4o") is False
        assert _is_reasoning_openai_model("gpt-4o-2024-11-20") is False
        assert _is_reasoning_openai_model("gpt-4-turbo") is False

    def test_supports_temperature_anthropic_classifier(self):
        """Opus 4.7 and 4.8 deprecate temperature; other Claude models accept it."""
        from evals.runners._providers import _supports_temperature_anthropic

        # Deny — Opus 4.7 and 4.8 return HTTP 400 on temperature (sampling params removed).
        assert _supports_temperature_anthropic("claude-opus-4-7") is False
        assert _supports_temperature_anthropic("claude-opus-4-8") is False
        # Allow — verified via N=30 Sonnet 4.6 bench run.
        assert _supports_temperature_anthropic("claude-sonnet-4-6") is True
        assert _supports_temperature_anthropic("claude-sonnet-4-5") is True
        assert _supports_temperature_anthropic("claude-haiku-4-5") is True
        assert _supports_temperature_anthropic("claude-opus-4-6") is True

    def test_call_judge_openai_uses_json_schema_not_tools(self):
        """Judge must use response_format=json_schema (works across proxy→Gemini paths).
        Tool-calling format does not pass-through some proxies and OpenAI's gpt-5.x
        also bans tools+reasoning_effort, so json_schema is the universal choice."""
        from unittest.mock import MagicMock

        from evals.runners._providers import call_judge_openai

        message = MagicMock()
        message.content = '{"winner": "tie", "reasoning": "x", "criterion_scores": {}}'
        message.tool_calls = None
        choice = MagicMock()
        choice.message = message
        choice.finish_reason = "stop"
        response = MagicMock()
        response.choices = [choice]
        response.usage = FakeOpenAIUsage(prompt_tokens=30, completion_tokens=15)

        client = MagicMock()
        client.chat.completions.create = MagicMock(return_value=response)

        from evals.judges.pairwise_judge import JUDGE_SYSTEM_PROMPT, VERDICT_SCHEMA

        payload, _ = call_judge_openai(client, "q", "L", "R", "gpt-5.5", JUDGE_SYSTEM_PROMPT, 700, VERDICT_SCHEMA)
        assert payload["winner"] == "tie"

        kwargs = client.chat.completions.create.call_args.kwargs
        assert "max_completion_tokens" in kwargs and kwargs["max_completion_tokens"] == 700
        assert "max_tokens" not in kwargs
        assert "temperature" not in kwargs
        # gpt-5.x judge: reasoning_effort MUST be "none" so hidden reasoning
        # tokens don't consume the entire max_completion_tokens budget.
        assert kwargs.get("reasoning_effort") == "none", (
            "gpt-5.x judge must pin reasoning_effort='none' — otherwise default 'medium' "
            "lets hidden reasoning eat max_completion_tokens and message.content is empty"
        )
        # Critical regression assertion — never go back to tool_calls for the judge.
        assert "tools" not in kwargs, "judge must use response_format, not tools (proxy→Gemini paths drop tool_calls)"
        assert "tool_choice" not in kwargs
        assert "response_format" in kwargs and kwargs["response_format"]["type"] == "json_schema"
        # NOTE: `strict` is intentionally NOT set — see _providers.py for the
        # Gemini-via-OpenAI-compat-proxy compatibility issue with strict nested schemas.
        assert "strict" not in kwargs["response_format"]["json_schema"]

    def test_call_judge_openai_raises_on_empty_content(self):
        from unittest.mock import MagicMock

        from evals.runners._providers import call_judge_openai
        from evals.judges.pairwise_judge import JUDGE_SYSTEM_PROMPT, VERDICT_SCHEMA

        message = MagicMock()
        message.content = ""
        choice = MagicMock()
        choice.message = message
        choice.finish_reason = "length"
        response = MagicMock()
        response.choices = [choice]
        response.usage = FakeOpenAIUsage(prompt_tokens=10, completion_tokens=0)
        client = MagicMock()
        client.chat.completions.create = MagicMock(return_value=response)

        with pytest.raises(RuntimeError, match="empty content"):
            call_judge_openai(client, "q", "L", "R", "gpt-5.5", JUDGE_SYSTEM_PROMPT, 100, VERDICT_SCHEMA)

    def test_call_judge_openai_omits_reasoning_effort_for_gpt4o(self):
        """gpt-4o rejects `reasoning_effort` with HTTP 400. The judge path must
        only send it for reasoning models — same contract as `complete_openai`."""
        from unittest.mock import MagicMock

        from evals.runners._providers import call_judge_openai
        from evals.judges.pairwise_judge import JUDGE_SYSTEM_PROMPT, VERDICT_SCHEMA

        message = MagicMock()
        message.content = '{"winner": "tie", "reasoning": "x", "criterion_scores": {}}'
        choice = MagicMock()
        choice.message = message
        choice.finish_reason = "stop"
        response = MagicMock()
        response.choices = [choice]
        response.usage = FakeOpenAIUsage(prompt_tokens=30, completion_tokens=15)
        client = MagicMock()
        client.chat.completions.create = MagicMock(return_value=response)

        call_judge_openai(client, "q", "L", "R", "gpt-4o", JUDGE_SYSTEM_PROMPT, 500, VERDICT_SCHEMA)

        kwargs = client.chat.completions.create.call_args.kwargs
        assert "reasoning_effort" not in kwargs, (
            "gpt-4o rejects reasoning_effort — judge must only send it for gpt-5.x/o-series"
        )


# --------------------------------------------------------------------------- #
# Template / XSS escaping
# --------------------------------------------------------------------------- #


class TestPlatformInstructionStripping:
    """Footer-stripping fix for the BLOCKER: MCP system prompts contain
    Claude-Code-only protocol directives (Agent/Skills/Implants/Rules footer,
    route_and_load imperatives) that would otherwise be echoed by the LLM,
    polluting the bench comparison."""

    def test_strips_append_at_end_footer_instruction(self):
        from evals.runners.run_mcp_vs_vanilla import _strip_platform_instructions

        prompt = (
            "You are a helpful agent.\n"
            "Append at the end (labels in English, values are canonical IDs):\n"
            "**Agent**: [name] · **Skills**: [skills] · **Implants**: [implants] · **Rules**: [rules]\n"
            "\nAnswer the user."
        )
        out = _strip_platform_instructions(prompt)
        assert "Append at the end" not in out
        assert "**Agent**: [name]" not in out
        assert "BENCH MODE" in out

    def test_strips_route_and_load_imperatives(self):
        from evals.runners.run_mcp_vs_vanilla import _strip_platform_instructions

        prompt = (
            "Persona text.\n"
            "CRITICAL: You MUST call `route_and_load(query)` BEFORE answering ANY user query.\n"
            "This is a BLOCKING REQUIREMENT — do NOT answer without routing first.\n"
            "Body of agent prompt continues."
        )
        out = _strip_platform_instructions(prompt)
        assert "route_and_load" not in out
        assert "BLOCKING REQUIREMENT" not in out
        assert "Body of agent prompt continues." in out  # non-platform content preserved

    def test_appends_bench_mode_override(self):
        from evals.runners.run_mcp_vs_vanilla import _strip_platform_instructions

        out = _strip_platform_instructions("Anything.")
        assert "BENCH MODE" in out
        assert "Do NOT append any platform metadata footer" in out

    def test_preserves_unrelated_content(self):
        from evals.runners.run_mcp_vs_vanilla import _strip_platform_instructions

        prompt = "## Role\nYou are a senior engineer.\n## Skills\n- Python\n- Rust\n"
        out = _strip_platform_instructions(prompt)
        assert "You are a senior engineer." in out
        assert "Python" in out
        assert "Rust" in out


class TestTruncationDetection:
    """Per-query truncation flag fires when output_tokens approach the arm cap.
    Catches the case where a long-form prompt hits --max-tokens and gets cut
    mid-sentence — the report shows a ⚠ TRUNCATED badge so the reader knows
    the comparison is unfair."""

    def _build(self, vanilla_out: int, mcp_out: int, cap: int):
        from evals.runners._providers import PRICING
        from evals.runners.run_mcp_vs_vanilla import (
            BenchmarkResult, QueryRun, TrialResult, build_template_context,
        )
        from evals.judges.pairwise_judge import aggregate_with_swap

        def zero_usage(out: int) -> dict[str, int]:
            return {
                "input_tokens": 100,
                "output_tokens": out,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            }
        van = TrialResult(arm="vanilla", response_text="x", usage=zero_usage(vanilla_out), latency_ms=10)
        mcp = TrialResult(arm="mcp", response_text="y", usage=zero_usage(mcp_out), latency_ms=10,
                          mcp_meta={"agent": "u", "tier": "lite", "skills_loaded": [], "implants_loaded": [], "rules_loaded": []})
        jc = JudgeCall(winner="tie", reasoning="x", criterion_scores={},
                       usage={"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0})
        verdict = aggregate_with_swap(pos1=jc, pos2=jc, pos1_left_is="vanilla")
        run = QueryRun(idx=1, query="q", stream_idx=0, vanilla=van, mcp=mcp, verdict=verdict)
        result = BenchmarkResult(
            config={"provider": "openai", "provider_notes": "", "model": "gpt-4o", "judge_model": "gpt-4o",
                    "seed": 0, "dataset": "wildbench", "commit_sha": "x", "max_tokens": cap,
                    "judge_max_tokens": 4096, "concurrency": 1},
            runs=[run], dataset_hash="h", wall_time_s=0.0,
            arm_pricing=PRICING["openai"], judge_pricing=PRICING["openai"],
        )
        return build_template_context(result)

    def test_truncated_when_output_near_cap(self):
        from evals.judges.pairwise_judge import JudgeCall  # noqa: F401 — used by _build

        ctx = self._build(vanilla_out=2048, mcp_out=2048, cap=2048)
        assert ctx["per_query"][0]["vanilla_truncated"] is True
        assert ctx["per_query"][0]["mcp_truncated"] is True
        assert ctx["summary"]["truncation_count"] == 1

    def test_not_truncated_when_well_under_cap(self):
        ctx = self._build(vanilla_out=500, mcp_out=800, cap=8192)
        assert ctx["per_query"][0]["vanilla_truncated"] is False
        assert ctx["per_query"][0]["mcp_truncated"] is False
        assert ctx["summary"]["truncation_count"] == 0

    def test_one_arm_truncated_other_not(self):
        ctx = self._build(vanilla_out=400, mcp_out=8100, cap=8192)
        assert ctx["per_query"][0]["vanilla_truncated"] is False
        assert ctx["per_query"][0]["mcp_truncated"] is True
        assert ctx["summary"]["truncation_count"] == 1


class TestEmptyArmShortCircuit:
    """When either arm returns empty text, the judge can't tell quality
    from length. We short-circuit to a synthetic TIE without burning judge
    tokens."""

    def test_synthetic_empty_tie_returns_tie_no_contradiction(self):
        from evals.runners.run_mcp_vs_vanilla import _synthetic_empty_tie

        v = _synthetic_empty_tie("vanilla arm empty")
        assert v.final == "tie"
        assert v.contradicted is False
        assert v.pos1.winner == "tie"
        assert v.pos2.winner == "tie"
        assert v.total_usage["input_tokens"] == 0
        assert v.total_usage["output_tokens"] == 0
        assert "empty" in v.pos1.reasoning.lower()


class TestJudgeRetry:
    """Judge calls must retry on transient API errors — arms already do, and
    a one-off RateLimitError from the judge should not collapse the whole
    query into a runtime failure."""

    def test_judge_with_swap_retries_on_transient_error(self):
        import asyncio
        from unittest.mock import patch

        from evals.runners.run_mcp_vs_vanilla import judge_with_swap
        from evals.judges.pairwise_judge import JudgeCall

        # Bypass the retry's SDK-specific exception filter — register our
        # sentinel as a retryable error type for this test.
        class _Transient(Exception):
            pass

        zero_usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        ok_call = JudgeCall(
            winner="left",
            reasoning="r",
            criterion_scores={k: 3 for k in (
                "left_helpfulness", "left_correctness", "left_depth", "left_structure", "left_intent_fit",
                "right_helpfulness", "right_correctness", "right_depth", "right_structure", "right_intent_fit",
            )},
            usage=dict(zero_usage),
        )

        calls = {"n": 0}

        def fake_run_judge(**kwargs):
            calls["n"] += 1
            # Throw transient once on EACH position so we exercise the retry
            # path for both pos1 and pos2.
            if calls["n"] in (1, 3):
                raise _Transient("simulated rate-limit")
            return ok_call

        async def _no_sleep(_seconds):
            return None

        with patch("evals.runners.run_mcp_vs_vanilla._get_retryable", return_value=(_Transient,)), \
             patch("evals.runners.run_mcp_vs_vanilla.run_judge", side_effect=fake_run_judge), \
             patch("evals.runners.run_mcp_vs_vanilla.asyncio.sleep", side_effect=_no_sleep):
            verdict = asyncio.run(judge_with_swap(
                judge_provider=None,  # not used by the mocked run_judge
                judge_sync_client=None,
                query="q",
                vanilla_text="v",
                mcp_text="m",
                judge_model="claude-sonnet-4-6",
                judge_max_tokens=100,
            ))

        # Each position retried once (4 total: pos1 fail+ok, pos2 fail+ok).
        assert calls["n"] == 4
        assert verdict.final in {"vanilla", "mcp", "tie"}

    def test_judge_with_swap_retries_on_malformed_verdict(self):
        """A malformed verdict (e.g. missing `winner`) from a stochastic judge —
        common via a local proxy under concurrent load — is transient and must
        be re-rolled, not abort the whole run. Regression: a single incomplete
        tool_use payload crashed the entire benchmark with
        'Judge returned invalid winner: None'. This exercises the REAL retryable
        set (no `_get_retryable` patch), proving JudgeValidationError is wired in."""
        import asyncio
        from unittest.mock import patch

        from evals.runners.run_mcp_vs_vanilla import judge_with_swap
        from evals.judges.pairwise_judge import JudgeCall, JudgeValidationError

        zero_usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        ok_call = JudgeCall(
            winner="left",
            reasoning="r",
            criterion_scores={k: 3 for k in (
                "left_helpfulness", "left_correctness", "left_depth", "left_structure", "left_intent_fit",
                "right_helpfulness", "right_correctness", "right_depth", "right_structure", "right_intent_fit",
            )},
            usage=dict(zero_usage),
        )
        calls = {"n": 0}

        def fake_run_judge(**kwargs):
            calls["n"] += 1
            if calls["n"] in (1, 3):  # one malformed verdict on each position
                raise JudgeValidationError("Judge returned invalid winner: None (payload keys: [])")
            return ok_call

        async def _no_sleep(_seconds):
            return None

        with patch("evals.runners.run_mcp_vs_vanilla.run_judge", side_effect=fake_run_judge), \
             patch("evals.runners.run_mcp_vs_vanilla.asyncio.sleep", side_effect=_no_sleep):
            verdict = asyncio.run(judge_with_swap(
                judge_provider=None,
                judge_sync_client=None,
                query="q",
                vanilla_text="v",
                mcp_text="m",
                judge_model="claude-opus-4-8",
                judge_max_tokens=100,
            ))

        assert calls["n"] == 4  # each position: malformed once + ok
        assert verdict.final in {"vanilla", "mcp", "tie"}

    def test_malformed_verdict_is_in_real_retryable_set(self):
        """JudgeValidationError must be registered as retryable, else the bench
        aborts on the first malformed verdict instead of re-rolling."""
        from evals.runners.run_mcp_vs_vanilla import _retryable_api_errors
        from evals.judges.pairwise_judge import JudgeValidationError

        assert JudgeValidationError in _retryable_api_errors()

    def test_call_judge_anthropic_raises_validation_error_on_no_tool_use(self):
        """If the (proxy-relayed) judge returns no tool_use block, the provider
        must raise the retryable JudgeValidationError — not a bare RuntimeError
        that aborts the run un-retried. Same root cause as winner=None: an
        intermittently malformed judge response under proxy load."""
        from unittest.mock import MagicMock
        import pytest

        from evals.runners._providers import call_judge_anthropic
        from evals.judges.pairwise_judge import JudgeValidationError, VERDICT_SCHEMA

        client = MagicMock()
        resp = MagicMock()
        resp.content = []  # no tool_use block — judge produced no verdict
        resp.stop_reason = "end_turn"
        client.messages.create.return_value = resp

        with pytest.raises(JudgeValidationError, match="no tool_use"):
            call_judge_anthropic(
                client, "q", "L", "R", "claude-opus-4-8", "sys", 4096, VERDICT_SCHEMA,
            )


class TestHarnessContamination:
    """The bench sends no tools, so any tool-call XML / system-reminder in a
    completion is agentic-harness scaffolding leaked by the backend proxy. Such
    a response is not a faithful arm answer — the runner must re-roll to capture
    a valid one (the leak is intermittent), mirroring the judge's verdict re-roll."""

    def test_has_harness_artifacts_detects_leak_not_legit_code(self):
        from evals.runners._providers import has_harness_artifacts

        # Real leaked scaffolding (from a contaminated bench run) → True.
        assert has_harness_artifacts('ok\n<invocation>\n<parameter name="command">pwd</parameter>\n</invocation>')
        assert has_harness_artifacts("system<system-reminder>The user opened /tmp/</system-reminder>")
        assert has_harness_artifacts('text\n<invoke name="bash">')
        # Legitimate content using `<` must NOT trip the detector.
        assert not has_harness_artifacts("```csharp\npublic List<int> f() {}\n```")
        assert not has_harness_artifacts("Use <summary> tags; if a < b then ...")
        assert not has_harness_artifacts("")
        assert not has_harness_artifacts(None)

    def test_contaminated_response_error_in_retryable_set(self):
        from evals.runners.run_mcp_vs_vanilla import _retryable_api_errors
        from evals.runners._providers import ContaminatedResponseError

        assert ContaminatedResponseError in _retryable_api_errors()

    def test_run_arm_re_rolls_contaminated_completion_until_valid(self):
        """User requirement: a valid arm response must be returned. A contaminated
        completion is re-rolled via the REAL retryable set (no _get_retryable
        patch) until clean — not scored as garbage and not crashing the run."""
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import patch

        from evals.runners.run_mcp_vs_vanilla import run_arm_vanilla
        from evals.runners._providers import ContaminatedResponseError

        usage = {"input_tokens": 1, "output_tokens": 1,
                 "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        calls = {"n": 0}

        async def fake_complete(client, model, query, system_prompt, max_tokens):
            calls["n"] += 1
            if calls["n"] == 1:  # backend leaks harness scaffolding once
                raise ContaminatedResponseError("leaked <invocation> scaffolding")
            return "clean essay answer", dict(usage), 10

        provider = SimpleNamespace(complete=fake_complete)

        async def _no_sleep(_s):
            return None

        with patch("evals.runners.run_mcp_vs_vanilla.asyncio.sleep", side_effect=_no_sleep):
            res = asyncio.run(run_arm_vanilla(provider, None, "q", "claude-opus-4-8", 100))

        assert calls["n"] == 2  # contaminated once, re-rolled, then clean
        assert res.response_text == "clean essay answer"
        assert res.arm == "vanilla"


class TestPerModelPricing:
    """Per-model pricing lookup so report $-numbers stay correct regardless
    of which --model is passed."""

    def test_exact_match(self):
        from evals.runners._providers import get_pricing

        p = get_pricing("gpt-4o")
        assert p["input"] == 2.50
        assert p["output"] == 10.00

    def test_dated_snapshot_falls_back_to_alias(self):
        from evals.runners._providers import get_pricing

        # gpt-4o-2024-11-20 should match the gpt-4o entry via prefix.
        p = get_pricing("gpt-4o-2024-11-20")
        assert p["input"] == 2.50

    def test_anthropic_sonnet(self):
        from evals.runners._providers import get_pricing

        p = get_pricing("claude-sonnet-4-6")
        assert p["input"] == 3.00
        assert p["output"] == 15.00

    def test_anthropic_opus_current_judge(self):
        """Opus 4.8 is the default judge (.env JUDGE_MODEL). It must resolve to
        the real $5/$25 Opus rate, not the conservative unknown-model fallback —
        otherwise reported judge cost silently inflates to $15/$75."""
        from evals.runners._providers import get_pricing

        p = get_pricing("claude-opus-4-8")
        assert p["input"] == 5.00
        assert p["output"] == 25.00

    def test_unknown_model_returns_conservative_fallback(self):
        from evals.runners._providers import get_pricing

        p = get_pricing("some-future-mystery-model")
        # Should be higher than any real model so we never under-report cost.
        assert p["input"] >= 5.0
        assert p["output"] >= 25.0

    def test_longest_alias_match_wins_for_overlapping_prefixes(self):
        """A dated snapshot of a more-specific alias must not match the shorter
        alias first. Regression: insertion-order iteration let `gpt-5.5-pro-…`
        match `gpt-5.5-` and pick up the cheaper non-Pro rate."""
        from evals.runners._providers import get_pricing

        p = get_pricing("gpt-5.5-pro-2026-01-01")
        # Must resolve to gpt-5.5-pro, not gpt-5.5.
        assert p["input"] == 30.00
        assert p["output"] == 180.00


class TestRunJudgeDocstring:
    """`run_judge.__doc__` must actually be the docstring — a triple-quoted
    string placed after another statement in the body becomes an unused
    runtime literal, not `__doc__`, and that broke `help(run_judge)`."""

    def test_run_judge_has_docstring(self):
        from evals.judges.pairwise_judge import run_judge
        doc = run_judge.__doc__
        # Split per Ruff PT018: distinct assertions give clearer pytest output
        # — `doc is None` and `doc is "" / whitespace-only` are different bugs.
        assert doc is not None, "run_judge has no __doc__ (docstring placed after another statement?)"
        assert doc.strip(), "run_judge.__doc__ is whitespace-only"
        assert "Provider-agnostic" in doc


class TestVerdictValidation:
    """Provider payloads must conform to VERDICT_SCHEMA before being accepted —
    otherwise a malformed response would degrade to a real-looking TIE inside
    aggregate_with_swap, silently skewing benchmark numbers."""

    @staticmethod
    def _scores() -> dict[str, int]:
        return {k: 3 for k in (
            "left_helpfulness", "left_correctness", "left_depth",
            "left_structure", "left_intent_fit",
            "right_helpfulness", "right_correctness", "right_depth",
            "right_structure", "right_intent_fit",
        )}

    def test_accepts_well_formed_payload(self):
        from evals.judges.pairwise_judge import _validate_verdict_payload

        winner, reasoning, scores = _validate_verdict_payload({
            "winner": "left",
            "reasoning": "left is more direct.",
            "criterion_scores": self._scores(),
        })
        assert winner == "left"
        assert reasoning == "left is more direct."
        assert scores == self._scores()

    def test_rejects_invalid_winner(self):
        from evals.judges.pairwise_judge import _validate_verdict_payload
        import pytest

        with pytest.raises(RuntimeError, match="invalid winner"):
            _validate_verdict_payload({
                "winner": "banana",
                "reasoning": "x",
                "criterion_scores": self._scores(),
            })

    def test_rejects_missing_reasoning(self):
        from evals.judges.pairwise_judge import _validate_verdict_payload
        import pytest

        with pytest.raises(RuntimeError, match="reasoning"):
            _validate_verdict_payload({
                "winner": "left",
                "criterion_scores": self._scores(),
            })

    def test_rejects_incomplete_criterion_scores(self):
        from evals.judges.pairwise_judge import _validate_verdict_payload
        import pytest

        with pytest.raises(RuntimeError, match="criterion_scores"):
            _validate_verdict_payload({
                "winner": "left",
                "reasoning": "x",
                "criterion_scores": {"left_helpfulness": 3},
            })

    def test_rejects_non_integer_criterion_score(self):
        """VERDICT_SCHEMA pins `integer 1..5`; a string score must fail-fast,
        not bleed into criterion_scores as garbage analytics data."""
        from evals.judges.pairwise_judge import _validate_verdict_payload
        import pytest

        bad = self._scores()
        bad["left_helpfulness"] = "five"  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="non-integer"):
            _validate_verdict_payload({"winner": "left", "reasoning": "x", "criterion_scores": bad})

    def test_rejects_boolean_score_disguised_as_int(self):
        """`bool` is a subclass of `int` in Python, so `True` would silently
        sail through a plain `isinstance(int)` check as 1. The validator must
        reject it explicitly."""
        from evals.judges.pairwise_judge import _validate_verdict_payload
        import pytest

        bad = self._scores()
        bad["left_helpfulness"] = True  # type: ignore[assignment]
        with pytest.raises(RuntimeError, match="non-integer"):
            _validate_verdict_payload({"winner": "left", "reasoning": "x", "criterion_scores": bad})

    def test_rejects_out_of_range_criterion_score(self):
        from evals.judges.pairwise_judge import _validate_verdict_payload
        import pytest

        for bad_value in (0, 11, -1, 100):   # scale is 1..10 (Phase 2); 11 is out of range
            bad = self._scores()
            bad["right_correctness"] = bad_value
            with pytest.raises(RuntimeError, match="out-of-range"):
                _validate_verdict_payload({"winner": "right", "reasoning": "x", "criterion_scores": bad})

    def test_scale_is_1_to_10_and_single_sourced(self):
        """Phase 2 widened the scale to 1–10, single-sourced from _SCORE_MAX:
        the schema maxima, the validation range, and the constant must all agree."""
        from evals.judges.pairwise_judge import (
            _SCORE_MAX, _validate_verdict_payload, VERDICT_SCHEMA,
        )
        assert _SCORE_MAX == 10
        props = VERDICT_SCHEMA["input_schema"]["properties"]["criterion_scores"]["properties"]
        assert {p["maximum"] for p in props.values()} == {_SCORE_MAX}
        # A perfect 10 across the board must now validate.
        top = self._scores()
        for k in top:
            top[k] = _SCORE_MAX
        winner, _r, scores = _validate_verdict_payload({"winner": "left", "reasoning": "x", "criterion_scores": top})
        assert scores["left_helpfulness"] == 10

    def test_invalid_payload_raises_judge_validation_error(self):
        """Validation failures must be the typed JudgeValidationError (a
        RuntimeError subclass) so the runner's retry wrapper can re-roll a
        malformed verdict while existing `except RuntimeError` callers and the
        match-based tests above keep working. Mirrors the real crash: the judge
        returned a tool_use payload with no `winner`."""
        from evals.judges.pairwise_judge import _validate_verdict_payload, JudgeValidationError
        import pytest

        assert issubclass(JudgeValidationError, RuntimeError)
        with pytest.raises(JudgeValidationError, match="invalid winner"):
            _validate_verdict_payload({"reasoning": "x", "criterion_scores": self._scores()})  # winner missing


class TestPricingSplitBetweenArmAndJudge:
    """Cross-provider / different-model judge runs must bill arm tokens and
    judge tokens against their respective per-1M rates. Regression: a single
    `pricing` field was reused for both, mis-reporting judge spend whenever
    the judge model differed from the arm model."""

    def _build_ctx(
        self, *, arm_pricing: dict[str, float], judge_pricing: dict[str, float],
    ) -> tuple[dict, dict]:
        from evals.runners._providers import PRICING
        from evals.runners.run_mcp_vs_vanilla import (
            BenchmarkResult, QueryRun, TrialResult, build_template_context,
        )
        from evals.judges.pairwise_judge import aggregate_with_swap, JudgeCall

        def usage(out: int, *, cache_create: int = 0) -> dict[str, int]:
            return {
                "input_tokens": 1_000_000,
                "output_tokens": out,
                "cache_creation_input_tokens": cache_create,
                "cache_read_input_tokens": 0,
            }

        van = TrialResult(arm="vanilla", response_text="x", usage=usage(0), latency_ms=10)
        mcp = TrialResult(arm="mcp", response_text="y", usage=usage(0, cache_create=1_000_000), latency_ms=10,
                          mcp_meta={"agent": "u", "tier": "lite", "skills_loaded": [], "implants_loaded": [], "rules_loaded": []})
        jc = JudgeCall(winner="tie", reasoning="x", criterion_scores={},
                       usage=usage(1_000_000))
        verdict = aggregate_with_swap(pos1=jc, pos2=jc, pos1_left_is="vanilla")
        run = QueryRun(idx=1, query="q", stream_idx=0, vanilla=van, mcp=mcp, verdict=verdict)
        result = BenchmarkResult(
            config={"provider": "openai", "provider_notes": "", "model": "gpt-4o",
                    "judge_provider": "anthropic", "judge_model": "claude-opus-4-7",
                    "seed": 0, "dataset": "wildbench", "commit_sha": "x", "max_tokens": 2048,
                    "judge_max_tokens": 4096, "concurrency": 1},
            runs=[run], dataset_hash="h", wall_time_s=0.0,
            arm_pricing=arm_pricing, judge_pricing=judge_pricing,
        )
        return build_template_context(result), PRICING

    def test_judge_row_uses_judge_pricing_not_arm_pricing(self):
        # Arm: gpt-4o ($2.50 / $10.00). Judge: claude-opus-4-7 ($5.00 / $25.00).
        # The judge ran twice (positional swap), so judge usage doubles.
        from evals.runners._providers import get_pricing

        arm = get_pricing("gpt-4o")
        judge = get_pricing("claude-opus-4-7")
        ctx, _ = self._build_ctx(arm_pricing=arm, judge_pricing=judge)
        rows = {row["label"]: row for row in ctx["cost_breakdown"]}
        # Judge row's output billing: 2M output tokens × $25/1M = $50.0000.
        # If arm pricing leaked in here, the result would be $20.0000.
        assert rows["Judge (×2 swap)"]["output_usd"] == "50.0000"
        # Vanilla row: 1M input tokens at arm $2.50/1M.
        assert rows["Vanilla (arm)"]["input_usd"] == "2.5000"

    def test_cache_creation_column_present_in_every_row(self):
        from evals.runners._providers import get_pricing

        arm = get_pricing("claude-sonnet-4-6")
        ctx, _ = self._build_ctx(arm_pricing=arm, judge_pricing=arm)
        for row in ctx["cost_breakdown"]:
            assert "cache_creation_tokens" in row
            assert "cache_creation_usd" in row
        rows = {row["label"]: row for row in ctx["cost_breakdown"]}
        # MCP row sourced 1M cache_creation tokens; at sonnet $3.75/1M → $3.7500.
        assert rows["MCP (arm)"]["cache_creation_usd"] == "3.7500"

    def test_total_row_sums_dollars_from_per_role_rows(self):
        """TOTAL must aggregate the per-role-priced row dollars, not re-cost
        the summed tokens at a single arbitrary rate."""
        from evals.runners._providers import get_pricing

        arm = get_pricing("gpt-4o")
        judge = get_pricing("claude-opus-4-7")
        ctx, _ = self._build_ctx(arm_pricing=arm, judge_pricing=judge)
        rows = {row["label"]: row for row in ctx["cost_breakdown"]}
        total = float(rows["TOTAL"]["total_usd"])
        expected = (
            float(rows["Vanilla (arm)"]["total_usd"])
            + float(rows["MCP (arm)"]["total_usd"])
            + float(rows["Judge (×2 swap)"]["total_usd"])
        )
        assert abs(total - expected) < 1e-4

    def test_total_row_components_consistent_with_total(self):
        """TOTAL.input + TOTAL.output + TOTAL.cache_read + TOTAL.cache_creation
        must equal TOTAL.total_usd to 4 decimal places. Regression: components
        were previously computed by summing already-formatted 4dp strings while
        total_usd came from raw float, so sub-cent drift could make the visible
        TOTAL row not add up."""
        from evals.runners._providers import get_pricing

        arm = get_pricing("gpt-4o")
        judge = get_pricing("claude-opus-4-7")
        ctx, _ = self._build_ctx(arm_pricing=arm, judge_pricing=judge)
        rows = {row["label"]: row for row in ctx["cost_breakdown"]}
        t = rows["TOTAL"]
        components = (
            float(t["input_usd"]) + float(t["output_usd"])
            + float(t["cache_read_usd"]) + float(t["cache_creation_usd"])
        )
        assert abs(components - float(t["total_usd"])) < 1e-4, (
            f"TOTAL row inconsistent: components={components} vs total={t['total_usd']}"
        )


class TestStreamIdxIsNotSourceIdx:
    """`source_idx` used to advertise itself as the HuggingFace split row index,
    but `sample_queries` reads `enumerate` on a shuffled streaming iterator —
    that's the stream position, not a recoverable provenance pointer. The
    field was renamed to `stream_idx` to stop misleading downstream readers."""

    def test_queryrun_has_stream_idx_not_source_idx(self):
        from evals.runners.run_mcp_vs_vanilla import QueryRun
        fields = {f.name for f in QueryRun.__dataclass_fields__.values()}
        assert "stream_idx" in fields
        assert "source_idx" not in fields, (
            "source_idx is misleading (it's the shuffled-stream position, not the HF row index). "
            "Use stream_idx so downstream tooling doesn't try to pass it to fetch --idx."
        )


class TestBuildMcpSystemPromptRoutingFidelity:
    """The MCP arm must replicate `route_and_load`'s routing path, not bypass
    it with raw `match_keywords`. Each routing branch (cache hit, keyword
    override, ambiguous veto, meta-query, cache-miss fallback) must surface
    via `mcp_meta["routing_path"]` so the report can show how many bench
    queries reflect real production routing vs. the deterministic fallback."""

    @staticmethod
    def _patches(*, lookup_cache_result, keyword_veto_result, is_meta, match_keywords_result):
        """Build a context manager that patches the production routing
        primitives for build_mcp_system_prompt. The patched _load_and_enrich
        returns a dummy prompt + empty skill/implant/rule sets so we only
        observe the routing-decision logic.
        """
        import asyncio
        from unittest.mock import patch, AsyncMock, MagicMock

        # router.lookup_cache returns a RouterDecision-like object or None.
        cached_decision = None
        if lookup_cache_result is not None:
            cached_decision = MagicMock()
            cached_decision.target_agent = lookup_cache_result

        async def _async_load(agent_name, query, history, *_):
            return (f"prompt-for-{agent_name}", "ctx-hash", [], [], [], "lite")

        async def _async_lookup(query, ctx):
            return cached_decision

        fake_router = MagicMock()
        fake_router.lookup_cache = AsyncMock(side_effect=_async_lookup)
        fake_router.keyword_veto = MagicMock(return_value=keyword_veto_result)
        fake_router.match_keywords = MagicMock(return_value=match_keywords_result)
        return patch.multiple(
            "evals.runners.run_mcp_vs_vanilla",
            _get_router=MagicMock(return_value=fake_router),
            _strip_platform_instructions=MagicMock(side_effect=lambda p: p),
        ), patch.multiple(
            "src.server",
            _load_and_enrich=AsyncMock(side_effect=_async_load),
            _is_meta_query=MagicMock(return_value=is_meta),
        )

    def _run(self, *, pick_agent=None, **kwargs) -> dict:
        import asyncio
        from evals.runners.run_mcp_vs_vanilla import build_mcp_system_prompt
        patches = self._patches(**kwargs)
        # ExitStack-like manual entry since _patches returns a tuple
        ctx0 = patches[0]
        ctx1 = patches[1]
        with ctx0, ctx1:
            _prompt, meta = asyncio.run(build_mcp_system_prompt("any query", pick_agent=pick_agent))
        return meta

    def test_cache_hit_no_veto(self):
        meta = self._run(
            lookup_cache_result="software_engineer",
            keyword_veto_result=None,
            is_meta=False,
            match_keywords_result=[],
        )
        assert meta["agent"] == "software_engineer"
        assert meta["routing_path"] == "cache_hit"

    def test_cache_hit_keyword_override(self):
        meta = self._run(
            lookup_cache_result="software_engineer",
            keyword_veto_result="lawyer",
            is_meta=False,
            match_keywords_result=[],
        )
        assert meta["agent"] == "lawyer"
        assert meta["routing_path"] == "keyword_override_cached"

    def test_cache_hit_ambiguous_veto_falls_back_to_keyword(self):
        from src.engine.router import KEYWORD_VETO_ROUTE_REQUIRED
        meta = self._run(
            lookup_cache_result="software_engineer",
            keyword_veto_result=KEYWORD_VETO_ROUTE_REQUIRED,
            is_meta=False,
            match_keywords_result=[("data_analyst", 3)],
        )
        assert meta["agent"] == "data_analyst"
        assert meta["routing_path"] == "keyword_fallback_after_ambiguous_veto"

    def test_cache_miss_meta_query_picks_universal_agent(self):
        meta = self._run(
            lookup_cache_result=None,
            keyword_veto_result=None,
            is_meta=True,
            match_keywords_result=[("software_engineer", 5)],  # ignored — meta wins
        )
        assert meta["agent"] == "universal_agent"
        assert meta["routing_path"] == "meta_query"

    def test_cache_miss_non_meta_uses_keyword_fallback(self):
        meta = self._run(
            lookup_cache_result=None,
            keyword_veto_result=None,
            is_meta=False,
            match_keywords_result=[("debate_moderator", 2)],
        )
        assert meta["agent"] == "debate_moderator"
        assert meta["routing_path"] == "keyword_fallback"

    def test_cache_miss_no_keyword_hits_falls_back_to_universal_agent(self):
        meta = self._run(
            lookup_cache_result=None,
            keyword_veto_result=None,
            is_meta=False,
            match_keywords_result=[],
        )
        assert meta["agent"] == "universal_agent"
        assert meta["routing_path"] == "keyword_fallback"

    def test_cache_miss_uses_llm_picker_when_available(self):
        """With a picker wired in (production parity), cache-miss routing goes to
        the LLM-picker, not the keyword stand-in — the keyword hit is ignored."""
        async def _pick(_q):
            return "system_architect"
        meta = self._run(
            pick_agent=_pick,
            lookup_cache_result=None,
            keyword_veto_result=None,
            is_meta=False,
            match_keywords_result=[("debate_moderator", 9)],  # ignored when picker present
        )
        assert meta["agent"] == "system_architect"
        assert meta["routing_path"] == "llm_picker"

    def test_ambiguous_veto_uses_llm_picker_when_available(self):
        from src.engine.router import KEYWORD_VETO_ROUTE_REQUIRED
        async def _pick(_q):
            return "data_analyst"
        meta = self._run(
            pick_agent=_pick,
            lookup_cache_result="software_engineer",
            keyword_veto_result=KEYWORD_VETO_ROUTE_REQUIRED,
            is_meta=False,
            match_keywords_result=[("debate_moderator", 9)],  # ignored when picker present
        )
        assert meta["agent"] == "data_analyst"
        assert meta["routing_path"] == "llm_picker_after_ambiguous_veto"

    def test_meta_query_ignores_picker(self):
        """Meta-queries route to universal_agent regardless of any picker."""
        async def _pick(_q):
            raise AssertionError("picker must not be called for a meta-query")
        meta = self._run(
            pick_agent=_pick,
            lookup_cache_result=None,
            keyword_veto_result=None,
            is_meta=True,
            match_keywords_result=[],
        )
        assert meta["agent"] == "universal_agent"
        assert meta["routing_path"] == "meta_query"


class TestLlmPickAgent:
    """`_llm_pick_agent` maps the arm model's reply to a catalog agent name,
    falls back to universal_agent on an unrecognized/empty reply, and caches
    per query so the order-swap and multi-N re-runs don't repay the call."""

    def _pick(self, reply_text, catalog_names, *, query="q-unique"):
        import asyncio
        from unittest.mock import patch, MagicMock
        import evals.runners.run_mcp_vs_vanilla as mod

        fake_router = MagicMock()
        fake_router.get_agent_catalog = MagicMock(
            return_value=[{"name": n, "role": f"{n} role"} for n in catalog_names]
        )

        async def _complete(_client, _model, _query, _system, _max_tokens):
            return reply_text, {}, 1.0

        provider = MagicMock()
        provider.complete = _complete  # real async fn; _with_retries awaits it

        with patch.object(mod, "_get_router", MagicMock(return_value=fake_router)):
            mod._PICKER_CACHE.clear()
            return asyncio.run(mod._llm_pick_agent(provider, None, "m", query))

    def test_exact_name_reply(self):
        assert self._pick("system_architect", ["system_architect", "data_analyst"]) == "system_architect"

    def test_name_embedded_in_sentence(self):
        assert self._pick("The best fit is data_analyst.", ["system_architect", "data_analyst"]) == "data_analyst"

    def test_unrecognized_reply_falls_back_to_universal_agent(self):
        assert self._pick("none of the above", ["system_architect", "data_analyst"]) == "universal_agent"

    def test_empty_reply_falls_back_to_universal_agent(self):
        assert self._pick("   ", ["system_architect"]) == "universal_agent"


class TestTransientHfErrorClassification:
    """Dataset-load retries must fire on transient HF/network failures (5xx,
    429, connection/timeout) but NOT on deterministic ones (404, auth)."""

    @staticmethod
    def _http(code):
        class _Resp:
            status_code = code
        class _Err(Exception):
            response = _Resp()
        return _Err(f"http {code}")

    def test_5xx_is_transient(self):
        from evals.runners.run_mcp_vs_vanilla import _is_transient_hf_error
        assert _is_transient_hf_error(self._http(504)) is True
        assert _is_transient_hf_error(self._http(500)) is True

    def test_429_is_transient(self):
        from evals.runners.run_mcp_vs_vanilla import _is_transient_hf_error
        assert _is_transient_hf_error(self._http(429)) is True

    def test_4xx_not_transient(self):
        from evals.runners.run_mcp_vs_vanilla import _is_transient_hf_error
        assert _is_transient_hf_error(self._http(404)) is False
        assert _is_transient_hf_error(self._http(403)) is False

    def test_connection_error_by_name_is_transient(self):
        from evals.runners.run_mcp_vs_vanilla import _is_transient_hf_error
        class ConnectTimeout(Exception):
            pass
        assert _is_transient_hf_error(ConnectTimeout("x")) is True

    def test_plain_exception_not_transient(self):
        from evals.runners.run_mcp_vs_vanilla import _is_transient_hf_error
        assert _is_transient_hf_error(ValueError("nope")) is False


class TestSampleQueriesRetry:
    """A transient HF 504 during dataset load is retried (fixed seed ⇒ the
    retried load is deterministic); a non-transient 404 propagates."""

    class _DS:
        def __init__(self, rows):
            self.rows = rows
        def shuffle(self, **_kwargs):
            return self
        def __iter__(self):
            return iter(self.rows)

    class _Spec:
        hf_id = "x"
        config = None
        split = "train"
        @staticmethod
        def extract_query(row):
            return row["q"]

    def _patch(self, loader, monkeypatch):
        import evals.runners.run_mcp_vs_vanilla as mod
        monkeypatch.setattr(mod, "DATASETS", {"wildbench": self._Spec()})
        monkeypatch.setattr(mod, "_require_load_dataset", lambda: loader)
        monkeypatch.setattr(mod.time, "sleep", lambda *_a, **_k: None)  # skip real backoff
        return mod

    def test_retries_then_succeeds(self, monkeypatch):
        class _Resp:
            status_code = 504
        class _HfErr(Exception):
            response = _Resp()
        calls = {"n": 0}

        def loader(*_a, **_k):
            calls["n"] += 1
            if calls["n"] == 1:
                raise _HfErr("504 Gateway Time-out")
            return TestSampleQueriesRetry._DS(
                [{"q": f"this is usable query number {i}"} for i in range(10)]
            )

        mod = self._patch(loader, monkeypatch)
        out = mod.sample_queries("wildbench", 3, seed=42)
        assert calls["n"] == 2          # failed once, retried, succeeded
        assert len(out) == 3

    def test_non_transient_propagates(self, monkeypatch):
        import pytest
        class _Resp:
            status_code = 404
        class _HfErr(Exception):
            response = _Resp()

        def loader(*_a, **_k):
            raise _HfErr("404 not found")

        mod = self._patch(loader, monkeypatch)
        with pytest.raises(_HfErr):
            mod.sample_queries("wildbench", 3, seed=42)


class TestScoreMarginAggregation:
    """Swap-averaged score margin in aggregate_with_swap: additive L/R position
    bias cancels; ε dead-zone falls back to the holistic winner; missing scores
    degrade to 'unscored' without touching the winner-based `final`."""

    @staticmethod
    def _scores(left, right):
        keys = ["helpfulness", "correctness", "depth", "structure", "intent_fit"]
        d = {}
        for i, k in enumerate(keys):
            d["left_" + k] = left[i]
            d["right_" + k] = right[i]
        return d

    def _call(self, winner, scores):
        from evals.judges.pairwise_judge import JudgeCall
        return JudgeCall(winner=winner, reasoning="r", criterion_scores=scores, usage={})

    def test_swap_cancels_additive_position_bias(self):
        from evals.judges.pairwise_judge import aggregate_with_swap
        # True totals: vanilla=10, mcp=11 (mcp better by 1). Inject +1/criterion
        # bias on whatever sits LEFT. pos1 left=vanilla, pos2 left=mcp.
        pos1 = self._call("left", self._scores((3, 3, 3, 3, 3), (2, 2, 2, 2, 3)))   # left=van+bias=15, right=mcp=11
        pos2 = self._call("left", self._scores((3, 3, 3, 3, 4), (2, 2, 2, 2, 2)))   # left=mcp+bias=16, right=van=10
        v = aggregate_with_swap(pos1=pos1, pos2=pos2, pos1_left_is="vanilla")
        assert v.mcp_avg == 13.5 and v.vanilla_avg == 12.5
        assert v.margin == 1.0            # recovers the true +1, bias cancelled
        assert v.final_by_score == "mcp"

    def test_epsilon_dead_zone_falls_back_to_holistic(self):
        from evals.judges.pairwise_judge import aggregate_with_swap
        pos1 = self._call("left", self._scores((3, 3, 3, 3, 3), (2, 2, 2, 2, 3)))
        pos2 = self._call("left", self._scores((3, 3, 3, 3, 4), (2, 2, 2, 2, 2)))
        # margin = +1.0; with ε>=1 it's inside the dead-zone → use holistic `final`.
        # pos1 winner=left→vanilla, pos2 winner=left→mcp → contradiction → final=tie.
        v = aggregate_with_swap(pos1=pos1, pos2=pos2, pos1_left_is="vanilla", epsilon=2.0)
        assert v.margin == 1.0
        assert v.final == "tie" and v.contradicted is True
        assert v.final_by_score == "tie"          # fell back to holistic
        v0 = aggregate_with_swap(pos1=pos1, pos2=pos2, pos1_left_is="vanilla", epsilon=0.0)
        assert v0.final_by_score == "mcp"         # outside dead-zone → margin decides

    def test_unscored_when_scores_missing(self):
        from evals.judges.pairwise_judge import aggregate_with_swap
        v = aggregate_with_swap(pos1=self._call("left", {}), pos2=self._call("right", {}), pos1_left_is="vanilla")
        assert v.margin is None and v.mcp_avg is None and v.vanilla_avg is None
        assert v.final_by_score == "unscored"
        assert v.final == "vanilla"               # winner-based verdict untouched

    def test_margin_sign_flips_with_pos1_left_is(self):
        from evals.judges.pairwise_judge import aggregate_with_swap
        p1 = self._call("left", self._scores((5, 5, 5, 5, 5), (1, 1, 1, 1, 1)))
        p2 = self._call("left", self._scores((4, 4, 4, 4, 4), (2, 2, 2, 2, 2)))
        m_v = aggregate_with_swap(pos1=p1, pos2=p2, pos1_left_is="vanilla").margin
        m_m = aggregate_with_swap(pos1=p1, pos2=p2, pos1_left_is="mcp").margin
        assert m_v == -m_m and m_v != 0          # which arm is "mcp" only flips the sign


class TestTemplateEscaping:
    """User-supplied query/response text must be HTML-escaped to prevent XSS."""

    def _render(self, query: str, response_text: str) -> str:
        from evals.runners.run_mcp_vs_vanilla import (
            BenchmarkResult,
            QueryRun,
            TrialResult,
            render_html,
        )
        from evals.runners._providers import PRICING
        from evals.judges.pairwise_judge import aggregate_with_swap

        jc_left = _jc("left")
        jc_right = _jc("right")
        verdict = aggregate_with_swap(pos1=jc_left, pos2=jc_right, pos1_left_is="vanilla")

        usage = {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
        van = TrialResult(arm="vanilla", response_text=response_text, usage=usage, latency_ms=10)
        mcp = TrialResult(
            arm="mcp",
            response_text=response_text,
            usage=usage,
            latency_ms=10,
            mcp_meta={"agent": "x", "tier": "lite", "skills_loaded": [], "implants_loaded": [], "rules_loaded": []},
        )
        run = QueryRun(idx=1, query=query, stream_idx=0, vanilla=van, mcp=mcp, verdict=verdict)

        result = BenchmarkResult(
            config={
                "provider": "openai",
                "provider_notes": "test provider",
                "model": "m",
                "judge_model": "m",
                "seed": 0,
                "dataset": "d",
                "commit_sha": "sha",
                "max_tokens": 1,
            },
            runs=[run],
            dataset_hash="hash",
            wall_time_s=0.0,
            arm_pricing=PRICING["openai"],
            judge_pricing=PRICING["openai"],
        )
        template_path = REPO_ROOT / "evals" / "templates" / "report.html.j2"
        return render_html(result, template_path)

    def test_script_tag_in_query_is_escaped(self):
        pytest.importorskip("jinja2")
        html = self._render('<script>alert("xss")</script>', "ok")
        assert "<script>alert" not in html
        assert "&lt;script&gt;alert" in html

    def test_script_tag_in_response_is_escaped(self):
        pytest.importorskip("jinja2")
        html = self._render("benign", '<img src=x onerror="alert(1)">')
        assert '<img src=x onerror="alert(1)">' not in html
        assert "&lt;img src=x onerror=" in html

    def test_html_entities_in_query_are_preserved_literally(self):
        pytest.importorskip("jinja2")
        html = self._render("Hello & welcome <user>", "ok")
        assert "Hello &amp; welcome &lt;user&gt;" in html

    def test_judge_verdicts_use_boxing_round_corner_labels(self):
        """Judge verdicts render in boxing terms: Round N + left/right corner,
        not Position N + LEFT/RIGHT. winner shows the corner ('left corner'/
        'right corner'), and the swap seating stays truthful per round."""
        pytest.importorskip("jinja2")
        html = self._render("benign query", "ok")  # pos1 winner=left, pos2 winner=right
        # New vocabulary present
        assert "Round 1" in html and "Round 2" in html
        assert "left corner=vanilla" in html and "right corner=mcp" in html   # Round 1 seating
        assert "left corner=mcp" in html and "right corner=vanilla" in html   # Round 2 seating (swap)
        assert "<code>left corner</code>" in html   # pos1 winner=left → left corner
        assert "<code>right corner</code>" in html  # pos2 winner=right → right corner
        # Old vocabulary gone
        assert "Position 1" not in html and "Position 2" not in html
        assert "LEFT=" not in html and "RIGHT=" not in html
