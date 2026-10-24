"""Tiered preparation and escalating validation through the worker contract."""
import copy

import pytest

from src.engine import model_catalogs
from src.engine.subagent import prepare_subagent
from src.engine.subagent_validation import assess_result, contract_fingerprint, validate_result
from src.schemas.subagent import PreparedPackage
from tests.test_subagent import prepared, report, request, worker_tree  # noqa: F401


@pytest.fixture(autouse=True)
def catalogs(monkeypatch):
    monkeypatch.delenv("AGENTS_SUBAGENT_TIERING", raising=False)
    monkeypatch.setattr(model_catalogs, "codex_models", lambda path=None: ["gpt-6-luna", "gpt-6.1-sol"])
    monkeypatch.setattr(model_catalogs, "cursor_models", lambda path=None: ["default", "composer-2.5"])


def test_without_complexity_nothing_changes(worker_tree):
    package = prepared()
    assert package.execution is None and package.contract.execution is None
    assert "execution" not in package.model_dump() and "execution" not in package.contract.model_dump()
    response = validate_result(package.contract.model_dump(), report(package, "failed"))
    assert response.valid and response.escalation is None
    assert "escalation" not in response.model_dump()
    assert assess_result(package.contract.model_dump(), report(package))[1] is None


def test_complexity_puts_the_execution_in_contract_and_package(worker_tree):
    package = prepared(complexity="low", client="codex")
    contract = package.contract.model_dump()
    assert contract["execution"] == {"complexity": "low", "tier": "light", "client": "codex",
                                     "model": "gpt-6-luna", "reasoning_effort": "medium"}
    assert contract_fingerprint(contract) == package.contract_revision
    assert package.execution.spawn.model_dump() == {
        "tool": "spawn_agent", "arguments": {"model": "gpt-6-luna", "reasoning_effort": "medium", "fork_turns": "none"}}
    assert package.execution.source == "catalog" and package.execution.requested_tier == "light"
    assert "tier: light (low complexity)" in package.worker_context
    assert PreparedPackage.model_validate(package.model_dump()) == package


def test_tier_changes_the_contract_revision(worker_tree):
    light = prepared(complexity="low", client="claude")
    standard = prepared(complexity="standard", client="claude")
    plain = prepared()
    assert len({light.contract_revision, standard.contract_revision, plain.contract_revision}) == 3


def test_invalid_complexity_or_client_is_refused(worker_tree):
    for extra in ({"complexity": "trivial"}, {"client": "vscode"}, {"complexity": 1}):
        result = prepare_subagent(request(**extra))
        assert result.status == "error"


def test_floors_reach_the_contract(worker_tree):
    package = prepared(complexity="low", client="claude", task="Fix the authentication bypass")
    assert package.contract.execution.tier == "inherit" and package.contract.execution.model is None
    assert "risk:auth" in package.execution.floor_reasons


@pytest.mark.parametrize("status,checks,reason", [
    ("failed", [], "status_failed"),
    ("partial", [], "status_partial"),
    ("completed", ["failed"], "check_failed"),
])
def test_validation_escalates_shortfalls(worker_tree, status, checks, reason):
    package = prepared(complexity="low", client="cursor")
    value = report(package, status)
    value["evidence"] = [{"id": "e1", "reference": "pytest", "description": "ran"}]
    value["checks"] = [{"name": "suite", "status": state, "evidence_ids": ["e1"]} for state in checks]
    response, outcome = assess_result(package.contract.model_dump(), value)
    assert response.valid
    assert response.escalation.model_dump() == {"complexity": "standard", "tier": "standard", "reason": reason}
    assert (outcome.valid, outcome.status, outcome.escalation) == (True, status, response.escalation)
    assert outcome.failed_checks == len(checks)


def test_validation_escalates_invalid_results_and_accepts_good_ones(worker_tree):
    package = prepared(complexity="standard", client="claude")
    contract = package.contract.model_dump()
    broken = {**report(package), "task_id": "other"}
    response, outcome = assess_result(contract, broken)
    assert not response.valid
    assert response.escalation.model_dump() == {"complexity": "high", "tier": "inherit", "reason": "invalid_result"}
    assert outcome.status == "completed"
    response, outcome = assess_result(contract, "not json")
    assert not response.valid and response.escalation.reason == "invalid_result" and outcome.status is None
    for status in ("completed", "input_required"):
        response, outcome = assess_result(contract, report(package, status))
        assert response.valid and response.escalation is None and outcome.escalation is None


def test_inherit_never_escalates(worker_tree):
    package = prepared(complexity="high", client="codex")
    response = validate_result(package.contract.model_dump(), report(package, "failed"))
    assert response.valid and response.escalation is None


def test_tampered_execution_is_rejected_without_escalation(worker_tree):
    package = prepared(complexity="low", client="codex")
    contract = copy.deepcopy(package.contract.model_dump())
    contract["execution"]["tier"] = "inherit"
    response, outcome = assess_result(contract, report(package))
    assert not response.valid and response.escalation is None and outcome is None
    assert response.errors[0].code == "contract_revision_mismatch"
