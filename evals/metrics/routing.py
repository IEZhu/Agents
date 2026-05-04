"""Pure metric functions for routing evaluation."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Sequence


@dataclass
class RoutingResult:
    sample_id: str
    expected_agent: str
    predicted_agent: str
    predicted_top_k: list[str]
    method: str  # "cache" | "keyword" | "fallback"
    language: str | None = None
    source: str | None = None
    label_confidence: float | None = None


@dataclass
class RoutingMetrics:
    total: int
    top1_correct: int
    top3_correct: int
    method_counts: dict[str, int] = field(default_factory=dict)
    per_language: dict[str, tuple[int, int]] = field(default_factory=dict)  # lang -> (correct, total)
    per_source: dict[str, tuple[int, int]] = field(default_factory=dict)
    confusion: dict[tuple[str, str], int] = field(default_factory=dict)  # (expected, predicted) -> count
    worst_cases: list[RoutingResult] = field(default_factory=list)

    @property
    def top1_accuracy(self) -> float:
        return self.top1_correct / self.total if self.total else 0.0

    @property
    def top3_accuracy(self) -> float:
        return self.top3_correct / self.total if self.total else 0.0


def compute_metrics(results: Sequence[RoutingResult], worst_n: int = 10) -> RoutingMetrics:
    method_counts: Counter[str] = Counter()
    per_language: dict[str, list[int]] = defaultdict(lambda: [0, 0])  # [correct, total]
    per_source: dict[str, list[int]] = defaultdict(lambda: [0, 0])
    confusion: Counter[tuple[str, str]] = Counter()
    misses: list[RoutingResult] = []

    top1_correct = 0
    top3_correct = 0

    for r in results:
        method_counts[r.method] += 1
        is_top1 = r.predicted_agent == r.expected_agent
        is_top3 = r.expected_agent in r.predicted_top_k
        if is_top1:
            top1_correct += 1
        if is_top3:
            top3_correct += 1
        if not is_top1:
            misses.append(r)

        lang = r.language or "?"
        per_language[lang][1] += 1
        if is_top1:
            per_language[lang][0] += 1

        source = r.source or "?"
        per_source[source][1] += 1
        if is_top1:
            per_source[source][0] += 1

        confusion[(r.expected_agent, r.predicted_agent)] += 1

    misses.sort(key=lambda r: -(r.label_confidence or 0.0))

    return RoutingMetrics(
        total=len(results),
        top1_correct=top1_correct,
        top3_correct=top3_correct,
        method_counts=dict(method_counts),
        per_language={k: (v[0], v[1]) for k, v in per_language.items()},
        per_source={k: (v[0], v[1]) for k, v in per_source.items()},
        confusion=dict(confusion),
        worst_cases=misses[:worst_n],
    )


def format_markdown(metrics: RoutingMetrics) -> str:
    lines: list[str] = []
    lines.append(f"- **Top-1 accuracy**: {metrics.top1_correct}/{metrics.total} = {metrics.top1_accuracy:.1%}")
    lines.append(f"- **Top-3 accuracy**: {metrics.top3_correct}/{metrics.total} = {metrics.top3_accuracy:.1%}")
    lines.append("")
    lines.append("**Prediction method distribution:**")
    for method in sorted(metrics.method_counts):
        lines.append(f"- {method}: {metrics.method_counts[method]}")
    lines.append("")

    lines.append("**Per-language top-1 accuracy:**")
    for lang in sorted(metrics.per_language):
        c, t = metrics.per_language[lang]
        pct = c / t if t else 0.0
        lines.append(f"- `{lang}`: {c}/{t} ({pct:.0%})")
    lines.append("")

    lines.append("**Per-source top-1 accuracy:**")
    for src in sorted(metrics.per_source):
        c, t = metrics.per_source[src]
        pct = c / t if t else 0.0
        lines.append(f"- `{src}`: {c}/{t} ({pct:.0%})")
    lines.append("")

    if metrics.worst_cases:
        lines.append(f"**Top-{len(metrics.worst_cases)} miss-cases (sorted by label_confidence desc):**")
        lines.append("")
        lines.append("| id | expected | predicted | method | conf |")
        lines.append("|---|---|---|---|---|")
        for r in metrics.worst_cases:
            lines.append(
                f"| `{r.sample_id}` | `{r.expected_agent}` | `{r.predicted_agent}` "
                f"| {r.method} | {r.label_confidence or 0:.2f} |"
            )

    return "\n".join(lines)
