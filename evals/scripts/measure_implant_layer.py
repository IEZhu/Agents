"""Semantic-only measurement of the implant retrieval layer on implant labels.

Labels (``evals/datasets/implant_labels.jsonl``) say, per routing sample, which
implants would materially improve the answer — or none. They were written by a
labeller that saw the implant catalogue and the queries but not the retrieval
changes, and the trigger vocabulary was written without seeing the queries.

Samples are split deterministically by id hash into dev (tuning) and test
(reporting). ``IMPLANT_GATE_Z`` is chosen on dev only; the report shows test.

Configurations (index mode × gating), semantic top-up only:
  A  legacy   × legacy   — current semantic retrieval (baseline)
  B  legacy   × zscore
  C  triggers × legacy
  D  triggers × zscore   — task-shape index + relative gate

Metrics, semantic layer only (no preferred_implants fast-path, n=3):
  This is NOT the production before/after: production skips implants on lite
  and fills slots from the agent's preferred_implants before any semantic
  top-up, so the gate rarely runs there. The full selection path is measured
  by evals/scripts/implant_need_gate.py (policy P0).
  * P@1 / R@3 / MRR over samples that need at least one implant
  * hit@3     — share of implant-needing samples with a labelled implant in the top 3
  * none-acc  — share of "needs none" samples where the layer loads nothing
  * utility   — 0.5·hit@3 + 0.5·none-acc (loading nothing anywhere scores 0.5)
  * loaded    — mean implants injected per query; chars — mean injected chars

The z-score rows (B, D) scale an implant's distance by ``IMPLANT_TRIGGER_BOOST``
when one of its triggers occurs in the query. The factor is pinned by
``--trigger-boost`` (default 0.85, the shipped value) instead of read from the
environment; pass ``--trigger-boost 1.0`` for the no-boost ablation that
docs/query-decomposition-research.md asks evaluations to use.

Runs on a temporary copy of data/ (``_isolated_data``): switching the index
mode reindexes, and that must not touch the install's stores.

Usage:
    python -m evals.scripts.measure_implant_layer [--trigger-boost 1.0]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.scripts._isolated_data import isolate_data_dir  # noqa: E402

isolate_data_dir()  # before any engine import: reindexing must not touch the live data/

import src.engine.config as cfg  # noqa: E402
from evals.metrics.retrieval import RetrievalResult, compute_metrics  # noqa: E402
from evals.runners._loader import iter_valid, load_samples  # noqa: E402
from src.engine.implants import ImplantRetriever  # noqa: E402

DEFAULT_LABELS = REPO_ROOT / "evals" / "datasets" / "implant_labels.jsonl"
N_RESULTS = 3
Z_GRID = (0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0)


def split_of(sample_id: str) -> str:
    return "test" if int(hashlib.sha256(sample_id.encode()).hexdigest(), 16) % 2 else "dev"


def evaluate(rows, retriever: ImplantRetriever) -> dict:
    results, none_hits, none_total, loaded, chars, pos_hits = [], 0, 0, [], [], 0
    for sid, agent, query, expected in rows:
        got = retriever.retrieve(query, n_results=N_RESULTS, role=agent)
        names = [Path(d["filename"]).stem for d in got]
        loaded.append(len(got))
        chars.append(sum(len(d.get("content", "")) for d in got))
        if expected:
            results.append(RetrievalResult(sid, expected, names))
            pos_hits += bool(set(expected) & set(names))
        else:
            none_total += 1
            none_hits += not names
    m = compute_metrics(results)
    hit3 = pos_hits / len(results) if results else 0.0
    none_acc = none_hits / none_total if none_total else 0.0
    return {
        "hit@3": hit3,
        # Balanced utility: loading nothing everywhere scores exactly 0.5, so a
        # gate only wins by picking the right implants where they are needed.
        "utility": 0.5 * hit3 + 0.5 * none_acc,
        "P@1": m.precision_at.get(1, 0.0), "R@3": m.recall_at.get(3, 0.0), "MRR": m.mrr,
        "none_acc": none_acc,
        "loaded": statistics.fmean(loaded) if loaded else 0.0,
        "chars": statistics.fmean(chars) if chars else 0.0,
        "n_pos": m.samples_with_expected, "n_none": none_total,
    }


def _objective(r: dict) -> float:
    return r["utility"]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--trigger-boost", type=float, default=0.85,
                    help="IMPLANT_TRIGGER_BOOST for the z-score rows (1.0 = no boost)")
    args = ap.parse_args()

    labels = {r["id"]: r["expected_implants"] for r in map(json.loads, args.labels.read_text().splitlines())}
    rows = {"dev": [], "test": []}
    for s in iter_valid(load_samples()[0]):
        sid = s.label["id"]
        if sid in labels:
            rows[split_of(sid)].append((sid, s.label.get("expected_agent"), s.query, labels[sid]))

    saved = (cfg.IMPLANT_INDEX_MODE, cfg.IMPLANT_GATING, cfg.IMPLANT_GATE_Z, cfg.IMPLANT_TRIGGER_BOOST)
    cfg.IMPLANT_TRIGGER_BOOST = args.trigger_boost
    report = {}
    try:
        for index_mode in ("legacy", "triggers"):
            cfg.IMPLANT_INDEX_MODE = index_mode
            retriever = ImplantRetriever()  # reindexes when the mode changes
            cfg.IMPLANT_GATING = "legacy"
            report[(index_mode, "legacy")] = {"z": None, **evaluate(rows["test"], retriever)}
            cfg.IMPLANT_GATING = "zscore"
            dev_scores = {}
            for z in Z_GRID:
                cfg.IMPLANT_GATE_Z = z
                dev_scores[z] = _objective(evaluate(rows["dev"], retriever))
            best_z = max(dev_scores, key=dev_scores.get)
            cfg.IMPLANT_GATE_Z = best_z
            report[(index_mode, "zscore")] = {"z": best_z, **evaluate(rows["test"], retriever)}
    finally:
        cfg.IMPLANT_INDEX_MODE, cfg.IMPLANT_GATING, cfg.IMPLANT_GATE_Z, cfg.IMPLANT_TRIGGER_BOOST = saved

    if args.json:
        print(json.dumps({"trigger_boost": args.trigger_boost, "embedding_model": cfg.EMBEDDING_MODEL,
                          **{f"{k[0]}x{k[1]}": v for k, v in report.items()}}, indent=2))
        return 0
    print(f"# Implant semantic layer only (no preferred_implants) — test split "
          f"(dev {len(rows['dev'])} / test {len(rows['test'])} samples; "
          f"trigger boost {args.trigger_boost}; {cfg.EMBEDDING_MODEL})\n")
    print("| config | index | gating | z (dev) | P@1 | hit@3 | MRR | none-acc | utility | loaded | chars |")
    print("|---|---|---|---|---|---|---|---|---|---|---|")
    for label, key in zip("ABCD", report):
        r = report[key]
        print(f"| {label} | {key[0]} | {key[1]} | {r['z'] if r['z'] is not None else '—'} | {r['P@1']:.2f} | "
              f"{r['hit@3']:.2f} | {r['MRR']:.3f} | {r['none_acc']:.2f} | {r['utility']:.3f} | {r['loaded']:.2f} | {r['chars']:.0f} |")
    any_r = next(iter(report.values()))
    print(f"\nPositives in test: {any_r['n_pos']}; needs-none in test: {any_r['n_none']}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
