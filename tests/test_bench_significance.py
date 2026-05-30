"""Unit tests for the bench significance analyzer math.

The decision gate (binomial p, Wilson CI, paired McNemar) is only trustworthy
if the statistics are correct, so these pin known closed-form values.
"""
from __future__ import annotations

from evals.scripts.bench_significance import (
    analyze,
    analyze_margins,
    binom_two_sided_p,
    paired_compare,
    paired_margins,
    wilcoxon_signed_rank,
    wilson_ci,
    _query_margin,
    _rules_state,
)


def _cs(left, right):
    """Build a criterion_scores dict from two 5-tuples (helpfulness…intent_fit)."""
    keys = ["helpfulness", "correctness", "depth", "structure", "intent_fit"]
    d = {}
    for i, k in enumerate(keys):
        d["left_" + k] = left[i]
        d["right_" + k] = right[i]
    return d


def _mrun(query, l1, r1, l2, r2):
    """A run with pos1 (left=vanilla,right=mcp) and pos2 (left=mcp,right=vanilla)."""
    return {"query": query, "verdict": {
        "pos1": {"criterion_scores": _cs(l1, r1)},
        "pos2": {"criterion_scores": _cs(l2, r2)},
    }}


def _run(query: str, final: str, *, rules_loaded=None) -> dict:
    meta = {}
    if rules_loaded is not None:
        meta["rules_loaded"] = rules_loaded
    return {"query": query, "verdict": {"final": final}, "mcp": {"mcp_meta": meta}}


class TestBinomTwoSided:
    def test_all_one_side_is_symmetric_tail(self):
        # Only X=0 and X=10 are as-unlikely-as the observed X=0 → 2 * 0.5^10.
        assert binom_two_sided_p(0, 10) == 2 * (0.5 ** 10)

    def test_dead_center_is_one(self):
        # X=5 of 10 is the single most likely outcome ⇒ every outcome qualifies.
        assert binom_two_sided_p(5, 10) == 1.0

    def test_matches_known_eval_value(self):
        # 4 mcp vs 11 vanilla, n=15 decisive → the 0.1185 the report showed.
        assert round(binom_two_sided_p(4, 15), 4) == 0.1185

    def test_zero_trials_is_one(self):
        assert binom_two_sided_p(0, 0) == 1.0


class TestWilson:
    def test_symmetric_center(self):
        lo, hi = wilson_ci(5, 10)
        assert abs((lo + hi) / 2 - 0.5) < 1e-9   # 50% point estimate ⇒ symmetric
        assert round(lo, 3) == 0.237
        assert round(hi, 3) == 0.763

    def test_lower_bound_clears_half_when_dominant(self):
        lo, hi = wilson_ci(18, 20)
        assert lo > 0.5            # decision-gate PASS shape
        assert hi <= 1.0

    def test_zero_trials_guarded(self):
        assert wilson_ci(0, 0) == (0.0, 0.0)


class TestRulesState:
    def test_on_when_any_loaded(self):
        runs = [_run("q1", "mcp", rules_loaded=["honest-uncertainty"]), _run("q2", "tie", rules_loaded=[])]
        assert _rules_state(runs) == "ON"

    def test_off_when_all_empty(self):
        runs = [_run("q1", "mcp", rules_loaded=[]), _run("q2", "tie", rules_loaded=[])]
        assert _rules_state(runs) == "OFF"

    def test_unknown_when_field_absent(self):
        runs = [_run("q1", "mcp"), _run("q2", "tie")]
        assert _rules_state(runs) == "unknown"


class TestAnalyze:
    def test_counts_share_and_rules(self):
        report = {
            "config": {"model": "m"},
            "runs": [
                _run("q1", "mcp", rules_loaded=["r"]),
                _run("q2", "mcp", rules_loaded=["r"]),
                _run("q3", "vanilla", rules_loaded=["r"]),
                _run("q4", "tie", rules_loaded=["r"]),
            ],
        }
        s = analyze(report)
        assert (s["mcp"], s["vanilla"], s["ties"]) == (2, 1, 1)
        assert s["decisive"] == 3
        assert abs(s["mcp_decisive_share"] - 2 / 3) < 1e-9
        assert s["rules"] == "ON"


class TestPairedCompare:
    def test_gains_losses_and_mcnemar(self):
        base = {"runs": [_run("q1", "vanilla"), _run("q2", "mcp"), _run("q3", "vanilla"), _run("q4", "tie")]}
        other = {"runs": [_run("q1", "mcp"), _run("q2", "mcp"), _run("q3", "vanilla"), _run("q4", "mcp")]}
        c = paired_compare(base, other)
        assert c["paired_n"] == 4
        assert c["base_mcp_wins"] == 1
        assert c["other_mcp_wins"] == 3
        assert c["gains"] == 2          # q1 van→mcp, q4 tie→mcp
        assert c["losses"] == 0
        assert c["net"] == 2
        assert c["discordant"] == 2
        assert c["mcnemar_p"] == binom_two_sided_p(0, 2)   # == 0.5

    def test_join_excludes_non_overlapping_queries(self):
        base = {"runs": [_run("q1", "mcp"), _run("q2", "vanilla")]}
        other = {"runs": [_run("q1", "mcp"), _run("q3", "mcp")]}  # q3 only here, q2 only there
        c = paired_compare(base, other)
        assert c["paired_n"] == 1       # only q1 is in both
        assert c["gains"] == 0 and c["losses"] == 0

    def test_regression_direction(self):
        base = {"runs": [_run("q1", "mcp"), _run("q2", "mcp")]}
        other = {"runs": [_run("q1", "vanilla"), _run("q2", "mcp")]}
        c = paired_compare(base, other)
        assert c["gains"] == 0
        assert c["losses"] == 1         # q1 mcp→vanilla
        assert c["net"] == -1


class TestWilcoxon:
    def test_all_positive_is_smallest_exact_p(self):
        # n=5 distinct positive diffs → only all-same-sign assignments are as extreme
        # ⇒ two-sided p = 2 / 2^5.
        W, p = wilcoxon_signed_rank([1, 2, 3, 4, 5])
        assert W == 0.0
        assert abs(p - 2 / 32) < 1e-9

    def test_symmetric_is_one(self):
        _W, p = wilcoxon_signed_rank([1, -1, 2, -2])
        assert abs(p - 1.0) < 1e-9

    def test_epsilon_dead_zone_drops_values(self):
        _W, p = wilcoxon_signed_rank([0.3, -0.4], epsilon=0.5)   # all inside dead-zone
        assert p == 1.0
        _W2, p2 = wilcoxon_signed_rank([0.0, 0.0])
        assert p2 == 1.0

    def test_large_n_uses_normal_approx_and_stays_valid(self):
        W, p = wilcoxon_signed_rank([1.0] * 21)   # n>20 → normal approx branch
        assert 0.0 <= p <= 1.0
        assert p < 0.05                            # 21 consistent positives ⇒ significant


class TestQueryMargin:
    def test_margin_recovers_true_difference(self):
        # pos1 left=vanilla=15 right=mcp=11 ; pos2 left=mcp=16 right=vanilla=10
        run = _mrun("q1", (3, 3, 3, 3, 3), (2, 2, 2, 2, 3), (3, 3, 3, 3, 4), (2, 2, 2, 2, 2))
        assert _query_margin(run) == 1.0          # (11+16)/2 - (15+10)/2

    def test_none_when_scores_missing(self):
        assert _query_margin({"verdict": {"pos1": {"criterion_scores": {}}, "pos2": {"criterion_scores": {}}}}) is None


class TestMarginReports:
    def test_analyze_margins_counts(self):
        rep = {"runs": [
            _mrun("q1", (2, 2, 2, 2, 2), (3, 3, 3, 3, 3), (3, 3, 3, 3, 3), (2, 2, 2, 2, 2)),  # mcp +5
            _mrun("q2", (3, 3, 3, 3, 3), (2, 2, 2, 2, 2), (2, 2, 2, 2, 2), (3, 3, 3, 3, 3)),  # mcp -5
        ]}
        s = analyze_margins(rep, epsilon=0.0)
        assert s["n_scored"] == 2 and s["wins"] == 1 and s["losses"] == 1 and s["ties"] == 0

    def test_paired_margins_delta(self):
        base = {"runs": [_mrun("q1", (2, 2, 2, 2, 2), (2, 2, 2, 2, 2), (2, 2, 2, 2, 2), (2, 2, 2, 2, 2))]}   # margin 0
        other = {"runs": [_mrun("q1", (2, 2, 2, 2, 2), (3, 3, 3, 3, 3), (3, 3, 3, 3, 3), (2, 2, 2, 2, 2))]}  # margin +5
        pm = paired_margins(base, other)
        assert pm["paired_n"] == 1
        assert pm["mean_delta"] == 5.0
