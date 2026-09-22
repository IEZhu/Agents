"""Protocol-v2 bundles must describe fresh, complete issued content."""

from dataclasses import asdict
from types import SimpleNamespace

import pytest
import yaml

from src.engine import enrichment, rules
from src.engine.implants import ImplantRetriever
from src.engine.persona_bundle import _declared_ids, build_persona_bundle
from src.engine.skills import SkillRetriever
from src.utils import prompt_loader


def write_mdc(path, metadata, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{yaml.safe_dump(metadata)}---\n{body}", encoding="utf-8")


@pytest.fixture
def bundle_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt_loader, "REPO_ROOT", str(tmp_path))
    for variable, folder in (("AGENTS_DIR", "agents"), ("SKILLS_DIR", "skills"), ("IMPLANTS_DIR", "implants")):
        monkeypatch.setattr(prompt_loader, variable, str(tmp_path / folder))
    monkeypatch.setattr(rules, "RULES_DIR", str(tmp_path / "rules"))
    monkeypatch.setattr(rules, "RULES_ENABLED", True)
    monkeypatch.setattr(enrichment, "skill_retriever", SimpleNamespace(
        retrieve=lambda *args, **kwargs: [{"filename": "skill-core.mdc", "content": "STALE"}],
        format_skills_for_prompt=lambda items, **kwargs: SkillRetriever.format_skills_for_prompt(None, items, **kwargs),
    ))
    monkeypatch.setattr(enrichment, "implant_retriever", SimpleNamespace(
        retrieve=lambda *args, **kwargs: [{"filename": "implant-focus.mdc", "content": "STALE"}],
        format_implants_for_prompt=lambda items: ImplantRetriever.format_implants_for_prompt(None, items),
    ))
    agent = {
        "identity": {"name": "engineer", "role": "Implement and debug software"},
        "core_skills": ["skill-core"], "preferred_skills": ["skill-extra"],
        "capable_skills": [], "preferred_implants": ["implant-focus"],
    }
    write_mdc(tmp_path / "agents/engineer/system_prompt.mdc", agent, "Engineer persona\n@common.mdc")
    (tmp_path / "common.mdc").write_text("Imported guidance", encoding="utf-8")
    write_mdc(tmp_path / "skills/skill-core.mdc", {"description": "Core", "compiled": "Core summary"}, "Core body")
    write_mdc(tmp_path / "skills/skill-extra.mdc", {"description": "Extra"}, "Extra body")
    write_mdc(tmp_path / "implants/implant-focus.mdc", {"description": "Focus", "short_name": "Focus"}, "Focus body")
    write_mdc(tmp_path / "rules/rule-truth.mdc", {"name": "truth", "priority": 1}, "Use evidence")
    return tmp_path, agent


OPTIONAL_COMPONENT_KEYS = (
    "core_skills", "preferred_skills", "capable_skills", "preferred_implants",
)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", OPTIONAL_COMPONENT_KEYS)
async def test_null_component_list_matches_empty_and_omitted(bundle_tree, monkeypatch, key):
    tree, agent = bundle_tree
    monkeypatch.setattr(enrichment.skill_retriever, "retrieve", lambda *args, **kwargs: [])
    monkeypatch.setattr(enrichment.implant_retriever, "retrieve", lambda *args, **kwargs: [])
    path = tree / "agents/engineer/system_prompt.mdc"
    agent[key] = []
    write_mdc(path, agent, "Engineer persona")
    empty = await build_persona_bundle("engineer", "A", tier="deep")

    agent[key] = None
    write_mdc(path, agent, "Engineer persona")
    null = await build_persona_bundle("engineer", "A", tier="deep")

    del agent[key]
    write_mdc(path, agent, "Engineer persona")
    omitted = await build_persona_bundle("engineer", "A", tier="deep")
    assert asdict(null) == asdict(empty) == asdict(omitted)


@pytest.mark.asyncio
@pytest.mark.parametrize("key", OPTIONAL_COMPONENT_KEYS)
@pytest.mark.parametrize("value", [False, 0, "", {}, {"skill-core": True}, "skill-core", [None], ["skill-core", 1]])
async def test_invalid_optional_component_list_still_fails_bundle(bundle_tree, key, value):
    tree, agent = bundle_tree
    agent[key] = value
    write_mdc(tree / "agents/engineer/system_prompt.mdc", agent, "Engineer persona")
    with pytest.raises(ValueError, match=f"Agent {key} must be a list of component IDs"):
        await build_persona_bundle("engineer", "A", tier="deep")


@pytest.mark.parametrize("key", OPTIONAL_COMPONENT_KEYS)
def test_declared_component_ids_preserve_first_occurrence_order(key):
    prefix = "implant" if key == "preferred_implants" else "skill"
    values = [f"{prefix}-second.mdc", f"{prefix}-first", f"{prefix}-second"]
    assert _declared_ids({key: values}, key) == [f"{prefix}-second", f"{prefix}-first"]


@pytest.mark.asyncio
async def test_bundle_is_separate_fresh_and_deterministic(bundle_tree):
    first = await build_persona_bundle("engineer", "A", tier="deep")
    second = await build_persona_bundle("engineer", "B", history=["irrelevant"], tier="deep")
    assert first.bundle_revision == second.bundle_revision
    assert len(first.bundle_revision) == 64
    assert first.scope == "Implement and debug software"
    assert first.skills_loaded == ["skill-core"]
    assert first.implants_loaded == ["Focus"]
    assert first.rules_loaded == ["truth"]
    assert "Imported guidance" in first.persona_block
    assert "Core body" in first.skills_block
    assert "Use evidence" in first.rules_block
    assert "Focus body" in first.implants_block
    assert "STALE" not in str(asdict(first))
    assert "Core body" not in first.persona_block


@pytest.mark.asyncio
@pytest.mark.parametrize("relative", [
    "agents/engineer/system_prompt.mdc", "common.mdc", "skills/skill-core.mdc",
    "implants/implant-focus.mdc", "rules/rule-truth.mdc",
])
async def test_refresh_reads_actual_component_content(bundle_tree, relative):
    tree, _ = bundle_tree
    first = await build_persona_bundle("engineer", "Same request", tier="deep")
    target = tree / relative
    target.write_text(target.read_text() + "\nNew guidance", encoding="utf-8")
    second = await build_persona_bundle("engineer", "Same request", tier="deep")
    assert second.bundle_revision != first.bundle_revision
    assert "New guidance" in "\n".join([
        second.persona_block, second.rules_block, second.skills_block, second.implants_block,
    ])


@pytest.mark.asyncio
async def test_compiled_skill_reads_fresh_frontmatter(bundle_tree):
    tree, _ = bundle_tree
    first = await build_persona_bundle("engineer", "Same request", tier="standard")
    write_mdc(tree / "skills/skill-core.mdc", {"description": "Core", "compiled": "New summary"}, "Core body")
    second = await build_persona_bundle("engineer", "Same request", tier="standard")
    assert "New summary" in second.skills_block
    assert second.bundle_revision != first.bundle_revision


@pytest.mark.asyncio
async def test_scope_change_refreshes_gate_competency(bundle_tree):
    tree, agent = bundle_tree
    path = tree / "agents/engineer/system_prompt.mdc"
    first = await build_persona_bundle("engineer", "A", tier="deep")
    _, body = prompt_loader.read_mdc(str(path))
    agent["identity"]["role"] = "Implement, debug, and assess software architecture"
    write_mdc(path, agent, body)
    second = await build_persona_bundle("engineer", "A", tier="deep")
    assert first.persona_block == second.persona_block
    assert first.agent == second.agent
    assert first.scope != second.scope
    assert first.bundle_revision != second.bundle_revision


@pytest.mark.asyncio
async def test_rule_description_import_is_resolved_and_revisioned(bundle_tree):
    tree, _ = bundle_tree
    write_mdc(tree / "rules/rule-truth.mdc", {"name": "truth", "description": "@rule-description.mdc"}, "Use evidence")
    description = tree / "rule-description.mdc"
    description.write_text("Original rule description", encoding="utf-8")
    first = await build_persona_bundle("engineer", "A", tier="deep")
    assert "Original rule description" in first.rules_block
    assert "@rule-description.mdc" not in first.rules_block
    description.write_text("Updated rule description", encoding="utf-8")
    second = await build_persona_bundle("engineer", "A", tier="deep")
    assert "Updated rule description" in second.rules_block
    assert second.bundle_revision != first.bundle_revision


@pytest.mark.asyncio
async def test_standard_tier_falls_back_to_description_without_compiled(bundle_tree):
    tree, _ = bundle_tree
    write_mdc(tree / "skills/skill-core.mdc", {"description": "Use defensive parsing"}, "Full body")
    bundle = await build_persona_bundle("engineer", "A", tier="standard")
    assert "Use defensive parsing" in bundle.skills_block
    assert "Full body" not in bundle.skills_block


@pytest.mark.asyncio
async def test_missing_description_cannot_issue_empty_compiled_skill(bundle_tree):
    tree, _ = bundle_tree
    write_mdc(tree / "skills/skill-core.mdc", {}, "Full body")
    with pytest.raises(ValueError, match="description"):
        await build_persona_bundle("engineer", "A", tier="standard")


@pytest.mark.asyncio
async def test_order_changes_revision_but_not_role(bundle_tree):
    tree, agent = bundle_tree
    agent["core_skills"] = ["skill-core", "skill-extra"]
    path = tree / "agents/engineer/system_prompt.mdc"
    write_mdc(path, agent, "Engineer persona")
    first = await build_persona_bundle("engineer", "A", tier="lite")
    agent["core_skills"].reverse()
    write_mdc(path, agent, "Engineer persona")
    second = await build_persona_bundle("engineer", "A", tier="lite")
    assert first.agent == second.agent == "engineer"
    assert first.bundle_revision != second.bundle_revision
    assert second.skills_loaded == ["skill-extra", "skill-core"]


@pytest.mark.asyncio
@pytest.mark.parametrize("relative", [
    "agents/engineer/system_prompt.mdc", "common.mdc", "skills/skill-core.mdc",
    "implants/implant-focus.mdc", "rules/rule-truth.mdc",
])
async def test_missing_mandatory_source_fails_bundle(bundle_tree, relative):
    tree, _ = bundle_tree
    (tree / relative).unlink()
    with pytest.raises((FileNotFoundError, ValueError)):
        await build_persona_bundle("engineer", "A", tier="deep")


@pytest.mark.asyncio
@pytest.mark.parametrize("relative", [
    "agents/engineer/system_prompt.mdc", "skills/skill-core.mdc",
    "implants/implant-focus.mdc", "rules/rule-truth.mdc",
])
async def test_malformed_component_fails_bundle(bundle_tree, relative):
    tree, _ = bundle_tree
    (tree / relative).write_text("---\n[not, a, mapping]\n---\nBody", encoding="utf-8")
    with pytest.raises(ValueError):
        await build_persona_bundle("engineer", "A", tier="deep")


@pytest.mark.asyncio
async def test_malformed_rule_field_is_not_coerced_into_issued_text(bundle_tree):
    tree, _ = bundle_tree
    write_mdc(tree / "rules/rule-truth.mdc", {"name": "truth", "description": ["invalid"]}, "Evidence")
    with pytest.raises(ValueError, match="description must be text"):
        await build_persona_bundle("engineer", "A", tier="deep")


@pytest.mark.asyncio
async def test_selected_skill_import_is_resolved_and_revisioned(bundle_tree):
    tree, _ = bundle_tree
    path = tree / "skills/skill-core.mdc"
    write_mdc(path, {"description": "Core"}, "@common.mdc")
    first = await build_persona_bundle("engineer", "A", tier="deep")
    assert "Imported guidance" in first.skills_block
    (tree / "common.mdc").write_text("Changed import", encoding="utf-8")
    second = await build_persona_bundle("engineer", "A", tier="deep")
    assert second.bundle_revision != first.bundle_revision
    assert "Changed import" in second.skills_block


@pytest.mark.asyncio
async def test_policy_violation_is_not_issued(bundle_tree, monkeypatch):
    monkeypatch.setattr(enrichment.skill_retriever, "retrieve", lambda *args, **kwargs: [{"filename": "skill-unrelated.mdc"}])
    with pytest.raises(ValueError, match="outside.*policy"):
        await build_persona_bundle("engineer", "A")


@pytest.mark.asyncio
async def test_core_sources_do_not_depend_on_index_presence(bundle_tree, monkeypatch):
    monkeypatch.setattr(enrichment.skill_retriever, "retrieve", lambda *args, **kwargs: [])
    monkeypatch.setattr(enrichment.implant_retriever, "retrieve", lambda *args, **kwargs: [])
    bundle = await build_persona_bundle("engineer", "A", tier="deep")
    assert bundle.skills_loaded == ["skill-core"]
    assert bundle.implants_loaded == ["Focus"]


@pytest.mark.asyncio
@pytest.mark.parametrize("content", ["@common.mdc", "@../outside.mdc", "@missing.mdc"])
async def test_broken_import_cannot_be_activated(bundle_tree, content):
    tree, _ = bundle_tree
    (tree / "common.mdc").write_text(content, encoding="utf-8")
    with pytest.raises((FileNotFoundError, ValueError)):
        await build_persona_bundle("engineer", "A")


@pytest.mark.asyncio
@pytest.mark.parametrize("relative", ["skills/skill-core.mdc", "rules/rule-truth.mdc"])
async def test_symlinked_source_cannot_escape_repository(bundle_tree, relative, tmp_path_factory):
    tree, _ = bundle_tree
    outside = tmp_path_factory.mktemp("outside") / "source.mdc"
    outside.write_text((tree / relative).read_text(), encoding="utf-8")
    (tree / relative).unlink()
    (tree / relative).symlink_to(outside)
    with pytest.raises(ValueError, match="Security Error"):
        await build_persona_bundle("engineer", "A", tier="deep")


def test_legacy_prompt_loader_still_returns_missing_marker(bundle_tree):
    tree, _ = bundle_tree
    assert prompt_loader.load_file_content(str(tree / "missing.mdc")).startswith("[MISSING FILE:")
    assert prompt_loader.process_imports("@missing.mdc").startswith("[MISSING FILE:")


@pytest.mark.parametrize("directory", ["skills-extra", "implants-extra"])
def test_import_directory_prefix_is_not_separately_loaded_layer(bundle_tree, directory):
    tree, _ = bundle_tree
    target = tree / directory / "guidance.mdc"
    target.parent.mkdir()
    target.write_text("Inline this guidance", encoding="utf-8")
    assert prompt_loader.process_imports(f"@{directory}/guidance.mdc", strict=True) == "Inline this guidance"


@pytest.mark.asyncio
async def test_independent_results_do_not_mutate_previous_bundle(bundle_tree):
    tree, _ = bundle_tree
    first = await build_persona_bundle("engineer", "A", tier="deep")
    before = asdict(first)
    write_mdc(tree / "skills/skill-core.mdc", {"description": "Changed"}, "New body")
    second = await build_persona_bundle("engineer", "B", tier="deep")
    assert asdict(first) == before
    assert first.bundle_revision != second.bundle_revision


@pytest.mark.asyncio
async def test_greeting_does_not_strip_output_format_from_a_session_bundle(
    bundle_tree, monkeypatch,
):
    """A per-query suppression must never be baked into a session-scoped bundle.

    `persona.load_persona` returns NO_CHANGE while the same agent stays active,
    so a v2 bundle is built once and reused for every later turn. If the
    activating turn happened to be a greeting, applying
    `profile.suppress_persona_format` here would leave the persona's
    `## Output Format` removed for the rest of the conversation — for
    code_reviewer or medical_expert that is the whole response contract. The v1
    path re-derives per query because SESSION_CACHE is keyed on the query hash.
    """
    from src.engine.intent import classify_intent

    tree, agent = bundle_tree
    write_mdc(
        tree / "agents/engineer/system_prompt.mdc", agent,
        "Engineer persona\n\n## Output Format\n\nAnswer with Analysis, then Code.\n",
    )
    monkeypatch.setattr(enrichment, "INTENT_CLASSIFIER_ENABLED", True)

    # The mode really does request suppression — this is not a vacuous test.
    assert classify_intent("hi").suppress_persona_format is True

    bundle = await build_persona_bundle("engineer", "hi")
    assert "## Output Format" in bundle.persona_block
    assert "Answer with Analysis, then Code." in bundle.persona_block
