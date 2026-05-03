"""
Measurement #1 — routing accuracy.

For every labeled sample, simulate a cold-start prediction:
  1. Keyword match via `SemanticRouter.match_keywords()`; top match wins if hits ≥ 1.
  2. Else → `universal_agent` fallback (matches LLM-side default for OOS queries).

The persistent semantic router cache (`INSTALL_DATA_DIR/router_cache.npz`)
is intentionally bypassed: it is pre-populated on developer machines and
would make this measurement non-deterministic across environments.

Reports top-1 / top-3 accuracy, per-source / per-language breakdown,
confusion matrix, and worst miss-cases.

Usage:
    python -m evals.runners.run_routing
    python -m evals.runners.run_routing --json
    python -m evals.runners.run_routing --out evals/reports/routing.md
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.metrics.routing import RoutingResult, compute_metrics, format_markdown  # noqa: E402
from evals.runners._loader import iter_valid, load_samples  # noqa: E402
from src.engine.router import SemanticRouter  # noqa: E402


def predict_one(router: SemanticRouter, query: str, sample_id: str, label: dict) -> RoutingResult:
    keyword_hits = router.match_keywords(query)
    top_k = [m[0] for m in keyword_hits[:3]]

    if keyword_hits and keyword_hits[0][1] > 0:
        return RoutingResult(
            sample_id=sample_id,
            expected_agent=label["expected_agent"],
            predicted_agent=keyword_hits[0][0],
            predicted_top_k=top_k,
            method="keyword",
            language=label.get("language"),
            source=label["id"].rsplit("-", 1)[0],
            label_confidence=label.get("label_confidence"),
        )

    return RoutingResult(
        sample_id=sample_id,
        expected_agent=label["expected_agent"],
        predicted_agent="universal_agent",
        predicted_top_k=["universal_agent"],
        method="fallback",
        language=label.get("language"),
        source=label["id"].rsplit("-", 1)[0],
        label_confidence=label.get("label_confidence"),
    )


def run() -> tuple[list[RoutingResult], dict]:
    samples, stats = load_samples()
    router = SemanticRouter()
    results: list[RoutingResult] = []
    for sample in iter_valid(samples):
        try:
            r = predict_one(router, sample.query, sample.label["id"], sample.label)
        except Exception as exc:  # pragma: no cover
            print(f"  ERROR predicting for {sample.label['id']}: {exc!r}", file=sys.stderr)
            continue
        results.append(r)
    return results, {
        "total_samples": stats.total,
        "drift_count": stats.drift,
        "drift_ids": stats.drift_ids,
        "fetch_errors": stats.fetch_errors,
        "used_local_cache": stats.used_local_cache,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_routing", description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    parser.add_argument("--out", type=Path, help="write markdown report to this path")
    args = parser.parse_args(argv)

    results, loader_meta = run()
    metrics = compute_metrics(results)

    if args.json:
        payload = {
            "loader": loader_meta,
            "top1_accuracy": metrics.top1_accuracy,
            "top3_accuracy": metrics.top3_accuracy,
            "method_counts": metrics.method_counts,
            "per_language": {k: list(v) for k, v in metrics.per_language.items()},
            "per_source": {k: list(v) for k, v in metrics.per_source.items()},
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    report = "# Routing accuracy\n\n"
    report += f"Loader: total={loader_meta['total_samples']} drift={loader_meta['drift_count']} "
    report += f"fetch_errors={loader_meta['fetch_errors']} local_cache={loader_meta['used_local_cache']}\n\n"
    report += format_markdown(metrics)
    report += "\n"

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
