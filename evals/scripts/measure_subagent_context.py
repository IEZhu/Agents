"""Deterministic source-only size measurement; imports test isolation first."""
import argparse
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import tests.conftest  # isolate derived stores, without initializing a retriever
from src.engine import enrichment, rules
from src.engine.persona_bundle import build_persona_bundle
from src.engine.subagent import prepare_subagent
from src.engine.skills import SkillRetriever
from src.engine.implants import ImplantRetriever
from src.schemas.subagent import PreparedPackage

TASKS = {
    "software_engineer": "Implement input validation and regression tests for a Python API endpoint.",
    "code_reviewer": "Review a Python API diff for correctness, security, and missing tests.",
    "deep_researcher": "Research available evidence and produce source-backed conclusions with uncertainties.",
    "ai_senior_engineer": "Design a reliable LLM retrieval and evaluation pipeline with measurable acceptance criteria.",
    "lawyer": "Analyze the supplied contract clause, identify legal risks and explain jurisdiction assumptions.",
    "universal_agent": "Clarify the task, compare options and recommend actionable next steps.",
}


def skill_retrieve(query, *, mandatory=None, preferred=None, capable=None, n_results=3, **kwargs):
    # Ordinary bundle adds core skills itself; return the first non-core declared
    # preferred/capable IDs, with stable declaration-order deduplication.
    ids = list(dict.fromkeys([*(preferred or []), *(capable or [])]))
    ids = [value for value in ids if value not in (mandatory or [])][:n_results]
    return [{"filename": value + ".mdc"} for value in ids]


def implant_retrieve(query, *, preferred_implants=None, n_results=2, **kwargs):
    return [{"filename": value + ".mdc"} for value in (preferred_implants or [])[:n_results]]


async def main(output: Path | None):
    root = Path.cwd()
    measurements, coverage = [], []
    skill_stub = SimpleNamespace(retrieve=skill_retrieve,
        format_skills_for_prompt=lambda items, **kw: SkillRetriever.format_skills_for_prompt(None, items, **kw))
    implant_stub = SimpleNamespace(retrieve=implant_retrieve,
        format_implants_for_prompt=lambda items: ImplantRetriever.format_implants_for_prompt(None, items))
    with patch.object(enrichment, "_skill_retriever", skill_stub), patch.object(enrichment, "_implant_retriever", implant_stub), patch("src.component_toggles.disabled", return_value=frozenset()), patch.object(rules, "RULES_ENABLED", True):
        for role, task in TASKS.items():
            request = {"agent_name": role, "task": task, "task_id": "size-measurement",
                       "acceptance_criteria": ["Address the assignment and report evidence and limitations."]}
            worker = prepare_subagent(request)
            assert isinstance(worker, PreparedPackage), worker
            ordinary = await build_persona_bundle(role, task, tier="standard")
            blocks = [getattr(ordinary, key) for key in ("persona_block", "rules_block", "skills_block", "implants_block")]
            measurements.append({"agent": role, "task": task, "worker_context_chars": worker.context_chars,
                "worker_components": worker.descriptor.model_dump(),
                "ordinary_instruction_chars": len("\n\n".join(block for block in blocks if block)),
                "ordinary_block_chars": {key: len(getattr(ordinary, key)) for key in ("persona_block", "rules_block", "skills_block", "implants_block")},
                "ordinary_components": {"skills": ordinary.skills_loaded, "implants": ordinary.implants_loaded, "rules": ordinary.rules_loaded}})
        for path in sorted((root / "agents").glob("*/system_prompt.mdc")):
            for result_type in ("generic", "review", "implementation", "research"):
                role = path.parent.name
                result = prepare_subagent({"agent_name": role, "task": TASKS.get(role, "Complete the assigned specialist task within the supplied scope."),
                    "task_id": "coverage", "acceptance_criteria": ["Address the assignment and report evidence and limitations."],
                    "result_type": result_type})
                coverage.append({"agent": role, "result_type": result_type, "status": result.status,
                                 **({"context_chars": result.context_chars, "skills": result.descriptor.skills_loaded,
                                     "omitted_for_budget": [decision.id for decision in result.selection if decision.reason == "context_budget"]}
                                    if isinstance(result, PreparedPackage) else {"errors": [item.model_dump() for item in result.errors]})})
    report = {"method": {"worker": "Full worker_context with task, criteria and the built-in generic result format (plain text by default); defaults max_skills=3/max_implants=0/max_context_chars=16000. Installation toggles all on.",
              "ordinary": "build_persona_bundle tier=standard; fresh full persona and all 6 universal rules; skill retriever stub selects first n_results preferred/capable non-core IDs in declared order, ordinary builder also adds core IDs; implant stub selects declared preferred IDs in order and builder applies ordinary count policy; source rendering unchanged.",
              "counting": "Python len Unicode characters, joining nonempty ordinary blocks with two newlines. Ordinary response descriptor/footer/JSON envelope excluded; worker JSON result schema and assignment included. No model, token or task-quality measurements."},
              "measurements": measurements, "coverage": coverage}
    if output is not None:
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"measurements": [{"agent": row["agent"], "worker": row["worker_context_chars"], "ordinary": row["ordinary_instruction_chars"]} for row in measurements],
                      "coverage_count": len(coverage), "failures": [row for row in coverage if row["status"] != "prepared"],
                      "range": [min(row["context_chars"] for row in coverage if row["status"] == "prepared"), max(row["context_chars"] for row in coverage if row["status"] == "prepared")],
                      "budget_omissions": [row for row in coverage if row.get("omitted_for_budget")]}))

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, help="Optional JSON report destination")
    args = parser.parse_args()
    asyncio.run(main(args.output))
