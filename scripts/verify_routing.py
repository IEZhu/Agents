#!/usr/bin/env python3
"""End-to-end routing+enrichment harness for the 3-tier skill model.

For each test case:
  1. Call route_and_load(query) — verify expected agent is routed (or in candidates).
  2. Call _load_and_enrich(expected_agent, query) — verify required skills load.
  3. Report pass/fail matrix.

Run from repo root:
    .venv/bin/python scripts/verify_routing.py
"""
from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

# Bootstrap
REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))

from src.server import _load_and_enrich, route_and_load  # noqa: E402


@dataclass
class Case:
    query: str
    expected_agent: str
    required_skills: list[str]  # at least one of these must appear
    required_implants: list[str] = field(default_factory=list)
    forbidden_skills: list[str] = field(default_factory=list)
    description: str = ""


# ─── Test matrix (relaxed for 3-tier model) ─────────────────────────────────
# Note: required_skills are interpreted as "at least one of these must load".
# Under the 3-tier model, an agent's core_skill is mandatory; preferred/capable
# are loaded only when semantically relevant. So we list a few candidates and
# accept any match.

CASES: list[Case] = [
    # Lawyers — core skill-legal-citation must always load
    Case(
        query="Налоговый кодекс РФ статья 220 имущественные вычеты при продаже квартиры",
        expected_agent="russian_lawyer",
        required_skills=["skill-legal-citation"],
        description="russian_lawyer: core skill-legal-citation",
    ),
    Case(
        query="Cyprus VAT registration threshold for non-resident companies",
        expected_agent="cypriot_lawyer",
        required_skills=["skill-legal-citation"],
        description="cypriot_lawyer",
    ),
    Case(
        query="amparo constitucional contra acto de autoridad en México",
        expected_agent="mexican_lawyer",
        required_skills=["skill-legal-citation"],
        description="mexican_lawyer",
    ),
    Case(
        query="régimen simple de tributación en Colombia obligaciones",
        expected_agent="colombian_lawyer",
        required_skills=["skill-legal-citation"],
        description="colombian_lawyer",
    ),
    Case(
        query="налоговый кодекс Казахстан декларация физического лица",
        expected_agent="kazakh_lawyer",
        required_skills=["skill-legal-citation"],
        description="kazakh_lawyer",
    ),
    Case(
        query="Georgian Civil Code property registration small business",
        expected_agent="georgian_lawyer",
        required_skills=["skill-legal-citation"],
        description="georgian_lawyer",
    ),
    Case(
        query="Srpska EU accession law property foreign owner Serbia",
        expected_agent="serbian_lawyer",
        required_skills=["skill-legal-citation"],
        description="serbian_lawyer",
    ),
    Case(
        query="permiso de residencia España visado no lucrativo trámites",
        expected_agent="spanish_lawyer",
        required_skills=["skill-legal-citation"],
        description="spanish_lawyer",
    ),
    Case(
        query="California labor law employment at-will termination notice",
        expected_agent="us_lawyer",
        required_skills=["skill-legal-citation"],
        description="us_lawyer",
    ),

    # Dev cluster — core skill must always load
    Case(
        query="refactor this Python async function and fix the race condition in the websocket handler",
        expected_agent="software_engineer",
        required_skills=["skill-dev-clean-code"],
        description="software_engineer: core skill-dev-clean-code",
    ),
    Case(
        query="review this pull request for bugs and test coverage gaps",
        expected_agent="code_reviewer",
        required_skills=["skill-dev-clean-code"],
        description="code_reviewer: core skill-dev-clean-code",
    ),
    Case(
        query="audit this REST API endpoint for SQL injection and IDOR vulnerabilities",
        expected_agent="security_expert",
        required_skills=["skill-dev-security"],
        description="security_expert: core skill-dev-security",
    ),

    # Domain agents — each has a domain-anchor core skill
    Case(
        query="explain Bayes theorem with worked example for hypothesis testing",
        expected_agent="math_scientist",
        required_skills=["skill-mathematical-reasoning"],
        description="math_scientist: core skill-mathematical-reasoning",
    ),
    Case(
        query="help me decide whether to migrate from PostgreSQL to MongoDB, pros and cons",
        expected_agent="debate_moderator",
        required_skills=["skill-decision-frameworks"],
        description="debate_moderator: core skill-decision-frameworks",
    ),
    Case(
        query="write the API documentation for an OAuth2 token refresh endpoint",
        expected_agent="tech_writer",
        required_skills=["skill-tech-writing"],
        description="tech_writer: core skill-tech-writing",
    ),
    Case(
        query="design a prompt for extracting structured JSON from unstructured user feedback",
        expected_agent="prompt_engineer",
        required_skills=["skill-prompt-design-process"],
        description="prompt_engineer: core skill-prompt-design-process",
    ),
    Case(
        query="teach me derivatives using the Socratic method, scaffold from intuition",
        expected_agent="education_tutor",
        required_skills=["skill-pedagogy"],
        description="education_tutor: core skill-pedagogy",
    ),

    # Untouched / no-core agents — verify they still route and don't crash
    Case(
        query="patient with elevated ALT and AST, differential diagnosis for liver injury",
        expected_agent="medical_expert",
        required_skills=[],  # no core; rely on semantic match
        description="medical_expert: no core, semantic only",
    ),
    Case(
        query="design a Kubernetes deployment manifest with rolling update strategy",
        expected_agent="devops_engineer",
        required_skills=[],
        description="devops_engineer: no core",
    ),
    Case(
        query="protocol for sleep optimization: magnesium glycinate dosage and timing",
        expected_agent="bio_hacker",
        required_skills=["skill-bio-protocol-design"],
        description="bio_hacker: core skill-bio-protocol-design",
    ),
    Case(
        query="how to fix bash script that hangs on stdin when piped into another command",
        expected_agent="sysadmin",
        required_skills=[],
        description="sysadmin: no core",
    ),

    # Cross-domain — verify no drift to wrong agents
    Case(
        query="write a Python decorator that memoizes function results using LRU cache",
        expected_agent="software_engineer",
        required_skills=["skill-dev-clean-code"],
        description="cross-domain: programming → software_engineer",
    ),
    Case(
        query="synthesize the literature on long COVID epidemiology with peer-reviewed sources",
        expected_agent="deep_researcher",
        required_skills=["skill-source-trust-tiers"],
        description="deep_researcher: core skill-source-trust-tiers",
    ),
]


@dataclass
class Result:
    case: Case
    routing_outcome: str
    routed_agent: Optional[str]
    in_candidates: bool
    skills_loaded: list[str]
    implants_loaded: list[str]
    missing_required: list[str]
    forbidden_present: list[str]
    enrichment_error: Optional[str] = None

    @property
    def routing_ok(self) -> bool:
        if self.routed_agent == self.case.expected_agent:
            return True
        return self.routing_outcome == "route_required" and self.in_candidates

    @property
    def enrichment_ok(self) -> bool:
        # At least one required skill must load (or none required).
        if self.case.required_skills:
            if not any(s in self.skills_loaded for s in self.case.required_skills):
                return False
        if self.forbidden_present:
            return False
        return self.enrichment_error is None

    @property
    def passed(self) -> bool:
        return self.routing_ok and self.enrichment_ok


async def run_case(case: Case) -> Result:
    raw = await route_and_load(query=case.query)
    payload = json.loads(raw)
    status = payload.get("status", "UNKNOWN")
    routed_agent = None
    in_candidates = False
    if status in ("SUCCESS", "SUCCESS_SAMPLED", "NO_CHANGE"):
        outcome = "cache_hit"
        routed_agent = payload.get("agent")
    elif status == "ROUTE_REQUIRED":
        outcome = "route_required"
        cand_names = [c.get("name") for c in payload.get("candidates", [])]
        in_candidates = case.expected_agent in cand_names
    else:
        outcome = f"unexpected:{status}"

    skills_loaded: list[str] = []
    implants_loaded: list[str] = []
    enrichment_error: Optional[str] = None
    try:
        _, _, skills_loaded, implants_loaded, _, _ = await _load_and_enrich(
            agent_name=case.expected_agent,
            query=case.query,
            chat_history_list=[],
        )
    except Exception as e:
        enrichment_error = repr(e)

    missing = []
    if case.required_skills:
        if not any(s in skills_loaded for s in case.required_skills):
            missing = list(case.required_skills)  # report all expected
    forbidden_present = [s for s in case.forbidden_skills if s in skills_loaded]

    return Result(
        case=case,
        routing_outcome=outcome,
        routed_agent=routed_agent,
        in_candidates=in_candidates,
        skills_loaded=skills_loaded,
        implants_loaded=implants_loaded,
        missing_required=missing,
        forbidden_present=forbidden_present,
        enrichment_error=enrichment_error,
    )


async def main() -> int:
    print(f"Running {len(CASES)} cases against the 3-tier skill model.\n")
    results: list[Result] = []
    for c in CASES:
        r = await run_case(c)
        results.append(r)
        status = "PASS" if r.passed else "FAIL"
        marker_route = "✓" if r.routing_ok else "✗"
        marker_enrich = "✓" if r.enrichment_ok else "✗"
        print(f"[{status}] route{marker_route} enrich{marker_enrich}  {c.expected_agent:22}  {c.description}")
        print(f"          query: {c.query[:80]}")
        print(f"          skills_loaded ({len(r.skills_loaded)}): {r.skills_loaded}")
        if r.implants_loaded:
            print(f"          implants_loaded: {r.implants_loaded}")
        if not r.routing_ok:
            print(f"          ⚠ routing: outcome={r.routing_outcome} routed_to={r.routed_agent} in_candidates={r.in_candidates}")
        if r.missing_required:
            print(f"          ✗ none of required: {r.missing_required}")
        if r.forbidden_present:
            print(f"          ✗ forbidden_present: {r.forbidden_present}")
        if r.enrichment_error:
            print(f"          ✗ enrichment_error: {r.enrichment_error}")
        print()

    passed = sum(1 for r in results if r.passed)
    routing_passed = sum(1 for r in results if r.routing_ok)
    enrichment_passed = sum(1 for r in results if r.enrichment_ok)
    print("=" * 78)
    print(f"Overall:    {passed}/{len(results)} fully passed")
    print(f"Routing:    {routing_passed}/{len(results)} ok")
    print(f"Enrichment: {enrichment_passed}/{len(results)} ok")
    print("=" * 78)
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
