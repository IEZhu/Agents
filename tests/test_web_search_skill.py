"""Tests for the web-search capability: skill artifact, agent wiring, and the
always-on search trigger in the rules layer.

These guard the mechanism (the skill exists, is well-formed, is wired to the
right agents, and loads even on short/lite-tier queries for core agents) without
invoking an LLM — so they are deterministic and fast. Whether an agent *acts* on
the trigger is a behavioral property checked manually, not here.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest
import yaml

from src.utils.prompt_loader import split_frontmatter

REPO_ROOT = Path(__file__).resolve().parents[1]
SKILL_ID = "skill-web-search"
SKILL_FILE = REPO_ROOT / "skills/skill-web-search.mdc"
RULE_FILE = REPO_ROOT / "rules/rule-no-fabrication.mdc"

# Wiring contract: changing a tier is a deliberate change — update this map too.
WIRING = {
    "core_skills": ["daily_briefing", "purchase_researcher"],
    "preferred_skills": [
        "deep_researcher", "investigative_analyst", "website_analyst", "sysadmin",
        "software_engineer", "devops_engineer", "security_expert", "lawyer",
        "medical_expert", "data_analyst", "product_manager", "database_admin",
        "bio_hacker",
    ],
    "capable_skills": [
        "code_reviewer", "ai_senior_engineer", "black_hole_finder", "data_forensic",
        "3d_print_finder", "universal_agent", "debate_moderator",
    ],
}
# A representative slice of agents that must NOT get the skill (self-contained).
EXCLUDED = ["literary_writer", "math_scientist", "education_tutor", "blender_scripter"]

ALL_TIERS = ("core_skills", "preferred_skills", "capable_skills")


def _frontmatter(path: Path) -> dict:
    frontmatter, _ = split_frontmatter(path.read_text(encoding="utf-8"))
    assert frontmatter is not None, f"{path} has no frontmatter"
    return yaml.safe_load(frontmatter)


# --- Skill artifact ------------------------------------------------------------

def test_skill_file_exists_and_parses():
    assert SKILL_FILE.is_file(), f"{SKILL_FILE} missing"
    fm = _frontmatter(SKILL_FILE)
    for key in ("description", "compiled", "keywords"):
        assert fm.get(key), f"skill frontmatter missing/empty '{key}'"


def test_compiled_is_self_sufficient_for_standard_tier():
    """Standard tier shows ONLY `compiled` — it must carry the trigger + ladder."""
    compiled = _frontmatter(SKILL_FILE)["compiled"].lower()
    assert "search" in compiled and "don't guess" in compiled  # the WHEN trigger
    assert "websearch" in compiled and "webfetch" in compiled  # the ladder


def test_keywords_are_atomic_for_keyword_boost():
    """keyword_boost matches a keyword literally appearing in the query, so
    compound 'a / b / c' entries never fire. Keep keywords atomic."""
    for kw in _frontmatter(SKILL_FILE)["keywords"]:
        assert "/" not in kw, f"non-atomic keyword would never keyword-boost: {kw!r}"


# --- Agent wiring --------------------------------------------------------------

@pytest.mark.parametrize(
    "agent,tier",
    [(a, t) for t, agents in WIRING.items() for a in agents],
)
def test_skill_wired_to_correct_tier(agent, tier):
    fm = _frontmatter(REPO_ROOT / f"agents/{agent}/system_prompt.mdc")
    assert SKILL_ID in (fm.get(tier) or []), f"{agent}: {SKILL_ID} not in {tier}"
    for other in ALL_TIERS:
        if other != tier:
            assert SKILL_ID not in (fm.get(other) or []), f"{agent}: also in {other}"


@pytest.mark.parametrize("agent", EXCLUDED)
def test_skill_not_wired_to_excluded_agents(agent):
    fm = _frontmatter(REPO_ROOT / f"agents/{agent}/system_prompt.mdc")
    for tier in ALL_TIERS:
        assert SKILL_ID not in (fm.get(tier) or []), f"{agent} should not have {SKILL_ID}"


# --- Rules layer: the always-on WHEN trigger -----------------------------------

def test_no_fabrication_rule_carries_search_trigger():
    """The decision to search must live in the always-on rule layer so it fires
    even in lite tier where preferred/capable skills do not load."""
    body = RULE_FILE.read_text(encoding="utf-8").lower()
    assert "search" in body and "fetch the web" in body


# --- Behavioral: core skill loads even at lite tier (n_results=0) ---------------

def test_core_skill_loads_in_lite_tier(tmp_path, monkeypatch):
    """Core agents get the skill on EVERY tier; lite passes n_results=0, so only
    mandatory (core) skills load — this proves the core wiring is reachable."""
    from src.engine import skills
    from src.engine.vector_store import NumpyVectorStore

    def unexpected_embedding(*args, **kwargs):
        pytest.fail("Mandatory-only retrieval must not use embeddings")

    monkeypatch.setattr(skills, "embed_texts", unexpected_embedding)
    monkeypatch.setattr(skills, "embed_query", unexpected_embedding)

    _, body = split_frontmatter(SKILL_FILE.read_text(encoding="utf-8"))
    filename = SKILL_FILE.name
    # Skip __init__: it can reindex every skill in the shared installation.
    retriever = object.__new__(skills.SkillRetriever)
    retriever.store = NumpyVectorStore(name="skills", data_dir=str(tmp_path))
    retriever.store.replace(
        ids=[filename],
        embeddings=np.array([[1.0, 0.0]], dtype=np.float32),
        documents=[body],
        metadatas=[{"filename": filename, "body": body}],
    )
    res = retriever.retrieve(
        "x", mandatory=[SKILL_ID], preferred=["skill-dev-debugging"], n_results=0,
    )
    assert [skill["filename"] for skill in res] == [filename]
    assert res[0]["content"] == body
    assert res[0]["tier"] == "mandatory"
