"""Train and evaluate a "does this query need any implant?" gate for the implant layer.

This is the implant layer's own calibration ("дообучение"): a small L2 logistic
regression over cheap per-query features, fitted on the dev split of
``evals/datasets/implant_labels.jsonl`` and scored on the test split. It never
touches the skill or routing layers.

Policies compared on test (all use the production implant list unless noted):
  P0  production    — lite tier loads nothing; otherwise the agent's
                      preferred_implants up to the tier budget, topped up by
                      legacy semantic retrieval under the shipped defaults
                      (IMPLANT_GATING=legacy, IMPLANTS_RELEVANCE_THRESHOLD);
                      the enrichment.py behaviour with flags off
  P1  none          — never load implants
  P2  intent gate   — production list, gated by classify_intent's implant budget
  P3  learned gate  — production list, gated by the trained classifier
  P4  learned gate + triggers rerank — gated; the agent's preferred implants
                      reordered by trigger-index distance, top 2 (unboosted
                      unless --trigger-boost is given)
  P5  oracle gate   — production list, gated by the label itself (upper bound)

Metrics: hit@3 on samples that need an implant, none-acc on samples that need
none, utility = 0.5·hit@3 + 0.5·none-acc, mean implants loaded. The utility
difference vs P0 carries a 95% bootstrap interval over test samples.

Runs on a temporary copy of data/ (``_isolated_data``): switching the index
mode reindexes, and that must not touch the install's stores.

Usage:
    python -m evals.scripts.implant_need_gate [--trigger-boost 1.0] [--save-weights PATH]
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.scripts._isolated_data import isolate_data_dir  # noqa: E402

isolate_data_dir()  # before any engine import: reindexing must not touch the live data/

import src.engine.config as cfg  # noqa: E402
from evals.runners._loader import iter_valid, load_samples  # noqa: E402
from evals.runners.run_retrieval import _agent_preferred_implants  # noqa: E402
from evals.scripts.measure_implant_layer import DEFAULT_LABELS, split_of  # noqa: E402
from src.engine.embedder import embed_query  # noqa: E402
from src.engine.enrichment import _legacy_infer_tier  # noqa: E402
from src.engine.implants import ImplantRetriever, _trigger_hit  # noqa: E402
from src.engine.intent import classify_intent  # noqa: E402

FEATURES = ("log_len", "legacy_z1", "trig_z1", "any_trigger", "tier_deep", "tier_lite", "intent_budget")
TIER_BUDGET = {"standard": 2, "deep": cfg.IMPLANTS_DEEP_TIER_DEFAULT}


def _all_distances(retriever: ImplantRetriever, query: str, agent: str | None, boost: float = 1.0):
    """Distances to every implant; trigger hits are multiplied by ``boost`` (1.0 = none)."""
    emb = embed_query(f"Query: {query}\nRole: {agent}" if agent else f"Query: {query}")
    res = retriever.store.query(query_embedding=emb, n_results=retriever.store.count())
    q = query.lower()
    out = {}
    for i, cid in enumerate(res.ids):
        d = res.distances[i]
        if boost != 1.0 and _trigger_hit((res.metadatas[i] or {}).get("triggers") or [], q):
            d *= boost
        out[Path(cid).stem] = d
    return out


def _top1_z(dists: dict[str, float]) -> float:
    vals = list(dists.values())
    sd = statistics.pstdev(vals) or 1e-9
    return (min(vals) - statistics.fmean(vals)) / sd


def collect(samples, labels, trigger_boost: float = 1.0):
    """One record per labelled sample: features, candidate lists, label."""
    records = []
    for s in samples:
        sid = s.label["id"]
        if sid not in labels:
            continue
        agent = s.label.get("expected_agent")
        records.append({
            "id": sid, "split": split_of(sid), "query": s.query, "agent": agent,
            "expected": labels[sid], "pref": list(_agent_preferred_implants(agent)),
            "tier": _legacy_infer_tier(s.query),
            "intent_budget": classify_intent(s.query).implant_budget,
        })
    saved_mode = cfg.IMPLANT_INDEX_MODE
    try:
        cfg.IMPLANT_INDEX_MODE = "legacy"
        legacy = ImplantRetriever()
        for r in records:
            d = _all_distances(legacy, r["query"], r["agent"])
            r["legacy_z1"] = _top1_z(d)
            r["legacy_rank"] = sorted(d, key=d.get)
            r["legacy_dist"] = d
        cfg.IMPLANT_INDEX_MODE = "triggers"
        trig = ImplantRetriever()
        for r in records:
            d = _all_distances(trig, r["query"], r["agent"], boost=trigger_boost)
            r["trig_z1"] = _top1_z(d)
            r["trig_dist"] = d
            q = r["query"].lower()
            r["any_trigger"] = float(any(_trigger_hit((m or {}).get("triggers") or [], q)
                                         for m in trig.store.get_all_metadatas()))
    finally:
        cfg.IMPLANT_INDEX_MODE = saved_mode
    for r in records:
        r["x"] = [
            math.log(len(r["query"]) + 1), r["legacy_z1"], r["trig_z1"], r["any_trigger"],
            float(r["tier"] == "deep"), float(r["tier"] == "lite"), float(r["intent_budget"] > 0),
        ]
    return records


def production_list(r) -> list[str]:
    """What enrichment.py injects today (INTENT_CLASSIFIER_ENABLED off)."""
    if r["tier"] == "lite":
        return []
    n = min(max(TIER_BUDGET[r["tier"]], len(r["pref"])), cfg.MAX_PREFERRED_IMPLANTS)
    chosen = r["pref"][:n]
    # Same gate as ImplantRetriever._legacy_candidates on the semantic top-up.
    passing = [c for c in r["legacy_rank"] if r["legacy_dist"][c] < cfg.IMPLANTS_RELEVANCE_THRESHOLD]
    for name in passing:
        if len(chosen) >= n:
            break
        if name not in chosen:
            chosen.append(name)
    return chosen


def fit_logistic(X: np.ndarray, y: np.ndarray, l2: float = 1.0, steps: int = 5000, lr: float = 0.1):
    mu, sd = X.mean(0), X.std(0) + 1e-9
    Z = (X - mu) / sd
    w, b = np.zeros(Z.shape[1]), 0.0
    for _ in range(steps):
        p = 1 / (1 + np.exp(-(Z @ w + b)))
        w -= lr * (Z.T @ (p - y) / len(y) + l2 * w / len(y))
        b -= lr * float(np.mean(p - y))
    return {"mu": mu.tolist(), "sd": sd.tolist(), "w": w.tolist(), "b": b, "features": list(FEATURES)}


def predict(model, x) -> float:
    z = (np.array(x) - np.array(model["mu"])) / np.array(model["sd"])
    return float(1 / (1 + np.exp(-(z @ np.array(model["w"]) + model["b"]))))


def score(records, lists):
    per = []
    for r, got in zip(records, lists):
        if r["expected"]:
            per.append(("pos", float(bool(set(r["expected"]) & set(got[:3]))), len(got)))
        else:
            per.append(("neg", float(not got), len(got)))
    return per


def summarize(per) -> dict:
    pos = [v for k, v, _ in per if k == "pos"]
    neg = [v for k, v, _ in per if k == "neg"]
    hit, none_acc = statistics.fmean(pos), statistics.fmean(neg)
    return {"hit@3": hit, "none_acc": none_acc, "utility": 0.5 * hit + 0.5 * none_acc,
            "loaded": statistics.fmean(n for *_, n in per)}


def bootstrap_diff(per_a, per_b, n: int = 2000, seed: int = 7):
    rng = np.random.default_rng(seed)
    idx = np.arange(len(per_a))
    diffs = []
    for _ in range(n):
        s = rng.choice(idx, size=len(idx), replace=True)
        a, b = [per_a[i] for i in s], [per_b[i] for i in s]
        if not any(k == "pos" for k, *_ in a) or not any(k == "neg" for k, *_ in a):
            continue
        diffs.append(summarize(b)["utility"] - summarize(a)["utility"])
    return float(np.percentile(diffs, 2.5)), float(np.percentile(diffs, 97.5))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--labels", type=Path, default=DEFAULT_LABELS)
    ap.add_argument("--save-weights", type=Path)
    ap.add_argument("--trigger-boost", type=float, default=1.0,
                    help="multiplier on trigger-hit distances for trig_z1 and the P4 rerank; "
                         "1.0 = no boost, as the research plan asks for evaluations "
                         "(production default IMPLANT_TRIGGER_BOOST=0.85)")
    args = ap.parse_args()

    labels = {r["id"]: r["expected_implants"] for r in map(json.loads, args.labels.read_text().splitlines())}
    records = collect(list(iter_valid(load_samples()[0])), labels, trigger_boost=args.trigger_boost)
    dev = [r for r in records if r["split"] == "dev"]
    test = [r for r in records if r["split"] == "test"]

    model = fit_logistic(np.array([r["x"] for r in dev]), np.array([float(bool(r["expected"])) for r in dev]))
    if args.save_weights:
        args.save_weights.write_text(json.dumps(model, indent=2) + "\n")

    def gated(r, base):
        return base if predict(model, r["x"]) >= 0.5 else []

    def rerank(r):
        return sorted(r["pref"], key=lambda n: r["trig_dist"].get(n, 9.0))[:2]

    policies = {
        "P0 production": [production_list(r) for r in test],
        "P1 none": [[] for _ in test],
        "P2 intent gate": [production_list(r) if r["intent_budget"] > 0 else [] for r in test],
        "P3 learned gate": [gated(r, production_list(r)) for r in test],
        "P4 learned gate + triggers rerank": [gated(r, rerank(r)) for r in test],
        "P5 oracle gate": [production_list(r) if r["expected"] else [] for r in test],
    }
    per = {k: score(test, v) for k, v in policies.items()}
    base = per["P0 production"]

    need_acc = statistics.fmean(
        float((predict(model, r["x"]) >= 0.5) == bool(r["expected"])) for r in test
    )
    print(f"# Implant need gate — test split ({len(test)} samples; trained on dev {len(dev)}; "
          f"trigger boost {args.trigger_boost}; {cfg.EMBEDDING_MODEL})\n")
    print(f"Learned gate accuracy on 'needs any implant': {need_acc:.2f}\n")
    print("| policy | hit@3 | none-acc | utility | Δ utility vs P0 (95% CI) | loaded |")
    print("|---|---|---|---|---|---|")
    for name, p in per.items():
        s = summarize(p)
        ci = "—" if name.startswith("P0") else "{:+.3f} [{:+.3f}, {:+.3f}]".format(
            s["utility"] - summarize(base)["utility"], *bootstrap_diff(base, p))
        print(f"| {name} | {s['hit@3']:.2f} | {s['none_acc']:.2f} | {s['utility']:.3f} | {ci} | {s['loaded']:.2f} |")
    print("\nWeights (standardized features):")
    for f, w in zip(FEATURES, model["w"]):
        print(f"  {f:>14}: {w:+.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
