"""
Pairwise LLM-as-judge with positional swap.

Provider-agnostic: the actual API call lives in `evals/runners/_providers.py`.
This module owns the pure logic — judge system prompt, verdict schema,
swap aggregation — that does not depend on which SDK is used.

Each judge call returns a structured verdict shaped by `VERDICT_SCHEMA`:
  {"winner": "left" | "right" | "tie", "reasoning": str, "criterion_scores": {...}}

The two provider adapters arrive at the same payload by different routes:
  - Anthropic: tool-use with `tools` + `tool_choice` on the `submit_verdict` tool.
  - OpenAI:    `response_format=json_schema` (structured outputs), since
               function-calling does not pass through reliably via OpenAI-compat
               proxy layers that relay to Gemini.

`aggregate_with_swap` maps two judge calls (with positions swapped between them)
back to an arm-level verdict and resolves contradictions to TIE.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

# Scoring scale + criteria — single-sourced so the prompt, schema, validation, and
# margin code all agree. Widened 1–5 → 1–10 (Phase 2) to fight score compression:
# on a 5-point scale judges cluster at 4–5, shrinking the per-query margin signal.
# Changing _SCORE_MAX here updates the prompt text, the schema maxima, the
# validation range, and the margin scale at once — no drift.
_SCORE_MIN = 1
_SCORE_MAX = 10
_CRITERIA = ("helpfulness", "correctness", "depth", "structure", "intent_fit")
_ALLOWED_WINNERS = frozenset({"left", "right", "tie"})
_REQUIRED_CRITERION_KEYS = frozenset(
    f"{side}_{c}" for side in ("left", "right") for c in _CRITERIA
)

JUDGE_SYSTEM_PROMPT = f"""You are an impartial expert evaluator. You will be shown a USER QUERY and two AI responses labelled LEFT and RIGHT. Decide which response better serves the user.

Evaluation criteria (apply all five, weight equally):
1. **helpfulness** — does it answer the question and move the user forward?
2. **correctness** — are the claims factually accurate, free of fabrication?
3. **depth** — is the level of detail appropriate (not too thin, not bloated)?
4. **structure** — is it organised, skimmable, free of redundancy?
5. **intent-fit** — does it address what the user actually asked, not adjacent topics?

Score each criterion {_SCORE_MIN}–{_SCORE_MAX} for each response, then choose the overall winner.

Use the FULL {_SCORE_MIN}–{_SCORE_MAX} range and be discriminative: reserve the top scores ({_SCORE_MAX-1}–{_SCORE_MAX}) for genuinely excellent work and the bottom ({_SCORE_MIN}–{_SCORE_MIN+2}) for clear deficiencies — do NOT default to high scores. A real quality difference between the two responses must surface as a score difference; identical scores on every criterion should be rare and mean the responses are truly indistinguishable on that criterion. Calibrate to quality, not politeness.

Return your verdict as a structured object matching the verdict schema (the host wires this as a tool call for Anthropic and as a JSON-schema response for OpenAI — both paths land in the same shape). Choose:
- "left" — LEFT is clearly better
- "right" — RIGHT is clearly better
- "tie" — both are roughly equivalent OR both are bad

Be decisive: prefer "left"/"right" over "tie" unless the responses are genuinely indistinguishable. Position (LEFT vs RIGHT) is randomised — do not let order influence you.
"""

# Criterion-score properties, built from `_CRITERIA`/`_SCORE_MIN`/`_SCORE_MAX` so
# the schema's bounds can never drift from the validation range or the prompt.
_CRITERION_SCORE_PROPERTIES: dict[str, Any] = {
    f"{side}_{c}": {"type": "integer", "minimum": _SCORE_MIN, "maximum": _SCORE_MAX}
    for side in ("left", "right")
    for c in _CRITERIA
}

# Anthropic-shaped schema (`name` + `input_schema`). The OpenAI provider unwraps
# `input_schema` and passes it as the `schema` field of a `response_format`
# `json_schema` block; see `call_judge_openai` for the conversion.
VERDICT_SCHEMA: dict[str, Any] = {
    "name": "submit_verdict",
    "description": "Submit the pairwise verdict for this query.",
    "input_schema": {
        "type": "object",
        "properties": {
            "winner": {
                "type": "string",
                "enum": ["left", "right", "tie"],
                "description": "Which response is better overall.",
            },
            "reasoning": {
                "type": "string",
                "maxLength": 600,
                "description": "Concise justification (1-3 sentences).",
            },
            "criterion_scores": {
                "type": "object",
                "properties": _CRITERION_SCORE_PROPERTIES,
                "required": list(_CRITERION_SCORE_PROPERTIES),
            },
        },
        "required": ["winner", "reasoning", "criterion_scores"],
    },
}


@dataclass(frozen=True)
class JudgeCall:
    winner: Literal["left", "right", "tie"]
    reasoning: str
    criterion_scores: dict[str, int]
    usage: dict[str, int]


@dataclass(frozen=True)
class SwapVerdict:
    final: Literal["vanilla", "mcp", "tie"]
    pos1: JudgeCall
    pos2: JudgeCall
    contradicted: bool
    total_usage: dict[str, int]
    # Swap-averaged per-criterion score margin (additive L/R position bias cancels).
    # All defaulted so existing constructors (e.g. a synthetic empty-tie) keep working.
    mcp_avg: float | None = None
    vanilla_avg: float | None = None
    margin: float | None = None              # mcp_avg - vanilla_avg; None if scores absent
    final_by_score: Literal["vanilla", "mcp", "tie", "unscored"] = "unscored"


# Tie dead-zone for the swap-averaged score margin (scale 0..len(_CRITERIA)*_SCORE_MAX).
# Default 0: "tie" means the averaged totals are exactly equal. Significance is decided
# by the Wilcoxon signed-rank test on per-query margins (evals/scripts/bench_significance.py),
# not by a hand-tuned dead-zone — a non-zero ε is a researcher degree of freedom
# (gaming surface), so keep it at 0 for the live verdict.
MARGIN_EPSILON = 0.0


def _arm_total(scores: dict[str, int], side: str) -> float | None:
    """Sum a side's per-criterion scores (max = len(_CRITERIA)*_SCORE_MAX). Returns
    None if any score is missing — e.g. a synthetic empty-tie stub or a legacy/partial
    payload — so the margin degrades to 'unscored' rather than raising."""
    keys = [f"{side}_{c}" for c in _CRITERIA]
    if not scores or any(k not in scores for k in keys):
        return None
    return float(sum(scores[k] for k in keys))


class JudgeValidationError(RuntimeError):
    """A judge response did not conform to VERDICT_SCHEMA.

    Subclasses RuntimeError (so existing `except RuntimeError` callers and tests
    keep working) but is a distinct type the runner's retry wrapper targets to
    re-roll a malformed verdict. A stochastic judge — especially via a local
    proxy under concurrent load — occasionally returns an incomplete tool_use
    payload (e.g. missing `winner`); that is transient and a re-roll usually
    fixes it. Fail-fast is preserved: once retries are exhausted this propagates
    and the run stops rather than silently degrading the verdict to TIE.
    """


def _validate_verdict_payload(payload: dict[str, Any]) -> tuple[str, str, dict[str, int]]:
    """Validate that the judge payload conforms to VERDICT_SCHEMA before use.

    Fail-fast on malformed responses instead of letting unknown winners silently
    degrade to TIE in `aggregate_with_swap` — that would skew benchmark numbers
    without raising an error.

    Score values are checked against the schema's `integer 1..5` constraint:
    `bool` is rejected explicitly because `isinstance(True, int) is True` in
    Python and a `True`/`False` score should not silently sail through as 1/0.
    """
    winner = payload.get("winner")
    reasoning = payload.get("reasoning")
    scores = payload.get("criterion_scores")
    if winner not in _ALLOWED_WINNERS:
        raise JudgeValidationError(
            f"Judge returned invalid winner: {winner!r} (payload keys: {sorted(payload)})"
        )
    if not isinstance(reasoning, str):
        raise JudgeValidationError(f"Judge returned non-string reasoning: {type(reasoning).__name__}")
    if not isinstance(scores, dict) or _REQUIRED_CRITERION_KEYS - scores.keys():
        missing = sorted(_REQUIRED_CRITERION_KEYS - (scores.keys() if isinstance(scores, dict) else set()))
        raise JudgeValidationError(f"Judge returned incomplete criterion_scores; missing keys: {missing}")
    for key in _REQUIRED_CRITERION_KEYS:
        v = scores[key]
        if isinstance(v, bool) or not isinstance(v, int):
            raise JudgeValidationError(
                f"Judge returned non-integer criterion score for {key!r}: {v!r} (type {type(v).__name__})"
            )
        if not (_SCORE_MIN <= v <= _SCORE_MAX):
            raise JudgeValidationError(
                f"Judge returned out-of-range criterion score for {key!r}: {v} (expected {_SCORE_MIN}..{_SCORE_MAX})"
            )
    return winner, reasoning, {k: scores[k] for k in _REQUIRED_CRITERION_KEYS}


def run_judge(
    *,
    provider,
    sync_client,
    query: str,
    left: str,
    right: str,
    model: str,
    max_tokens: int = 4096,
) -> JudgeCall:
    """Provider-agnostic judge call. `provider` is a ProviderImpl from _providers.py.

    `max_tokens` defaults to 4096 (vs the legacy 1024) so reasoning models
    (gpt-5.x) have headroom for both hidden reasoning tokens and the
    structured tool_call / json_schema output. Anthropic and non-reasoning
    models simply use less of the budget.
    """
    payload, usage = provider.call_judge(
        sync_client,
        query,
        left,
        right,
        model,
        JUDGE_SYSTEM_PROMPT,
        max_tokens,
        VERDICT_SCHEMA,
    )
    winner, reasoning, scores = _validate_verdict_payload(payload)
    return JudgeCall(
        winner=winner,  # type: ignore[arg-type]
        reasoning=reasoning,
        criterion_scores=scores,
        usage=usage,
    )


def aggregate_with_swap(
    *,
    pos1: JudgeCall,
    pos2: JudgeCall,
    pos1_left_is: Literal["vanilla", "mcp"],
    epsilon: float = MARGIN_EPSILON,
) -> SwapVerdict:
    """Map two judge calls (positions swapped between them) to a single arm-level verdict.

    `pos1_left_is`: which arm sat on the LEFT in the first call (the second call
    is the swap, so its LEFT is the other arm).

    Resolution table:
      - both calls pick the same ARM (after un-swapping) → that arm wins
      - calls disagree on the arm → contradiction → TIE
      - any call returns "tie" → falls back to the other call's choice (or tie if both)
    """
    pos2_left_is = "mcp" if pos1_left_is == "vanilla" else "vanilla"

    def call_to_arm(call: JudgeCall, left_is: str) -> str:
        right_is = "mcp" if left_is == "vanilla" else "vanilla"
        if call.winner == "left":
            return left_is
        if call.winner == "right":
            return right_is
        return "tie"

    arm1 = call_to_arm(pos1, pos1_left_is)
    arm2 = call_to_arm(pos2, pos2_left_is)

    if arm1 == arm2:
        final = arm1
        contradicted = False
    elif arm1 == "tie" or arm2 == "tie":
        non_tie = arm1 if arm2 == "tie" else arm2
        final = non_tie
        contradicted = False
    else:
        final = "tie"
        contradicted = True

    # Swap-averaged score margin: each arm occupies opposite slots across the two
    # calls, so averaging the two orders cancels an additive L/R position offset
    # exactly. This recovers signal the holistic-winner contradiction→TIE rule
    # discards. None if either call lacks complete scores (then 'unscored').
    mcp_side_p1 = "right" if pos1_left_is == "vanilla" else "left"
    van_side_p1 = "left" if pos1_left_is == "vanilla" else "right"
    mcp_side_p2 = "right" if pos2_left_is == "vanilla" else "left"
    van_side_p2 = "left" if pos2_left_is == "vanilla" else "right"

    mcp_p1, mcp_p2 = _arm_total(pos1.criterion_scores, mcp_side_p1), _arm_total(pos2.criterion_scores, mcp_side_p2)
    van_p1, van_p2 = _arm_total(pos1.criterion_scores, van_side_p1), _arm_total(pos2.criterion_scores, van_side_p2)

    if None in (mcp_p1, mcp_p2, van_p1, van_p2):
        mcp_avg = vanilla_avg = margin = None
        final_by_score: str = "unscored"
    else:
        mcp_avg = (mcp_p1 + mcp_p2) / 2.0
        vanilla_avg = (van_p1 + van_p2) / 2.0
        margin = mcp_avg - vanilla_avg
        if margin > epsilon:
            final_by_score = "mcp"
        elif margin < -epsilon:
            final_by_score = "vanilla"
        else:
            final_by_score = final  # holistic winner breaks ties inside the dead-zone

    total = {
        k: pos1.usage.get(k, 0) + pos2.usage.get(k, 0)
        for k in {"input_tokens", "output_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"}
    }

    return SwapVerdict(
        final=final,  # type: ignore[arg-type]
        pos1=pos1,
        pos2=pos2,
        contradicted=contradicted,
        total_usage=total,
        mcp_avg=mcp_avg,
        vanilla_avg=vanilla_avg,
        margin=margin,
        final_by_score=final_by_score,  # type: ignore[arg-type]
    )
