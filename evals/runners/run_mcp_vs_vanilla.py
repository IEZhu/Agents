"""
Benchmark: Agents-Core MCP vs Vanilla LLM (provider-agnostic).

Pipeline (N user queries from a public HF dataset):
  1. Sample queries (deterministic via seed; streaming, no full download).
  2. For each query, run two arms in parallel:
       A) vanilla — provider.complete() with no system prompt
       B) mcp     — provider.complete() with system = enriched prompt built
                    in-process via SemanticRouter + _load_and_enrich, with
                    platform-only footer instructions stripped.
  3. Pairwise LLM-as-judge with positional swap (controls position bias).
     If either arm returned empty text, judge is skipped → synthetic TIE.
  4. Render single-file HTML report (Jinja2 template, Chart.js via CDN).

Provider selection:
    --provider openai     → uses OPENAI_API_KEY (default model: gpt-4o)
    --provider anthropic  → uses ANTHROPIC_API_KEY (default model: claude-sonnet-4-6)

Usage:
    python -m evals.runners.run_mcp_vs_vanilla [--n 10] [--seed 42]
        [--provider openai|anthropic] [--model ...] [--judge-model ...]
        [--dataset wildbench|lmsys_chat_1m|...]
        [--concurrency 8] [--max-tokens 2048] [--judge-max-tokens 4096]
        [--out evals/reports/<date>_mcp_vs_vanilla.html]
        [--save-json] [--dry-run]
"""

from __future__ import annotations

import argparse
import asyncio
import datetime as dt
import hashlib
import json
import logging
import os
import re
import statistics
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Awaitable, Callable, Literal

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.judges.pairwise_judge import (  # noqa: E402
    JudgeCall,
    JudgeValidationError,
    SwapVerdict,
    _CRITERIA,
    _SCORE_MAX as JUDGE_SCORE_MAX,
    _SCORE_MIN as JUDGE_SCORE_MIN,
    aggregate_with_swap,
    per_criterion_breakdown,
    run_judge,
)
from evals.runners._providers import ContaminatedResponseError, ProviderImpl, get_pricing, get_provider  # noqa: E402
from evals.scripts.fetch import DATASETS, _require_load_dataset  # noqa: E402

logger = logging.getLogger("bench")


@dataclass
class TrialResult:
    arm: Literal["vanilla", "mcp"]
    response_text: str
    usage: dict[str, int]
    latency_ms: int
    mcp_meta: dict[str, Any] | None = None


@dataclass
class QueryRun:
    idx: int
    query: str
    # Position in the shuffled streaming iterator at sample time — NOT the
    # HuggingFace split row index. Streaming + `.shuffle(buffer_size=…)`
    # discards the original row index, so this field cannot be passed back to
    # `evals.scripts.fetch --idx` to recover the source row; it only exists
    # for in-run debugging. Provenance lives in `dataset_hash` + the query
    # text itself.
    stream_idx: int
    vanilla: TrialResult
    mcp: TrialResult
    verdict: SwapVerdict


@dataclass
class BenchmarkResult:
    config: dict[str, Any]
    runs: list[QueryRun]
    dataset_hash: str
    wall_time_s: float
    # Pricing is split per role so cross-provider / different-model judge runs
    # bill arm tokens and judge tokens against the correct per-1M rates.
    arm_pricing: dict[str, float]
    judge_pricing: dict[str, float]
    generated_at: str = field(
        default_factory=lambda: dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    )


# --------------------------------------------------------------------------- #
# Sampling
# --------------------------------------------------------------------------- #


def _is_transient_hf_error(exc: BaseException) -> bool:
    """True for transient HuggingFace Hub / network failures worth retrying.

    Retry on 5xx and 429 (rate-limit) HTTP statuses and on connection/timeout
    transport errors. Do NOT retry 4xx like 404 (dataset/revision missing) or
    401/403 (auth) — those are deterministic, so a retry only wastes time.
    """
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is not None:
        return status == 429 or 500 <= status < 600
    # No HTTP status attached ⇒ connection/timeout/transport-level failure.
    name = type(exc).__name__
    return any(token in name for token in (
        "Timeout", "ConnectError", "ConnectionError", "ReadError",
        "TransportError", "ProtocolError",
    ))


def _retry_sync_transient(fn, *, what: str, attempts: int = 4, base_delay: float = 3.0):
    """Run a sync callable, retrying transient HF/network errors with backoff.

    HuggingFace Hub returns intermittent 5xx (e.g. 504 Gateway Time-out) when
    resolving dataset files. A fixed seed makes the retried call deterministic,
    so re-running the whole load+sample is safe. Non-transient errors (404,
    auth, schema) propagate immediately rather than burning the retry budget.
    """
    last: BaseException | None = None
    for attempt in range(attempts):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001 — re-raised below unless transient
            if not _is_transient_hf_error(exc) or attempt == attempts - 1:
                raise
            last = exc
            delay = base_delay * (2 ** attempt)
            logger.warning(
                "transient error during %s (%s); retrying in %.0fs (attempt %d/%d)",
                what, type(exc).__name__, delay, attempt + 2, attempts,
            )
            time.sleep(delay)
    assert last is not None  # unreachable: loop either returns or raises
    raise last


def sample_queries(dataset_key: str, n: int, seed: int) -> list[tuple[int, str]]:
    """Stream the dataset and yield N (stream_idx, query) pairs with a deterministic shuffle.

    `stream_idx` is the enumerate position after `.shuffle(buffer_size=…)`,
    NOT the original HuggingFace split row index. Don't pass it back to
    `evals.scripts.fetch --idx` — that consumer expects the unshuffled
    split index, which is unrecoverable once streaming + buffered shuffle
    has been applied.
    """
    if dataset_key not in DATASETS:
        raise SystemExit(f"unknown dataset {dataset_key!r}; choices: {sorted(DATASETS)}")
    spec = DATASETS[dataset_key]
    load_dataset = _require_load_dataset()

    def _load_and_sample() -> list[tuple[int, str]]:
        ds = load_dataset(spec.hf_id, name=spec.config, split=spec.split, streaming=True)
        ds = ds.shuffle(seed=seed, buffer_size=max(n * 50, 500))
        collected: list[tuple[int, str]] = []
        for i, row in enumerate(ds):
            try:
                q = spec.extract_query(row)
            except Exception:
                continue
            if not q or len(q.strip()) < 10:
                continue
            collected.append((i, q.strip()))
            if len(collected) >= n:
                break
        return collected

    # HF Hub 5xx/timeouts on dataset resolution are transient; the fixed seed
    # makes the retried load+shuffle deterministic, so retrying is safe.
    out = _retry_sync_transient(_load_and_sample, what=f"load+sample {dataset_key!r}")
    if len(out) < n:
        raise SystemExit(f"dataset {dataset_key} yielded only {len(out)}/{n} usable queries")
    return out


def dataset_hash(queries: list[tuple[int, str]]) -> str:
    h = hashlib.sha256()
    for idx, q in queries:
        h.update(f"{idx}\x00{q}\x01".encode("utf-8"))
    return h.hexdigest()[:16]


# --------------------------------------------------------------------------- #
# MCP system-prompt builder (in-process, bypasses MCP protocol)
# --------------------------------------------------------------------------- #


# Patterns that target platform-only instructions baked into agent prompts —
# these tell the LLM to behave as a Claude Code participant (call MCP tools,
# append a routing footer, etc.) and are MEANINGLESS at the bench level.
# Leaving them in pollutes the MCP arm with output that has nothing to do
# with the user's question, biasing the judge.
_PLATFORM_INSTRUCTION_PATTERNS: tuple[re.Pattern[str], ...] = (
    # "Append at the end (labels in English, ...): **Agent**: [name] · ..."
    re.compile(r"Append at the end[^\n]*?\n[^\n]*?\*\*Agent\*\*:[^\n]*?\n?", re.IGNORECASE),
    # Bare footer template line, with or without preceding "Append".
    re.compile(r"\*\*Agent\*\*:\s*\[[^\]]+\][^\n]*?\*\*Rules\*\*:\s*\[[^\]]+\][^\n]*\n?", re.IGNORECASE),
    # Routing-protocol imperatives that ask the model to call MCP tools.
    re.compile(r"^CRITICAL: You MUST call.*?route_and_load.*?$", re.MULTILINE | re.IGNORECASE),
    re.compile(r"You MUST call .*?route_and_load.*?[\.\n]", re.IGNORECASE),
    re.compile(r"Before answering ANY user query.*?[\.\n]", re.IGNORECASE),
    re.compile(r"This is a BLOCKING REQUIREMENT[^\n]*\n", re.IGNORECASE),
    # "Respond in the same language as the user's query (auto-detect)." line
    # — duplicates language-match rule but is keyed to the routing protocol.
    re.compile(r"Respond in the same language as the user's query \(auto-detect\)\.[^\n]*\n?", re.IGNORECASE),
)

# Appended at the end to override any residual footer-instructions the regexes
# missed. Last-instruction-wins is a stronger nudge than regex stripping.
_BENCH_MODE_OVERRIDE = """

---
BENCH MODE: This is an evaluation context, not an interactive Claude Code session. Do NOT append any platform metadata footer (no "Agent:", "Skills:", "Implants:", "Rules:" lines). Do NOT mention MCP tools, the routing protocol, or any orchestration directives — none of those exist in this context. Respond ONLY with content that addresses the user's query above.
"""


def _strip_platform_instructions(prompt: str) -> str:
    """Remove platform-only directives from an enriched MCP prompt.

    Targets footer-append instructions and routing-protocol imperatives that
    would otherwise be dutifully echoed by the LLM in the MCP arm, polluting
    the comparison with platform metadata noise.
    """
    out = prompt
    for pattern in _PLATFORM_INSTRUCTION_PATTERNS:
        out = pattern.sub("", out)
    return out + _BENCH_MODE_OVERRIDE


# Lazy singleton — SemanticRouter loads .npz vector stores from disk on init;
# re-instantiating per query is wasteful and not thread-safe with concurrent
# .npz reads.
_ROUTER_SINGLETON: Any = None


def _get_router() -> Any:
    global _ROUTER_SINGLETON
    if _ROUTER_SINGLETON is None:
        from src.engine.router import SemanticRouter
        _ROUTER_SINGLETON = SemanticRouter()
    return _ROUTER_SINGLETON


# Per-process cache so the order-swap (two judge passes) and multi-N re-runs don't
# repay the picker call for a query already routed this run. Keyed by
# (provider, model, query) so picks can't leak across providers/models in the same
# process (multi-config sweeps, in-process test runs).
_PICKER_CACHE: dict[tuple[str, str, str], str] = {}


async def _llm_pick_agent(provider: ProviderImpl, client, model: str, query: str) -> str:
    """Production-faithful stand-in for the ROUTE_REQUIRED LLM-picker.

    Production returns ROUTE_REQUIRED on a cache miss and lets the *calling* LLM
    pick an agent from the candidate catalog. The bench reproduces that by asking
    the arm model — the same model that then answers — to choose, instead of the
    keyword stand-in that misroutes free-form prompts (financial-statement →
    fitness_coach, quantum-circuit A* → education_tutor). The pick is bounded to a
    tiny completion and is not billed into arm usage (small, and cached per
    (provider, model, query)).
    """
    cache_key = (provider.name, model, query)
    if cache_key in _PICKER_CACHE:
        return _PICKER_CACHE[cache_key]
    router = _get_router()
    catalog = router.get_agent_catalog()
    catalog_names = [c["name"] for c in catalog]  # stable order → deterministic fallback
    names = set(catalog_names)
    listing = "\n".join(f"- {c['name']}: {c.get('role', '')}" for c in catalog)
    picker_system = (
        "You are a router for a multi-agent system. Choose the single best "
        "specialist agent for the user's query from the list below. Reply with "
        "ONLY the agent name (the identifier before the colon), nothing else.\n\n"
        f"Agents:\n{listing}"
    )
    text, _usage, _latency_ms = await _with_retries(
        lambda: provider.complete(client, model, query, picker_system, 32)
    )
    cleaned = (text or "").strip().lower()
    agent = "universal_agent"
    if cleaned:
        first = cleaned.split()[0].strip(".,`*:\"'")
        # Exact first-token match, else first catalog name that appears in the reply.
        agent = first if first in names else next((n for n in catalog_names if n in cleaned), "universal_agent")
    _PICKER_CACHE[cache_key] = agent
    return agent


async def build_mcp_system_prompt(
    query: str,
    pick_agent: "Callable[[str], Awaitable[str]] | None" = None,
) -> tuple[str, dict[str, Any]]:
    """Replicate `src.server.route_and_load` for the no-LLM-picker portion.

    Production flow on a fresh query (no sticky agent, no context_hash):
      1. `router.lookup_cache(query)` — semantic cache hit?
         - Hit → apply `keyword_veto`:
             - veto == agent       → cache confirms
             - veto is another id  → keyword override
             - veto == ROUTE_REQUIRED → ambiguous, production hands to LLM-picker
         - Miss → fall to step 2.
      2. `_is_meta_query(query)` → `universal_agent`.
      3. Otherwise → ROUTE_REQUIRED → LLM-picker chooses an agent from
         `router.get_agent_catalog()`.

    When `pick_agent` is provided (the bench wires it to the arm model — see
    `_llm_pick_agent`), step 3's ROUTE_REQUIRED is reproduced faithfully: the arm
    model picks from `router.get_agent_catalog()`, mirroring production. When it's
    omitted (unit tests, or a no-LLM context), the bench falls back to the top
    `match_keywords` hit (or `universal_agent` if no hit). Every branch is recorded
    in `mcp_meta["routing_path"]` so the report shows how many queries used a real
    cache hit, the LLM-picker, or the deterministic keyword fallback — that
    distinction matters for interpreting the bench result.
    """
    from src.server import _load_and_enrich, _is_meta_query
    from src.engine.router import KEYWORD_VETO_ROUTE_REQUIRED

    router = _get_router()

    def _keyword_fallback() -> str:
        hits = router.match_keywords(query)
        return hits[0][0] if hits and hits[0][1] > 0 else "universal_agent"

    cached = await router.lookup_cache(query, {})
    if cached is not None:
        veto = router.keyword_veto(query, cached.target_agent)
        if veto is None:
            agent_name = cached.target_agent
            routing_path = "cache_hit"
        elif veto == KEYWORD_VETO_ROUTE_REQUIRED:
            # Production: ambiguous → LLM-picker (or deterministic keyword stand-in).
            if pick_agent is not None:
                agent_name = await pick_agent(query)
                routing_path = "llm_picker_after_ambiguous_veto"
            else:
                agent_name = _keyword_fallback()
                routing_path = "keyword_fallback_after_ambiguous_veto"
        else:
            agent_name = veto
            routing_path = "keyword_override_cached"
    elif _is_meta_query(query):
        agent_name = "universal_agent"
        routing_path = "meta_query"
    else:
        # Production cache miss → ROUTE_REQUIRED → LLM-picker (or keyword stand-in).
        if pick_agent is not None:
            agent_name = await pick_agent(query)
            routing_path = "llm_picker"
        else:
            agent_name = _keyword_fallback()
            routing_path = "keyword_fallback"

    prompt, _ctx_hash, skills, implants, rules, tier = await _load_and_enrich(agent_name, query, [])
    cleaned = _strip_platform_instructions(prompt)
    return cleaned, {
        "agent": agent_name,
        "tier": tier,
        "routing_path": routing_path,
        "skills_loaded": list(skills),
        "implants_loaded": list(implants),
        "rules_loaded": list(rules),
    }


# --------------------------------------------------------------------------- #
# Transient-error retry wrapper
# --------------------------------------------------------------------------- #


def _retryable_api_errors() -> tuple[type[BaseException], ...]:
    """Collect SDK-specific retryable error classes lazily — avoids importing
    openai/anthropic at module load time when neither is needed."""
    errs: list[type[BaseException]] = []
    try:
        from openai import APIConnectionError as _O1, APITimeoutError as _O2, RateLimitError as _O3
        errs += [_O1, _O2, _O3]
    except ImportError:
        pass
    try:
        from anthropic import APIConnectionError as _A1, APITimeoutError as _A2, RateLimitError as _A3
        errs += [_A1, _A2, _A3]
    except ImportError:
        pass
    # A malformed structured verdict (e.g. missing `winner`) is transient for a
    # stochastic judge — especially via a proxy under concurrent load — so a
    # re-roll is the right treatment, same contract as transient API errors.
    # Fail-fast survives: once `_with_retries` exhausts attempts, it propagates.
    errs.append(JudgeValidationError)
    # A leaked agentic-harness completion is intermittent backend contamination;
    # re-roll the arm call to capture a faithful answer (the bench sends no tools).
    errs.append(ContaminatedResponseError)
    return tuple(errs)


_RETRYABLE_CACHE: tuple[type[BaseException], ...] | None = None


def _get_retryable() -> tuple[type[BaseException], ...]:
    global _RETRYABLE_CACHE
    if _RETRYABLE_CACHE is None:
        _RETRYABLE_CACHE = _retryable_api_errors()
    return _RETRYABLE_CACHE


async def _with_retries(coro_factory, *, attempts: int = 3, base_delay: float = 2.0) -> Any:
    """Run an async coroutine factory with exponential backoff on transient API errors."""
    retryable = _get_retryable()
    last_exc: BaseException | None = None
    for attempt in range(attempts):
        try:
            return await coro_factory()
        except retryable as exc:
            last_exc = exc
            if attempt == attempts - 1:
                break
            delay = base_delay * (2 ** attempt)
            logger.warning("transient API error (%s); retrying in %.1fs (attempt %d/%d)", type(exc).__name__, delay, attempt + 2, attempts)
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


# --------------------------------------------------------------------------- #
# Arm runners
# --------------------------------------------------------------------------- #


async def run_arm_vanilla(provider: ProviderImpl, client, query: str, model: str, max_tokens: int) -> TrialResult:
    text, usage, latency_ms = await _with_retries(
        lambda: provider.complete(client, model, query, None, max_tokens)
    )
    if not text.strip():
        logger.warning("vanilla arm returned empty content for query=%r", query[:80])
    return TrialResult(arm="vanilla", response_text=text, usage=usage, latency_ms=latency_ms)


async def run_arm_mcp(provider: ProviderImpl, client, query: str, model: str, max_tokens: int) -> TrialResult:
    async def _pick(q: str) -> str:
        return await _llm_pick_agent(provider, client, model, q)

    system_prompt, mcp_meta = await build_mcp_system_prompt(query, pick_agent=_pick)
    text, usage, latency_ms = await _with_retries(
        lambda: provider.complete(client, model, query, system_prompt, max_tokens)
    )
    if not text.strip():
        logger.warning("mcp arm returned empty content for query=%r (agent=%s)", query[:80], mcp_meta.get("agent"))
    return TrialResult(arm="mcp", response_text=text, usage=usage, latency_ms=latency_ms, mcp_meta=mcp_meta)


# --------------------------------------------------------------------------- #
# Judge orchestration
# --------------------------------------------------------------------------- #


async def judge_with_swap(
    *,
    judge_provider: ProviderImpl,
    judge_sync_client,
    query: str,
    vanilla_text: str,
    mcp_text: str,
    judge_model: str,
    judge_max_tokens: int,
) -> SwapVerdict:
    """Judge can be a different provider than arms — e.g. Gemini arms judged
    by Claude. This breaks position bias AND avoids per-provider quirks
    (Gemini is unreliable as a json_schema judge via OpenAI-compat layers).

    Each position is wrapped in `_with_retries` so a transient rate-limit or
    timeout from the judge provider does not fail the whole query — arms
    already retry, the judge must match that contract.
    """

    async def _judge_one(left_text: str, right_text: str) -> JudgeCall:
        return await asyncio.to_thread(
            run_judge,
            provider=judge_provider,
            sync_client=judge_sync_client,
            query=query,
            left=left_text,
            right=right_text,
            model=judge_model,
            max_tokens=judge_max_tokens,
        )

    pos1 = await _with_retries(lambda: _judge_one(vanilla_text, mcp_text))
    pos2 = await _with_retries(lambda: _judge_one(mcp_text, vanilla_text))
    return aggregate_with_swap(pos1=pos1, pos2=pos2, pos1_left_is="vanilla")


def _synthetic_empty_tie(reason: str) -> SwapVerdict:
    """Synthesize a TIE verdict when at least one arm returned empty text.

    Skipping the judge avoids spending tokens on a comparison the judge can't
    do honestly ("" vs "real text" is decided by length, not quality).
    Both pos1/pos2 are stubbed with `winner='tie'` and zero usage so downstream
    aggregation/cost code treats them uniformly.
    """
    zero_usage = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    stub = JudgeCall(winner="tie", reasoning=reason, criterion_scores={}, usage=dict(zero_usage))
    return SwapVerdict(final="tie", pos1=stub, pos2=stub, contradicted=False, total_usage=dict(zero_usage))


async def run_one_query(
    *,
    idx: int,
    stream_idx: int,
    query: str,
    provider: ProviderImpl,
    async_client,
    judge_provider: ProviderImpl,
    judge_sync_client,
    model: str,
    judge_model: str,
    max_tokens: int,
    judge_max_tokens: int,
    semaphore: asyncio.Semaphore,
) -> QueryRun:
    async with semaphore:
        vanilla, mcp = await asyncio.gather(
            run_arm_vanilla(provider, async_client, query, model, max_tokens),
            run_arm_mcp(provider, async_client, query, model, max_tokens),
        )
        if not vanilla.response_text.strip() or not mcp.response_text.strip():
            empty_arm = "vanilla" if not vanilla.response_text.strip() else "mcp"
            verdict = _synthetic_empty_tie(
                f"Skipped judge: {empty_arm} arm returned empty text (likely token-budget exhaustion or content filter)."
            )
        else:
            verdict = await judge_with_swap(
                judge_provider=judge_provider,
                judge_sync_client=judge_sync_client,
                query=query,
                vanilla_text=vanilla.response_text,
                mcp_text=mcp.response_text,
                judge_model=judge_model,
                judge_max_tokens=judge_max_tokens,
            )
    return QueryRun(idx=idx, query=query, stream_idx=stream_idx, vanilla=vanilla, mcp=mcp, verdict=verdict)


# --------------------------------------------------------------------------- #
# Aggregation + rendering
# --------------------------------------------------------------------------- #


def _cost_usd(usage: dict[str, int], pricing: dict[str, float]) -> float:
    return (
        usage["input_tokens"] / 1_000_000 * pricing["input"]
        + usage["output_tokens"] / 1_000_000 * pricing["output"]
        + usage["cache_read_input_tokens"] / 1_000_000 * pricing["cache_read"]
        + usage["cache_creation_input_tokens"] / 1_000_000 * pricing["cache_creation"]
    )


def _cost_split(usage: dict[str, int], pricing: dict[str, float]) -> dict[str, float]:
    """Break a usage dict into per-component USD costs (input vs output vs cache)."""
    return {
        "input_usd": usage["input_tokens"] / 1_000_000 * pricing["input"],
        "output_usd": usage["output_tokens"] / 1_000_000 * pricing["output"],
        "cache_read_usd": usage["cache_read_input_tokens"] / 1_000_000 * pricing["cache_read"],
        "cache_creation_usd": usage["cache_creation_input_tokens"] / 1_000_000 * pricing["cache_creation"],
    }


def _sum_usages(usages: list[dict[str, int]]) -> dict[str, int]:
    out = {"input_tokens": 0, "output_tokens": 0, "cache_creation_input_tokens": 0, "cache_read_input_tokens": 0}
    for u in usages:
        for k in out:
            out[k] += u.get(k, 0)
    return out


def build_template_context(result: BenchmarkResult) -> dict[str, Any]:
    runs = result.runs
    n = len(runs)
    mcp_wins = sum(1 for r in runs if r.verdict.final == "mcp")
    vanilla_wins = sum(1 for r in runs if r.verdict.final == "vanilla")
    ties = sum(1 for r in runs if r.verdict.final == "tie")
    contradictions = sum(1 for r in runs if r.verdict.contradicted)
    arm_pricing = result.arm_pricing
    judge_pricing = result.judge_pricing

    def tot(usage_key: str, arm: str) -> int:
        return sum(getattr(r, arm).usage[usage_key] for r in runs)

    def total_arm_tokens(arm: str) -> int:
        return sum(sum(getattr(r, arm).usage.values()) for r in runs)

    avg_tokens_vanilla = round(total_arm_tokens("vanilla") / n) if n else 0
    avg_tokens_mcp = round(total_arm_tokens("mcp") / n) if n else 0
    token_overhead_abs = avg_tokens_mcp - avg_tokens_vanilla
    token_overhead_pct = round(100 * token_overhead_abs / avg_tokens_vanilla, 1) if avg_tokens_vanilla else 0

    cost_vanilla = sum(_cost_usd(r.vanilla.usage, arm_pricing) for r in runs)
    cost_mcp = sum(_cost_usd(r.mcp.usage, arm_pricing) for r in runs)
    cost_judge = sum(_cost_usd(r.verdict.total_usage, judge_pricing) for r in runs)
    total_cost = cost_vanilla + cost_mcp + cost_judge

    # Per-arm aggregate usage + per-component cost split.
    # Costs are kept as floats through aggregation and formatted exactly once
    # at the end — formatting per-component first and re-summing the formatted
    # strings would drift by sub-cent rounding (Copilot regression), making
    # TOTAL.input + TOTAL.output + … ≠ TOTAL.total in the rendered HTML.
    vanilla_usage_total = _sum_usages([r.vanilla.usage for r in runs])
    mcp_usage_total = _sum_usages([r.mcp.usage for r in runs])
    judge_usage_total = _sum_usages([r.verdict.total_usage for r in runs])

    def _numeric_row(label: str, usage: dict[str, int], pricing_table: dict[str, float]) -> dict[str, Any]:
        split = _cost_split(usage, pricing_table)
        return {
            "label": label,
            "input_tokens": usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "cache_read_tokens": usage["cache_read_input_tokens"],
            "cache_creation_tokens": usage["cache_creation_input_tokens"],
            "_input_usd": split["input_usd"],
            "_output_usd": split["output_usd"],
            "_cache_read_usd": split["cache_read_usd"],
            "_cache_creation_usd": split["cache_creation_usd"],
            "_total_usd": sum(split.values()),
        }

    def _format_row(r: dict[str, Any]) -> dict[str, Any]:
        return {
            "label": r["label"],
            "input_tokens": r["input_tokens"],
            "output_tokens": r["output_tokens"],
            "cache_read_tokens": r["cache_read_tokens"],
            "cache_creation_tokens": r["cache_creation_tokens"],
            "input_usd": f"{r['_input_usd']:.4f}",
            "output_usd": f"{r['_output_usd']:.4f}",
            "cache_read_usd": f"{r['_cache_read_usd']:.4f}",
            "cache_creation_usd": f"{r['_cache_creation_usd']:.4f}",
            "total_usd": f"{r['_total_usd']:.4f}",
        }

    vanilla_num = _numeric_row("Vanilla (arm)", vanilla_usage_total, arm_pricing)
    mcp_num = _numeric_row("MCP (arm)", mcp_usage_total, arm_pricing)
    judge_num = _numeric_row("Judge (×2 swap)", judge_usage_total, judge_pricing)

    total_num = {
        "label": "TOTAL",
        "input_tokens": sum(r["input_tokens"] for r in (vanilla_num, mcp_num, judge_num)),
        "output_tokens": sum(r["output_tokens"] for r in (vanilla_num, mcp_num, judge_num)),
        "cache_read_tokens": sum(r["cache_read_tokens"] for r in (vanilla_num, mcp_num, judge_num)),
        "cache_creation_tokens": sum(r["cache_creation_tokens"] for r in (vanilla_num, mcp_num, judge_num)),
        "_input_usd": sum(r["_input_usd"] for r in (vanilla_num, mcp_num, judge_num)),
        "_output_usd": sum(r["_output_usd"] for r in (vanilla_num, mcp_num, judge_num)),
        "_cache_read_usd": sum(r["_cache_read_usd"] for r in (vanilla_num, mcp_num, judge_num)),
        "_cache_creation_usd": sum(r["_cache_creation_usd"] for r in (vanilla_num, mcp_num, judge_num)),
        "_total_usd": total_cost,
    }
    cost_breakdown = [_format_row(r) for r in (vanilla_num, mcp_num, judge_num, total_num)]

    agents = [r.mcp.mcp_meta["agent"] for r in runs if r.mcp.mcp_meta]
    agent_counts = dict(Counter(agents))
    routing_summary = ", ".join(f"{a}×{c}" for a, c in sorted(agent_counts.items(), key=lambda kv: -kv[1])[:4])

    # Distribution of routing paths — how many queries used a real cache hit
    # vs. the deterministic keyword fallback vs. meta-query. Surface this so
    # the report makes clear which slice of the bench reflects production
    # routing fidelity vs. the no-LLM-picker stand-in.
    routing_paths = [r.mcp.mcp_meta.get("routing_path", "unknown") for r in runs if r.mcp.mcp_meta]
    routing_path_counts = dict(Counter(routing_paths))
    routing_path_summary = ", ".join(
        f"{p}×{c}" for p, c in sorted(routing_path_counts.items(), key=lambda kv: -kv[1])
    )

    def median_lat(arm: str) -> int:
        vals = [getattr(r, arm).latency_ms for r in runs]
        return int(statistics.median(vals)) if vals else 0

    if mcp_wins > vanilla_wins:
        overall_winner = "mcp"
        overall_winner_label = "MCP wins"
        overall_winner_margin = f"{mcp_wins} vs {vanilla_wins} ({ties} ties)"
    elif vanilla_wins > mcp_wins:
        overall_winner = "vanilla"
        overall_winner_label = "Vanilla wins"
        overall_winner_margin = f"{vanilla_wins} vs {mcp_wins} ({ties} ties)"
    else:
        overall_winner = "tie"
        overall_winner_label = "Overall: TIE"
        overall_winner_margin = f"{mcp_wins} MCP · {vanilla_wins} Vanilla · {ties} ties"

    summary = {
        "mcp_wins": mcp_wins,
        "vanilla_wins": vanilla_wins,
        "ties": ties,
        "contradictions": contradictions,
        "mcp_win_pct": round(100 * mcp_wins / n, 1) if n else 0,
        "overall_winner": overall_winner,
        "overall_winner_label": overall_winner_label,
        "overall_winner_margin": overall_winner_margin,
        "avg_tokens_vanilla": avg_tokens_vanilla,
        "avg_tokens_mcp": avg_tokens_mcp,
        "token_overhead_abs": token_overhead_abs,
        "token_overhead_pct": token_overhead_pct,
        "total_cost_usd": f"{total_cost:.4f}",
        "cost_arms_usd": f"{cost_vanilla + cost_mcp:.4f}",
        "cost_judge_usd": f"{cost_judge:.4f}",
        "median_latency_vanilla_ms": median_lat("vanilla"),
        "median_latency_mcp_ms": median_lat("mcp"),
        "routing_unique_agents": len(agent_counts),
        "routing_summary": routing_summary or "—",
        "routing_path_summary": routing_path_summary or "—",
        "routing_path_counts": routing_path_counts,
    }

    def _resolved_arm(winner: str, left_arm: str) -> str:
        """Map raw judge winner ('left'/'right'/'tie') to the actual arm name."""
        right_arm = "mcp" if left_arm == "vanilla" else "vanilla"
        if winner == "left":
            return left_arm
        if winner == "right":
            return right_arm
        return "tie"

    def _winner_corner(winner: str) -> str:
        """Display the raw judge pick in boxing terms — left/right corner ('tie' unchanged)."""
        if winner == "left":
            return "left corner"
        if winner == "right":
            return "right corner"
        return "tie"

    # Detect truncation: an arm hitting >= 98% of the max_tokens cap almost
    # certainly stopped at the limit rather than naturally completing.
    arm_max = result.config.get("max_tokens", 0)
    truncation_threshold = int(arm_max * 0.98) if arm_max else 0

    def _is_truncated(usage: dict[str, int]) -> bool:
        return arm_max > 0 and usage["output_tokens"] >= truncation_threshold

    def _cat_winner(margin: float) -> str:
        """Per-category Δ winner for badge colour — sign of (mcp - vanilla)."""
        return "mcp" if margin > 0 else "vanilla" if margin < 0 else "tie"

    truncation_count = 0
    per_query: list[dict[str, Any]] = []
    for r in runs:
        vanilla_truncated = _is_truncated(r.vanilla.usage)
        mcp_truncated = _is_truncated(r.mcp.usage)
        if vanilla_truncated or mcp_truncated:
            truncation_count += 1
        # Per-category (swap-averaged) scores per arm — None when the verdict is
        # unscored (synthetic empty-tie / missing scores); the template hides the
        # table in that case. Column sums equal vanilla_avg / mcp_avg by construction.
        cat_rows = per_criterion_breakdown(r.verdict.pos1.criterion_scores, r.verdict.pos2.criterion_scores, pos1_left_is="vanilla")
        category_scores = None
        if cat_rows is not None:
            category_scores = {
                "rows": [
                    {
                        "criterion": row["criterion"].replace("_", "-"),
                        "vanilla": f"{row['vanilla']:.1f}",
                        "mcp": f"{row['mcp']:.1f}",
                        "margin_str": f"{row['margin']:+.1f}",
                        "winner": _cat_winner(row["margin"]),
                    }
                    for row in cat_rows
                ],
                "total": {
                    "vanilla": f"{r.verdict.vanilla_avg:.1f}",
                    "mcp": f"{r.verdict.mcp_avg:.1f}",
                    "margin_str": f"{r.verdict.margin:+.1f}",
                    "winner": _cat_winner(r.verdict.margin),
                },
            }
        per_query.append({
            "query": r.query,
            "query_preview": r.query[:120] + ("…" if len(r.query) > 120 else ""),
            "final_verdict": r.verdict.final,
            "contradicted": r.verdict.contradicted,
            "margin": r.verdict.margin,
            "final_by_score": r.verdict.final_by_score,
            "margin_str": ("n/a" if r.verdict.margin is None else f"{r.verdict.margin:+.1f}"),
            "margin_scale": len(_CRITERIA) * (result.config.get("judge_score_max", JUDGE_SCORE_MAX) - JUDGE_SCORE_MIN),  # max |mcp−vanilla|: len(_CRITERIA) × (max − min)
            "category_scores": category_scores,
            "vanilla_response": r.vanilla.response_text,
            "vanilla_usage": r.vanilla.usage,
            "vanilla_latency_ms": r.vanilla.latency_ms,
            "vanilla_truncated": vanilla_truncated,
            "mcp_response": r.mcp.response_text,
            "mcp_usage": r.mcp.usage,
            "mcp_latency_ms": r.mcp.latency_ms,
            "mcp_truncated": mcp_truncated,
            "mcp_meta": r.mcp.mcp_meta or {"agent": "?", "tier": "?", "routing_path": "?", "skills_loaded": [], "implants_loaded": [], "rules_loaded": []},
            "judge_pos1": {
                "winner": r.verdict.pos1.winner,
                "winner_arm": _resolved_arm(r.verdict.pos1.winner, "vanilla"),
                "winner_corner": _winner_corner(r.verdict.pos1.winner),
                "reasoning": r.verdict.pos1.reasoning,
            },
            "judge_pos2": {
                "winner": r.verdict.pos2.winner,
                "winner_arm": _resolved_arm(r.verdict.pos2.winner, "mcp"),
                "winner_corner": _winner_corner(r.verdict.pos2.winner),
                "reasoning": r.verdict.pos2.reasoning,
            },
        })

    summary["truncation_count"] = truncation_count
    summary["arm_max_tokens"] = arm_max

    chart_data = {
        "win_rate": {
            "labels": ["MCP wins", "Vanilla wins", "Tie"],
            "values": [mcp_wins, vanilla_wins, ties],
        },
        "tokens": {
            "labels": ["Vanilla", "MCP"],
            "input": [round(tot("input_tokens", "vanilla") / n), round(tot("input_tokens", "mcp") / n)] if n else [0, 0],
            "output": [round(tot("output_tokens", "vanilla") / n), round(tot("output_tokens", "mcp") / n)] if n else [0, 0],
            "cache_read": [round(tot("cache_read_input_tokens", "vanilla") / n), round(tot("cache_read_input_tokens", "mcp") / n)] if n else [0, 0],
            "cache_creation": [round(tot("cache_creation_input_tokens", "vanilla") / n), round(tot("cache_creation_input_tokens", "mcp") / n)] if n else [0, 0],
        },
    }

    return {
        "date": dt.date.today().isoformat(),
        "model": result.config["model"],
        "judge_model": result.config["judge_model"],
        "judge_score_max": result.config.get("judge_score_max", JUDGE_SCORE_MAX),
        "judge_provider": result.config.get("judge_provider", result.config["provider"]),
        "cross_provider_judge": result.config.get("judge_provider") not in (None, result.config["provider"]),
        "provider": result.config["provider"],
        "provider_notes": result.config["provider_notes"],
        "n": n,
        "seed": result.config["seed"],
        "dataset_key": result.config["dataset"],
        "commit_sha": result.config.get("commit_sha", "unknown"),
        "summary": summary,
        "cost_breakdown": cost_breakdown,
        "per_query": per_query,
        "chart_data": chart_data,
        "dataset_hash": result.dataset_hash,
        "generated_at": result.generated_at,
        "wall_time_s": round(result.wall_time_s, 1),
        "arm_pricing": arm_pricing,
        "judge_pricing": judge_pricing,
        "cross_provider_pricing": arm_pricing != judge_pricing,
    }


def render_html(result: BenchmarkResult, template_path: Path) -> str:
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(template_path.parent),
        autoescape=select_autoescape(["html", "j2"]),
        trim_blocks=False,
        lstrip_blocks=False,
    )
    tpl = env.get_template(template_path.name)
    return tpl.render(**build_template_context(result))


# --------------------------------------------------------------------------- #
# CLI / main
# --------------------------------------------------------------------------- #


def _git_sha() -> str:
    try:
        out = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, stderr=subprocess.DEVNULL)
        return out.decode().strip()
    except Exception:
        return "nogit"


async def main_async(args: argparse.Namespace) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stderr,
    )
    # Quiet noisy SDK loggers — keep our bench logger at INFO.
    for noisy in ("httpx", "httpcore", "openai", "anthropic", "urllib3", "datasets", "filelock"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    provider = get_provider(args.provider)
    model = args.model or provider.default_model
    # Judge selection precedence: CLI flag > env var > arm provider default.
    # `JUDGE_PROVIDER` / `JUDGE_MODEL` env vars let users swap judges without
    # editing scripts — e.g. `./scripts/set_judge.sh opus` updates .env once
    # and every subsequent bench run picks it up.
    judge_provider_name = args.judge_provider or os.getenv("JUDGE_PROVIDER") or args.provider
    judge_provider = get_provider(judge_provider_name)
    judge_model = args.judge_model or os.getenv("JUDGE_MODEL") or judge_provider.default_judge_model

    queries = sample_queries(args.dataset, args.n, args.seed)
    ds_hash = dataset_hash(queries)

    print(
        f"[bench] provider={provider.name} model={model} "
        f"judge_provider={judge_provider.name} judge_model={judge_model} "
        f"dataset={args.dataset} n={len(queries)} seed={args.seed} hash={ds_hash} "
        f"concurrency={args.concurrency}",
        file=sys.stderr,
    )
    for i, (s_idx, q) in enumerate(queries, 1):
        print(f"  [{i:>2}] (stream={s_idx}) {q[:100]}{'…' if len(q) > 100 else ''}", file=sys.stderr)

    if args.dry_run:
        print(f"[bench] --dry-run: skipping API calls", file=sys.stderr)
        return 0

    if not os.getenv(provider.env_key):
        raise SystemExit(f"{provider.env_key} not set in env (required for --provider {provider.name})")
    if not os.getenv(judge_provider.env_key):
        raise SystemExit(f"{judge_provider.env_key} not set in env (required for --judge-provider {judge_provider.name})")

    async_client = provider.make_async_client()
    # Arms use only the async client; the judge runs synchronously inside
    # `asyncio.to_thread`, so it needs a sync client of the judge provider.
    # If judge_provider == arm provider, sharing one sync client is cheaper;
    # otherwise the arm's sync client would be created and never used.
    if judge_provider.name == provider.name:
        judge_sync_client = provider.make_sync_client()
    else:
        judge_sync_client = judge_provider.make_sync_client()
    # Pre-warm the SemanticRouter singleton before launching concurrent tasks.
    # `_get_router` is sync (no `await`) so under asyncio cooperative
    # scheduling it would be atomic anyway, but constructing it eagerly here
    # removes any ambiguity and avoids the first-query latency spike.
    _get_router()
    semaphore = asyncio.Semaphore(args.concurrency)

    t0 = time.perf_counter()
    runs = await asyncio.gather(*[
        run_one_query(
            idx=i,
            stream_idx=s_idx,
            query=q,
            provider=provider,
            async_client=async_client,
            judge_provider=judge_provider,
            judge_sync_client=judge_sync_client,
            model=model,
            judge_model=judge_model,
            max_tokens=args.max_tokens,
            judge_max_tokens=args.judge_max_tokens,
            semaphore=semaphore,
        )
        for i, (s_idx, q) in enumerate(queries, 1)
    ])
    wall = time.perf_counter() - t0

    # Per-model pricing table — falls back to provider-level if unknown.
    # Arm vs judge pricing is split so cross-provider / different-model judge
    # runs bill each role against the correct per-1M rates.
    arm_pricing = get_pricing(model)
    judge_pricing = get_pricing(judge_model)
    result = BenchmarkResult(
        config={
            "provider": provider.name,
            "provider_notes": provider.notes,
            "model": model,
            "judge_provider": judge_provider.name,
            "judge_model": judge_model,
            "seed": args.seed,
            "dataset": args.dataset,
            "commit_sha": _git_sha(),
            "max_tokens": args.max_tokens,
            "judge_max_tokens": args.judge_max_tokens,
            "judge_score_max": JUDGE_SCORE_MAX,
            "concurrency": args.concurrency,
        },
        runs=runs,
        dataset_hash=ds_hash,
        wall_time_s=wall,
        arm_pricing=arm_pricing,
        judge_pricing=judge_pricing,
    )

    template_path = REPO_ROOT / "evals" / "templates" / "report.html.j2"
    html = render_html(result, template_path)

    if args.out:
        out_path = Path(args.out)
    else:
        # Timestamp (not just date) so repeated same-day runs don't overwrite.
        stamp = dt.datetime.now().strftime("%Y-%m-%d_%H%M%S")
        out_path = REPO_ROOT / "evals" / "reports" / f"{stamp}_mcp_vs_vanilla_{provider.name}.html"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")

    if args.save_json:
        json_path = out_path.with_suffix(".json")
        json_path.write_text(json.dumps({
            "config": result.config,
            "dataset_hash": result.dataset_hash,
            "wall_time_s": result.wall_time_s,
            "generated_at": result.generated_at,
            "arm_pricing": result.arm_pricing,
            "judge_pricing": result.judge_pricing,
            "runs": [
                {
                    "idx": r.idx,
                    "stream_idx": r.stream_idx,
                    "query": r.query,
                    "vanilla": asdict(r.vanilla),
                    "mcp": asdict(r.mcp),
                    "verdict": {
                        "final": r.verdict.final,
                        "contradicted": r.verdict.contradicted,
                        "margin": r.verdict.margin,
                        "final_by_score": r.verdict.final_by_score,
                        "mcp_avg": r.verdict.mcp_avg,
                        "vanilla_avg": r.verdict.vanilla_avg,
                        "total_usage": r.verdict.total_usage,
                        "pos1": asdict(r.verdict.pos1),
                        "pos2": asdict(r.verdict.pos2),
                    },
                }
                for r in result.runs
            ],
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"[bench] wrote raw JSON → {json_path}", file=sys.stderr)

    print(f"[bench] wrote report → {out_path}", file=sys.stderr)
    print(
        f"[bench] wall={wall:.1f}s · mcp_wins={sum(1 for r in runs if r.verdict.final == 'mcp')}/{len(runs)} "
        f"· contradictions={sum(1 for r in runs if r.verdict.contradicted)}",
        file=sys.stderr,
    )
    return 0


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="run_mcp_vs_vanilla", description=__doc__)
    p.add_argument("--n", type=int, default=10, help="number of queries to sample (default: 10)")
    p.add_argument("--seed", type=int, default=42)
    p.add_argument(
        "--provider",
        default="openai",
        choices=["openai", "anthropic"],
        help="LLM provider for both arms and judge (default: openai)",
    )
    p.add_argument("--model", default=None, help="model under test for both arms (default: provider's default)")
    p.add_argument(
        "--judge-provider", "--judge_provider",
        default=None,
        choices=["openai", "anthropic"],
        help="separate provider for judge (default: same as --provider). Use to break self-judging bias OR to escape model-specific structured-output bugs (e.g. Gemini → claude-sonnet-4-6 judge).",
    )
    p.add_argument("--judge-model", "--judge_model", default=None, help="judge model (default: judge-provider's default)")
    p.add_argument(
        "--dataset",
        default="wildbench",
        choices=sorted(DATASETS),
        help="HF source dataset (default: wildbench — open; use lmsys_chat_1m only if you have HF_TOKEN with access)",
    )
    p.add_argument(
        "--max-tokens", "--max_tokens",
        type=int,
        default=8192,
        help="max_tokens cap for each arm response (default: 8192). Long-form prompts (essays, code reviews) "
        "can hit the cap; check the report for the ⚠ TRUNCATED badge. Cost is 'up to' — most responses use less.",
    )
    p.add_argument(
        "--judge-max-tokens", "--judge_max_tokens",
        type=int,
        default=4096,
        help="max_tokens for each judge call (default: 4096 — gives reasoning models headroom)",
    )
    p.add_argument(
        "--concurrency",
        type=int,
        default=8,
        help="max queries in flight simultaneously (default: 8). Each query uses 2 arm + 2 judge API calls.",
    )
    p.add_argument("--out", help="output HTML path (default: evals/reports/<date>_<HHMMSS>_mcp_vs_vanilla_<provider>.html)")
    p.add_argument("--save-json", "--save_json", action="store_true", help="also dump raw run data as <out>.json")
    p.add_argument("--dry-run", "--dry_run", action="store_true", help="sample queries and print them; no API calls")
    return p.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
