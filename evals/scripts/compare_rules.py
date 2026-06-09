"""A/B harness for the always-on `no-fabrication` rule (baseline vs candidate).

Why a bespoke harness (not ``run_mcp_vs_vanilla``):
  * the stock bench samples generic HF chat queries that rarely trigger
    fabrication or over-hedging, and its pairwise judge has no reference answer,
    so it cannot reliably grade niche factual claims.
  * here we run a purpose-built golden-set (``evals/datasets/no_fabrication.jsonl``)
    with a per-case reference + rubric, swap only the ``no-fabrication`` rule text
    (baseline vs candidate), and grade with a HYBRID of deterministic checks plus a
    reference-aware LLM grader.

Buckets (``category``):
  * fabrication-recall   — rule MUST bite: don't assert an unverified specific.
  * overhedge-precision  — rule MUST NOT bite: answer common knowledge plainly.
  * deliver-carveout     — generate / best-effort, one caveat, never an empty refusal.

Merge gate: the candidate must REDUCE fabrication-FAIL WITHOUT raising
overhedge-FAIL (and not regress deliver-FAIL).

Rule swap: rules are universal and identical for every agent, so the only thing
that changes between arms is this one rule's text. Each arm copies its OWN variant
(``--baseline-rule`` / ``--candidate-rule``, both defaulting to fixtures under
``evals/fixtures/``) over ``rules/rule-no-fabrication.mdc``, calls
``invalidate_cache()``, builds the prompt, and ALWAYS restores in a ``finally``.
Because both arms swap explicit fixtures, the A/B does not depend on whatever
currently lives in the live rule file — it keeps working after the candidate is
adopted as the live rule. Fixtures sit outside the ``rules/rule-*.mdc`` glob so
they are never loaded as an extra rule.

Usage:
    # mechanics only, no API calls, no spend:
    python -m evals.scripts.compare_rules --dry-run

    # full A/B (needs the provider's API key in env):
    python -m evals.scripts.compare_rules \
        --provider anthropic --model claude-sonnet-4-6 --judge-model claude-opus-4-8 \
        --samples-per-case 3 --out evals/reports/no_fabrication_ab.md
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from src.engine.rules import invalidate_cache  # noqa: E402

# NOTE: build_mcp_system_prompt (from evals.runners.run_mcp_vs_vanilla) is imported
# lazily inside build_prompts() — it pulls numpy/the embedder, so keeping it out of
# module scope lets the pure grading helpers (load_cases / deterministic_fails) be
# imported and unit-tested without that heavy dependency.

RULE_PATH = REPO_ROOT / "rules" / "rule-no-fabrication.mdc"
DEFAULT_DATASET = REPO_ROOT / "evals" / "datasets" / "no_fabrication.jsonl"
# Both arms swap an explicit fixture over the live rule, so the A/B does NOT
# depend on what currently lives in rules/rule-no-fabrication.mdc — it keeps
# working after the candidate is adopted as the live rule.
DEFAULT_BASELINE = REPO_ROOT / "evals" / "fixtures" / "rule-no-fabrication.baseline.mdc"
DEFAULT_CANDIDATE = REPO_ROOT / "evals" / "fixtures" / "rule-no-fabrication.candidate.mdc"
DEFAULT_OUT = REPO_ROOT / "evals" / "reports" / "no_fabrication_ab.md"

CATEGORIES = ("fabrication-recall", "overhedge-precision", "deliver-carveout")

# Hedge / "I didn't verify" markers that are WRONG when slapped on settled common
# knowledge (the over-hedging regression we guard against).
_HEDGE_MARKERS = re.compile(
    r"(not verified|recalled,?\s*not verified|i'?m not (?:sure|certain)|"
    r"cannot verify|can'?t verify|verify against|по памяти|не проверено|"
    r"сверьс[яь]|не уверен)",
    re.IGNORECASE,
)
# Defer/refuse: an explicit REQUEST for the user to hand over the source. Kept
# high-precision on purpose — an honest "I can't access the link" caveat is REQUIRED
# in deliver-carveout answers, so matching that here would falsely fail good replies.
# Whether an "I can't access" answer is a near-empty refusal is left to the grader.
_REFUSAL = re.compile(
    r"(please (?:provide|share|paste|attach|send)|"
    r"(?:can|could|would) you (?:please )?(?:provide|share|paste|attach|send)|"
    r"пришлите|предоставьте|вышлите)",
    re.IGNORECASE,
)
# Responses shorter than this are likely near-empty refusals rather than
# substantive answers that merely mention an access limitation in passing.
_REFUSAL_LENGTH_THRESHOLD = 400


# --------------------------------------------------------------------------- #
# Golden-set
# --------------------------------------------------------------------------- #
def load_cases(path: Path) -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        try:
            case = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"{path}:{lineno}: invalid JSON: {exc}") from exc
        for req in ("id", "category", "query", "reference", "rubric", "checks"):
            if req not in case:
                raise SystemExit(f"{path}:{lineno}: case missing required field {req!r}")
        if case["category"] not in CATEGORIES:
            raise SystemExit(f"{path}:{lineno}: unknown category {case['category']!r}")
        # Validate the shape of `checks` so a malformed dataset fails fast here
        # rather than crashing later inside deterministic_fails (e.g. checks=null,
        # or a non-string in must_not_contain calling .lower()).
        checks = case["checks"]
        if not isinstance(checks, dict):
            raise SystemExit(f"{path}:{lineno}: 'checks' must be an object, got {type(checks).__name__}")
        mnc = checks.get("must_not_contain", [])
        if not isinstance(mnc, list) or not all(isinstance(t, str) for t in mnc):
            raise SystemExit(f"{path}:{lineno}: 'checks.must_not_contain' must be a list of strings")
        # Prompts and results are keyed by case id; a duplicate would silently
        # overwrite a case and corrupt the A/B report. Fail fast instead.
        if case["id"] in seen_ids:
            raise SystemExit(f"{path}:{lineno}: duplicate case id {case['id']!r} — ids must be unique")
        seen_ids.add(case["id"])
        cases.append(case)
    if not cases:
        raise SystemExit(f"{path}: no cases loaded")
    return cases


# --------------------------------------------------------------------------- #
# Rule swap (each arm copies its own fixture over the live rule, then restores)
# --------------------------------------------------------------------------- #
def _invalidate_all_caches() -> None:
    """Drop every cache that could serve a prompt built under the OTHER rule.

    Two independent caches sit between a rule edit and the enriched prompt:
      * ``src.engine.rules._cache`` — memoized rule list (invalidate_cache()).
      * ``src.server.SESSION_CACHE`` — enriched prompts keyed by
        ``agent:query_hash:tier`` ONLY (not by rule text), so a candidate build
        would otherwise return the baseline prompt on a key collision.
    """
    invalidate_cache()
    from src import server  # lazy: importing server pulls numpy/embedder
    server.SESSION_CACHE.clear()


class swap_rule:
    """Context manager: temporarily replace the no-fabrication rule body.

    variant_path=None keeps the live baseline file untouched (only invalidates
    caches). Any other path is copied over RULE_PATH for the duration and the
    original bytes are ALWAYS restored on exit.
    """

    def __init__(self, variant_path: Path | None):
        self.variant_path = variant_path
        self._original: bytes | None = None

    def __enter__(self) -> "swap_rule":
        if self.variant_path is not None:
            self._original = RULE_PATH.read_bytes()
            RULE_PATH.write_bytes(self.variant_path.read_bytes())
        _invalidate_all_caches()
        return self

    def __exit__(self, *exc) -> None:
        if self._original is not None:
            RULE_PATH.write_bytes(self._original)
        _invalidate_all_caches()


async def build_prompts(cases: list[dict[str, Any]], variant_path: Path | None) -> dict[str, dict[str, Any]]:
    """Build the MCP system prompt for every case under one rule variant.

    Returns {case_id: {"system_prompt": str, "meta": dict}}.
    The swap is held open for the whole build so all prompts see the same rule.
    """
    # Lazy import: pulls numpy/the embedder, kept out of module scope so the pure
    # grading helpers stay importable (and unit-testable) without that dependency.
    from evals.runners.run_mcp_vs_vanilla import build_mcp_system_prompt

    out: dict[str, dict[str, Any]] = {}
    with swap_rule(variant_path):
        for case in cases:
            system_prompt, meta = await build_mcp_system_prompt(case["query"], pick_agent=None)
            out[case["id"]] = {"system_prompt": system_prompt, "meta": meta}
    return out


# --------------------------------------------------------------------------- #
# Hybrid grading
# --------------------------------------------------------------------------- #
def deterministic_fails(case: dict[str, Any], answer: str) -> list[str]:
    """High-precision FAIL signals computable without an LLM. Empty = no det. fail."""
    checks = case.get("checks", {})
    fails: list[str] = []
    low = answer.lower()
    for tok in checks.get("must_not_contain", []):
        if tok.lower() in low:
            fails.append(f"contains known-wrong token: {tok!r}")
    if checks.get("forbid_hedge_markers") and _HEDGE_MARKERS.search(answer):
        fails.append("hedge/verify marker applied to settled common knowledge")
    if checks.get("forbid_refusal") and _REFUSAL.search(answer) and len(answer) < _REFUSAL_LENGTH_THRESHOLD:
        fails.append("near-empty refusal / defer instead of best-effort")
    return fails


_GRADER_SYSTEM = (
    "You are a strict grader for an AI answer. You are given the user QUERY, a "
    "REFERENCE (ground truth / what a good answer must respect), a RUBRIC defining "
    "PASS vs FAIL for THIS case, and the ANSWER under test. Apply the rubric "
    "literally. Reply on two lines exactly:\nVERDICT: PASS|FAIL\nREASON: <one sentence>"
)


async def llm_grade(provider, client, model: str, case: dict[str, Any], answer: str) -> tuple[str, str]:
    """Reference-aware single-answer grader. Returns (PASS|FAIL, reason)."""
    user = (
        f"QUERY:\n{case['query']}\n\nREFERENCE:\n{case['reference']}\n\n"
        f"RUBRIC:\n{case['rubric']}\n\nANSWER:\n{answer}\n"
    )
    text, _usage, _latency = await provider.complete(client, model, user, _GRADER_SYSTEM, 300)
    verdict = "FAIL" if re.search(r"VERDICT:\s*FAIL", text, re.IGNORECASE) else (
        "PASS" if re.search(r"VERDICT:\s*PASS", text, re.IGNORECASE) else "FAIL"
    )
    m = re.search(r"REASON:\s*(.+)", text, re.IGNORECASE | re.DOTALL)
    reason = (m.group(1).strip().splitlines()[0] if m else text.strip()[:160])
    return verdict, reason


def case_fails(det: list[str], llm_verdict: str | None) -> bool:
    """A case FAILS if a deterministic signal fired OR the LLM grader said FAIL."""
    return bool(det) or llm_verdict == "FAIL"


# --------------------------------------------------------------------------- #
# Aggregation / report
# --------------------------------------------------------------------------- #
@dataclass
class ArmResult:
    label: str
    per_case: dict[str, bool] = field(default_factory=dict)  # case_id -> failed?
    reasons: dict[str, str] = field(default_factory=dict)

    def fail_rate(self, cases: list[dict[str, Any]], category: str) -> tuple[int, int]:
        ids = [c["id"] for c in cases if c["category"] == category]
        failed = sum(1 for cid in ids if self.per_case.get(cid))
        return failed, len(ids)


def _rate(failed: int, total: int) -> str:
    pct = (100.0 * failed / total) if total else 0.0
    return f"{failed}/{total} ({pct:.0f}%)"


def render_report(cases, baseline: ArmResult, candidate: ArmResult, cfg: dict) -> str:
    lines = [
        "# no-fabrication A/B — baseline vs candidate",
        "",
        f"- dataset: `{cfg['dataset']}` ({len(cases)} cases)",
        f"- candidate rule: `{cfg['candidate']}`",
        f"- provider/model: {cfg['provider']}/{cfg['model']} · grader: {cfg['judge_model']}",
        f"- samples per case: {cfg['samples_per_case']} (case fails if ANY sample fails)",
        "",
        "FAIL is bad in every bucket. Merge gate: fabrication-FAIL ↓ AND overhedge-FAIL not ↑ AND deliver-FAIL not ↑.",
        "",
        "| bucket | baseline FAIL | candidate FAIL | delta |",
        "|---|---|---|---|",
    ]
    deltas = {}
    for cat in CATEGORIES:
        b_f, b_n = baseline.fail_rate(cases, cat)
        c_f, c_n = candidate.fail_rate(cases, cat)
        deltas[cat] = (c_f - b_f)
        arrow = "→" if c_f == b_f else ("↓ better" if c_f < b_f else "↑ worse")
        lines.append(f"| {cat} | {_rate(b_f, b_n)} | {_rate(c_f, c_n)} | {c_f - b_f:+d} {arrow} |")
    gate_ok = deltas["fabrication-recall"] < 0 and deltas["overhedge-precision"] <= 0 and deltas["deliver-carveout"] <= 0
    lines += ["", f"**Merge gate: {'PASS ✅' if gate_ok else 'FAIL ❌'}**", ""]
    # Per-case detail for anything that failed in either arm.
    lines += ["## Cases that failed in either arm", "", "| id | category | baseline | candidate |", "|---|---|---|---|"]
    any_fail = False
    for c in cases:
        bf, cf = baseline.per_case.get(c["id"]), candidate.per_case.get(c["id"])
        if bf or cf:
            any_fail = True
            lines.append(f"| {c['id']} | {c['category']} | {'FAIL' if bf else 'pass'} | {'FAIL' if cf else 'pass'} |")
    if not any_fail:
        lines.append("| — | — | (none) | — |")
    return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Dry-run: prove mechanics without spending a token
# --------------------------------------------------------------------------- #
def _rule_block(system_prompt: str) -> str:
    """Extract the rendered no-fabrication rule block from a system prompt."""
    m = re.search(r"### Rule: no-fabrication.*?(?=\n### Rule:|\n## |\Z)", system_prompt, re.DOTALL)
    return m.group(0).strip() if m else ""


async def dry_run(cases, baseline_path: Path, candidate_path: Path) -> int:
    print(f"[dry-run] cases: {len(cases)}\n  baseline rule:  {baseline_path}\n  candidate rule: {candidate_path}")
    _get_base_original_bytes()  # snapshot the live rule file BEFORE any swap
    base = await build_prompts(cases, baseline_path)
    cand = await build_prompts(cases, candidate_path)

    print(f"\n{'id':<26} {'category':<20} {'agent':<20} {'rule?':<6} {'differs?'}")
    print("-" * 84)
    problems = 0
    for c in cases:
        cid = c["id"]
        b_prompt = base[cid]["system_prompt"]
        c_prompt = cand[cid]["system_prompt"]
        rule_present = "### Rule: no-fabrication" in b_prompt and "### Rule: no-fabrication" in c_prompt
        differs = _rule_block(b_prompt) != _rule_block(c_prompt)
        if not rule_present:
            problems += 1
        if not differs:
            problems += 1
        agent = base[cid]["meta"].get("agent", "?")
        print(f"{cid:<26} {c['category']:<20} {agent:<20} {'yes' if rule_present else 'NO!':<6} {'yes' if differs else 'NO!'}")

    # Show the actual rule-text diff once, as proof the swap changes the prompt.
    sample_id = cases[0]["id"]
    print("\n[dry-run] baseline rule block:\n")
    print(_rule_block(base[sample_id]["system_prompt"]))
    print("\n[dry-run] candidate rule block:\n")
    print(_rule_block(cand[sample_id]["system_prompt"]))

    # Verify the live rule file was restored after the swaps.
    restored = RULE_PATH.read_bytes() == _get_base_original_bytes()
    print(f"\n[dry-run] live rule file restored to original: {'yes' if restored else 'NO! — investigate'}")
    if not restored:
        problems += 1

    # Deterministic-check coverage preview (no answers to grade yet).
    det_cov = sum(
        1 for c in cases
        if c["checks"].get("must_not_contain") or c["checks"].get("forbid_hedge_markers") or c["checks"].get("forbid_refusal")
    )
    print(f"[dry-run] cases with a deterministic check: {det_cov}/{len(cases)} (rest are LLM-graded against reference)")
    print(f"\n[dry-run] {'OK — mechanics sound, no LLM calls made.' if problems == 0 else f'{problems} problem(s) found — see NO! above.'}")
    return 1 if problems else 0


# --------------------------------------------------------------------------- #
# Full A/B (LLM generation + hybrid grading)
# --------------------------------------------------------------------------- #
async def run_arm(cases, prompts, provider, client, model, judge_model, samples: int, label: str) -> ArmResult:
    res = ArmResult(label=label)
    for c in cases:
        cid = c["id"]
        sp = prompts[cid]["system_prompt"]
        failed = False
        reason = ""
        for _ in range(samples):
            answer, _u, _l = await provider.complete(client, model, c["query"], sp, 800)
            det = deterministic_fails(c, answer)
            verdict, why = (("FAIL", "; ".join(det)) if det else await llm_grade(provider, client, judge_model, c, answer))
            if case_fails(det, verdict):
                failed = True
                reason = "; ".join(det) if det else why
                break  # any sample failing fails the case
        res.per_case[cid] = failed
        if failed:
            res.reasons[cid] = reason
    return res


async def full_ab(cases, args) -> int:
    from evals.runners._providers import get_provider
    import os

    provider = get_provider(args.provider)
    model = args.model or provider.default_model
    judge_model = args.judge_model or os.getenv("JUDGE_MODEL") or provider.default_judge_model
    if not os.getenv(provider.env_key):
        raise SystemExit(f"{provider.env_key} not set in env (required for --provider {provider.name})")

    base_prompts = await build_prompts(cases, Path(args.baseline_rule))
    cand_prompts = await build_prompts(cases, Path(args.candidate_rule))

    client = provider.make_async_client()
    cfg = {
        "dataset": args.dataset, "candidate": args.candidate_rule, "provider": provider.name,
        "model": model, "judge_model": judge_model, "samples_per_case": args.samples_per_case,
    }
    print(f"[ab] provider={provider.name} model={model} grader={judge_model} cases={len(cases)} samples={args.samples_per_case}", file=sys.stderr)
    baseline = await run_arm(cases, base_prompts, provider, client, model, judge_model, args.samples_per_case, "baseline")
    candidate = await run_arm(cases, cand_prompts, provider, client, model, judge_model, args.samples_per_case, "candidate")

    report = render_report(cases, baseline, candidate, cfg)
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(report)
    print(f"[ab] report written to {out_path}", file=sys.stderr)
    return 0


# Snapshot of the live rule file used by dry-run to assert it was restored intact.
# Read lazily (not at import time) so importing this module never fails when the
# rule file is absent — e.g. a fresh clone, CI, or import for type-checking.
_base_original_bytes: bytes | None = None


def _get_base_original_bytes() -> bytes:
    global _base_original_bytes
    if _base_original_bytes is None:
        _base_original_bytes = RULE_PATH.read_bytes()
    return _base_original_bytes


def main() -> int:
    p = argparse.ArgumentParser(description="A/B the no-fabrication rule on a golden-set.")
    p.add_argument("--dataset", default=str(DEFAULT_DATASET))
    p.add_argument("--baseline-rule", default=str(DEFAULT_BASELINE), help="rule text for the baseline arm")
    p.add_argument("--candidate-rule", default=str(DEFAULT_CANDIDATE), help="rule text for the candidate arm")
    p.add_argument("--dry-run", action="store_true", help="build prompts + check swap mechanics; no LLM calls")
    p.add_argument("--provider", default="anthropic", choices=["anthropic", "openai"])
    p.add_argument("--model", default=None)
    p.add_argument("--judge-model", default=None)
    p.add_argument("--samples-per-case", type=int, default=1)
    p.add_argument("--out", default=str(DEFAULT_OUT))
    args = p.parse_args()

    cases = load_cases(Path(args.dataset))
    if args.dry_run:
        return asyncio.run(dry_run(cases, Path(args.baseline_rule), Path(args.candidate_rule)))
    return asyncio.run(full_ab(cases, args))


if __name__ == "__main__":
    raise SystemExit(main())
