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

JUDGE_SYSTEM_PROMPT = """You are an impartial expert evaluator. You will be shown a USER QUERY and two AI responses labelled LEFT and RIGHT. Decide which response better serves the user.

Evaluation criteria (apply all five, weight equally):
1. **helpfulness** — does it answer the question and move the user forward?
2. **correctness** — are the claims factually accurate, free of fabrication?
3. **depth** — is the level of detail appropriate (not too thin, not bloated)?
4. **structure** — is it organised, skimmable, free of redundancy?
5. **intent-fit** — does it address what the user actually asked, not adjacent topics?

Score each criterion 1–5 for each response, then choose the overall winner.

Output ONLY via the `submit_verdict` tool. Choose:
- "left" — LEFT is clearly better
- "right" — RIGHT is clearly better
- "tie" — both are roughly equivalent OR both are bad

Be decisive: prefer "left"/"right" over "tie" unless the responses are genuinely indistinguishable. Position (LEFT vs RIGHT) is randomised — do not let order influence you.
"""

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
                "properties": {
                    "left_helpfulness": {"type": "integer", "minimum": 1, "maximum": 5},
                    "left_correctness": {"type": "integer", "minimum": 1, "maximum": 5},
                    "left_depth": {"type": "integer", "minimum": 1, "maximum": 5},
                    "left_structure": {"type": "integer", "minimum": 1, "maximum": 5},
                    "left_intent_fit": {"type": "integer", "minimum": 1, "maximum": 5},
                    "right_helpfulness": {"type": "integer", "minimum": 1, "maximum": 5},
                    "right_correctness": {"type": "integer", "minimum": 1, "maximum": 5},
                    "right_depth": {"type": "integer", "minimum": 1, "maximum": 5},
                    "right_structure": {"type": "integer", "minimum": 1, "maximum": 5},
                    "right_intent_fit": {"type": "integer", "minimum": 1, "maximum": 5},
                },
                "required": [
                    "left_helpfulness", "left_correctness", "left_depth", "left_structure", "left_intent_fit",
                    "right_helpfulness", "right_correctness", "right_depth", "right_structure", "right_intent_fit",
                ],
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


_ALLOWED_WINNERS = frozenset({"left", "right", "tie"})
_REQUIRED_CRITERION_KEYS = frozenset({
    "left_helpfulness", "left_correctness", "left_depth",
    "left_structure", "left_intent_fit",
    "right_helpfulness", "right_correctness", "right_depth",
    "right_structure", "right_intent_fit",
})


def _validate_verdict_payload(payload: dict[str, Any]) -> tuple[str, str, dict[str, int]]:
    """Validate that the judge payload conforms to VERDICT_SCHEMA before use.

    Fail-fast on malformed responses instead of letting unknown winners silently
    degrade to TIE in `aggregate_with_swap` — that would skew benchmark numbers
    without raising an error.
    """
    winner = payload.get("winner")
    reasoning = payload.get("reasoning")
    scores = payload.get("criterion_scores")
    if winner not in _ALLOWED_WINNERS:
        raise RuntimeError(f"Judge returned invalid winner: {winner!r}")
    if not isinstance(reasoning, str):
        raise RuntimeError(f"Judge returned non-string reasoning: {type(reasoning).__name__}")
    if not isinstance(scores, dict) or _REQUIRED_CRITERION_KEYS - scores.keys():
        missing = sorted(_REQUIRED_CRITERION_KEYS - (scores.keys() if isinstance(scores, dict) else set()))
        raise RuntimeError(f"Judge returned incomplete criterion_scores; missing keys: {missing}")
    return winner, reasoning, dict(scores)


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
    # Default bumped from 1024 → 4096 so reasoning models (gpt-5.x) have
    # headroom for both hidden reasoning tokens and the structured tool_call
    # output. Anthropic / non-reasoning models simply use less of the budget.
    """Provider-agnostic judge call. `provider` is a ProviderImpl from _providers.py."""
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
    )
