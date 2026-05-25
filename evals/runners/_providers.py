"""
Provider abstraction: OpenAI vs Anthropic.

Both providers expose the same surface:
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

import json
import time
from dataclasses import dataclass
from typing import Any, Callable


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


def get_pricing(model: str) -> dict[str, float]:
    """Look up pricing for a model. Tries exact match, then prefix match
    (handles dated snapshots like `gpt-4o-2024-11-20` mapping to `gpt-4o`).
    Falls back to a conservative high estimate so the report never under-reports.
    """
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

    Verified: Claude Opus 4.7 returns HTTP 400 `'temperature' is deprecated for
    this model.` Other Claude models (Sonnet 4.6, Haiku 4.5, older Opus) still
    accept `temperature=0` — confirmed by an N=30 Sonnet 4.6 bench run.
    """
    m = model.lower()
    # Deny list grows as new models deprecate temperature.
    deny_prefixes = ("claude-opus-4-7",)
    return not any(m.startswith(p) for p in deny_prefixes)


# --------------------------------------------------------------------------- #
# Shared judge user-prompt template
# --------------------------------------------------------------------------- #


def judge_user_prompt(query: str, left: str, right: str) -> str:
    return (
        f"USER QUERY:\n{query}\n\n"
        f"---\nLEFT:\n{left}\n\n"
        f"---\nRIGHT:\n{right}\n\n"
        f"---\nSubmit your verdict via the submit_verdict tool."
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
    #      avoids that conflict entirely. We still skip reasoning_effort here
    #      (safer; the judge doesn't need extra thinking depth).
    response = client.chat.completions.create(
        model=model,
        max_completion_tokens=max_tokens,
        messages=[
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": judge_user_prompt(query, left, right)},
        ],
        response_format={
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
    )
    choice = response.choices[0]
    content = choice.message.content
    if not content:
        raise RuntimeError(
            f"OpenAI judge returned empty content; finish_reason={choice.finish_reason}"
        )
    try:
        args = json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"OpenAI judge content is not valid JSON: {exc}; first 200 chars: {content[:200]!r}") from exc
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
) -> tuple[str, dict[str, int], int]:
    # Opus 4.7 deprecates `temperature` — see _supports_temperature_anthropic.
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
        raise RuntimeError(f"Anthropic judge returned no tool_use; stop_reason={response.stop_reason}")
    return payload, normalise_usage_anthropic(response.usage)


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
    complete: Callable  # async
    call_judge: Callable  # sync
    pricing: dict[str, float]
    env_key: str  # name of the API-key env var
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


_PROVIDERS = {
    "openai": _make_openai_provider,
    "anthropic": _make_anthropic_provider,
}


def get_provider(name: str) -> ProviderImpl:
    if name not in _PROVIDERS:
        raise ValueError(f"unknown provider {name!r}; choices: {sorted(_PROVIDERS)}")
    return _PROVIDERS[name]()
