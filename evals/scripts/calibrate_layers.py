"""Per-layer calibration sweep for the skill and implant selection layers.

Each selection layer has its own sensitivity knobs, and they should be tuned
against that layer's own labels — not shared constants tuned by feel:

  * skills   — relevance threshold, preferred-pool boost, keyword boost
  * implants — relevance threshold (implants carry no keywords today)

The sweep runs the real retrievers over the labelled routing set (the same
samples ``run_retrieval`` uses), forwarding each sample's expected agent pools
exactly as production does, and reports MRR / recall@k per grid point. It needs
no API calls: embeddings are local.

Caveat printed with every report: implant ground truth is a proxy (the expected
agent's ``preferred_implants``), so implant numbers rank settings but do not
measure usefulness. A per-layer labelled set is the prerequisite for tuning
implants for real (see docs/layer-sensitivity-plan.md).

Usage:
    python -m evals.scripts.calibrate_layers            # both layers, markdown
    python -m evals.scripts.calibrate_layers --layer skills --top 10
"""
from __future__ import annotations

import argparse
import itertools
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.scripts._isolated_data import isolate_data_dir  # noqa: E402

isolate_data_dir()  # before any engine import: the retrievers must not reindex the live data/

from evals.metrics.retrieval import RetrievalResult, compute_metrics  # noqa: E402
from evals.runners._loader import iter_valid, load_samples  # noqa: E402
from evals.runners.run_retrieval import N_RESULTS, _agent_preferred_implants, _agent_skill_pools  # noqa: E402
import src.engine.implants as implants_mod  # noqa: E402
import src.engine.skills as skills_mod  # noqa: E402

SKILL_GRID = {
    "threshold": (0.65, 0.75, 0.85),
    "boost_factor": (1.0, 0.85, 0.7),
    "keyword_boost": (1.0, 0.85, 0.7),
}
IMPLANT_GRID = {"threshold": (0.7, 0.8, 0.85, 0.9, 0.95)}


def _stem(d: dict) -> str:
    return Path(d.get("filename", "")).stem


def sweep_skills(samples) -> list[tuple[dict, object]]:
    retriever = skills_mod.SkillRetriever()
    labelled = [s for s in samples if s.label.get("expected_skills")]
    rows = []
    for th, bf, kb in itertools.product(*SKILL_GRID.values()):
        skills_mod.SKILLS_RELEVANCE_THRESHOLD = th
        results = []
        for s in labelled:
            core, preferred, capable = _agent_skill_pools(s.label.get("expected_agent"))
            got = retriever.retrieve(
                s.query,
                # Mandatory skills load regardless of the knobs; leave them out so
                # the sweep measures only what the knobs control.
                preferred=list(preferred) or None,
                capable=list(capable) or None,
                n_results=N_RESULTS,
                boost_factor=bf,
                keyword_boost=kb,
            )
            expected = [x for x in s.label["expected_skills"] if x not in core]
            if expected:
                results.append(RetrievalResult(s.label["id"], expected, [_stem(d) for d in got]))
        rows.append(({"threshold": th, "boost_factor": bf, "keyword_boost": kb}, compute_metrics(results)))
    return rows


def sweep_implants(samples) -> list[tuple[dict, object]]:
    retriever = implants_mod.ImplantRetriever()
    rows = []
    # The threshold only gates the legacy path; zscore ignores it, so every row
    # would measure the same gate. Force legacy for the sweep, then restore.
    cfg = implants_mod._cfg
    prev_gating, prev_threshold = cfg.IMPLANT_GATING, implants_mod.IMPLANTS_RELEVANCE_THRESHOLD
    cfg.IMPLANT_GATING = "legacy"
    try:
        for th in IMPLANT_GRID["threshold"]:
            implants_mod.IMPLANTS_RELEVANCE_THRESHOLD = th
            results = []
            for s in samples:
                agent = s.label.get("expected_agent")
                expected = s.label.get("expected_implants") or list(_agent_preferred_implants(agent))
                if not expected:
                    continue
                got = retriever.retrieve(s.query, n_results=N_RESULTS, role=agent)
                results.append(RetrievalResult(s.label["id"], expected, [_stem(d) for d in got]))
            rows.append(({"threshold": th}, compute_metrics(results)))
    finally:
        cfg.IMPLANT_GATING, implants_mod.IMPLANTS_RELEVANCE_THRESHOLD = prev_gating, prev_threshold
    return rows


def _table(title: str, rows, top: int) -> str:
    rows = sorted(rows, key=lambda r: (r[1].mrr, r[1].recall_at.get(5, 0.0)), reverse=True)[:top]
    keys = list(rows[0][0]) if rows else []
    head = "| " + " | ".join(keys + ["MRR", "P@1", "R@3", "R@5", "n"]) + " |"
    sep = "|" + "---|" * (len(keys) + 5)
    body = [
        "| " + " | ".join([str(p[k]) for k in keys] + [
            f"{m.mrr:.3f}", f"{m.precision_at.get(1, 0):.2f}",
            f"{m.recall_at.get(3, 0):.2f}", f"{m.recall_at.get(5, 0):.2f}", str(m.samples_with_expected),
        ]) + " |"
        for p, m in rows
    ]
    return "\n".join([f"### {title}", "", head, sep, *body, ""])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--layer", choices=["skills", "implants", "both"], default="both")
    ap.add_argument("--top", type=int, default=8)
    args = ap.parse_args()

    samples = list(iter_valid(load_samples()[0]))
    out = [f"# Layer calibration sweep ({implants_mod._cfg.EMBEDDING_MODEL})", ""]
    if args.layer in ("skills", "both"):
        out.append(_table("Skills (semantic pool only; core skills excluded)", sweep_skills(samples), args.top))
    if args.layer in ("implants", "both"):
        out.append(_table("Implants (proxy labels: agent preferred_implants)", sweep_implants(samples), args.top))
    print("\n".join(out))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
