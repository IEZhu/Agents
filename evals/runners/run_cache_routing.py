"""
Measurement #4 — semantic-cache routing with the configured embedding model.

The router reuses an earlier decision when the nearest cached query is at least
`ROUTER_SIMILARITY_THRESHOLD` similar (`src/engine/router.py`). This eval treats
every labeled query as a cached decision and checks, leave-one-out, whether each
query's nearest other query carries the same expected agent, and how similar it is.

Reports, overall and per language:
  * nearest-neighbour agent accuracy, with no threshold;
  * coverage and precision at the configured threshold: the share of queries whose
    nearest neighbour is more similar than it (cache hits; the router requires a
    cosine distance below 1 - threshold), and the share of hits with the right agent;
  * precision at fixed coverage (10/20/30/50%) with the similarity that gives it.
    Queries tied with that similarity count as hits too, so the coverage reached can
    exceed the target. Similarity scales differ between models, so compare models at
    equal coverage and pick a new model's threshold from these rows;
  * cross-language neighbours: queries whose nearest neighbour is in another language.

Texts come from the loader (inline `query` fields or a fetch); queries are embedded
with `embed_query`, so the model's query prompt applies. The report counts each
dataset's rows, drifted and failed fetches, and repeated queries, which are embedded
once: a copy would be its own nearest neighbour. Nothing is written to the data
directory. Set EMBEDDING_MODEL to choose the model; the report names it.

Usage:
    python -m evals.runners.run_cache_routing
    python -m evals.runners.run_cache_routing --dataset evals/datasets/routing.jsonl \\
        --dataset evals/datasets/routing_ru.jsonl --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.runners._loader import ROUTING_JSONL, iter_valid, load_samples  # noqa: E402

COVERAGES = (0.1, 0.2, 0.3, 0.5)


def nearest(vectors: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Index and cosine similarity of each row's nearest other row.

    A zero or non-finite vector has no direction and would make every similarity
    NaN, so it is refused.
    """
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    if not np.all(np.isfinite(norms) & (norms > 0)):
        raise ValueError("every embedding needs a finite, non-zero norm")
    unit = vectors / norms
    sims = unit @ unit.T
    np.fill_diagonal(sims, -np.inf)
    idx = sims.argmax(axis=1)
    return idx, sims[np.arange(len(unit)), idx]


def _share(flags) -> float | None:
    flags = list(flags)
    return sum(flags) / len(flags) if flags else None


def cache_metrics(agents: list[str], languages: list[str], idx: np.ndarray, sim: np.ndarray,
                  threshold: float) -> dict:
    match = [agents[i] == agents[j] for i, j in enumerate(idx)]

    def block(rows: list[int]) -> dict:
        hits = [i for i in rows if sim[i] > threshold]
        out = {"queries": len(rows), "nn_accuracy": _share(match[i] for i in rows),
               "coverage": len(hits) / len(rows) if rows else None,
               "precision": _share(match[i] for i in hits), "at_coverage": {}}
        ranked = sorted(rows, key=lambda i: -sim[i])
        for coverage in COVERAGES:
            entry = {"precision": None, "similarity": None, "coverage": None, "tied": 0}
            if rows:
                # At least the stated share: 50% of five queries is three, not two.
                target = max(1, -(-round(coverage * 100) * len(rows) // 100))
                cutoff = sim[ranked[target - 1]]
                hits = [i for i in rows if sim[i] >= cutoff]
                entry = {"precision": _share(match[i] for i in hits), "similarity": float(cutoff),
                         "coverage": len(hits) / len(rows), "tied": len(hits) - target}
            out["at_coverage"][f"{coverage:.0%}"] = entry
        return out

    every = list(range(len(agents)))
    result = {"all": block(every)}
    for language in sorted(set(languages)):
        result[language] = block([i for i in every if languages[i] == language])
    cross = [i for i in every if languages[idx[i]] != languages[i]]
    result["cross_language"] = {"queries": len(cross), "nn_accuracy": _share(match[i] for i in cross)}
    return result


def _pct(value: float | None) -> str:
    return "—" if value is None else f"{value:.0%}"


def _cell(value: dict) -> str:
    similarity = "—" if value["similarity"] is None else f"{value['similarity']:.3f}"
    reached = f", {_pct(value['coverage'])} with ties" if value["tied"] else ""
    return f"{_pct(value['precision'])} ({similarity}{reached})"


def to_markdown(report: dict) -> str:
    lines = [f"# Cache routing: `{report['model']}`", "",
             f"{report['samples']} labeled queries; configured threshold {report['threshold']}.", "",
             "| Set | queries | NN accuracy | coverage @ threshold | precision @ threshold | "
             + " | ".join(f"precision @ {c:.0%} (similarity)" for c in COVERAGES) + " |",
             "|---|---:|---:|---:|---:|" + "---:|" * len(COVERAGES)]
    for name, block in report["sets"].items():
        if name == "cross_language":
            continue
        cells = [_cell(value) for value in block["at_coverage"].values()]
        lines.append(f"| {name} | {block['queries']} | {_pct(block['nn_accuracy'])} | {_pct(block['coverage'])} | "
                     f"{_pct(block['precision'])} | " + " | ".join(cells) + " |")
    cross = report["sets"]["cross_language"]
    lines += ["", f"Cross-language nearest neighbours: {cross['queries']}, with the right agent: {_pct(cross['nn_accuracy'])}.",
              "", "Loaded: " + "; ".join(
                  f"{d['dataset']}: {d['total']} rows, {d['used']} used (drift {d['drift']}, "
                  f"fetch errors {d['fetch_errors']})" for d in report["datasets"])
              + f". Repeated queries left out: {report['repeated']}."]
    return "\n".join(lines)


def _shown(path: Path) -> str:
    return str(path.relative_to(REPO_ROOT)) if path.is_relative_to(REPO_ROOT) else str(path)


def collect(paths: list[Path]) -> tuple[list, list[dict], int]:
    """Labeled samples from each dataset once, per-dataset loader counts, and repeats left out."""
    paths = [path.resolve() for path in paths]
    if len(set(paths)) != len(paths):
        raise SystemExit("each --dataset may be given only once")
    samples, datasets, seen, repeated = [], [], set(), 0
    for path in paths:
        loaded, stats = load_samples(path)
        usable = [s for s in iter_valid(loaded) if s.label.get("expected_agent")]
        datasets.append({"dataset": _shown(path), "total": stats.total, "drift": stats.drift,
                         "fetch_errors": stats.fetch_errors, "used": len(usable)})
        for sample in usable:
            key = " ".join(sample.query.split())
            if key in seen:
                repeated += 1
                continue
            seen.add(key)
            samples.append(sample)
    return samples, datasets, repeated


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Semantic-cache routing quality of the embedding model.")
    parser.add_argument("--dataset", action="append", type=Path,
                        help=f"labeled set (repeatable; default {ROUTING_JSONL.relative_to(REPO_ROOT)})")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", type=Path, help="markdown report path")
    args = parser.parse_args(argv)

    samples, datasets, repeated = collect(args.dataset or [ROUTING_JSONL])
    if len(samples) < 2:
        raise SystemExit("need at least two labeled queries")

    from src.engine import config
    from src.engine.embedder import embed_query

    vectors = np.stack([embed_query(s.query) for s in samples])
    idx, sim = nearest(vectors)
    report = {"model": config.EMBEDDING_MODEL, "threshold": config.ROUTER_SIMILARITY_THRESHOLD,
              "samples": len(samples), "datasets": datasets, "repeated": repeated,
              "sets": cache_metrics([s.label["expected_agent"] for s in samples],
                                    [s.label.get("language", "?") for s in samples], idx, sim,
                                    config.ROUTER_SIMILARITY_THRESHOLD)}
    text = json.dumps(report, indent=1) if args.json else to_markdown(report)
    if args.out:
        args.out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
