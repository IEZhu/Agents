#!/usr/bin/env python3
"""Migrate 51 agents from {preferred_skills, capabilities} to the new
{core_skills, preferred_skills, capable_skills} 3-tier model.

Strategy (per agent):
  1. Resolve current capabilities → their skill lists via the registry snapshot.
  2. Apply Category C replacements: capability → new skill (legal-reasoning → skill-legal-citation).
  3. Pick core_skills: 1–2 most domain-specific (hardcoded per agent or empty).
  4. preferred_skills = current `preferred_skills` ∪ resolved-skills ∪ category-C-additions, deduped, minus core.
  5. capable_skills = Category E reattachments (per the plan) + extra "tangential" skills.
  6. For Category B agents: queue directive content to be appended to body.

Capability → new-skill replacements (Category C):
  consultative-intake     → skill-consultative-intake
  legal-reasoning         → skill-legal-citation
  trust-weighted-research → skill-source-trust-tiers
  data-investigation      → skill-forensic-process
  bio-health              → skill-bio-protocol-design
  epistemic-analysis      → skill-epistemic-method
  prompt-design           → skill-prompt-design-process
  creative-writing        → skill-creative-craft

Category B agents (single-use capability) get a body section appended:
  agent → section_title → directive content
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parents[2]
_REGISTRY_PATH = Path(__file__).with_name("registry_snapshot.yaml")
if not _REGISTRY_PATH.is_file():
    raise FileNotFoundError(f"Missing registry snapshot: {_REGISTRY_PATH}")
REGISTRY = yaml.safe_load(_REGISTRY_PATH.read_text(encoding="utf-8")) or {}

# Category C: old capability → new skill that replaces the directive.
# When agent had this capability, ADD the new skill (the bundle skills are
# still added too).
CATEGORY_C_NEW_SKILL = {
    "consultative-intake": "skill-consultative-intake",
    "legal-reasoning": "skill-legal-citation",
    "trust-weighted-research": "skill-source-trust-tiers",
    "data-investigation": "skill-forensic-process",
    "bio-health": "skill-bio-protocol-design",
    "epistemic-analysis": "skill-epistemic-method",
    "prompt-design": "skill-prompt-design-process",
    "creative-writing": "skill-creative-craft",
}

# Per-agent core_skills selection (1–2 most domain-anchoring skills).
# Empty list = no mandatory skill, all goes to preferred/capable.
CORE_PER_AGENT: dict[str, list[str]] = {
    "3d_print_finder": ["skill-3d-platforms"],
    "agent_builder": ["skill-tech-writing"],
    "ai_senior_engineer": [],
    "alerts_describer": [],
    "bio_hacker": ["skill-bio-protocol-design"],
    "black_hole_finder": ["skill-epistemic-method"],
    "blender_scripter": ["skill-blender-scripting"],
    "child_psychologist": ["skill-psy-child-dev"],
    "code_reviewer": ["skill-dev-clean-code"],
    "colombian_lawyer": ["skill-legal-citation"],
    "cypriot_lawyer": ["skill-legal-citation"],
    "daily_briefing": [],
    "data_analyst": ["skill-analysis-critical"],
    "data_forensic": ["skill-forensic-process"],
    "database_admin": [],
    "debate_moderator": ["skill-decision-frameworks"],
    "deep_researcher": ["skill-source-trust-tiers"],
    "devops_engineer": [],
    "diagram_architect": ["skill-mermaid-best-practices"],
    "document_ocr_expert": [],
    "education_tutor": ["skill-pedagogy"],
    "fitness_coach": ["skill-fitness-programming"],
    "georgian_lawyer": ["skill-legal-citation"],
    "instagram_analyst": [],
    "install_to_repo": [],
    "investigative_analyst": ["skill-fact-verification"],
    "kazakh_lawyer": ["skill-legal-citation"],
    "literary_writer": ["skill-creative-craft"],
    "math_scientist": ["skill-mathematical-reasoning"],
    "mcp_builder": ["skill-mcp-development"],
    "medical_expert": [],
    "mexican_lawyer": ["skill-legal-citation"],
    "presentation_coach": [],
    "product_manager": ["skill-product-frameworks"],
    "prompt_engineer": ["skill-prompt-design-process"],
    "psychologist": ["skill-psy-cbt"],
    "purchase_researcher": ["skill-purchase-research"],
    "roblox_studio_expert": ["skill-roblox-development"],
    "russian_lawyer": ["skill-legal-citation"],
    "security_expert": ["skill-dev-security"],
    "semantic_expert": ["skill-epistemic-method"],
    "serbian_lawyer": ["skill-legal-citation"],
    "software_engineer": ["skill-dev-clean-code"],
    "spanish_lawyer": ["skill-legal-citation"],
    "sysadmin": [],
    "system_architect": ["skill-system-design"],
    "tech_writer": ["skill-tech-writing"],
    "universal_agent": [],
    "us_lawyer": ["skill-legal-citation"],
    "ux_designer": ["skill-ux-principles"],
    "website_analyst": [],
}

# Category E: skills from unused capabilities → reattach to obvious agents.
EXTRA_CAPABLE: dict[str, list[str]] = {
    "ai_senior_engineer": [
        "skill-react-pattern", "skill-agentic-loops", "skill-agent-handoff",
        "skill-prompt-security",
    ],
    "purchase_researcher": ["skill-purchase-research"],
    "mcp_builder": ["skill-mcp-development", "skill-dev-api-design"],
    "software_engineer": ["skill-mcp-development", "skill-dev-api-design"],
    "fitness_coach": ["skill-fitness-programming", "skill-bio-protocols"],
    "bio_hacker": ["skill-bio-protocols", "skill-bio-mechanism"],
    "prompt_engineer": ["skill-prompt-security"],
}

# Category B: directive content to append to agent body.
CATEGORY_B_BODY: dict[str, dict[str, str]] = {
    "3d_print_finder": {
        "title": "Search Strategy",
        "body": "- PQRS scoring (Platform-Quality-Relevance-Source) for every result.\n"
                "- Tier 1 platforms first (Printables, Thangs, MyMiniFactory, Cults3D).\n"
                "- Filter by FDM-specific criteria (printability, bed size, support overhang).\n",
    },
    "blender_scripter": {
        "title": "Scripting Principles",
        "body": "- Prefer `bmesh` over `bpy.ops` in loops (performance, undo stack).\n"
                "- Mesh must be manifold (watertight, normals out, no self-intersect).\n"
                "- Parametric design — drive geometry from parameters, not hand-tweaked vertices.\n"
                "- Validate printability before export (min wall thickness, overhang, support).\n",
    },
    "child_psychologist": {
        "title": "Practice Principles",
        "body": "- Age-stage first (developmental milestones before pathology lens).\n"
                "- Normalize before pathologize — many behaviors are stage-appropriate.\n"
                "- Attachment lens for parent-child dynamics.\n"
                "- Digital context assessment (screen time, content, social platforms).\n"
                "- Parent coaching: authoritative boundaries, not authoritarian.\n"
                "- Never diagnose — refer to clinical evaluation when warranted.\n",
    },
    "code_reviewer": {
        "title": "Review Discipline",
        "body": "- Review for: correctness, security, performance, readability, test coverage.\n"
                "- **Flag, don't fix** — the reviewer's job is identification, not implementation.\n"
                "- Distinguish blockers (must fix) from suggestions (nice-to-have).\n",
    },
    "math_scientist": {
        "title": "Solution Process",
        "body": "- Define variables explicitly with units.\n"
                "- Show every step of derivation.\n"
                "- Verify with back-substitution into the original equation.\n"
                "- Dimensional analysis to catch unit errors.\n"
                "- Estimate the answer before computing (order-of-magnitude sanity check).\n",
    },
    "education_tutor": {
        "title": "Teaching Principles",
        "body": "- Socratic method: lead with questions, not statements.\n"
                "- Scaffold complexity — each step builds on the previous.\n"
                "- Operate within the Zone of Proximal Development (ZPD).\n"
                "- Active recall over passive review.\n"
                "- Check understanding before advancing.\n",
    },
    "software_engineer": {
        "title": "Performance Discipline",
        "body": "- **Measure first** — never optimize without a benchmark.\n"
                "- Profile before optimizing (hot path identification).\n"
                "- Benchmark before/after each optimization to verify improvement.\n",
    },
    "product_manager": {
        "title": "Prioritization",
        "body": "- User problem first — feature follows problem, not vice versa.\n"
                "- RICE prioritization (Reach × Impact × Confidence / Effort).\n"
                "- Define measurable outcomes — every initiative needs a metric.\n",
    },
    "psychologist": {
        "title": "Session Approach",
        "body": "- Socratic questions to surface beliefs without imposing them.\n"
                "- I-statements model — own feelings, avoid blame.\n"
                "- OFNR framework (Observation, Feeling, Need, Request).\n"
                "- Cognitive triangle: thoughts → feelings → behaviors.\n",
    },
    "roblox_studio_expert": {
        "title": "Server-Auth Principles",
        "body": "- **Server authority** — game state lives on server, never trust client.\n"
                "- Performance budgets per frame (16ms target for 60fps).\n"
                "- Object pooling for frequently spawned entities.\n"
                "- Anti-exploit patterns: validate all RemoteEvent payloads.\n",
    },
    "system_architect": {
        "title": "Design Process",
        "body": "- Requirements first — non-functional (latency, scale, availability) before functional.\n"
                "- Back-of-envelope estimation (RPS, data volume, costs).\n"
                "- C4 model levels: Context → Container → Component → Code.\n"
                "- Trade-offs explicit (e.g., consistency vs availability per CAP).\n"
                "- Failure modes mandatory: what breaks first?\n"
                "- Diagrams required for non-trivial designs.\n",
    },
    "ux_designer": {
        "title": "Design Heuristics",
        "body": "- Nielsen's 10 usability heuristics as baseline.\n"
                "- Gestalt principles (proximity, similarity, closure) for visual grouping.\n"
                "- WCAG AA minimum for accessibility.\n"
                "- Mobile-first responsive design.\n"
                "- User testing validates assumptions — never ship without it.\n",
    },
}


SPLIT_RE = re.compile(r"^---\s*\n(.*?\n)---\s*\n(.*)$", re.DOTALL)


def parse_frontmatter(text: str) -> tuple[dict, str, str]:
    m = SPLIT_RE.match(text)
    if not m:
        raise ValueError("no frontmatter")
    fm = yaml.safe_load(m.group(1)) or {}
    return fm, m.group(1), m.group(2)


def resolve_caps(caps: list[str]) -> list[str]:
    """Expand capabilities to their bundle skills."""
    out: list[str] = []
    seen: set[str] = set()
    for c in caps:
        entry = REGISTRY.get(c)
        if not isinstance(entry, dict):
            continue
        for s in entry.get("skills", []) or []:
            if s not in seen:
                out.append(s)
                seen.add(s)
    return out


def build_new_skill_lists(agent: str, fm: dict) -> tuple[list[str], list[str], list[str]]:
    current_preferred = list(fm.get("preferred_skills", []) or [])
    current_caps = list(fm.get("capabilities", []) or [])

    # Resolve capabilities to skills
    cap_skills = resolve_caps(current_caps)

    # Category C: for each old capability, if it has a replacement skill, add it
    cat_c_additions: list[str] = []
    for c in current_caps:
        new_skill = CATEGORY_C_NEW_SKILL.get(c)
        if new_skill:
            cat_c_additions.append(new_skill)

    # Build candidate preferred pool
    candidate = []
    seen: set[str] = set()
    for s in current_preferred + cap_skills + cat_c_additions:
        if s not in seen:
            candidate.append(s)
            seen.add(s)

    # Assign core
    core = CORE_PER_AGENT.get(agent, [])
    # Validate: core skills must exist; if not in candidate, add them anyway (they're picked from existing skill set)
    for c in core:
        if c not in seen:
            candidate.append(c)
            seen.add(c)
    # Preferred = candidate minus core
    preferred = [s for s in candidate if s not in core]

    # Capable = extra reattachments (Category E)
    capable = list(EXTRA_CAPABLE.get(agent, []))
    # Filter capable: drop any already in core/preferred
    capable = [s for s in capable if s not in core and s not in preferred]

    return core, preferred, capable


def render_frontmatter(fm: dict, agent: str) -> str:
    """Build YAML frontmatter preserving original style — easier than diff editing.

    Output ordering: identity, routing, [core_skills], [preferred_skills], [capable_skills],
    preferred_implants, then any other passthrough fields.
    """
    core, preferred, capable = build_new_skill_lists(agent, fm)

    # Drop old keys from a copy
    new_fm: dict = {}
    new_fm["identity"] = fm["identity"]
    new_fm["routing"] = fm["routing"]
    new_fm["core_skills"] = core
    new_fm["preferred_skills"] = preferred
    new_fm["capable_skills"] = capable
    if "preferred_implants" in fm:
        new_fm["preferred_implants"] = fm["preferred_implants"]
    for k, v in fm.items():
        if k in {"identity", "routing", "preferred_skills", "capabilities",
                 "capable_skills", "core_skills", "preferred_implants"}:
            continue
        new_fm[k] = v

    # Custom YAML dump preserving order
    return yaml.dump(new_fm, sort_keys=False, allow_unicode=True, default_flow_style=False)


def maybe_append_body_section(agent: str, body: str) -> str:
    """If this is a Category B agent, append the directive content as a new section."""
    section = CATEGORY_B_BODY.get(agent)
    if not section:
        return body
    title = section["title"]
    if f"## {title}" in body:
        return body  # already added
    addition = f"\n\n## {title}\n\n{section['body']}"
    return body.rstrip() + addition + "\n"


def main() -> int:
    paths = sorted((REPO_ROOT / "agents").glob("*/system_prompt.mdc"))
    if not paths:
        print("No agent files found", file=sys.stderr)
        return 1
    modified = 0
    for p in paths:
        agent = p.parent.name
        text = p.read_text(encoding="utf-8")
        try:
            fm, _fm_raw, body = parse_frontmatter(text)
        except Exception as e:
            print(f"SKIP {agent}: {e}", file=sys.stderr)
            continue
        new_fm_text = render_frontmatter(fm, agent)
        new_body = maybe_append_body_section(agent, body)
        new_text = f"---\n{new_fm_text}---\n{new_body}"
        p.write_text(new_text, encoding="utf-8")
        modified += 1
        core, preferred, capable = build_new_skill_lists(agent, fm)
        marker = " + body" if agent in CATEGORY_B_BODY else ""
        print(f"  ✓ {agent}: core={len(core)} preferred={len(preferred)} capable={len(capable)}{marker}")

    print(f"\nmigrated: {modified} / {len(paths)} agents")
    return 0


if __name__ == "__main__":
    sys.exit(main())
