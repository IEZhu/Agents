#!/usr/bin/env python3
"""One-shot migration: add preferred_implants frontmatter to 47 agents.

For each (agent, [implants]) in MAPPING:
  - Open agents/<agent>/system_prompt.mdc
  - Detect the indent style used for the existing `capabilities:` list
  - Insert `preferred_implants:\n<indent>- <implant>\n...` right before `capabilities:`
  - Skip if `preferred_implants:` already exists (idempotent)

Usage: python3 scripts/add_preferred_implants.py [--check]
  --check: report what would change, do not write.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
AGENTS = REPO / "agents"

MAPPING: dict[str, list[str]] = {
    "3d_print_finder": ["implant-chain-of-verification", "implant-premortem"],
    "agent_builder": [
        "implant-self-discover",
        "implant-plan-and-solve-plus",
        "implant-verify-assumptions",
    ],
    "ai_senior_engineer": [
        "implant-self-discover",
        "implant-verify-assumptions",
        "implant-plan-and-solve-plus",
    ],
    "alerts_describer": [
        "implant-skeleton-of-thought",
        "implant-narrative-of-thought",
        "implant-output-priming",
    ],
    "bio_hacker": [
        "implant-chain-of-verification",
        "implant-uncertainty-quantification",
        "implant-causal-reasoning",
    ],
    "black_hole_finder": [
        "implant-maieutic-prompting",
        "implant-chain-of-verification",
        "implant-step-back-prompting",
    ],
    "blender_scripter": [
        "implant-regression-first",
        "implant-iteration-budget",
        "implant-verify-assumptions",
    ],
    "child_psychologist": [
        "implant-step-back-prompting",
        "implant-system-2-attention",
        "implant-maieutic-prompting",
    ],
    "code_reviewer": [
        "implant-role-play-expert",
        "implant-chain-of-verification",
        "implant-regression-first",
    ],
    "colombian_lawyer": [
        "implant-layer-of-thoughts",
        "implant-logic-of-thought",
        "implant-chain-of-verification",
    ],
    "cypriot_lawyer": [
        "implant-layer-of-thoughts",
        "implant-logic-of-thought",
        "implant-chain-of-verification",
    ],
    "daily_briefing": ["implant-chain-of-verification", "implant-chain-of-note"],
    "data_analyst": [
        "implant-causal-reasoning",
        "implant-chain-of-verification",
        "implant-uncertainty-quantification",
    ],
    "data_forensic": [
        "implant-narrative-of-thought",
        "implant-cumulative-reasoning",
        "implant-chain-of-verification",
    ],
    "database_admin": [
        "implant-regression-first",
        "implant-verify-assumptions",
        "implant-iteration-budget",
    ],
    "debate_moderator": [
        "implant-multi-agent-debate",
        "implant-steel-man",
        "implant-premortem",
    ],
    "deep_researcher": [
        "implant-chain-of-verification",
        "implant-step-back-prompting",
        "implant-uncertainty-quantification",
    ],
    "diagram_architect": ["implant-skeleton-of-thought", "implant-output-priming"],
    "document_ocr_expert": [
        "implant-chain-of-verification",
        "implant-contextual-compression",
    ],
    "education_tutor": [
        "implant-maieutic-prompting",
        "implant-analogical-prompting",
        "implant-step-back-prompting",
    ],
    "fitness_coach": [
        "implant-chain-of-verification",
        "implant-uncertainty-quantification",
    ],
    "georgian_lawyer": [
        "implant-layer-of-thoughts",
        "implant-logic-of-thought",
        "implant-chain-of-verification",
    ],
    "instagram_analyst": [
        "implant-step-back-prompting",
        "implant-chain-of-verification",
    ],
    "install_to_repo": [
        "implant-plan-and-solve-plus",
        "implant-verify-assumptions",
        "implant-iteration-budget",
    ],
    "investigative_analyst": [
        "implant-cumulative-reasoning",
        "implant-chain-of-verification",
        "implant-causal-reasoning",
    ],
    "kazakh_lawyer": [
        "implant-layer-of-thoughts",
        "implant-logic-of-thought",
        "implant-chain-of-verification",
    ],
    "literary_writer": [
        "implant-analogical-prompting",
        "implant-generated-knowledge",
        "implant-self-refine",
    ],
    "mcp_builder": [
        "implant-verify-assumptions",
        "implant-plan-and-solve-plus",
        "implant-iteration-budget",
    ],
    "medical_expert": [
        "implant-chain-of-verification",
        "implant-uncertainty-quantification",
        "implant-causal-reasoning",
    ],
    "mexican_lawyer": [
        "implant-layer-of-thoughts",
        "implant-logic-of-thought",
        "implant-chain-of-verification",
    ],
    "presentation_coach": [
        "implant-skeleton-of-thought",
        "implant-premortem",
        "implant-self-refine",
    ],
    "product_manager": [
        "implant-premortem",
        "implant-second-order-thinking",
        "implant-verify-assumptions",
    ],
    "prompt_engineer": [
        "implant-self-refine",
        "implant-chain-of-verification",
        "implant-contrastive-cot",
    ],
    "psychologist": [
        "implant-maieutic-prompting",
        "implant-system-2-attention",
        "implant-step-back-prompting",
    ],
    "purchase_researcher": [
        "implant-chain-of-verification",
        "implant-uncertainty-quantification",
    ],
    "roblox_studio_expert": [
        "implant-regression-first",
        "implant-iteration-budget",
    ],
    "russian_lawyer": [
        "implant-layer-of-thoughts",
        "implant-logic-of-thought",
        "implant-chain-of-verification",
    ],
    "security_expert": [
        "implant-role-play-expert",
        "implant-premortem",
        "implant-steel-man",
    ],
    "semantic_expert": [
        "implant-narrative-of-thought",
        "implant-thread-of-thought",
        "implant-decomposed-prompting",
    ],
    "serbian_lawyer": [
        "implant-layer-of-thoughts",
        "implant-logic-of-thought",
        "implant-chain-of-verification",
    ],
    "spanish_lawyer": [
        "implant-layer-of-thoughts",
        "implant-logic-of-thought",
        "implant-chain-of-verification",
    ],
    "system_architect": [
        "implant-plan-and-solve-plus",
        "implant-step-back-prompting",
        "implant-premortem",
    ],
    "tech_writer": [
        "implant-skeleton-of-thought",
        "implant-self-refine",
        "implant-chain-of-verification",
    ],
    "universal_agent": [
        "implant-self-discover",
        "implant-step-back-prompting",
        "implant-decomposed-prompting",
    ],
    "us_lawyer": [
        "implant-layer-of-thoughts",
        "implant-logic-of-thought",
        "implant-chain-of-verification",
    ],
    "ux_designer": [
        "implant-role-play-expert",
        "implant-premortem",
        "implant-second-order-thinking",
    ],
    "website_analyst": [
        "implant-causal-reasoning",
        "implant-chain-of-verification",
    ],
}


CAPS_RE = re.compile(r"^(?P<indent>[ \t]*)capabilities:\s*$", re.MULTILINE)
ITEM_RE = re.compile(r"^(?P<indent>[ \t]*-\s+)(?P<quote>['\"]?)")


def detect_item_style(text: str, caps_start: int) -> str:
    """Return the exact prefix used by the first item under capabilities:
    e.g. '- ' or '  - ' or '  - "' (with quote)."""
    after = text[caps_start:]
    next_lines = after.splitlines()[1:]
    for ln in next_lines:
        m = ITEM_RE.match(ln)
        if m:
            return m.group("indent") + m.group("quote")
        if ln.strip() and not ln.startswith((" ", "\t")):
            break
    return "- "  # fallback


def make_block(implants: list[str], style: str) -> str:
    if style.endswith(('"', "'")):
        q = style[-1]
        indent = style[:-1]
        lines = [f"{indent}{q}{i}{q}" for i in implants]
    else:
        lines = [f"{style}{i}" for i in implants]
    return "preferred_implants:\n" + "\n".join(lines) + "\n"


def already_has_preferred_implants(text: str) -> bool:
    return re.search(r"^preferred_implants:\s*$", text, re.MULTILINE) is not None


def process(agent: str, implants: list[str], check_only: bool) -> str:
    path = AGENTS / agent / "system_prompt.mdc"
    text = path.read_text(encoding="utf-8")
    if already_has_preferred_implants(text):
        return f"SKIP {agent}: already has preferred_implants"

    matches = list(CAPS_RE.finditer(text))
    if not matches:
        return f"FAIL {agent}: no `capabilities:` line found"

    m = matches[0]
    style = detect_item_style(text, m.start())
    block = make_block(implants, style)
    new_text = text[: m.start()] + block + text[m.start() :]
    if check_only:
        return f"WOULD-WRITE {agent}: inject {len(implants)} implants (style {style!r})"
    path.write_text(new_text, encoding="utf-8")
    return f"OK   {agent}: +{len(implants)} implants"


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="dry run")
    args = ap.parse_args(argv)

    results = []
    for agent, implants in sorted(MAPPING.items()):
        results.append(process(agent, implants, args.check))

    fails = [r for r in results if r.startswith("FAIL")]
    skips = [r for r in results if r.startswith("SKIP")]
    ok = [r for r in results if r.startswith(("OK", "WOULD"))]

    for r in results:
        print(r)
    print(f"\n--- {len(ok)} written/would-write, {len(skips)} skipped, {len(fails)} failed ---")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
