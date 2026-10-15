"""Real worker instructions must fit when cooperation and artifacts coexist."""
from pathlib import Path

import pytest

from src.engine import rules, subagent
from src.engine.subagent_validation import contract_fingerprint, validate_result
from src.schemas.subagent import PreparedPackage, RESULT_MODELS
from src.utils import prompt_loader
from tests.test_subagent import report
from tests.test_subagent_artifacts import manifest


ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def real_worker_sources(monkeypatch):
    """Read this checkout's complete authored sources, independent of local settings."""
    monkeypatch.setattr(prompt_loader, "REPO_ROOT", str(ROOT))
    for key, folder in (("AGENTS_DIR", "agents"), ("SKILLS_DIR", "skills"),
                        ("IMPLANTS_DIR", "implants")):
        monkeypatch.setattr(prompt_loader, key, str(ROOT / folder))
    monkeypatch.setattr(rules, "RULES_DIR", str(ROOT / "rules"))
    monkeypatch.setattr(rules, "RULES_ENABLED", True)
    monkeypatch.setattr(subagent.component_toggles, "disabled", lambda kind: frozenset())


@pytest.mark.parametrize("result_type", list(RESULT_MODELS))
@pytest.mark.parametrize("max_skills", [0, 3])
def test_real_cooperation_and_artifact_context_fits_default_budget(
        real_worker_sources, result_type, max_skills):
    pinned = manifest()
    package = subagent.prepare_subagent({
        "agent_name": "software_engineer", "task": "Review the assigned change.",
        "acceptance_criteria": ["Report evidence."], "task_id": "task-1",
        "result_type": result_type, "max_skills": max_skills,
        "artifact_manifest": pinned,
    })
    assert isinstance(package, PreparedPackage), package
    assert package.context_chars == len(package.worker_context) <= 16000
    assert package.contract.budgets.max_context_chars == 16000
    assert package.contract.artifact_manifest.model_dump() == pinned
    assert contract_fingerprint(package.contract.model_dump()) == package.contract_revision
    assert package.result_schema == RESULT_MODELS[result_type].model_json_schema()
    assert validate_result(package.contract.model_dump(), report(package)).valid

    # Neither mandatory rule may be omitted or shortened during preparation.
    mandatory = subagent.WORKER_RULE_IDS | {subagent.ARTIFACT_WORKER_RULE_ID}
    assert mandatory <= set(package.descriptor.rules_loaded)
    for name in mandatory:
        decision = next(item for item in package.selection if item.id == name)
        assert decision.selected and decision.reason == "mandatory_worker_rule"
        _, body = prompt_loader.read_mdc(str(ROOT / f"rules/subagents/rule-{name}.mdc"))
        assert f"## Rule: {name}\n{body}" in package.worker_context
    expected_call = (
        f'artifact_read(manifest_id="{pinned["manifest_id"]}", '
        f'manifest_sha256="{pinned["sha256"]}", '
        f'artifact_id="{pinned["entries"][0]["artifact"]["artifact_id"]}", '
        'offset=0, max_bytes=16384)'
    )
    assert expected_call in package.worker_context
