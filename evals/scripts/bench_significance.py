"""
Significance + methodology-health analysis for MCP-vs-vanilla bench reports.

The bench report (evals/reports/*.json) records a 3-way verdict per query
(vanilla / mcp / tie). The headline win counts alone don't say whether a result
is real or noise, so this read-only analyzer post-processes `runs[].verdict.*`
and prints the stats the decision gate needs:

  - decisive win split (ties excluded) + MCP decisive-win-share
  - two-sided exact binomial p-value vs p=0.5 (is the split distinguishable
    from a coin flip?)
  - Wilson 95% CI on the MCP decisive-win-share (ship the fix only if the
    lower bound clears 0.5)
  - contradiction rate (order-swap health; high ⇒ position bias / ambiguity)
  - routing_path distribution (after the LLM-picker fix this should read
    `llm_picker`, not `keyword_fallback`)
  - tier distribution + whether the rules layer was ON or OFF (for ablations)

With TWO OR MORE reports, it also runs a PAIRED comparison of every later
report against the first (the baseline), joining on query text. Because both
runs answer the SAME queries, the correct test is McNemar's exact test on the
discordant pairs (queries whose MCP-won status flipped), not a two-sample
proportion test. This answers:
  - "did rules help?"  → full-rules report  vs  RULES_ENABLED=0 report
  - "did the fix beat baseline?" → new report  vs  archived baseline

Usage:
    python -m evals.scripts.bench_significance
        Analyze the most recently modified report in evals/reports/.

    python -m evals.scripts.bench_significance <baseline.json> <variant.json> [...]
        Analyze each, then paired-compare each variant against the baseline.

Read-only: reads the given JSON(s), writes nothing.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import Counter
from math import comb, erfc
from pathlib import Path
from typing import Any

from evals.judges.pairwise_judge import _CRITERIA, _SCORE_MIN, _arm_total  # single source of truth — keep margin re-derivation in sync

_REPORTS_DIR = Path(__file__).resolve().parents[1] / "reports"


def _latest_report() -> Path | None:
    reports = sorted(_REPORTS_DIR.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    return reports[0] if reports else None


def binom_two_sided_p(k: int, n: int, p: float = 0.5) -> float:
    """Exact two-sided binomial p-value by the method of small p-values:
    sum the probability of every outcome no more likely than the observed one.
    Correct for any p; for p=0.5 it reduces to the symmetric two-tailed test
    (and to the exact McNemar p when fed the discordant-pair counts)."""
    if n == 0:
        return 1.0
    probs = [comb(n, i) * (p ** i) * ((1 - p) ** (n - i)) for i in range(n + 1)]
    observed = probs[k]
    # Relative slack so the observed outcome and its float-equal mirror are always
    # included, without an absolute floor that would wrongly pull in far-more-likely
    # outcomes when `observed` is tiny (extreme k/n).
    return min(1.0, sum(pr for pr in probs if pr <= observed * (1.0 + 1e-9)))


def wilson_ci(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a binomial proportion (better than normal
    approximation at small n / extreme proportions). Default z ⇒ 95%."""
    if n == 0:
        return (0.0, 0.0)
    phat = k / n
    denom = 1.0 + z * z / n
    center = (phat + z * z / (2 * n)) / denom
    half = (z * ((phat * (1 - phat) / n + z * z / (4 * n * n)) ** 0.5)) / denom
    return (max(0.0, center - half), min(1.0, center + half))


def _rules_state(runs: list[dict[str, Any]]) -> str:
    """ON if any run carried a non-empty rules_loaded, OFF if all empty,
    'unknown' if the field is absent. Lets ablation reports self-identify."""
    seen = False
    any_loaded = False
    for r in runs:
        meta = r.get("mcp", {}).get("mcp_meta") or {}
        if "rules_loaded" in meta:
            seen = True
            if meta.get("rules_loaded"):
                any_loaded = True
    if not seen:
        return "unknown"
    return "ON" if any_loaded else "OFF"


def analyze(report: dict[str, Any]) -> dict[str, Any]:
    runs = report.get("runs", [])
    finals = Counter(r.get("verdict", {}).get("final") for r in runs)
    contradicted = sum(1 for r in runs if r.get("verdict", {}).get("contradicted"))
    routing = Counter((r.get("mcp", {}).get("mcp_meta") or {}).get("routing_path") for r in runs)
    tiers = Counter((r.get("mcp", {}).get("mcp_meta") or {}).get("tier") for r in runs)

    mcp = finals.get("mcp", 0)
    vanilla = finals.get("vanilla", 0)
    ties = finals.get("tie", 0)
    decisive = mcp + vanilla
    share = (mcp / decisive) if decisive else 0.0
    lo, hi = wilson_ci(mcp, decisive)
    p = binom_two_sided_p(mcp, decisive)

    return {
        "n": len(runs),
        "mcp": mcp,
        "vanilla": vanilla,
        "ties": ties,
        "decisive": decisive,
        "mcp_decisive_share": share,
        "wilson95": (lo, hi),
        "binom_p": p,
        "contradiction_rate": (contradicted / len(runs)) if runs else 0.0,
        "contradicted": contradicted,
        "routing": dict(routing),
        "tiers": dict(tiers),
        "rules": _rules_state(runs),
        "config": report.get("config", {}),
    }


def _final_by_query(report: dict[str, Any]) -> dict[tuple[str, int], str]:
    """Map (query text, occurrence) → final verdict. Query text is a stable join
    key across runs (idx can drift if sampling changes; the same seed keeps the
    text). The occurrence counter keeps duplicate query texts in one report from
    collapsing — sample_queries does not de-dup, so the Nth occurrence of a text
    in the baseline pairs with the Nth in the other report and paired_n stays
    correct."""
    out: dict[tuple[str, int], str] = {}
    seen: Counter = Counter()
    for r in report.get("runs", []):
        q = r.get("query")
        if q is None:
            continue
        # Advance the occurrence per row, BEFORE the value filter, so a row missing
        # `final` in only one report doesn't shift later occurrence numbers and
        # misalign the cross-report join.
        seen[q] += 1
        final = r.get("verdict", {}).get("final")
        if final is not None:
            out[(q, seen[q])] = final
    return out


def paired_compare(baseline: dict[str, Any], other: dict[str, Any]) -> dict[str, Any]:
    """Paired McNemar comparison on the binary outcome 'MCP won', over the
    queries present in BOTH reports. `gains` = queries MCP lost/tied in baseline
    but won in `other`; `losses` = the reverse. McNemar's exact p tests whether
    MCP's win-rate changed (H0: gains and losses are equally likely)."""
    b_final = _final_by_query(baseline)
    o_final = _final_by_query(other)
    common = sorted(set(b_final) & set(o_final))

    gains = losses = base_wins = other_wins = 0
    for q in common:
        bw = b_final[q] == "mcp"
        ow = o_final[q] == "mcp"
        base_wins += bw
        other_wins += ow
        if ow and not bw:
            gains += 1
        elif bw and not ow:
            losses += 1

    discordant = gains + losses
    mcnemar_p = binom_two_sided_p(min(gains, losses), discordant)
    return {
        "paired_n": len(common),
        "base_mcp_wins": base_wins,
        "other_mcp_wins": other_wins,
        "gains": gains,
        "losses": losses,
        "net": gains - losses,
        "discordant": discordant,
        "mcnemar_p": mcnemar_p,
    }


# --------------------------------------------------------------------------- #
# Swap-averaged score-margin (position-bias-robust), re-derived from stored scores
# --------------------------------------------------------------------------- #

def _query_margin(run: dict[str, Any]) -> float | None:
    """Swap-averaged mcp-minus-vanilla score margin for one run, re-derived from
    the stored per-criterion scores (no judge re-call). Report convention (see
    judge_with_swap): pos1 left=vanilla/right=mcp; pos2 left=mcp/right=vanilla.
    Additive L/R position bias cancels under the average."""
    v = run.get("verdict", {})
    p1 = (v.get("pos1") or {}).get("criterion_scores")
    p2 = (v.get("pos2") or {}).get("criterion_scores")
    mcp_p1, van_p1 = _arm_total(p1, "right"), _arm_total(p1, "left")
    mcp_p2, van_p2 = _arm_total(p2, "left"), _arm_total(p2, "right")
    if None in (mcp_p1, van_p1, mcp_p2, van_p2):
        return None
    return ((mcp_p1 + mcp_p2) - (van_p1 + van_p2)) / 2.0


def _normal_sf(z: float) -> float:
    """Upper-tail standard-normal survival function, stdlib only (no scipy)."""
    return 0.5 * erfc(z / (2 ** 0.5))


def wilcoxon_signed_rank(values: list[float], epsilon: float = 0.0) -> tuple[float, float]:
    """Two-sided Wilcoxon signed-rank test of H0: median(values)=0.

    Exact (enumerate all 2^n sign assignments over the rank vector) for n<=20;
    normal approximation with tie + continuity correction for larger n. Values
    inside the epsilon dead-zone are dropped (standard zero handling). Stdlib only.
    Returns (W, two-sided p)."""
    diffs = [v for v in values if abs(v) > epsilon]
    n = len(diffs)
    if n == 0:
        return (0.0, 1.0)
    order = sorted(range(n), key=lambda i: abs(diffs[i]))
    ranks = [0.0] * n
    i = 0
    while i < n:  # average-rank tied |diff| groups
        j = i
        while j + 1 < n and abs(diffs[order[j + 1]]) == abs(diffs[order[i]]):
            j += 1
        avg = (i + 1 + j + 1) / 2.0
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    w_plus = sum(ranks[i] for i in range(n) if diffs[i] > 0)
    w_minus = sum(ranks[i] for i in range(n) if diffs[i] < 0)
    W = min(w_plus, w_minus)
    total = w_plus + w_minus
    if n <= 20:
        from itertools import product
        count = 0
        for signs in product((0, 1), repeat=n):
            wp = sum(ranks[i] for i in range(n) if signs[i])
            if min(wp, total - wp) <= W + 1e-9:
                count += 1
        return (W, min(1.0, count / (2 ** n)))
    mu = n * (n + 1) / 4.0
    tie = sum(t ** 3 - t for t in Counter(abs(d) for d in diffs).values())
    var = n * (n + 1) * (2 * n + 1) / 24.0 - tie / 48.0
    if var <= 0:
        return (W, 1.0)
    z = (W - mu + 0.5) / (var ** 0.5)
    return (W, min(1.0, 2.0 * _normal_sf(abs(z))))


def analyze_margins(report: dict[str, Any], epsilon: float = 0.0) -> dict[str, Any]:
    margins = [m for r in report.get("runs", []) if (m := _query_margin(r)) is not None]
    n = len(margins)
    wins = sum(1 for m in margins if m > epsilon)
    losses = sum(1 for m in margins if m < -epsilon)
    ties = sum(1 for m in margins if abs(m) <= epsilon)
    W, p = wilcoxon_signed_rank(margins, epsilon=epsilon)
    # Margin scale = max possible |mcp−vanilla| = #criteria × (max − min). Old (1-5)
    # reports omit judge_score_max → default 5, so the label reads ±20 for them and ±45 for 1-10.
    scale = len(_CRITERIA) * (report.get("config", {}).get("judge_score_max", 5) - _SCORE_MIN)
    return {
        "n_scored": n,
        "wins": wins, "losses": losses, "ties": ties,
        "mean_margin": (sum(margins) / n) if n else 0.0,
        "median_margin": statistics.median(margins) if margins else 0.0,
        "wilcoxon_W": W, "wilcoxon_p": p, "epsilon": epsilon, "scale": scale,
    }


def _margins_by_query(report: dict[str, Any]) -> dict[tuple[str, int], float]:
    # (query, occurrence) key — same rationale as _final_by_query: preserve
    # duplicate query texts so paired margin deltas don't drop rows.
    out: dict[tuple[str, int], float] = {}
    seen: Counter = Counter()
    for r in report.get("runs", []):
        q = r.get("query")
        if q is None:
            continue
        seen[q] += 1  # advance per occurrence before the value filter (see _final_by_query)
        m = _query_margin(r)
        if m is not None:
            out[(q, seen[q])] = m
    return out


def paired_margins(baseline: dict[str, Any], other: dict[str, Any], epsilon: float = 0.0) -> dict[str, Any]:
    """Paired per-query margin delta (other − baseline) over common queries, with
    a Wilcoxon signed-rank test. Continuous analog of paired_compare's McNemar —
    uses magnitude, not just win-flip sign, so it has more power."""
    bm, om = _margins_by_query(baseline), _margins_by_query(other)
    common = sorted(set(bm) & set(om))
    deltas = [om[q] - bm[q] for q in common]
    W, p = wilcoxon_signed_rank(deltas, epsilon=epsilon)
    base_scale = baseline.get("config", {}).get("judge_score_max", 5)
    other_scale = other.get("config", {}).get("judge_score_max", 5)
    return {
        "paired_n": len(common),
        "mean_delta": (sum(deltas) / len(deltas)) if deltas else 0.0,
        "wilcoxon_W": W, "wilcoxon_p": p,
        "scale_mismatch": base_scale != other_scale,
    }


def _fmt_report(stats: dict[str, Any], path: Path) -> str:
    lo, hi = stats["wilson95"]
    cfg = stats["config"]
    ships = lo > 0.5
    sig = stats["binom_p"] < 0.05
    return "\n".join([
        f"Report: {path.name}",
        f"  model={cfg.get('model')}  judge={cfg.get('judge_model')}  "
        f"dataset={cfg.get('dataset')}  seed={cfg.get('seed')}  rules={stats['rules']}",
        "",
        f"Verdicts (N={stats['n']}):  mcp={stats['mcp']}  vanilla={stats['vanilla']}  ties={stats['ties']}",
        f"Decisive (ties excluded): {stats['decisive']}  →  MCP share = {stats['mcp_decisive_share']:.1%}",
        f"  two-sided exact binomial p (vs coin flip) = {stats['binom_p']:.4f}"
        f"  [{'significant' if sig else 'NOT significant'} at α=0.05]",
        f"  Wilson 95% CI on MCP share = [{lo:.1%}, {hi:.1%}]",
        f"  DECISION GATE (lower bound > 50%): {'PASS — MCP wins' if ships else 'not met'}",
        "",
        "Methodology health:",
        f"  contradiction rate = {stats['contradiction_rate']:.1%}  ({stats['contradicted']}/{stats['n']})",
        f"  routing_path = {stats['routing']}",
        f"  tier         = {stats['tiers']}",
    ])


def _fmt_paired(cmp: dict[str, Any], base_name: str, other_name: str) -> str:
    sig = cmp["mcnemar_p"] < 0.05
    verb = "more" if cmp["net"] > 0 else ("fewer" if cmp["net"] < 0 else "the same number of")
    return "\n".join([
        f"PAIRED  {other_name}  vs  {base_name}  (baseline)",
        f"  paired queries (in both) = {cmp['paired_n']}",
        f"  MCP wins: baseline {cmp['base_mcp_wins']}  →  variant {cmp['other_mcp_wins']}",
        f"  flips: +{cmp['gains']} gained, -{cmp['losses']} lost  (net {cmp['net']:+d} MCP wins, {verb})",
        f"  McNemar exact p (did MCP win-rate change?) = {cmp['mcnemar_p']:.4f}"
        f"  [{'significant' if sig else 'NOT significant'} at α=0.05]",
    ])


def _fmt_margins(m: dict[str, Any]) -> str:
    sig = m["wilcoxon_p"] < 0.05
    return "\n".join([
        f"Score margin (swap-averaged, position-bias-robust, ε={m['epsilon']}):",
        f"  mcp={m['wins']}  vanilla={m['losses']}  tie={m['ties']}  (scored {m['n_scored']})",
        f"  mean margin = {m['mean_margin']:+.2f}  median = {m['median_margin']:+.2f}  (scale ±{m['scale']}, + favors MCP)",
        f"  Wilcoxon signed-rank p (margin ≠ 0) = {m['wilcoxon_p']:.4f}"
        f"  [{'significant' if sig else 'NOT significant'} at α=0.05]",
    ])


def _fmt_paired_margins(pm: dict[str, Any], base_name: str, other_name: str) -> str:
    sig = pm["wilcoxon_p"] < 0.05
    direction = "favors variant" if pm["mean_delta"] > 0 else ("favors baseline" if pm["mean_delta"] < 0 else "no shift")
    lines = [
        f"PAIRED MARGIN  {other_name}  vs  {base_name}  (baseline)",
        f"  paired queries = {pm['paired_n']}  ·  mean Δmargin = {pm['mean_delta']:+.2f}  ({direction})",
        f"  Wilcoxon signed-rank p (Δmargin ≠ 0) = {pm['wilcoxon_p']:.4f}"
        f"  [{'significant' if sig else 'NOT significant'} at α=0.05]",
    ]
    if pm.get("scale_mismatch"):
        lines.append("  ⚠ scale mismatch: reports use different judge_score_max (1-5 vs 1-10) — Δmargin is NOT comparable; re-run both on the same scale.")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("reports", nargs="*", help="report JSON(s); first is the paired-comparison baseline (default: newest in evals/reports/)")
    parser.add_argument("--epsilon", type=float, default=0.0, help="tie dead-zone for the score margin (default 0.0; significance comes from Wilcoxon, not ε)")
    args = parser.parse_args(argv)

    if args.reports:
        paths = [Path(p) for p in args.reports]
    else:
        latest = _latest_report()
        paths = [latest] if latest else []

    if not paths or any(p is None or not p.exists() for p in paths):
        print(f"error: report(s) not found ({paths})", file=sys.stderr)
        return 1

    reports = [json.loads(p.read_text(encoding="utf-8")) for p in paths]

    blocks = [
        _fmt_report(analyze(rep), path) + "\n\n" + _fmt_margins(analyze_margins(rep, args.epsilon))
        for rep, path in zip(reports, paths)
    ]
    print(("\n\n" + "─" * 60 + "\n\n").join(blocks))

    if len(reports) >= 2:
        print("\n" + "=" * 60 + "\nPAIRED COMPARISONS (vs first report as baseline)\n" + "=" * 60)
        for rep, path in zip(reports[1:], paths[1:]):
            print()
            print(_fmt_paired(paired_compare(reports[0], rep), paths[0].name, path.name))
            print()
            print(_fmt_paired_margins(paired_margins(reports[0], rep, args.epsilon), paths[0].name, path.name))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
