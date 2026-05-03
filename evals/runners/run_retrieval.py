"""
Measurement #2 + #3 — skill / implant retrieval.

For every labeled sample, ask the retrievers what they would load and compare
against the golden expected_skills / expected_implants.

Reports precision@k, recall@k and MRR for skills (k = 1, 3, 5).
Implants are reported only as retrieval-rate stats since the golden set has
no labeled expected_implants yet.

Usage:
    python -m evals.runners.run_retrieval
    python -m evals.runners.run_retrieval --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.metrics.retrieval import RetrievalResult, compute_metrics, format_markdown  # noqa: E402
from evals.runners._loader import EvalSample, LoaderStats, iter_valid, load_samples  # noqa: E402
from src.engine.implants import ImplantRetriever  # noqa: E402
from src.engine.skills import SkillRetriever  # noqa: E402

N_RESULTS = 5


def run(
    preloaded: tuple[list[EvalSample], LoaderStats] | None = None,
) -> tuple[list[RetrievalResult], list[RetrievalResult], dict]:
    samples, stats = preloaded if preloaded is not None else load_samples()
    skills_retriever = SkillRetriever()
    implants_retriever = ImplantRetriever()

    skill_results: list[RetrievalResult] = []
    implant_results: list[RetrievalResult] = []

    for sample in iter_valid(samples):
        sid = sample.label["id"]
        expected_skills = sample.label.get("expected_skills") or []
        expected_implants = sample.label.get("expected_implants") or []

        try:
            retrieved_skills_raw = skills_retriever.retrieve(sample.query, n_results=N_RESULTS)
            retrieved_skills = [
                Path(d.get("filename", "")).stem or d.get("metadata", {}).get("name", "")
                for d in retrieved_skills_raw
            ]
        except Exception as exc:  # pragma: no cover
            print(f"  ERROR retrieving skills for {sid}: {exc!r}", file=sys.stderr)
            retrieved_skills = []

        try:
            retrieved_implants_raw = implants_retriever.retrieve(
                sample.query, n_results=N_RESULTS, role=sample.label.get("expected_agent")
            )
            retrieved_implants = [
                Path(d.get("filename", "")).stem or d.get("metadata", {}).get("name", "")
                for d in retrieved_implants_raw
            ]
        except Exception as exc:  # pragma: no cover
            print(f"  ERROR retrieving implants for {sid}: {exc!r}", file=sys.stderr)
            retrieved_implants = []

        skill_results.append(RetrievalResult(sample_id=sid, expected=expected_skills, retrieved=retrieved_skills))
        implant_results.append(RetrievalResult(sample_id=sid, expected=expected_implants, retrieved=retrieved_implants))

    loader_meta = {
        "total_samples": stats.total,
        "drift_count": stats.drift,
        "fetch_errors": stats.fetch_errors,
        "used_local_cache": stats.used_local_cache,
    }
    return skill_results, implant_results, loader_meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_retrieval", description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", type=Path, help="markdown report path")
    args = parser.parse_args(argv)

    skill_results, implant_results, loader_meta = run()
    skill_metrics = compute_metrics(skill_results)
    implant_metrics = compute_metrics(implant_results)

    if args.json:
        payload = {
            "loader": loader_meta,
            "skills": {
                "samples_with_expected": skill_metrics.samples_with_expected,
                "precision_at": skill_metrics.precision_at,
                "recall_at": skill_metrics.recall_at,
                "mrr": skill_metrics.mrr,
            },
            "implants": {
                "samples_with_expected": implant_metrics.samples_with_expected,
                "precision_at": implant_metrics.precision_at,
                "recall_at": implant_metrics.recall_at,
                "mrr": implant_metrics.mrr,
            },
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    report = "# Retrieval (skills + implants)\n\n"
    report += f"Loader: total={loader_meta['total_samples']} drift={loader_meta['drift_count']} "
    report += f"fetch_errors={loader_meta['fetch_errors']} local_cache={loader_meta['used_local_cache']}\n\n"
    report += format_markdown(skill_metrics, "Skills") + "\n\n"
    report += format_markdown(implant_metrics, "Implants") + "\n"

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(report, encoding="utf-8")
        print(f"wrote {args.out}")
    else:
        print(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
