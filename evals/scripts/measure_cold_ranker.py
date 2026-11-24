"""Measure cold-start agent rankers on the labelled routing sets (#734).

A cold router activation (no semantic-cache hit) can return a short candidate
list or resolve directly. This script measures three rankers over the labelled
queries and the precision/coverage of resolving top-1 directly:

  keyword    SemanticRouter.match_keywords: agents with >= 1 domain_keyword hit,
             ordered by hit count (ties keep the router's alphabetical order).
  embedding  cosine similarity of the query (embed_query) with each agent's
             catalog card (embed_texts): display name, role, trigger command and
             domain keywords, from the metadata the router's catalog loads.
  combined   keyword hits first (ties inside one hit count broken by embedding
             similarity), then the remaining agents in embedding order.

Direct resolution is scored per margin between the first and second candidate:
the keyword hit gap (hits1 - hits2, 0 when fewer than two agents hit) and the
embedding cosine gap (sim1 - sim2). Coverage is the share of all items resolved
at that margin; precision is the share of the resolved ones whose top-1 is the
expected agent (95% Wilson interval beside it).

Offline and isolated: ``isolate_data_dir`` runs before any engine import so the
live router cache is never read or written, and HF_HUB_OFFLINE=1 is the default.
Pin the embedder with EMBEDDING_MODEL (or a repo-root .env) to match an install.

``evals/datasets/routing.jsonl`` keeps no query text: the loader takes it from the
gitignored ``_unlabeled.jsonl`` or from the source datasets, which an offline run
without that file cannot do. Such a file is reported as unavailable and skipped;
``routing_ru.jsonl`` carries its text inline. Pass any number of datasets.

Usage:
    EMBEDDING_MODEL=microsoft/harrier-oss-v1-270m \
        python -m evals.scripts.measure_cold_ranker
    python -m evals.scripts.measure_cold_ranker --dataset evals/datasets/routing.jsonl \
        --out evals/reports/cold_ranker.md --json-out /path/result.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("LANGFUSE_TRACING_ENABLED", "false")

from evals.scripts._isolated_data import isolate_data_dir  # noqa: E402

isolate_data_dir()  # before any engine import: the router must not touch the live router cache

import numpy as np  # noqa: E402

from evals.runners._loader import iter_valid, load_samples  # noqa: E402
from src.engine.embedder import embed_query, embed_texts  # noqa: E402
from src.engine.router import SemanticRouter  # noqa: E402

DEFAULT_DATASETS = ("evals/datasets/routing.jsonl", "evals/datasets/routing_ru.jsonl")
KS = (1, 3, 5, 8)
KEYWORD_GAPS = (1, 2, 3, 4)
EMBEDDING_GAPS = (0.0, 0.01, 0.02, 0.03, 0.05, 0.08)
UNIVERSAL = "universal_agent"


def wilson(k: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return (float("nan"), float("nan"))
    p = k / n
    d = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / d
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return (max(0.0, centre - half), min(1.0, centre + half))


def catalog_card(router: SemanticRouter, name: str) -> str:
    d = router._agent_descriptions.get(name, {})
    parts = [d.get("display_name") or name, d.get("role", ""), d.get("trigger_command", "")]
    parts.append(", ".join(router._agent_keywords.get(name, [])))
    return "\n".join(p for p in parts if p)


def load_dataset(path: Path) -> tuple[list[tuple[str, str, str]], dict]:
    """Return ([(id, query, expected_agent)], status) for one labelled file."""
    try:
        samples, stats = load_samples(path)
    except Exception as exc:  # missing `datasets`, offline, missing file
        return [], {"path": str(path), "available": False, "reason": f"{type(exc).__name__}: {exc}"[:200]}
    rows = [(s.label["id"], s.query, s.label["expected_agent"]) for s in iter_valid(samples)]
    status = {"path": str(path), "available": bool(rows), "labels": stats.total,
              "usable": len(rows), "drift": stats.drift, "fetch_errors": stats.fetch_errors}
    if not rows:
        status["reason"] = (f"no usable sample: {stats.total} labels, {stats.drift} drifted, "
                            f"{stats.fetch_errors} fetch errors")
    return rows, status


def rank_keyword(router: SemanticRouter, query: str) -> list[tuple[str, int]]:
    return router.match_keywords(query)


def rank_embedding(sims: np.ndarray, names: list[str]) -> list[tuple[str, float]]:
    order = np.argsort(-sims, kind="stable")
    return [(names[i], float(sims[i])) for i in order]


def rank_combined(kw: list[tuple[str, int]], sims: np.ndarray, names: list[str]) -> list[str]:
    idx = {n: i for i, n in enumerate(names)}
    hit_sorted = sorted(kw, key=lambda m: (-m[1], -float(sims[idx[m[0]]])))
    out = [n for n, _ in hit_sorted]
    seen = set(out)
    out += [n for n, _ in rank_embedding(sims, names) if n not in seen]
    return out


def hit_at(ranked: list[str], expected: str, k: int) -> bool:
    return expected in ranked[:k]


def accuracy_table(rankings: dict[str, list[list[str]]], expected: list[str], mask: list[bool]) -> dict:
    out = {}
    n = sum(mask)
    for ranker, ranks in rankings.items():
        row = {"n": n}
        for k in KS:
            hits = sum(hit_at(r, e, k) for r, e, m in zip(ranks, expected, mask) if m)
            lo, hi = wilson(hits, n)
            row[f"top{k}"] = {"hits": hits, "acc": hits / n if n else float("nan"), "ci": [lo, hi]}
        out[ranker] = row
    return out


def margin_table(top1: list[str], gaps: list[float], expected: list[str], thresholds, mask: list[bool]) -> list[dict]:
    n = sum(mask)
    rows = []
    for t in thresholds:
        sel = [(a, e) for a, g, e, m in zip(top1, gaps, expected, mask) if m and g >= t - 1e-12]
        ok = sum(a == e for a, e in sel)
        lo, hi = wilson(ok, len(sel))
        rows.append({
            "margin": t, "resolved": len(sel), "n": n,
            "coverage": len(sel) / n if n else float("nan"),
            "correct": ok, "precision": ok / len(sel) if sel else float("nan"),
            "precision_ci": [lo, hi],
        })
    return rows


def measure(rows: list[tuple[str, str, str]], router: SemanticRouter, names: list[str], card_vecs: np.ndarray) -> dict:
    expected = [e for _, _, e in rows]
    kw_all = [rank_keyword(router, q) for _, q, _ in rows]
    q_vecs = np.vstack([embed_query(q) for _, q, _ in rows]).astype(np.float64)
    q_vecs /= np.linalg.norm(q_vecs, axis=1, keepdims=True)
    sims = q_vecs @ card_vecs.T
    emb_all = [rank_embedding(s, names) for s in sims]
    comb_all = [rank_combined(kw, s, names) for kw, s in zip(kw_all, sims)]
    kw_ranked = [[n for n, _ in kw] for kw in kw_all]
    emb_ranked = [[n for n, _ in e] for e in emb_all]

    no_signal = sum(1 for kw in kw_all if not kw)
    rankings = {"keyword": kw_ranked, "embedding": emb_ranked, "combined": comb_all}
    all_mask = [True] * len(rows)
    spec_mask = [e != UNIVERSAL for e in expected]

    kw_gap = [(kw[0][1] - (kw[1][1] if len(kw) > 1 else 0)) if kw else 0 for kw in kw_all]
    kw_top1 = [kw[0][0] if kw else "" for kw in kw_all]
    emb_gap = [e[0][1] - e[1][1] for e in emb_all]
    emb_top1 = [e[0][0] for e in emb_all]
    # combined direct rule: a keyword gap >= 1 resolves on keywords, otherwise the embedding gap decides.
    comb_top1 = [c[0] for c in comb_all]

    result = {
        "n": len(rows), "n_specialist": sum(spec_mask), "n_universal": len(rows) - sum(spec_mask),
        "keyword_no_signal": no_signal,
        "keyword_no_signal_rate": no_signal / len(rows) if rows else float("nan"),
        "accuracy_all": accuracy_table(rankings, expected, all_mask),
        "accuracy_specialist": accuracy_table(rankings, expected, spec_mask),
        "margins_all": {
            "keyword_gap_hits": margin_table(kw_top1, kw_gap, expected, KEYWORD_GAPS, all_mask),
            "embedding_gap_cosine": margin_table(emb_top1, emb_gap, expected, EMBEDDING_GAPS, all_mask),
        },
        "margins_specialist": {
            "keyword_gap_hits": margin_table(kw_top1, kw_gap, expected, KEYWORD_GAPS, spec_mask),
            "embedding_gap_cosine": margin_table(emb_top1, emb_gap, expected, EMBEDDING_GAPS, spec_mask),
        },
        "embedding_gap_quantiles": {
            q: float(np.quantile(emb_gap, q)) for q in (0.1, 0.25, 0.5, 0.75, 0.9)
        },
    }
    # Combined top-1 gated by the embedding gap, keyword rank kept when keywords agree on a unique leader.
    combined_gate = []
    for t in EMBEDDING_GAPS:
        sel = [
            (comb_top1[i], expected[i]) for i in range(len(rows))
            if kw_gap[i] >= 1 or emb_gap[i] >= t - 1e-12
        ]
        ok = sum(a == e for a, e in sel)
        lo, hi = wilson(ok, len(sel))
        combined_gate.append({"margin": t, "resolved": len(sel), "n": len(rows),
                              "coverage": len(sel) / len(rows) if rows else float("nan"),
                              "correct": ok, "precision": ok / len(sel) if sel else float("nan"),
                              "precision_ci": [lo, hi]})
    result["margins_all"]["combined_keyword_gap1_or_embedding_gap"] = combined_gate
    per_item = []
    for i, (iid, _, e) in enumerate(rows):
        per_item.append({"id": iid, "expected": e, "kw_top": kw_ranked[i][:3], "emb_top": emb_ranked[i][:3],
                         "kw_gap": kw_gap[i], "emb_gap": round(emb_gap[i], 4),
                         "emb_rank": emb_ranked[i].index(e) + 1 if e in emb_ranked[i] else None})
    result["items"] = per_item
    return result


def fmt_acc(tbl: dict) -> list[str]:
    lines = ["| ranker | n | top-1 | top-3 | top-5 | top-8 |", "|---|---|---|---|---|---|"]
    for ranker, row in tbl.items():
        cells = []
        for k in KS:
            c = row[f"top{k}"]
            cells.append(f"{c['acc'] * 100:.1f}% ({c['hits']}/{row['n']}) [{c['ci'][0] * 100:.0f}-{c['ci'][1] * 100:.0f}]")
        lines.append(f"| {ranker} | {row['n']} | " + " | ".join(cells) + " |")
    return lines


def fmt_margin(rows: list[dict], unit: str) -> list[str]:
    lines = [f"| margin ({unit}) | resolved | coverage | precision | 95% CI |", "|---|---|---|---|---|"]
    for r in rows:
        p = "n/a" if r["resolved"] == 0 else f"{r['precision'] * 100:.1f}% ({r['correct']}/{r['resolved']})"
        ci = "n/a" if r["resolved"] == 0 else f"{r['precision_ci'][0] * 100:.0f}-{r['precision_ci'][1] * 100:.0f}%"
        lines.append(f"| >= {r['margin']:g} | {r['resolved']}/{r['n']} | {r['coverage'] * 100:.1f}% | {p} | {ci} |")
    return lines


def format_report(payload: dict) -> str:
    out = ["# Cold-start ranker measurement (#734)", ""]
    out.append(f"Embedder: `{payload['embedding_model']}`; agents in catalog: {payload['agents']}.")
    for st in payload["datasets"]:
        if st["available"]:
            out.append(f"- `{st['path']}`: {st['usable']}/{st['labels']} labels usable "
                       f"(drift {st['drift']}, fetch errors {st['fetch_errors']}).")
        else:
            out.append(f"- `{st['path']}`: unavailable ({st['reason']}).")
    for name, res in payload["results"].items():
        out += ["", f"## {name}", "",
                f"n = {res['n']} ({res['n_specialist']} specialist, {res['n_universal']} universal_agent); "
                f"keyword has no signal on {res['keyword_no_signal']}/{res['n']} "
                f"({res['keyword_no_signal_rate'] * 100:.1f}%).", "",
                "Accuracy, all items (cells: accuracy (hits/n) [95% Wilson]):", ""]
        out += fmt_acc(res["accuracy_all"])
        out += ["", f"Accuracy, specialist-labelled items only (n = {res['n_specialist']}):", ""]
        out += fmt_acc(res["accuracy_specialist"])
        for scope, key in (("all items", "margins_all"), ("specialist-labelled only", "margins_specialist")):
            m = res[key]
            out += ["", f"Direct resolution of top-1, {scope}", "", "Keyword gap:", ""]
            out += fmt_margin(m["keyword_gap_hits"], "hits")
            out += ["", "Embedding gap:", ""]
            out += fmt_margin(m["embedding_gap_cosine"], "cosine")
            if "combined_keyword_gap1_or_embedding_gap" in m:
                out += ["", "Combined ranker top-1, resolved when keyword gap >= 1 or embedding gap >= margin:", ""]
                out += fmt_margin(m["combined_keyword_gap1_or_embedding_gap"], "cosine")
        q = res["embedding_gap_quantiles"]
        out += ["", "Embedding top1-top2 gap quantiles: " + ", ".join(f"p{int(k * 100)}={v:.4f}" for k, v in q.items())]
    return "\n".join(out) + "\n"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="measure_cold_ranker", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dataset", action="append", help="labelled jsonl (repeatable); default: routing + routing_ru")
    ap.add_argument("--out", type=Path, help="write the markdown report here")
    ap.add_argument("--json-out", type=Path, help="write the full JSON (with per-item rows) here")
    args = ap.parse_args(argv)

    import src.engine.config as cfg

    router = SemanticRouter()
    names = list(router.available_agents)
    cards = [catalog_card(router, n) for n in names]
    card_vecs = np.asarray(embed_texts(cards), dtype=np.float64)
    card_vecs /= np.linalg.norm(card_vecs, axis=1, keepdims=True)

    statuses, results, pooled = [], {}, []
    for ds in args.dataset or DEFAULT_DATASETS:
        path = (REPO_ROOT / ds) if not Path(ds).is_absolute() else Path(ds)
        rows, st = load_dataset(path)
        statuses.append(st)
        if not rows:
            continue
        unknown = sorted({e for _, _, e in rows} - set(names))
        if unknown:
            st["labels_outside_catalog"] = unknown
        results[path.stem] = measure(rows, router, names, card_vecs)
        pooled += rows
    if len([s for s in statuses if s["available"]]) > 1:
        results["pooled"] = measure(pooled, router, names, card_vecs)

    payload = {"embedding_model": cfg.EMBEDDING_MODEL, "agents": len(names), "datasets": statuses, "results": results}
    report = format_report(payload)
    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(report)
    return 0 if results else 1


if __name__ == "__main__":
    raise SystemExit(main())
