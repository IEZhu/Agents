"""Pure metric functions for skill/implant retrieval evaluation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence


@dataclass
class RetrievalResult:
    sample_id: str
    expected: list[str]      # ground-truth IDs (skill or implant names)
    retrieved: list[str]     # predicted IDs in rank order


@dataclass
class RetrievalMetrics:
    total: int
    samples_with_expected: int  # records where expected is non-empty (P/R defined)
    precision_at: dict[int, float] = field(default_factory=dict)
    recall_at: dict[int, float] = field(default_factory=dict)
    mrr: float = 0.0


def _precision_at_k(retrieved: list[str], expected: set[str], k: int) -> float:
    if k <= 0 or not retrieved:
        return 0.0
    top = retrieved[:k]
    hits = sum(1 for r in top if r in expected)
    return hits / k


def _recall_at_k(retrieved: list[str], expected: set[str], k: int) -> float:
    if not expected:
        return 0.0
    top = set(retrieved[:k])
    return len(top & expected) / len(expected)


def _reciprocal_rank(retrieved: list[str], expected: set[str]) -> float:
    for i, r in enumerate(retrieved, 1):
        if r in expected:
            return 1.0 / i
    return 0.0


def compute_metrics(results: Sequence[RetrievalResult], ks: tuple[int, ...] = (1, 3, 5)) -> RetrievalMetrics:
    with_expected = [r for r in results if r.expected]

    # Without any labeled expectations, P/R/MRR are undefined — return empty
    # dicts and a sentinel ``mrr=0.0`` so JSON consumers see ``precision_at: {}``
    # (clear "no data") instead of a fabricated zero score, and so callers can
    # gate on ``samples_with_expected == 0`` without having to second-guess
    # whether the zeros are real misses or absence of ground truth.
    if not with_expected:
        return RetrievalMetrics(
            total=len(results),
            samples_with_expected=0,
            precision_at={},
            recall_at={},
            mrr=0.0,
        )

    n = len(with_expected)
    precision_at = {k: 0.0 for k in ks}
    recall_at = {k: 0.0 for k in ks}
    mrr_sum = 0.0

    for r in with_expected:
        expected_set = set(r.expected)
        for k in ks:
            precision_at[k] += _precision_at_k(r.retrieved, expected_set, k)
            recall_at[k] += _recall_at_k(r.retrieved, expected_set, k)
        mrr_sum += _reciprocal_rank(r.retrieved, expected_set)

    return RetrievalMetrics(
        total=len(results),
        samples_with_expected=n,
        precision_at={k: precision_at[k] / n for k in ks},
        recall_at={k: recall_at[k] / n for k in ks},
        mrr=mrr_sum / n,
    )


def format_markdown(metrics: RetrievalMetrics, label: str) -> str:
    lines: list[str] = [f"### {label}"]
    lines.append(
        f"- Samples with non-empty `expected_{label.lower()}`: "
        f"{metrics.samples_with_expected}/{metrics.total}"
    )
    if metrics.samples_with_expected == 0:
        lines.append("- No labeled expectations — metrics not computed.")
        return "\n".join(lines)
    for k in sorted(metrics.precision_at):
        lines.append(f"- precision@{k}: {metrics.precision_at[k]:.2f}")
    for k in sorted(metrics.recall_at):
        lines.append(f"- recall@{k}: {metrics.recall_at[k]:.2f}")
    lines.append(f"- MRR: {metrics.mrr:.2f}")
    return "\n".join(lines)
