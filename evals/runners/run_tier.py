"""
Measurement #4 — tier inference accuracy.

For every labeled sample, call `infer_tier(query)` and compare against
`expected_tier`. Reports overall accuracy plus a confusion matrix.

`--compare` scores both arms on the same samples — the legacy length+regex rule
against the intent classifier — and reports a paired exact McNemar test plus the
deep-tier share, which is the cost-relevant quantity. Because the classifier's
lexicons were written while looking at one half of this set, the run also reports
a held-out half (partitioned by the parity of a hash of the sample id — a plain
split, not a stratified one), which is the only
number that is evidence of generalisation.

Usage:
    python -m evals.runners.run_tier
    python -m evals.runners.run_tier --json
    python -m evals.runners.run_tier --compare
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.runners._loader import EvalSample, LoaderStats, iter_valid, load_samples  # noqa: E402
from src.engine.enrichment import _legacy_infer_tier, infer_tier  # noqa: E402
from src.engine.intent import classify_intent  # noqa: E402

TIERS = ("lite", "standard", "deep")
_TIER_RANK = {tier: index for index, tier in enumerate(TIERS)}


def _is_valid(tier: str | None) -> bool:
    """Whether a label carries a tier this runner can compare against."""
    return tier in _TIER_RANK


def _is_heldout(sample_id: str) -> bool:
    """Stable, seed-free half of the set, stratification-free but deterministic.

    Uses the sample id rather than row order so the split does not move when the
    dataset is re-sorted or extended.
    """
    return int(hashlib.md5(sample_id.encode()).hexdigest(), 16) % 2 == 1


def _exact_mcnemar(pairs: list[tuple[bool, bool]]) -> tuple[int, int, float]:
    """Two-sided exact sign test over discordant pairs.

    Returns (legacy_only_right, classifier_only_right, p). Discordant-only is the
    point: agreements carry no information about which arm is better.
    """
    b = sum(1 for legacy_ok, intent_ok in pairs if legacy_ok and not intent_ok)
    c = sum(1 for legacy_ok, intent_ok in pairs if intent_ok and not legacy_ok)
    n = b + c
    if n == 0:
        return b, c, 1.0
    tail = sum(math.comb(n, k) for k in range(min(b, c) + 1))
    return b, c, min(1.0, 2 * tail / 2 ** n)


def _arm_stats(rows: list[dict], key: str) -> dict:
    total = len(rows)
    correct = sum(1 for r in rows if r[key] == r["expected"])
    return {
        "total": total,
        "correct": correct,
        "accuracy": correct / total if total else 0.0,
        "deep_share": (sum(1 for r in rows if r[key] == "deep") / total) if total else 0.0,
        # `.get` guards a row whose expected_tier is missing or misspelled:
        # iter_valid filters on fetch_error/drift, not on label completeness, so
        # a bad label must score as wrong rather than crash the whole run.
        # Rows with a missing or misspelled expected_tier count as wrong in
        # `correct`, but must not land in `under`/`over`: ranking them as -1 put
        # every such row in `over` for BOTH arms and inflated the headline
        # over-provisioning number.
        "under": sum(
            1 for r in rows
            if _is_valid(r["expected"]) and _is_valid(r[key])
            and _TIER_RANK[r[key]] < _TIER_RANK[r["expected"]]
        ),
        "over": sum(
            1 for r in rows
            if _is_valid(r["expected"]) and _is_valid(r[key])
            and _TIER_RANK[r[key]] > _TIER_RANK[r["expected"]]
        ),
        "per_expected": {
            tier: [
                sum(1 for r in rows if r["expected"] == tier and r[key] == tier),
                sum(1 for r in rows if r["expected"] == tier),
            ]
            for tier in TIERS
        },
    }


def run_compare(
    preloaded: tuple[list[EvalSample], LoaderStats] | None = None,
) -> tuple[list[dict], dict]:
    """Score both arms on every valid sample."""
    samples, stats = preloaded if preloaded is not None else load_samples()
    rows: list[dict] = []
    for sample in iter_valid(samples):
        profile = classify_intent(sample.query)
        rows.append({
            "id": sample.label.get("id", ""),
            "expected": sample.label.get("expected_tier"),
            "legacy": _legacy_infer_tier(sample.query),
            "intent": profile.tier,
            "mode": profile.mode,
            "depth_score": profile.depth_score,
            "heldout": _is_heldout(sample.label["id"]),
            "language": sample.label.get("language"),
        })
    return rows, {
        "total_samples": stats.total,
        "drift_count": stats.drift,
        "fetch_errors": stats.fetch_errors,
        "used_local_cache": stats.used_local_cache,
    }


def _render_compare(rows: list[dict], loader_meta: dict) -> str:
    report = "# Tier inference — legacy vs intent classifier\n\n"
    report += f"Loader: total={loader_meta['total_samples']} drift={loader_meta['drift_count']}\n\n"
    for name, subset in (
        ("full set", rows),
        ("held-out half", [r for r in rows if r["heldout"]]),
        ("tuning half", [r for r in rows if not r["heldout"]]),
    ):
        if not subset:
            continue
        legacy = _arm_stats(subset, "legacy")
        intent = _arm_stats(subset, "intent")
        b, c, p = _exact_mcnemar([
            (r["legacy"] == r["expected"], r["intent"] == r["expected"]) for r in subset
        ])
        report += f"## {name} (n={len(subset)})\n\n"
        report += "| arm | accuracy | deep-share | under | over | " + " | ".join(f"`{t}`" for t in TIERS) + " |\n"
        report += "|---|---|---|---|---|" + "|".join(["---"] * len(TIERS)) + "|\n"
        for label, arm in (("legacy", legacy), ("classifier", intent)):
            per = " | ".join(f"{arm['per_expected'][t][0]}/{arm['per_expected'][t][1]}" for t in TIERS)
            report += (
                f"| {label} | {arm['correct']}/{arm['total']} = {arm['accuracy']:.1%} "
                f"| {arm['deep_share']:.1%} | {arm['under']} | {arm['over']} | {per} |\n"
            )
        report += (
            f"\nPaired exact McNemar: legacy-only-right={b}, classifier-only-right={c}, "
            f"two-sided p={p:.4f}"
        )
        report += " — **not significant**\n\n" if p >= 0.05 else " — significant at 0.05\n\n"
    modes = Counter(r["mode"] for r in rows)
    report += "**Mode distribution (full set):** "
    report += ", ".join(f"`{m}` {n}" for m, n in modes.most_common()) + "\n"
    return report


def run(
    preloaded: tuple[list[EvalSample], LoaderStats] | None = None,
) -> tuple[list[dict], dict]:
    samples, stats = preloaded if preloaded is not None else load_samples()
    results: list[dict] = []
    for sample in iter_valid(samples):
        predicted = infer_tier(sample.query)
        expected = sample.label.get("expected_tier")
        results.append({
            "id": sample.label["id"],
            "expected": expected,
            "predicted": predicted,
            "correct": predicted == expected,
            "language": sample.label.get("language"),
        })
    return results, {
        "total_samples": stats.total,
        "drift_count": stats.drift,
        "fetch_errors": stats.fetch_errors,
        "used_local_cache": stats.used_local_cache,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_tier", description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--compare", action="store_true",
                        help="score the legacy rule and the intent classifier side by side")
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

    if args.compare:
        rows, loader_meta = run_compare()
        if args.json:
            # Every subset and every McNemar result the text report shows, so an
            # automated consumer can reproduce the documented comparison.
            subsets = {
                "full": rows,
                "heldout": [r for r in rows if r["heldout"]],
                "tuning": [r for r in rows if not r["heldout"]],
            }
            payload = {"loader": loader_meta, "rows": rows}
            for name, subset in subsets.items():
                b, c, pvalue = _exact_mcnemar([
                    (r["legacy"] == r["expected"], r["intent"] == r["expected"])
                    for r in subset
                ])
                payload[name] = {
                    "legacy": _arm_stats(subset, "legacy"),
                    "intent": _arm_stats(subset, "intent"),
                    "mcnemar": {
                        "legacy_only_right": b, "intent_only_right": c, "p_value": pvalue,
                    },
                }
            print(json.dumps(payload, indent=2))
            return 0
        report = _render_compare(rows, loader_meta)
        if args.out:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_text(report, encoding="utf-8")
            print(f"wrote {args.out}")
        else:
            print(report)
        return 0

    results, loader_meta = run()
    total = len(results)
    correct = sum(1 for r in results if r["correct"])

    confusion: Counter[tuple[str, str]] = Counter()
    per_expected: dict[str, list[int]] = {t: [0, 0] for t in TIERS}
    for r in results:
        confusion[(r["expected"], r["predicted"])] += 1
        if r["expected"] in per_expected:
            per_expected[r["expected"]][1] += 1
            if r["correct"]:
                per_expected[r["expected"]][0] += 1

    if args.json:
        print(json.dumps({
            "loader": loader_meta,
            "total": total,
            "correct": correct,
            "accuracy": correct / total if total else 0.0,
            "per_expected": {t: per_expected[t] for t in TIERS},
            "confusion": [[e, p, n] for (e, p), n in confusion.items()],
        }, indent=2))
        return 0

    accuracy = correct / total if total else 0.0
    report = "# Tier inference\n\n"
    report += f"Loader: total={loader_meta['total_samples']} drift={loader_meta['drift_count']}\n\n"
    report += f"- **Accuracy**: {correct}/{total} = {accuracy:.1%}\n\n"
    report += "**Per expected tier:**\n"
    for t in TIERS:
        c, n = per_expected[t]
        pct = c / n if n else 0.0
        report += f"- `{t}`: {c}/{n} ({pct:.0%})\n"
    report += "\n**Confusion matrix (rows=expected, cols=predicted):**\n\n"
    report += "| expected \\ predicted | " + " | ".join(f"`{t}`" for t in TIERS) + " |\n"
    report += "|---|" + "|".join(["---"] * len(TIERS)) + "|\n"
    for e in TIERS:
        row = "| " + f"`{e}`"
        for p in TIERS:
            row += f" | {confusion.get((e, p), 0)}"
        row += " |\n"
        report += row

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
