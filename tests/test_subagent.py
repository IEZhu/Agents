"""Worker contracts: source policy, budget accounting, strict exchange validation."""
import copy
import json
from pathlib import Path

import pytest
import yaml

from src.engine import rules, subagent
from src.engine.subagent import prepare_subagent
from src.engine.subagent_validation import contract_fingerprint, validate_result
from src.schemas.subagent import MAX_RESULT_CHARS, PreparedPackage, RESULT_MODELS
from src.utils import prompt_loader


def write_mdc(path, metadata, body):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\n{yaml.safe_dump(metadata)}---\n{body}", encoding="utf-8")


@pytest.fixture
def worker_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(prompt_loader, "REPO_ROOT", str(tmp_path))
    for key, folder in (("AGENTS_DIR", "agents"), ("SKILLS_DIR", "skills"), ("IMPLANTS_DIR", "implants")):
        monkeypatch.setattr(prompt_loader, key, str(tmp_path / folder))
    monkeypatch.setattr(rules, "RULES_DIR", str(tmp_path / "rules"))
    monkeypatch.setattr(rules, "RULES_ENABLED", True)
    monkeypatch.setattr(subagent.component_toggles, "disabled", lambda kind: frozenset())
    skills = ["skill-api", "skill-test", "skill-clean", "skill-docs"]
    implants = ["implant-debug", "implant-plan"]
    agent = {"identity": {"name": "engineer", "role": "Software implementation"},
             "core_skills": skills[:1], "preferred_skills": skills[1:3],
             "capable_skills": skills[3:], "preferred_implants": implants}
    write_mdc(tmp_path / "agents/engineer/system_prompt.mdc", agent, "Full persona. Not a worker.")
    write_mdc(tmp_path / "agents/engineer/subagent.mdc",
              {"agent": "engineer", "skills": skills, "implants": implants}, "Compact engineering judgment.")
    for component_id, description in zip(skills, ["API endpoint contract", "Test validation regression", "Clean code", "Documentation"]):
        write_mdc(tmp_path / f"skills/{component_id}.mdc",
                  {"description": description, "compiled": component_id + " COMPILED", "keywords": []},
                  component_id + " FULL BODY")
    for component_id in implants:
        write_mdc(tmp_path / f"implants/{component_id}.mdc", {"description": component_id, "one_liner": "VAGUE"},
                  component_id + " OPERATIONAL FULL BODY")
    for name in ("no-fabrication", "answer-timestamp", "language-match", "agent-cooperation", "thread-status"):
        write_mdc(tmp_path / f"rules/rule-{name}.mdc", {"name": name, "description": name}, name + " instructions")
    for name in subagent.WORKER_RULE_IDS:
        write_mdc(tmp_path / f"rules/subagents/rule-{name}.mdc", {"name": name, "description": name}, name + " instructions")
    return tmp_path


def request(**extra):
    return {"agent_name": "engineer", "task": "Test validation regression", "task_id": "task-1",
            "acceptance_criteria": ["Show evidence"], **extra}


def prepared(**extra):
    result = prepare_subagent(request(**extra))
    assert isinstance(result, PreparedPackage), result
    return result


def report(package, status="completed"):
    payload_key = {"generic": "items", "review": "findings", "implementation": "changes", "research": "conclusions"}[package.contract.result_type]
    return {"protocol": package.protocol, "task_id": package.task_id, "attempt_id": package.attempt_id,
            "bundle_revision": package.descriptor.bundle_revision, "contract_revision": package.contract_revision,
            "status": status, "result": {"summary": "Observed result", payload_key: []}, "evidence": [],
            "artifacts": [], "checks": [], "limitations": ["Failure reason"] if status == "failed" else [],
            "remaining_work": ["Unfinished task"] if status == "partial" else [],
            "question": "Provide the missing input" if status == "input_required" else None}


def codes(response):
    return {error.code for error in response.errors}


def test_default_policy_is_ranked_compiled_and_stateless(worker_tree, monkeypatch):
    cache = ["unchanged"]
    monkeypatch.setattr(rules, "_cache", cache)
    package = prepared()
    assert package.descriptor.skills_loaded == ["skill-test", "skill-api", "skill-clean"]
    assert package.descriptor.implants_loaded == []
    assert "skill-test COMPILED" in package.worker_context
    assert "skill-test FULL BODY" not in package.worker_context
    assert "Full persona" not in package.worker_context
    assert "answer-timestamp instructions" not in package.worker_context
    assert "language-match instructions" not in package.worker_context
    assert "thread-status instructions" not in package.worker_context
    assert next(item for item in package.selection if item.id == "thread-status").reason == "presentation_rule"
    assert "agent-cooperation instructions" not in package.worker_context
    assert "worker-cooperation instructions" in package.worker_context
    assert next(item for item in package.selection if item.id == "agent-cooperation").reason == "worker_equivalent"
    assert package.context_chars == len(package.worker_context) <= 16000
    assert package.worker_context.count("Compact engineering judgment.") == 1
    assert rules._cache is cache
    next_package = prepared()
    assert package.bundle_id != next_package.bundle_id
    assert package.contract == next_package.contract
    assert package.worker_context == next_package.worker_context


def test_ranking_depends_on_assignment_and_ties_follow_profile_order(worker_tree):
    assert prepared(task="Documentation", max_skills=1).descriptor.skills_loaded == ["skill-docs"]
    assert prepared(task="Unrelated", acceptance_criteria=["Done"], max_skills=2).descriptor.skills_loaded == ["skill-api", "skill-test"]


def test_opt_in_implants_deliver_full_body(worker_tree):
    package = prepared(max_implants=2)
    assert package.descriptor.implants_loaded == ["implant-debug", "implant-plan"]
    assert "implant-debug OPERATIONAL FULL BODY" in package.worker_context
    assert "VAGUE" not in package.worker_context


def test_toggles_apply_but_worker_contract_rules_are_mandatory(worker_tree, monkeypatch):
    monkeypatch.setattr(subagent.component_toggles, "disabled", lambda kind: {
        "skills": {"skill-test"}, "implants": {"implant-debug"},
        "rules": {"no-fabrication", "worker-scope", "worker-cooperation", "agent-cooperation"}}[kind])
    package = prepared(max_implants=2)
    assert "skill-test" not in package.descriptor.skills_loaded
    assert "implant-debug" not in package.descriptor.implants_loaded
    assert "no-fabrication" not in package.descriptor.rules_loaded
    assert "worker-scope" in package.descriptor.rules_loaded
    assert "worker-cooperation" in package.descriptor.rules_loaded
    assert next(item for item in package.selection if item.id == "agent-cooperation").reason == "worker_equivalent"
    monkeypatch.setattr(rules, "RULES_ENABLED", False)
    assert set(prepared().descriptor.rules_loaded) == subagent.WORKER_RULE_IDS


def test_revisions_separate_contract_and_delivered_sources(worker_tree):
    first = prepared()
    other_task = prepared(task_id="task-2", attempt_id="2", task="Test validation regression again")
    assert first.descriptor.bundle_revision == other_task.descriptor.bundle_revision
    assert first.contract_revision != other_task.contract_revision
    assert contract_fingerprint(first.contract.model_dump()) == first.contract_revision
    path = worker_tree / "skills/skill-test.mdc"
    text = path.read_text()
    path.write_text(text.replace("FULL BODY", "UNDELIVERED SOURCE EDIT"))
    assert prepared().descriptor.bundle_revision == first.descriptor.bundle_revision
    path.write_text(text.replace("COMPILED", "CHANGED COMPILED"))
    assert prepared().descriptor.bundle_revision != first.descriptor.bundle_revision


@pytest.mark.parametrize("relative", ["agents/engineer/subagent.mdc", "rules/subagents/rule-worker-scope.mdc",
                                       "rules/subagents/rule-worker-cooperation.mdc",
                                       "rules/rule-no-fabrication.mdc", "skills/skill-test.mdc"])
def test_delivered_instruction_changes_revision(worker_tree, relative):
    first = prepared()
    path = worker_tree / relative
    if relative.startswith("skills"):
        path.write_text(path.read_text().replace("COMPILED", "UPDATED"))
    else:
        path.write_text(path.read_text() + "\nFresh directive.")
    assert first.descriptor.bundle_revision != prepared().descriptor.bundle_revision


def test_schema_changes_bundle_revision(worker_tree, monkeypatch):
    first = prepared()
    original = RESULT_MODELS["generic"].model_json_schema
    monkeypatch.setattr(RESULT_MODELS["generic"], "model_json_schema", lambda: {**original(), "description": "New schema"})
    assert prepared().descriptor.bundle_revision != first.descriptor.bundle_revision


@pytest.mark.parametrize("relative,code", [
    ("agents/engineer/system_prompt.mdc", "unknown_agent"),
    ("agents/engineer/subagent.mdc", "compact_profile_missing"),
    ("rules/subagents/rule-worker-input.mdc", "worker_rules_missing"),
    ("rules/subagents/rule-worker-cooperation.mdc", "worker_rules_missing"),
    ("skills/skill-test.mdc", "invalid_source"),
])
def test_missing_sources_never_return_partial_package(worker_tree, relative, code):
    (worker_tree / relative).unlink()
    response = prepare_subagent(request())
    assert response.status == "error" and code in codes(response)
    assert set(response.model_dump()) == {"protocol", "status", "errors"}


def test_missing_compiled_skill_is_an_error(worker_tree):
    path = worker_tree / "skills/skill-test.mdc"
    write_mdc(path, {"description": "Test validation regression"}, "Full text cannot replace compact text")
    assert "missing_component_text" in codes(prepare_subagent(request()))


@pytest.mark.parametrize("metadata,body", [
    ({"agent": "wrong", "skills": [], "implants": []}, "Body"),
    ({"agent": "engineer", "skills": ["../secret"], "implants": []}, "Body"),
    ({"agent": "engineer", "skills": ["skill-foreign"], "implants": []}, "Body"),
    ({"agent": "engineer", "skills": [], "implants": []}, "word " * 251),
    ({"agent": "engineer", "skills": "skill-api", "implants": []}, "Body"),
])
def test_invalid_profiles_fail(worker_tree, metadata, body):
    write_mdc(worker_tree / "agents/engineer/subagent.mdc", metadata, body)
    assert prepare_subagent(request()).status == "error"


def test_source_symlinks_cannot_escape_installation(worker_tree, tmp_path_factory):
    outside = tmp_path_factory.mktemp("outside") / "profile.mdc"
    path = worker_tree / "agents/engineer/subagent.mdc"
    outside.write_bytes(path.read_bytes())
    path.unlink()
    path.symlink_to(outside)
    assert "invalid_source" in codes(prepare_subagent(request()))


@pytest.mark.parametrize("extra", [
    {"agent_name": "../engineer"}, {"agent_name": "unknown"}, {"task": " "}, {"task": {}},
    {"acceptance_criteria": []}, {"acceptance_criteria": "done"}, {"acceptance_criteria": [1]},
    {"context": [False]}, {"context": "[]"}, {"scope": None}, {"result_type": "custom"},
    {"attempt_id": ""}, {"task_id": "x" * 129}, {"max_skills": True}, {"max_skills": 6},
    {"max_skills": "3"}, {"max_implants": -1}, {"max_context_chars": 2000.0},
    {"max_context_chars": 64001}, {"task": "x" * 4001}, {"context": ["a"] * 51},
    {"context": ["a" * 4000] * 13}, {"unexpected": "value"},
])
def test_invalid_requests(worker_tree, extra):
    response = prepare_subagent(request(**extra))
    assert response.status == "error" and 1 <= len(response.errors) <= 20
    assert not hasattr(response, "worker_context")


def test_budget_never_crops_mandatory_or_optional_instructions(worker_tree):
    assert "context_budget_exceeded" in codes(prepare_subagent(request(max_context_chars=2000)))
    baseline = prepared(max_skills=0)
    budget = baseline.context_chars + 35
    result = prepared(max_context_chars=budget)
    assert result.context_chars <= budget
    assert any(decision.reason == "context_budget" for decision in result.selection)
    for component_id in ("skill-api", "skill-test", "skill-clean"):
        assert (component_id + " COMPILED" in result.worker_context) == (component_id in result.descriptor.skills_loaded)
    assert "Compact engineering judgment." in result.worker_context
    assert all(name + " instructions" in result.worker_context for name in subagent.WORKER_RULE_IDS)


def test_cooperation_rule_cannot_be_dropped_to_fit_budget(worker_tree):
    path = worker_tree / "rules/subagents/rule-worker-cooperation.mdc"
    path.write_text(path.read_text() + "\n" + "Mandatory cooperation instruction. " * 500)
    response = prepare_subagent(request())
    assert response.status == "error"
    assert "context_budget_exceeded" in codes(response)
    assert not hasattr(response, "worker_context")


@pytest.mark.parametrize("result_type", list(RESULT_MODELS))
@pytest.mark.parametrize("status", ["completed", "partial", "input_required", "failed"])
def test_every_payload_and_status(worker_tree, result_type, status):
    package = prepared(result_type=result_type)
    value = report(package, status)
    assert validate_result(package.contract.model_dump(), value).valid
    assert validate_result(package.contract.model_dump(), json.dumps(value)).valid


@pytest.mark.parametrize("field,value", [("task_id", "another"), ("attempt_id", "2"),
                                         ("bundle_revision", "0" * 64), ("contract_revision", "0" * 64),
                                         ("protocol", "agents-core/subagent/2")])
def test_replayed_or_wrong_identity(worker_tree, field, value):
    package = prepared()
    result = report(package)
    result[field] = value
    assert not validate_result(package.contract.model_dump(), result).valid


def test_contract_mutation_and_schema_injection_rejected(worker_tree):
    package = prepared()
    contract = package.contract.model_dump()
    contract["task"] = "Another assignment"
    assert "contract_revision_mismatch" in codes(validate_result(contract, report(package)))
    contract = package.contract.model_dump()
    contract["result_schema"] = {"$ref": "https://example.invalid/schema"}
    assert not validate_result(contract, report(package)).valid


@pytest.mark.parametrize("mutation", [
    {"status": "completed", "remaining_work": ["unfinished"]},
    {"status": "completed", "question": "Missing input?"},
    {"status": "input_required", "question": None},
    {"status": "input_required", "question": " "},
    {"status": "partial", "remaining_work": []},
    {"status": "failed", "limitations": []},
    {"extra": "key"}, {"result": {"summary": "S", "items": [], "extra": "key"}},
    {"result": {"summary": "S", "findings": []}}, {"question": 1},
])
def test_invalid_status_and_closed_shapes(worker_tree, mutation):
    package = prepared()
    assert not validate_result(package.contract.model_dump(), {**report(package), **mutation}).valid


def test_evidence_and_artifacts_are_opaque_but_references_are_checked(worker_tree, monkeypatch):
    package = prepared(result_type="review")
    result = report(package)
    result["evidence"] = [{"id": "e1", "reference": "https://example.invalid/proof", "description": "Observed"}]
    result["artifacts"] = [{"path": "../../unopened.txt", "purpose": "Opaque reference"}]
    result["checks"] = [{"name": "Check", "status": "not_run", "evidence_ids": ["e1"]}]
    result["result"]["findings"] = [{"severity": "high", "location": "/not/opened.py:1", "explanation": "Issue",
                                       "recommendation": "Fix", "evidence_ids": ["e1"]}]
    monkeypatch.setattr(Path, "open", lambda *args, **kwargs: pytest.fail("Validation read an artifact"))
    assert validate_result(package.contract.model_dump(), result).valid
    result["checks"][0]["evidence_ids"] = ["missing"]
    assert "unknown_evidence_id" in codes(validate_result(package.contract.model_dump(), result))
    result["evidence"].append(copy.deepcopy(result["evidence"][0]))
    assert "duplicate_evidence_id" in codes(validate_result(package.contract.model_dump(), result))


@pytest.mark.parametrize("raw", ["```json\n{}\n```", "{} trailing", "{} {}", '{"a":1,"a":2}',
                                  '{"a":NaN}', '{"a":Infinity}', "[]", "null", " " ])
def test_strict_raw_json(worker_tree, raw):
    response = validate_result(prepared().contract.model_dump(), raw)
    assert not response.valid and response.verification == "structural_only"


@pytest.mark.parametrize("value", [
    {"x": "x" * (MAX_RESULT_CHARS + 1)}, {"x": [0] * 101}, {"x": float("inf")},
    {"x": {1: "not a string key"}}, {"x": (1, 2)}, {"x": "\ud800"},
])
def test_bounded_invalid_json_trees(worker_tree, value):
    assert not validate_result(prepared().contract.model_dump(), value).valid


def test_depth_and_diagnostics_are_bounded(worker_tree):
    value = {}
    for _ in range(20):
        value = {"nested": value}
    response = validate_result(prepared().contract.model_dump(), value)
    assert "structure_too_large" in codes(response)
    response = validate_result(prepared().contract.model_dump(), {f"unknown{i}": "x" for i in range(90)})
    assert len(response.errors) == 20
    assert all(len(item.message) <= 240 and len(item.path) <= 256 for item in response.errors)


def test_all_shipped_profiles_are_authored_and_references_exist(monkeypatch):
    monkeypatch.setattr(subagent.component_toggles, "disabled", lambda kind: frozenset())
    root = Path(__file__).resolve().parents[1]
    agents = sorted((root / "agents").glob("*/system_prompt.mdc"))
    assert agents
    for path in agents:
        _, profile, body = subagent.read_profile(path.parent.name)
        assert 0 < len(body.split()) <= 250
        for kind in ("skills", "implants"):
            for component_id in profile[kind]:
                source = subagent._fresh_component(kind, component_id)
                text = source["metadata"].get("compiled") if kind == "skills" else source["content"]
                assert isinstance(text, str) and text.strip(), (path.parent.name, component_id)


@pytest.mark.parametrize("result_type,item", [
    ("generic", "A bounded observation"),
    ("implementation", {"path": "src/example.py", "description": "Changed validation"}),
    ("research", {"claim": "Supported claim", "evidence_ids": ["e1"]}),
])
def test_nonempty_payload_items_and_nested_rejections(worker_tree, result_type, item):
    package = prepared(result_type=result_type)
    result = report(package)
    field = {"generic": "items", "implementation": "changes", "research": "conclusions"}[result_type]
    result["result"][field] = [item]
    result["evidence"] = [{"id": "e1", "reference": "source:line-1", "description": "Observed evidence"}]
    assert validate_result(package.contract.model_dump(), result).valid
    result["result"][field] = [False] if result_type == "generic" else [{**item, "extra": "forbidden"}]
    assert not validate_result(package.contract.model_dump(), result).valid
    if result_type == "research":
        result["result"][field] = [{"claim": "Claim", "evidence_ids": ["missing"]}]
        assert "unknown_evidence_id" in codes(validate_result(package.contract.model_dump(), result))


def test_result_string_and_array_limits(worker_tree):
    package = prepared()
    result = report(package)
    result["result"]["summary"] = "x" * 4001
    assert not validate_result(package.contract.model_dump(), result).valid
    result["result"]["summary"] = "Summary"
    result["result"]["items"] = ["Observation"] * 51
    assert not validate_result(package.contract.model_dump(), result).valid
    result = report(package)
    result["checks"] = [{"name": "check", "status": "suggested", "evidence_ids": []}]
    assert not validate_result(package.contract.model_dump(), result).valid


@pytest.mark.parametrize("field", ["max_skills", "max_implants", "max_context_chars"])
def test_retained_contract_budgets_cannot_be_filled_from_defaults(worker_tree, field):
    package = prepared()
    contract = package.contract.model_dump()
    del contract["budgets"][field]
    contract["contract_revision"] = contract_fingerprint(contract)
    response = validate_result(contract, report(package))
    assert not response.valid
    assert any(error.path == f"$.contract.budgets.{field}" and error.code == "missing" for error in response.errors)
