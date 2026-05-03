"""
Measurement #4 — tier inference accuracy.

For every labeled sample, call `infer_tier(query)` and compare against
`expected_tier`. Reports overall accuracy plus a confusion matrix.

Usage:
    python -m evals.runners.run_tier
    python -m evals.runners.run_tier --json
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.runners._loader import EvalSample, LoaderStats, iter_valid, load_samples  # noqa: E402
from src.engine.enrichment import infer_tier  # noqa: E402

TIERS = ("lite", "standard", "deep")


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
    parser.add_argument("--out", type=Path)
    args = parser.parse_args(argv)

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
