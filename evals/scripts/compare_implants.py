"""A/B comparison of implant retrieval — semantic-only vs preferred-implants fast-path.

Runs ``run_retrieval`` twice on the same dataset:

  * **Baseline** — ``expected_from_agent=True``, ``use_preferred_implants=False``
    (pure semantic retrieval, ground truth = expected_agent's preferred_implants).

  * **Treatment** — ``expected_from_agent=True``, ``use_preferred_implants=True``
    (preferred_implants forwarded into the retriever's fast-path; ground truth
    identical to baseline so the metrics are comparable).

Emits a markdown delta report to ``evals/reports/implants_preferred_ab.md``
(or stdout with ``--stdout``) showing precision@k, recall@k and MRR for both
modes plus the delta. This is the "proof that it got better" artifact.

Usage:
    python -m evals.scripts.compare_implants
    python -m evals.scripts.compare_implants --stdout
    python -m evals.scripts.compare_implants --out path/to/report.md
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.metrics.retrieval import RetrievalMetrics, compute_metrics  # noqa: E402
from evals.runners._loader import load_samples  # noqa: E402
from evals.runners.run_retrieval import run  # noqa: E402

DEFAULT_OUT = REPO_ROOT / "evals" / "reports" / "implants_preferred_ab.md"


def _fmt_delta(baseline: float, treatment: float) -> str:
    delta = treatment - baseline
    sign = "+" if delta >= 0 else ""
    return f"{sign}{delta:.2f}"


def _row(label: str, baseline: float, treatment: float) -> str:
    return f"| {label} | {baseline:.2f} | {treatment:.2f} | {_fmt_delta(baseline, treatment)} |"


def render_report(
    baseline: RetrievalMetrics,
    treatment: RetrievalMetrics,
    loader_meta: dict,
) -> str:
    lines: list[str] = [
        "# Implants A/B — semantic-only vs preferred-implants fast-path",
        "",
        "Ground truth derived per-sample from each ``expected_agent``'s declared",
        "``preferred_implants`` in agent frontmatter. Baseline runs the retriever",
        "with no fast-path; treatment forwards the same implants into the retriever",
        "so they are deterministically loaded ahead of semantic results.",
        "",
        f"- Total samples: {loader_meta['total_samples']}",
        f"- Drift: {loader_meta['drift_count']}",
        f"- Fetch errors: {loader_meta['fetch_errors']}",
        f"- Samples with non-empty expected (baseline): {baseline.samples_with_expected}",
        f"- Samples with non-empty expected (treatment): {treatment.samples_with_expected}",
        "",
        "| Metric | Baseline (semantic only) | Treatment (preferred fast-path) | Δ |",
        "| --- | --- | --- | --- |",
    ]

    if baseline.samples_with_expected == 0 or treatment.samples_with_expected == 0:
        lines.append("| _no expected labels — derivation produced 0 ground-truth_ | — | — | — |")
        return "\n".join(lines) + "\n"

    for k in sorted(baseline.precision_at):
        lines.append(_row(f"precision@{k}", baseline.precision_at[k], treatment.precision_at.get(k, 0.0)))
    for k in sorted(baseline.recall_at):
        lines.append(_row(f"recall@{k}", baseline.recall_at[k], treatment.recall_at.get(k, 0.0)))
    lines.append(_row("MRR", baseline.mrr, treatment.mrr))
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="compare_implants", description=__doc__)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="markdown report path")
    parser.add_argument("--stdout", action="store_true", help="print report to stdout instead of writing")
    args = parser.parse_args(argv)

    # Load the dataset once so both passes see identical samples (and only one HF call).
    preloaded = load_samples()

    _, base_impl, base_meta = run(
        preloaded=preloaded,
        use_preferred_implants=False,
        expected_from_agent=True,
    )
    _, treat_impl, treat_meta = run(
        preloaded=preloaded,
        use_preferred_implants=True,
        expected_from_agent=True,
    )

    # Sanity: both runs consume the same preloaded sample list, so every
    # loader-side field except the mode toggles must agree. A divergence here
    # would mean run() mutated state under us or the loader returned
    # different counts for identical input — either way, surface the bug
    # immediately instead of rendering misleading deltas.
    _MODE_KEYS = {"use_preferred_implants", "expected_from_agent"}
    base_static = {k: v for k, v in base_meta.items() if k not in _MODE_KEYS}
    treat_static = {k: v for k, v in treat_meta.items() if k not in _MODE_KEYS}
    assert base_static == treat_static, (
        f"Loader metadata diverged between baseline and treatment runs: "
        f"baseline={base_static!r} treatment={treat_static!r}"
    )

    base_metrics = compute_metrics(base_impl)
    treat_metrics = compute_metrics(treat_impl)
    report = render_report(base_metrics, treat_metrics, base_meta)

    if args.stdout:
        print(report)
    else:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
