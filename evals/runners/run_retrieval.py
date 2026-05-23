"""
Measurement #2 + #3 — skill / implant retrieval.

For every labeled sample, ask the retrievers what they would load and compare
against the golden expected_skills / expected_implants.

Reports precision@k, recall@k and MRR for skills and implants (k = 1, 3, 5).

For implants, the golden set in ``routing.jsonl`` rarely carries explicit
``expected_implants``. When ``--expected-from-agent`` is set, the loader-side
ground truth is derived from each sample's ``expected_agent``'s declared
``preferred_implants`` in agent frontmatter — i.e. "this agent has opted into
these implants, so retrieval ought to surface them." Combined with
``--use-preferred-implants`` (which forwards those same implants into the
retriever's fast-path), this produces a deterministic A/B comparison between
pure semantic retrieval and preferred-implants-augmented retrieval.

Usage:
    # Baseline (semantic-only, no labelled expectations):
    python -m evals.runners.run_retrieval --json

    # A/B-ready baseline (derive expected from agent; semantic-only):
    python -m evals.runners.run_retrieval --expected-from-agent --json

    # Treatment (derive expected; forward preferred_implants fast-path):
    python -m evals.runners.run_retrieval --expected-from-agent --use-preferred-implants --json
"""

from __future__ import annotations

import argparse
import json
import sys
from functools import lru_cache
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from evals.metrics.retrieval import RetrievalResult, compute_metrics, format_markdown  # noqa: E402
from evals.runners._loader import EvalSample, LoaderStats, iter_valid, load_samples  # noqa: E402
from src.engine.implants import ImplantRetriever  # noqa: E402
from src.engine.skills import SkillRetriever  # noqa: E402
from src.utils.prompt_loader import get_agent_metadata  # noqa: E402

N_RESULTS = 5


@lru_cache(maxsize=128)
def _agent_preferred_implants(agent_name: str | None) -> tuple[str, ...]:
    """Return the declared preferred_implants of an agent (as stems, no .mdc).

    Cached per-agent so a 110-row eval triggers at most one frontmatter read
    per distinct ``expected_agent``.

    Validation policy: ``get_agent_metadata()`` already swallows read/parse
    errors and returns ``{}`` (see ``src/utils/prompt_loader.py``), so we
    cannot distinguish "agent missing" from "frontmatter broken" by catching
    exceptions here. Instead we detect the empty-dict signal explicitly and
    surface a stderr warning naming the offending agent so degraded samples
    don't slip out of precision/recall/MRR silently. We deliberately do
    **not** raise — a single misconfigured agent should not crash the whole
    eval batch — but degraded samples are now loud.
    """
    if not agent_name:
        return ()
    meta = get_agent_metadata(agent_name)
    if not meta:
        # get_agent_metadata returns {} for missing file, security-rejected
        # path, or YAML parse error. All three are configuration bugs that
        # silently degrade implant metrics if left invisible.
        print(
            f"WARNING: agent {agent_name!r} has no loadable frontmatter "
            f"(missing file or malformed YAML) — preferred_implants treated as empty",
            file=sys.stderr,
        )
        return ()
    if not isinstance(meta, dict):
        print(
            f"WARNING: agent {agent_name!r} frontmatter parsed to "
            f"{type(meta).__name__}, expected dict — preferred_implants ignored",
            file=sys.stderr,
        )
        return ()
    raw = meta.get("preferred_implants") or []
    if not isinstance(raw, list):
        print(
            f"WARNING: agent {agent_name!r} preferred_implants has type "
            f"{type(raw).__name__}, expected list — ignored",
            file=sys.stderr,
        )
        return ()
    return tuple(Path(x).stem for x in raw if isinstance(x, str))


def run(
    preloaded: tuple[list[EvalSample], LoaderStats] | None = None,
    use_preferred_implants: bool = False,
    expected_from_agent: bool = False,
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
        expected_agent = sample.label.get("expected_agent")

        if expected_from_agent and not expected_implants:
            expected_implants = list(_agent_preferred_implants(expected_agent))

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
            forward_preferred: list[str] | None = None
            if use_preferred_implants:
                forward_preferred = list(_agent_preferred_implants(expected_agent)) or None
            retrieved_implants_raw = implants_retriever.retrieve(
                sample.query,
                n_results=N_RESULTS,
                role=expected_agent,
                preferred_implants=forward_preferred,
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
        "use_preferred_implants": use_preferred_implants,
        "expected_from_agent": expected_from_agent,
    }
    return skill_results, implant_results, loader_meta


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="run_retrieval", description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--out", type=Path, help="markdown report path")
    parser.add_argument(
        "--use-preferred-implants",
        action="store_true",
        help="forward each expected_agent's preferred_implants to the retriever (fast-path).",
    )
    parser.add_argument(
        "--expected-from-agent",
        action="store_true",
        help="derive expected_implants from expected_agent's preferred_implants when sample has none.",
    )
    args = parser.parse_args(argv)

    skill_results, implant_results, loader_meta = run(
        use_preferred_implants=args.use_preferred_implants,
        expected_from_agent=args.expected_from_agent,
    )
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
    report += f"fetch_errors={loader_meta['fetch_errors']} local_cache={loader_meta['used_local_cache']}\n"
    report += (
        f"Mode: use_preferred_implants={loader_meta['use_preferred_implants']} "
        f"expected_from_agent={loader_meta['expected_from_agent']}\n\n"
    )
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
