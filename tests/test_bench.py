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
        """Opus 4.7 deprecates temperature; other Claude models accept it."""
        from evals.runners._providers import _supports_temperature_anthropic

        # Deny — Opus 4.7 returns HTTP 400 on temperature.
        assert _supports_temperature_anthropic("claude-opus-4-7") is False
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

        for bad_value in (0, 6, -1, 100):
            bad = self._scores()
            bad["right_correctness"] = bad_value
            with pytest.raises(RuntimeError, match="out-of-range"):
                _validate_verdict_payload({"winner": "right", "reasoning": "x", "criterion_scores": bad})


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
