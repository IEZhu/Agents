"""
Provider abstraction: OpenAI vs Anthropic vs a local OpenAI-compatible server.

All providers expose the same surface:
  - async `complete(client, model, query, system_prompt | None, max_tokens) -> (text, usage_dict, latency_ms)`
  - sync `call_judge(client, query, left, right, model, system_prompt, max_tokens, verdict_schema) -> (payload_dict, usage_dict)`
  - `pricing` dict (USD per 1M tokens)
  - `default_model` / `default_judge_model`
  - client factories

Usage normalisation:
  All token counts are mapped to a unified 4-field dict so the rest of the
  runner / report does not need to know which provider produced them:
    {input_tokens, output_tokens, cache_creation_input_tokens, cache_read_input_tokens}

  For OpenAI, `cache_creation_input_tokens` is always 0 (OpenAI does not bill
  cache creation separately) and `cache_read_input_tokens` mirrors
  `usage.prompt_tokens_details.cached_tokens` when present.
"""

from __future__ import annotations

import asyncio
import itertools
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

from evals.judges.pairwise_judge import JudgeValidationError


# --------------------------------------------------------------------------- #
# Agentic-harness contamination
# --------------------------------------------------------------------------- #
# The benchmark sends NO `tools`, so a clean Messages API completion can never
# contain tool-call XML or injected system-reminders. When the configured
# backend is an agentic relay (e.g. a Claude-Code-style proxy), it occasionally
# leaks that scaffolding into a plain completion — e.g.
# `<invocation><parameter name="command">pwd</parameter></invocation>` or a
# hallucinated `<system-reminder>...`. These markers detect that leak so the
# runner can re-roll the call and capture a faithful answer instead of garbage.
_HARNESS_ARTIFACT_MARKERS: tuple[str, ...] = (
    "<invocation>",
    "</invocation>",
    "<parameter name=",
    "<system-reminder>",
    "</system-reminder>",
    "<function_calls>",
    "<invoke name=",
    "antml:invoke",
    "antml:parameter",
)


def has_harness_artifacts(text: str | None) -> bool:
    """True if `text` carries agentic-harness scaffolding leaked by the backend.

    High-specificity multi-char markers only — legitimate content that uses `<`
    in code (generics, ``<summary>``, ``a < b``) does not match.
    """
    return bool(text) and any(m in text for m in _HARNESS_ARTIFACT_MARKERS)


class ContaminatedResponseError(RuntimeError):
    """An arm completion leaked agentic-harness scaffolding instead of a faithful
    answer. Raised so the runner re-rolls the call — the leak is intermittent
    backend behavior, the same class as the judge's malformed-verdict case.
    Subclasses RuntimeError; fail-fast survives once retries are exhausted.
    """


# --------------------------------------------------------------------------- #
# Usage normalisation
# --------------------------------------------------------------------------- #


def normalise_usage_anthropic(usage) -> dict[str, int]:
    return {
        "input_tokens": getattr(usage, "input_tokens", 0) or 0,
        "output_tokens": getattr(usage, "output_tokens", 0) or 0,
        "cache_creation_input_tokens": getattr(usage, "cache_creation_input_tokens", 0) or 0,
        "cache_read_input_tokens": getattr(usage, "cache_read_input_tokens", 0) or 0,
    }


def normalise_usage_openai(usage) -> dict[str, int]:
    prompt = getattr(usage, "prompt_tokens", 0) or 0
    completion = getattr(usage, "completion_tokens", 0) or 0
    details = getattr(usage, "prompt_tokens_details", None)
    cached = (getattr(details, "cached_tokens", 0) if details else 0) or 0
    return {
        "input_tokens": max(0, prompt - cached),
        "output_tokens": completion,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": cached,
    }


# --------------------------------------------------------------------------- #
# Pricing (USD per 1M tokens)
#
# All values are estimates — verify against current published pricing before
# using costs in this report for any decision.
# --------------------------------------------------------------------------- #


# Per-model pricing (USD per 1M tokens). All values are estimates — verify
# against current published pricing before using costs for any decision.
# Cache write/read rates for Anthropic follow the standard 1.25× / 0.1× input
# pattern (Reasoned, not verified for each individual model).
MODEL_PRICING: dict[str, dict[str, float]] = {
    # OpenAI — verified at developers.openai.com per-model pages.
    "gpt-4o": {"input": 2.50, "output": 10.00, "cache_read": 1.25, "cache_creation": 0.0},
    "gpt-4o-mini": {"input": 0.15, "output": 0.60, "cache_read": 0.075, "cache_creation": 0.0},
    "gpt-5.5": {"input": 5.00, "output": 30.00, "cache_read": 0.50, "cache_creation": 0.0},
    "gpt-5.5-pro": {"input": 30.00, "output": 180.00, "cache_read": 3.00, "cache_creation": 0.0},

    # Anthropic — verified input/output at platform.claude.com.
    "claude-haiku-4-5": {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_creation": 1.25},
    "claude-sonnet-4-6": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_creation": 3.75},
    "claude-sonnet-4-5": {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_creation": 3.75},
    "claude-opus-4-8": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_creation": 6.25},
    "claude-opus-4-7": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_creation": 6.25},
    "claude-opus-4-6": {"input": 5.00, "output": 25.00, "cache_read": 0.50, "cache_creation": 6.25},

    # Google Gemini — verified at ai.google.dev/gemini-api/docs/pricing (standard tier, prompts ≤200K).
    # Routed through OPENAI_BASE_URL when a local proxy exposes an OpenAI-compatible /v1 endpoint;
    # `cache_creation` is 0 — Gemini doesn't bill cache creation separately, matching OpenAI pattern.
    #
    # Some local proxies expose their own aliases (e.g. `gemini-3-pro-high`) that map to Google
    # models with specific reasoning-effort tiers. Pricing below uses Google's published Pro / Flash
    # rates as estimates — costs through a subscription-relaying proxy are phantom anyway.
    "gemini-3.1-pro-preview": {"input": 2.00, "output": 12.00, "cache_read": 0.20, "cache_creation": 0.0},
    "gemini-3.1-flash-lite": {"input": 0.25, "output": 1.50, "cache_read": 0.025, "cache_creation": 0.0},
    "gemini-3-pro-high": {"input": 2.00, "output": 12.00, "cache_read": 0.20, "cache_creation": 0.0},
    "gemini-3-pro-low": {"input": 2.00, "output": 12.00, "cache_read": 0.20, "cache_creation": 0.0},
    "gemini-3.1-pro-low": {"input": 2.00, "output": 12.00, "cache_read": 0.20, "cache_creation": 0.0},
    "gemini-3.1-pro-high": {"input": 2.00, "output": 12.00, "cache_read": 0.20, "cache_creation": 0.0},
    "gemini-3.5-flash-low": {"input": 0.25, "output": 1.50, "cache_read": 0.025, "cache_creation": 0.0},
    "gemini-3-flash": {"input": 0.25, "output": 1.50, "cache_read": 0.025, "cache_creation": 0.0},
}


LOCAL_PRICING: dict[str, float] = {"input": 0.0, "output": 0.0, "cache_read": 0.0, "cache_creation": 0.0}


def get_pricing(model: str, provider: str | None = None) -> dict[str, float]:
    """Look up pricing for a model. Tries exact match, then prefix match
    (handles dated snapshots like `gpt-4o-2024-11-20` mapping to `gpt-4o`).
    Falls back to a conservative high estimate so the report never under-reports.
    Models served by the `local` provider cost nothing per token; their tags
    (`qwen3:8b`, `gemma3:27b`) would otherwise hit the high fallback.
    """
    if provider == "local":
        return LOCAL_PRICING
    if model in MODEL_PRICING:
        return MODEL_PRICING[model]
    # Try prefix match — dated snapshots map to their alias.
    # Sort by alias length descending so the most specific alias wins
    # (e.g. `gpt-5.5-pro-2026-01-01` matches `gpt-5.5-pro`, not `gpt-5.5`).
    for alias, pricing in sorted(MODEL_PRICING.items(), key=lambda item: len(item[0]), reverse=True):
        if model.startswith((alias + "-", alias + "@")):
            return pricing
    # Conservative fallback — better to over-estimate than under-report.
    return {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_creation": 18.75}


# Backwards-compat alias — provider-level fallback, used when --model is the
# provider default. New callers should prefer `get_pricing(model)`.
PRICING: dict[str, dict[str, float]] = {
    "openai": MODEL_PRICING["gpt-4o"],
    "anthropic": MODEL_PRICING["claude-sonnet-4-6"],
    "local": LOCAL_PRICING,
}


def _is_reasoning_openai_model(model: str) -> bool:
    """True if the OpenAI model accepts the `reasoning_effort` parameter.

    gpt-5.x family + o-series are reasoning models. gpt-4o and earlier reject
    `reasoning_effort` with HTTP 400 (Verified at developers.openai.com docs).
    """
    m = model.lower()
    return m.startswith("gpt-5") or m.startswith("o1") or m.startswith("o3") or m.startswith("o4")


def _supports_temperature_anthropic(model: str) -> bool:
    """False if the model REJECTS the `temperature` parameter outright.

    Claude Opus 4.7 returns HTTP 400 `'temperature' is deprecated for this
    model.` Opus 4.8 inherits the same request surface — `temperature`/`top_p`/
    `top_k` were removed on Opus 4.7 and remain removed on 4.8 (per the Anthropic
    model-migration guide) — so it is denied too. Other Claude models (Sonnet
    4.6, Haiku 4.5, older Opus) still accept `temperature=0` — confirmed by an
    N=30 Sonnet 4.6 bench run.
    """
    m = model.lower()
    # Deny list grows as new models deprecate temperature.
    deny_prefixes = ("claude-opus-4-7", "claude-opus-4-8")
    return not any(m.startswith(p) for p in deny_prefixes)


# --------------------------------------------------------------------------- #
# Shared judge user-prompt template
# --------------------------------------------------------------------------- #


def judge_user_prompt(query: str, left: str, right: str) -> str:
    # The instruction is provider-neutral: Anthropic submits via the
    # `submit_verdict` tool (tool_choice forces it), and OpenAI returns a
    # JSON object via `response_format=json_schema`. Either path is "matching
    # the verdict schema", so we phrase it that way to avoid telling the
    # OpenAI path it can call a tool that the request never declared.
    return (
        f"USER QUERY:\n{query}\n\n"
        f"---\nLEFT:\n{left}\n\n"
        f"---\nRIGHT:\n{right}\n\n"
        f"---\nSubmit your verdict as a JSON object matching the verdict schema "
        f"(winner, reasoning, criterion_scores)."
    )


# --------------------------------------------------------------------------- #
# OpenAI implementations
# --------------------------------------------------------------------------- #


async def complete_openai(
    client,
    model: str,
    query: str,
    system_prompt: str | None,
    max_tokens: int,
    *,
    sample: bool = True,  # no effect: temperature is locked, see below
) -> tuple[str, dict[str, int], int]:
    # OpenAI's newer model family (gpt-5.x and reasoning models) requires three
    # mitigations vs the gpt-4 era:
    #   1. `max_tokens` is rejected — use `max_completion_tokens`.
    #   2. `temperature` is locked to the default (1); explicit `temperature=0`
    #      returns HTTP 400. We omit it; some non-determinism is accepted.
    #   3. `reasoning_effort` defaults to `"medium"`; reasoning tokens count
    #      against `max_completion_tokens` but are NOT included in
    #      `choices[0].message.content`. Without intervention the entire budget
    #      can be consumed by hidden reasoning, leaving an empty visible reply.
    #      We set `"none"` so the comparison is a clean prompt-engineering test
    #      (no reasoning confound between vanilla and MCP arms).
    # All three adjustments are forward-compatible: `max_completion_tokens` and
    # the default temperature are accepted by older models; `reasoning_effort`
    # is ignored by non-reasoning models per OpenAI's parameter contract.
    t0 = time.perf_counter()
    messages: list[dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": query})
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_completion_tokens": max_tokens,
    }
    if _is_reasoning_openai_model(model):
        kwargs["reasoning_effort"] = "none"
    response = await client.chat.completions.create(**kwargs)
    latency_ms = int((time.perf_counter() - t0) * 1000)
    text = response.choices[0].message.content or ""
    if has_harness_artifacts(text):
        raise ContaminatedResponseError(
            f"OpenAI completion leaked agentic-harness scaffolding; re-rolling "
            f"(finish_reason={response.choices[0].finish_reason})"
        )
    return text, normalise_usage_openai(response.usage), latency_ms


def call_judge_openai(
    client,
    query: str,
    left: str,
    right: str,
    model: str,
    system_prompt: str,
    max_tokens: int,
    verdict_schema: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, int]]:
    # Uses `response_format=json_schema` (the modern OpenAI structured-outputs
    # pattern) instead of `tools` + `tool_choice`. Two reasons:
    #   1. Function-calling does NOT pass through reliably via OpenAI-compat proxy
    #      layers that relay to Gemini (observed: finish_reason=None, empty tool_calls).
    #   2. `tools + reasoning_effort` was banned on gpt-5.5 anyway; json_schema
    #      avoids that conflict entirely.
    # On gpt-5.x judges we also pin `reasoning_effort="none"` for the same reason
    # `complete_openai` does: the default `"medium"` makes hidden reasoning
    # tokens count against `max_completion_tokens`, and at the judge budget that
    # has been observed to consume the entire allowance and leave
    # `message.content` empty — which then trips the empty-content guard below.
    kwargs: dict[str, Any] = {
        "model": model,
        "max_completion_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": judge_user_prompt(query, left, right)},
        ],
        "response_format": {
            "type": "json_schema",
            "json_schema": {
                "name": verdict_schema["name"],
                "schema": verdict_schema["input_schema"],
                # `strict: true` enforces schema validity on OpenAI side. Drop
                # it: Gemini through an OpenAI-compat proxy layer chokes on
                # strict nested-object schemas (finish_reason=malformed_function_call).
                # We still get JSON via prompting + provider's best-effort
                # structured output; we json.loads + validate downstream.
            },
        },
    }
    if _is_reasoning_openai_model(model):
        kwargs["reasoning_effort"] = "none"
    response = client.chat.completions.create(**kwargs)
    choice = response.choices[0]
    content = choice.message.content
    if not content:
        raise JudgeValidationError(
            f"OpenAI judge returned empty content; finish_reason={choice.finish_reason}"
        )
    try:
        args = json.loads(content)
    except json.JSONDecodeError as exc:
        raise JudgeValidationError(f"OpenAI judge content is not valid JSON: {exc}; first 200 chars: {content[:200]!r}") from exc
    if not isinstance(args, dict):
        # Valid JSON but not an object (e.g. `[]` / `null` from a proxy) — would
        # raise an untyped AttributeError in _validate_verdict_payload's `.get`,
        # bypassing the JudgeValidationError retry path. Type it here instead.
        raise JudgeValidationError(
            f"OpenAI judge content is valid JSON but not an object "
            f"(got {type(args).__name__}); first 200 chars: {content[:200]!r}"
        )
    return args, normalise_usage_openai(response.usage)


# --------------------------------------------------------------------------- #
# Anthropic implementations
# --------------------------------------------------------------------------- #


async def complete_anthropic(
    client,
    model: str,
    query: str,
    system_prompt: str | None,
    max_tokens: int,
    *,
    sample: bool = True,  # no effect: temperature is 0 or locked, see below
) -> tuple[str, dict[str, int], int]:
    # Opus 4.7/4.8 deprecate `temperature` — see _supports_temperature_anthropic.
    # For models that still accept it, we keep `temperature=0` for determinism.
    t0 = time.perf_counter()
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "messages": [{"role": "user", "content": query}],
    }
    if _supports_temperature_anthropic(model):
        kwargs["temperature"] = 0
    if system_prompt:
        kwargs["system"] = [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}]
    response = await client.messages.create(**kwargs)
    latency_ms = int((time.perf_counter() - t0) * 1000)
    text = "".join(getattr(b, "text", "") for b in response.content if getattr(b, "type", "") == "text")
    if has_harness_artifacts(text):
        raise ContaminatedResponseError(
            f"Anthropic completion leaked agentic-harness scaffolding; re-rolling "
            f"(stop_reason={response.stop_reason})"
        )
    return text, normalise_usage_anthropic(response.usage), latency_ms


def call_judge_anthropic(
    client,
    query: str,
    left: str,
    right: str,
    model: str,
    system_prompt: str,
    max_tokens: int,
    verdict_schema: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, int]]:
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": max_tokens,
        "system": [{"type": "text", "text": system_prompt, "cache_control": {"type": "ephemeral"}}],
        "tools": [verdict_schema],
        "tool_choice": {"type": "tool", "name": verdict_schema["name"]},
        "messages": [{"role": "user", "content": judge_user_prompt(query, left, right)}],
    }
    if _supports_temperature_anthropic(model):
        kwargs["temperature"] = 0
    response = client.messages.create(**kwargs)
    payload: dict[str, Any] | None = None
    for block in response.content:
        if getattr(block, "type", None) == "tool_use" and block.name == verdict_schema["name"]:
            payload = dict(block.input)
            break
    if payload is None:
        raise JudgeValidationError(f"Anthropic judge returned no tool_use; stop_reason={response.stop_reason}")
    return payload, normalise_usage_anthropic(response.usage)


# --------------------------------------------------------------------------- #
# Local implementations (OpenAI-compatible server: Ollama, LM Studio,
# llama.cpp `llama-server`, `mlx_lm.server`)
# --------------------------------------------------------------------------- #
# Behaviour pinned against Ollama 0.33.3 on 2026-09-23 (evals/LOCAL_MODELS.md).
# Only Ollama was verified; other servers are expected to work but unchecked.
#   * `max_completion_tokens` is ignored (a 20-token cap produced 1,641 tokens),
#     so the cap goes in `max_tokens`.
#   * Thinking models (qwen3) put their reasoning in `message.reasoning`; the
#     top-level `think: false` is ignored on /v1, `reasoning_effort: "none"`
#     turns it off. Left on, reasoning ate a 200-token judge budget and returned
#     empty content with finish_reason=length.
#   * `response_format` json_schema is honoured, and temperature=0 gives
#     byte-identical repeats whatever the seed (greedy decoding).
# Some servers inline reasoning as <think>…</think> instead; it is stripped,
# including a block left unclosed when max_tokens cut the reasoning short.

LOCAL_BASE_URL_ENV = "LOCAL_LLM_BASE_URL"
LOCAL_DEFAULT_BASE_URL = "http://localhost:11434/v1"
LOCAL_DEFAULT_MODEL = "qwen3:8b"
# LOCAL_LLM_THINKING=1 applies only to calls at least this large (answers).
# Router picks (32 tokens), graders (300) and judges keep thinking off: with it
# on they return nothing, which would crash a run or grade every answer FAIL.
LOCAL_THINKING_MIN_TOKENS = 1024
_THINK_BLOCK = re.compile(r"<think>.*?(?:</think>\s*|\Z)", re.DOTALL | re.IGNORECASE)
_call_counter = itertools.count()


def _local_request(
    model: str, messages: list[dict[str, Any]], max_tokens: int, sample: bool = True,
) -> dict[str, Any]:
    # LOCAL_LLM_TEMPERATURE samples answers only. Graders, judges and router
    # picks run greedy (sample=False), so an arm difference under sampling comes
    # from the answers, not from evaluator or routing noise.
    temperature = float(os.getenv("LOCAL_LLM_TEMPERATURE", "0")) if sample else 0.0
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "temperature": temperature,
    }
    if temperature > 0:
        # Sampling: a fresh seed per call so repeated samples and retry re-rolls
        # differ, while a whole run stays reproducible (calls are sequential in
        # the rule A/B). At temperature 0 decoding is greedy and a seed changes
        # nothing, so none is sent.
        kwargs["seed"] = int(os.getenv("LOCAL_LLM_SEED", "7")) + next(_call_counter)
    thinking = os.getenv("LOCAL_LLM_THINKING", "0") == "1" and max_tokens >= LOCAL_THINKING_MIN_TOKENS
    if not thinking:
        kwargs["reasoning_effort"] = "none"
    return kwargs


def _local_text(response, what: str) -> str:
    choice = response.choices[0]
    text = _THINK_BLOCK.sub("", choice.message.content or "").strip()
    if not text and choice.finish_reason == "length":
        raise RuntimeError(
            f"local {what} hit max_tokens with no visible text (reasoning likely consumed "
            f"the budget); raise max_tokens or unset LOCAL_LLM_THINKING"
        )
    return text


async def complete_local(
    client,
    model: str,
    query: str,
    system_prompt: str | None,
    max_tokens: int,
    *,
    sample: bool = True,
) -> tuple[str, dict[str, int], int]:
    t0 = time.perf_counter()
    messages: list[dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": query})
    response = await client.chat.completions.create(**_local_request(model, messages, max_tokens, sample))
    latency_ms = int((time.perf_counter() - t0) * 1000)
    text = _local_text(response, "completion")
    if has_harness_artifacts(text):
        raise ContaminatedResponseError(
            f"local completion leaked agentic-harness scaffolding; re-rolling "
            f"(finish_reason={response.choices[0].finish_reason})"
        )
    return text, normalise_usage_openai(response.usage), latency_ms


def call_judge_local(
    client,
    query: str,
    left: str,
    right: str,
    model: str,
    system_prompt: str,
    max_tokens: int,
    verdict_schema: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, int]]:
    kwargs = _local_request(
        model,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": judge_user_prompt(query, left, right)},
        ],
        max_tokens,
        sample=False,
    )
    # Judges never think, whatever the budget: run_mcp_vs_vanilla's judge budget
    # defaults to 4096, above LOCAL_THINKING_MIN_TOKENS.
    kwargs["reasoning_effort"] = "none"
    kwargs["response_format"] = {
        "type": "json_schema",
        "json_schema": {"name": verdict_schema["name"], "schema": verdict_schema["input_schema"]},
    }
    response = client.chat.completions.create(**kwargs)
    try:
        content = _local_text(response, "judge")
    except RuntimeError as exc:
        raise JudgeValidationError(str(exc)) from exc
    args = _first_json_object(content)
    if args is None:
        raise JudgeValidationError(f"local judge returned no JSON object; first 200 chars: {content[:200]!r}")
    return args, normalise_usage_openai(response.usage)


def _first_json_object(text: str) -> dict[str, Any] | None:
    """First JSON object in *text*, skipping prose that may itself contain braces.

    Servers without structured-output support wrap the verdict in prose; decoding
    from each `{` in turn finds it even when the prose says "use {x}" first.
    """
    decoder = json.JSONDecoder()
    for i, ch in enumerate(text):
        if ch != "{":
            continue
        try:
            obj, _ = decoder.raw_decode(text, i)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


# --------------------------------------------------------------------------- #
# OpenRouter: hosted open-weight models behind one OpenAI-compatible API
# --------------------------------------------------------------------------- #
# Request shape checked against openrouter.ai/docs on 2026-09-24:
#   * `reasoning: {"enabled": false}` turns thinking off. Models whose reasoning
#     is mandatory (claude-opus-5.5: "Reasoning is mandatory for this endpoint")
#     reject it; OPENROUTER_REASONING=low|medium|high asks for that effort
#     instead and keeps the reasoning out of the answer text.
#   * `provider.order` + `allow_fallbacks: false` keeps every call on the listed
#     endpoints. Endpoint slugs name a host and a precision (`novita/bf16`,
#     `deepinfra/bf16`), so one list can pin several models: each call goes to
#     the first listed endpoint that serves its model. Unpinned, one run spreads
#     over hosts with different weights, a difference an A/B cannot tell from
#     an arm effect.
#   * `require_parameters: true` skips hosts that would silently drop `seed`,
#     `reasoning` or `response_format`.
# Unlike a local server, hosts batch requests, and a probe on 2026-09-24 got two
# different answers to one request at temperature 0 with a fixed seed from both
# novita/bf16 (gemma-4-31b-it) and deepinfra/bf16 (qwen3.8-27b).
#   * Hosts share a rate-limit pool across OpenRouter users and answer 429 for
#     minutes at a time ("temporarily rate-limited upstream"), longer than the
#     SDK's retries wait (its backoff is capped at 8 s).

OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
OPENROUTER_DEFAULT_MODEL = "google/gemma-4-31b-it"


def request_settings(provider_name: str) -> dict[str, str] | None:
    """Env-driven request settings beyond model and temperature, for run manifests.

    A resumed run must not mix answers or grades made with different settings.
    """
    if provider_name == "openrouter":
        return {"reasoning": os.getenv("OPENROUTER_REASONING", "off"), "seed": os.getenv("OPENROUTER_SEED", "7"),
                "grader_temperature": os.getenv("OPENROUTER_GRADER_TEMPERATURE", "0")}
    if provider_name == "local":
        return {"thinking": os.getenv("LOCAL_LLM_THINKING", "0"), "seed": os.getenv("LOCAL_LLM_SEED", "7")}
    return None


def openrouter_routing() -> dict[str, Any]:
    """`provider` routing object from OPENROUTER_PROVIDER (comma-separated endpoint slugs)."""
    routing: dict[str, Any] = {"require_parameters": True}
    order = [s.strip() for s in os.getenv("OPENROUTER_PROVIDER", "").split(",") if s.strip()]
    if order:
        routing.update(order=order, allow_fallbacks=False)
    return routing


def _openrouter_request(
    model: str, messages: list[dict[str, Any]], max_tokens: int, sample: bool = True,
) -> dict[str, Any]:
    # Same sampling contract as the local provider: OPENROUTER_TEMPERATURE applies
    # to answers only; graders and router picks (sample=False) run at 0. Parameters
    # no endpoint of a model accepts must be left out, or `require_parameters` finds
    # no endpoint: OPENROUTER_TEMPERATURE=default omits it for answers,
    # OPENROUTER_GRADER_TEMPERATURE=default for graders (only for a grader whose
    # endpoints reject it, since the endpoint default may sample), and
    # OPENROUTER_SEED=none omits the seed.
    if sample:
        temp_env = os.getenv("OPENROUTER_TEMPERATURE", "0")
        temperature = None if temp_env == "default" else float(temp_env)
    else:
        temperature = None if os.getenv("OPENROUTER_GRADER_TEMPERATURE", "0") == "default" else 0.0
    # Graders and router picks (sample=False) never think: their small budgets
    # would go to reasoning. So the grader must be a model that can turn it off.
    effort = os.getenv("OPENROUTER_REASONING", "off") if sample else "off"
    reasoning = {"enabled": False} if effort == "off" else {"effort": effort, "exclude": True}
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        "max_tokens": max_tokens,
        "extra_body": {"reasoning": reasoning, "provider": openrouter_routing()},
    }
    if temperature is not None:
        kwargs["temperature"] = temperature
    seed_env = os.getenv("OPENROUTER_SEED", "7")
    if seed_env != "none":
        # Hosts are not deterministic at temperature 0 (see above); a fixed seed
        # is the one lever left there. Sampled calls get a fresh seed each.
        seed = int(seed_env)
        kwargs["seed"] = seed + next(_call_counter) if temperature else seed
    return kwargs


async def _openrouter_create(client, kwargs: dict[str, Any]):
    """Create a completion, riding out upstream rate limits.

    429s are retried with a pause that doubles up to a minute, for at most
    OPENROUTER_RATE_LIMIT_WAIT seconds in total; other errors propagate.
    """
    from openai import RateLimitError

    budget = float(os.getenv("OPENROUTER_RATE_LIMIT_WAIT", "900"))
    delay, waited = 5.0, 0.0
    while True:
        try:
            return await client.chat.completions.create(**kwargs)
        except RateLimitError:
            pause = min(delay, budget - waited)
            if pause <= 0:
                raise
            await asyncio.sleep(pause)
            waited += pause
            delay = min(delay * 2, 60.0)


def _openrouter_text(response, what: str) -> str:
    choice = response.choices[0]
    text = _THINK_BLOCK.sub("", choice.message.content or "").strip()
    if not text and choice.finish_reason == "length":
        raise RuntimeError(f"openrouter {what} hit max_tokens with no visible text; raise max_tokens")
    return text


async def complete_openrouter(
    client,
    model: str,
    query: str,
    system_prompt: str | None,
    max_tokens: int,
    *,
    sample: bool = True,
) -> tuple[str, dict[str, int], int]:
    t0 = time.perf_counter()
    messages: list[dict[str, Any]] = []
    if system_prompt:
        messages.append({"role": "system", "content": system_prompt})
    messages.append({"role": "user", "content": query})
    response = await _openrouter_create(client, _openrouter_request(model, messages, max_tokens, sample))
    latency_ms = int((time.perf_counter() - t0) * 1000)
    text = _openrouter_text(response, "completion")
    if has_harness_artifacts(text):
        raise ContaminatedResponseError(
            f"openrouter completion leaked agentic-harness scaffolding; re-rolling "
            f"(finish_reason={response.choices[0].finish_reason})"
        )
    return text, normalise_usage_openai(response.usage), latency_ms


def call_judge_openrouter(
    client,
    query: str,
    left: str,
    right: str,
    model: str,
    system_prompt: str,
    max_tokens: int,
    verdict_schema: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, int]]:
    kwargs = _openrouter_request(
        model,
        [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": judge_user_prompt(query, left, right)},
        ],
        max_tokens,
        sample=False,
    )
    kwargs["response_format"] = {
        "type": "json_schema",
        "json_schema": {"name": verdict_schema["name"], "schema": verdict_schema["input_schema"]},
    }
    response = client.chat.completions.create(**kwargs)
    try:
        content = _openrouter_text(response, "judge")
    except RuntimeError as exc:
        raise JudgeValidationError(str(exc)) from exc
    args = _first_json_object(content)
    if args is None:
        raise JudgeValidationError(f"openrouter judge returned no JSON object; first 200 chars: {content[:200]!r}")
    return args, normalise_usage_openai(response.usage)


# --------------------------------------------------------------------------- #
# Dispatch
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ProviderImpl:
    name: str
    default_model: str
    default_judge_model: str
    make_async_client: Callable[[], Any]
    make_sync_client: Callable[[], Any]
    complete: Callable  # async (client, model, query, system_prompt, max_tokens, *, sample=True)
    call_judge: Callable  # sync
    pricing: dict[str, float]
    env_key: str  # name of the API-key env var; "" when no key is required
    notes: str  # disclaimer text shown in the HTML report


def _make_openai_provider() -> ProviderImpl:
    from openai import AsyncOpenAI, OpenAI

    return ProviderImpl(
        name="openai",
        default_model="gpt-4o",
        default_judge_model="gpt-4o",
        make_async_client=AsyncOpenAI,
        make_sync_client=OpenAI,
        complete=complete_openai,
        call_judge=call_judge_openai,
        pricing=PRICING["openai"],
        env_key="OPENAI_API_KEY",
        notes="MCP system prompts in this repo are Claude-authored. Running them against OpenAI is valid as a generalisation test, but absolute quality numbers may differ from Claude-on-Claude.",
    )


def _make_anthropic_provider() -> ProviderImpl:
    from anthropic import Anthropic, AsyncAnthropic

    return ProviderImpl(
        name="anthropic",
        default_model="claude-sonnet-4-6",
        default_judge_model="claude-sonnet-4-6",
        make_async_client=AsyncAnthropic,
        make_sync_client=Anthropic,
        complete=complete_anthropic,
        call_judge=call_judge_anthropic,
        pricing=PRICING["anthropic"],
        env_key="ANTHROPIC_API_KEY",
        notes="MCP system prompts in this repo are Claude-authored — comparison runs against the same Claude model the prompts were tuned for.",
    )


def _make_local_provider() -> ProviderImpl:
    from openai import AsyncOpenAI, OpenAI

    base_url = os.getenv(LOCAL_BASE_URL_ENV, LOCAL_DEFAULT_BASE_URL)
    model = os.getenv("LOCAL_LLM_MODEL", LOCAL_DEFAULT_MODEL)
    # Local servers ignore the key, but the SDK refuses to build a client without one.
    api_key = os.getenv("LOCAL_LLM_API_KEY", "local")
    # A 31B model on a laptop can take minutes for a long answer, and a model
    # swap between answer and judge adds load time on top.
    timeout = float(os.getenv("LOCAL_LLM_TIMEOUT", "900"))
    return ProviderImpl(
        name="local",
        default_model=model,
        default_judge_model=os.getenv("LOCAL_LLM_JUDGE_MODEL", model),
        make_async_client=lambda: AsyncOpenAI(base_url=base_url, api_key=api_key, timeout=timeout),
        make_sync_client=lambda: OpenAI(base_url=base_url, api_key=api_key, timeout=timeout),
        complete=complete_local,
        call_judge=call_judge_local,
        pricing=LOCAL_PRICING,
        env_key="",
        notes=(
            "Local open-weight model. MCP system prompts in this repo are Claude-authored, so "
            "absolute quality says little about Claude in production; use local runs for "
            "regressions and relative A/B deltas, and confirm decisions on the production model."
        ),
    )


def _make_openrouter_provider() -> ProviderImpl:
    from openai import AsyncOpenAI, OpenAI

    model = os.getenv("OPENROUTER_MODEL", OPENROUTER_DEFAULT_MODEL)
    api_key = os.getenv("OPENROUTER_API_KEY", "")
    timeout = float(os.getenv("OPENROUTER_TIMEOUT", "300"))
    # Concurrent runs meet 429s and upstream 5xx more often than a local server.
    retries = int(os.getenv("OPENROUTER_MAX_RETRIES", "6"))
    kwargs = {"base_url": OPENROUTER_BASE_URL, "api_key": api_key, "timeout": timeout, "max_retries": retries}
    return ProviderImpl(
        name="openrouter",
        default_model=model,
        default_judge_model=os.getenv("OPENROUTER_JUDGE_MODEL", model),
        make_async_client=lambda: AsyncOpenAI(**kwargs),
        make_sync_client=lambda: OpenAI(**kwargs),
        complete=complete_openrouter,
        call_judge=call_judge_openrouter,
        pricing=get_pricing(model),
        env_key="OPENROUTER_API_KEY",
        notes=(
            "Hosted open-weight model via OpenRouter. MCP system prompts in this repo are "
            "Claude-authored, so absolute quality says little about Claude in production; hosts "
            "are not deterministic at temperature 0, so compare arms against repeated baselines."
        ),
    )


_PROVIDERS = {
    "openai": _make_openai_provider,
    "anthropic": _make_anthropic_provider,
    "local": _make_local_provider,
    "openrouter": _make_openrouter_provider,
}


def judge_env_default(key: str, provider_name: str) -> str | None:
    """`JUDGE_PROVIDER` / `JUDGE_MODEL` env defaults, unless the run is local or OpenRouter.

    Those variables name first-party cloud judges (e.g. `claude-opus-4-8`).
    Applied to a local run they either send the judge to a cloud API the run is
    meant to avoid, or ask the local server for a model it doesn't have (HTTP
    404); OpenRouter names models differently (`anthropic/claude-...`). Such a
    run uses `LOCAL_LLM_JUDGE_MODEL` / `OPENROUTER_JUDGE_MODEL` or an explicit
    CLI flag instead.
    """
    return None if provider_name in ("local", "openrouter") else os.getenv(key)


def missing_credentials(provider: ProviderImpl) -> str | None:
    """Name of the unset API-key env var the provider needs, or None if ready."""
    if provider.env_key and not os.getenv(provider.env_key):
        return provider.env_key
    return None


def get_provider(name: str) -> ProviderImpl:
    if name not in _PROVIDERS:
        raise ValueError(f"unknown provider {name!r}; choices: {sorted(_PROVIDERS)}")
    return _PROVIDERS[name]()
