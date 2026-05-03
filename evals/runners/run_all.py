"""
Orchestrator — runs all deterministic evals and writes a single markdown report.

Phase 1 measurements:
  1. Routing accuracy
  2. Skill retrieval
  3. (Implant retrieval — informational, no labeled ground truth yet)
  4. Tier inference

Phase 2 (LLM-as-judge) is gated separately and not included here.

Usage:
    python -m evals.runners.run_all                         # print to stdout
    python -m evals.runners.run_all --baseline              # write to baseline.md
    python -m evals.runners.run_all --out evals/reports/2026-05-03_<sha>.md
"""

from __future__ import annotations

import argparse
import datetime as dt
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.metrics.retrieval import compute_metrics as compute_retrieval_metrics  # noqa: E402
from evals.metrics.retrieval import format_markdown as format_retrieval  # noqa: E402
from evals.metrics.routing import compute_metrics as compute_routing_metrics  # noqa: E402
from evals.metrics.routing import format_markdown as format_routing  # noqa: E402
from evals.runners.run_retrieval import run as run_retrieval  # noqa: E402
from evals.runners.run_routing import run as run_routing  # noqa: E402
from evals.runners.run_tier import run as run_tier  # noqa: E402

REPORTS_DIR = REPO_ROOT / "evals" / "reports"
BASELINE_PATH = REPORTS_DIR / "baseline.md"


def _git_sha() -> str:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
            cwd=REPO_ROOT,
        )
        return result.stdout.strip() or "nogit"
    except FileNotFoundError:
        return "nogit"


def _build_report(routing_results, routing_meta, skill_results, implant_results, tier_results) -> str:
    routing_metrics = compute_routing_metrics(routing_results)
    skill_metrics = compute_retrieval_metrics(skill_results)
    implant_metrics = compute_retrieval_metrics(implant_results)

    total = len(tier_results)
    tier_correct = sum(1 for r in tier_results if r["correct"])
    tier_accuracy = tier_correct / total if total else 0.0

    sha = _git_sha()
    today = dt.date.today().isoformat()

    lines: list[str] = []
    lines.append(f"# Agents-Core eval baseline — {today} ({sha})")
    lines.append("")
    lines.append("## BLUF")
    lines.append("")
    lines.append(f"- Routing top-1: **{routing_metrics.top1_accuracy:.1%}** "
                 f"({routing_metrics.top1_correct}/{routing_metrics.total})")
    lines.append(f"- Routing top-3: **{routing_metrics.top3_accuracy:.1%}**")
    if skill_metrics.samples_with_expected:
        lines.append(f"- Skills P@3: **{skill_metrics.precision_at[3]:.2f}** | "
                     f"R@5: **{skill_metrics.recall_at[5]:.2f}** | "
                     f"MRR: **{skill_metrics.mrr:.2f}**")
    else:
        lines.append("- Skills: no labeled expected_skills (skipped P/R)")
    lines.append(f"- Tier accuracy: **{tier_accuracy:.1%}** ({tier_correct}/{total})")
    lines.append("")
    lines.append(f"Loader: total={routing_meta['total_samples']}, "
                 f"drift={routing_meta['drift_count']}, "
                 f"fetch_errors={routing_meta['fetch_errors']}, "
                 f"local_cache={routing_meta['used_local_cache']}")
    lines.append("")

    lines.append("## 1. Routing accuracy")
    lines.append("")
    lines.append(format_routing(routing_metrics))
    lines.append("")

    lines.append("## 2. Skill retrieval")
    lines.append("")
    lines.append(format_retrieval(skill_metrics, "Skills"))
    lines.append("")

    lines.append("## 3. Implant retrieval (informational)")
    lines.append("")
    lines.append(format_retrieval(implant_metrics, "Implants"))
    lines.append("")

    lines.append("## 4. Tier inference")
    lines.append("")
    lines.append(f"- Accuracy: {tier_correct}/{total} = {tier_accuracy:.1%}")
    lines.append("")
    lines.append("**Confusion matrix (rows=expected, cols=predicted):**")
    lines.append("")
    tiers = ("lite", "standard", "deep")
    lines.append("| expected \\ predicted | " + " | ".join(f"`{t}`" for t in tiers) + " |")
    lines.append("|---|" + "|".join(["---"] * len(tiers)) + "|")
    from collections import Counter
    confusion: Counter[tuple[str, str]] = Counter()
    for r in tier_results:
        confusion[(r["expected"], r["predicted"])] += 1
    for e in tiers:
        row = f"| `{e}`"
        for p in tiers:
            row += f" | {confusion.get((e, p), 0)}"
        lines.append(row + " |")
    lines.append("")

    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_all", description=__doc__)
    parser.add_argument("--baseline", action="store_true",
                        help="write to evals/reports/baseline.md (committed)")
    parser.add_argument("--out", type=Path, help="explicit output path")
    args = parser.parse_args(argv)

    print("→ running routing eval...", file=sys.stderr)
    routing_results, routing_meta = run_routing()

    print("→ running retrieval eval...", file=sys.stderr)
    skill_results, implant_results, _ = run_retrieval()

    print("→ running tier eval...", file=sys.stderr)
    tier_results, _tier_meta = run_tier()

    report = _build_report(
        routing_results, routing_meta, skill_results, implant_results, tier_results
    )

    if args.baseline:
        out_path = BASELINE_PATH
    elif args.out:
        out_path = args.out
    else:
        print(report)
        return 0

    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report, encoding="utf-8")
    print(f"wrote {out_path}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
