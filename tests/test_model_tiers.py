"""Tier policy: complexity mapping, floors, latest-model resolution, spawn hints, escalation."""
import pytest

from src.engine import model_catalogs, model_tiers
from src.schemas.subagent import ContractExecution, PreparationRequest

CODEX = ["gpt-5.6-luna", "gpt-6-luna", "gpt-6-sol", "gpt-6.1-sol", "gpt-6-astra"]
CURSOR = ["default", "composer-2.5", "claude-sonnet-5-5"]


@pytest.fixture(autouse=True)
def catalogs(monkeypatch):
    for name in list(__import__("os").environ):
        if name.startswith("AGENTS_SUBAGENT_"):
            monkeypatch.delenv(name)
    monkeypatch.setattr(model_catalogs, "codex_models", lambda path=None: list(CODEX))
    monkeypatch.setattr(model_catalogs, "cursor_models", lambda path=None: list(CURSOR))


def req(complexity="low", **extra):
    return PreparationRequest.model_validate({
        "agent_name": "engineer", "task": "Rename the helper in the named file",
        "acceptance_criteria": ["The tests pass"], "complexity": complexity, **extra})


@pytest.mark.parametrize("name,client", [
    ("claude-code", "claude"), ("Claude-Code", "claude"), ("codex", "codex"), ("codex-mcp-client", "codex"),
    ("cursor", "cursor"), ("cursor-vscode", "cursor"), ("claude-desktop", None), ("claude-ai", None),
    ("antigravity", None), ("", None), (None, None),
])
def test_client_from_app(name, client):
    assert model_tiers.client_from_app(name) == client


@pytest.mark.parametrize("client,tier,model,effort,source", [
    ("claude", "light", "haiku", None, "alias"),
    ("claude", "standard", "sonnet", None, "alias"),
    ("codex", "light", "gpt-6-luna", "medium", "catalog"),
    ("codex", "standard", "gpt-6.1-sol", "medium", "catalog"),
    ("cursor", "light", "default", None, "pinned"),
    ("cursor", "standard", "composer-2.5", None, "catalog"),
    ("codex", "inherit", None, None, "inherit"),
    (None, "light", None, None, "inherit"),
])
def test_resolve_defaults(client, tier, model, effort, source):
    assert model_tiers.resolve(client, tier) == model_tiers.Resolution(model, effort, source)


def test_resolve_follows_newer_catalog_entries(monkeypatch):
    monkeypatch.setattr(model_catalogs, "codex_models", lambda path=None: CODEX + ["gpt-7-luna", "gpt-7.2-sol"])
    monkeypatch.setattr(model_catalogs, "cursor_models", lambda path=None: CURSOR + ["composer-3"])
    assert model_tiers.resolve("codex", "light").model == "gpt-7-luna"
    assert model_tiers.resolve("codex", "standard").model == "gpt-7.2-sol"
    assert model_tiers.resolve("cursor", "standard").model == "composer-3"


def test_resolve_falls_back_to_pinned_models(monkeypatch):
    monkeypatch.setattr(model_catalogs, "codex_models", lambda path=None: [])
    monkeypatch.setattr(model_catalogs, "cursor_models", lambda path=None: ["default"])
    assert model_tiers.resolve("codex", "light") == model_tiers.Resolution("gpt-6-luna", "medium", "pinned")
    assert model_tiers.resolve("codex", "standard") == model_tiers.Resolution("gpt-6.1-sol", "medium", "pinned")
    assert model_tiers.resolve("cursor", "standard") == model_tiers.Resolution("composer-2.5", None, "pinned")


def test_overrides_replace_models_and_efforts(monkeypatch, caplog):
    monkeypatch.setenv("AGENTS_SUBAGENT_MODEL_CURSOR_LIGHT", "composer-2.5")
    monkeypatch.setenv("AGENTS_SUBAGENT_MODEL_CODEX_LIGHT", " gpt-6-sol ")
    monkeypatch.setenv("AGENTS_SUBAGENT_EFFORT_CODEX_LIGHT", "")
    monkeypatch.setenv("AGENTS_SUBAGENT_EFFORT_CODEX_STANDARD", "LOW")
    monkeypatch.setenv("AGENTS_SUBAGENT_MODEL_CLAUDE_LIGHT", "bad model; rm -rf")
    monkeypatch.setenv("AGENTS_SUBAGENT_EFFORT_CLAUDE_STANDARD", "high")
    assert model_tiers.resolve("cursor", "light") == model_tiers.Resolution("composer-2.5", None, "override")
    assert model_tiers.resolve("codex", "light") == model_tiers.Resolution("gpt-6-sol", None, "override")
    assert model_tiers.resolve("codex", "standard") == model_tiers.Resolution("gpt-6.1-sol", "low", "catalog")
    assert model_tiers.resolve("claude", "light") == model_tiers.Resolution("haiku", None, "alias")
    assert model_tiers.resolve("claude", "standard") == model_tiers.Resolution("sonnet", "high", "alias")
    assert "AGENTS_SUBAGENT_MODEL_CLAUDE_LIGHT" in caplog.text


def test_cursor_never_gets_an_effort(monkeypatch):
    monkeypatch.setenv("AGENTS_SUBAGENT_EFFORT_CURSOR_STANDARD", "high")
    assert model_tiers.resolve("cursor", "standard").effort is None


@pytest.mark.parametrize("complexity,tier", [("low", "light"), ("standard", "standard"), ("high", "inherit")])
def test_plan_maps_complexity_to_tier(complexity, tier):
    plan = model_tiers.plan(req(complexity), "codex")
    assert (plan.complexity, plan.requested_tier, plan.tier, plan.floor_reasons) == (complexity, tier, tier, [])


@pytest.mark.parametrize("field,text,reason", [
    ("task", "Fix the SQL injection vulnerability in search", "risk:security"),
    ("task", "Update the OAuth authorization callback", "risk:auth"),
    ("context", "Rotate the API key stored in settings", "risk:secrets"),
    ("scope", "Encrypt the exported archive", "risk:crypto"),
    ("task", "Write the schema migration for orders", "risk:data"),
    ("task", "Deploy the hotfix to production", "risk:production"),
    ("task", "Adjust the Terraform module for the queue", "risk:infrastructure"),
    ("task", "Remove the race condition in the cache", "risk:concurrency"),
    ("task", "Choose the architecture for the sync service", "risk:architecture"),
    ("task", "Проверь безопасность эндпоинта", "risk:security"),
    ("task", "Напиши миграцию для таблицы заказов", "risk:data"),
])
def test_risk_floors_raise_to_inherit(field, text, reason):
    value = [text] if field in ("context", "scope") else text
    plan = model_tiers.plan(req("low", **{field: value}), "codex")
    assert plan.tier == "inherit" and plan.requested_tier == "light"
    assert reason in plan.floor_reasons
    assert plan.model is None and plan.spawn.arguments == {"fork_turns": "none"}


@pytest.mark.parametrize("extra,reason", [
    ({"acceptance_criteria": [f"Criterion {i}" for i in range(9)]}, "size:acceptance_criteria"),
    ({"scope": [f"Area {i}" for i in range(9)]}, "size:scope"),
    ({"context": ["word " * 700] * 4}, "size:text"),
])
def test_size_floors_raise_to_standard(extra, reason):
    plan = model_tiers.plan(req("low", **extra), "claude")
    assert (plan.tier, plan.model, plan.floor_reasons) == ("standard", "sonnet", [reason])


def test_floors_never_lower_and_report_only_raises():
    plan = model_tiers.plan(req("standard", acceptance_criteria=[f"C{i}" for i in range(9)]), "claude")
    assert (plan.tier, plan.floor_reasons) == ("standard", [])
    plan = model_tiers.plan(req("high"), "claude")
    assert (plan.tier, plan.floor_reasons) == ("inherit", [])


def test_non_goals_never_raise_the_tier():
    plan = model_tiers.plan(req("low", non_goals=["Printing tokens, Authorization headers or secret values",
                                                 "Deploying to production"]), "claude")
    assert (plan.tier, plan.floor_reasons) == ("light", [])
    assert model_tiers.plan(req("low", task="Rotate the deploy secret"), "claude").tier == "inherit"


def test_ordinary_words_do_not_trigger_floors():
    text = "Rename the function signature, update the token count in the docs and the release notes table"
    assert model_tiers.plan(req("low", task=text), "claude").tier == "light"


def test_artifact_floor_counts_entries_and_bytes():
    assert model_tiers.artifact_floor(entries=6, size_bytes=10) == ["size:artifacts"]
    assert model_tiers.artifact_floor(entries=1, size_bytes=65537) == ["size:artifacts"]
    assert model_tiers.artifact_floor(entries=5, size_bytes=65536) == []


def test_unknown_client_keeps_the_host_model():
    plan = model_tiers.plan(req("low"), None)
    assert (plan.tier, plan.client, plan.model, plan.source, plan.spawn) == ("inherit", None, None, "inherit", None)
    assert plan.requested_tier == "light" and plan.floor_reasons == ["client:unknown"]
    assert model_tiers.plan(req("high"), None).floor_reasons == []


def test_disabled_tiering_inherits(monkeypatch):
    monkeypatch.setenv("AGENTS_SUBAGENT_TIERING", "0")
    plan = model_tiers.plan(req("low", task="Deploy to production"), "claude")
    assert (plan.tier, plan.model, plan.source) == ("inherit", None, "disabled")
    assert plan.requested_tier == "light" and plan.floor_reasons == ["risk:production"]
    assert plan.spawn.model_dump() == {"tool": "Agent", "arguments": {}}


@pytest.mark.parametrize("client,spawn", [
    ("claude", {"tool": "Agent", "arguments": {"model": "haiku"}}),
    ("codex", {"tool": "spawn_agent", "arguments": {"model": "gpt-6-luna", "reasoning_effort": "medium",
                                                    "fork_turns": "none"}}),
    ("cursor", {"tool": "Task", "arguments": {"model": "default"}}),
])
def test_spawn_arguments_per_client(client, spawn):
    assert model_tiers.plan(req("low"), client).spawn.model_dump() == spawn


def test_claude_effort_override_reaches_the_spawn(monkeypatch):
    monkeypatch.setenv("AGENTS_SUBAGENT_EFFORT_CLAUDE_LIGHT", "low")
    assert model_tiers.plan(req("low"), "claude").spawn.arguments == {"model": "haiku", "effort": "low"}


def test_contract_part_holds_only_contract_fields():
    plan = model_tiers.plan(req("low"), "codex")
    assert plan.contract_part() == {"complexity": "low", "tier": "light", "client": "codex",
                                    "model": "gpt-6-luna", "reasoning_effort": "medium"}


def execution(tier, complexity="low"):
    return ContractExecution(complexity=complexity, tier=tier, client="codex", model=None, reasoning_effort=None)


@pytest.mark.parametrize("valid,status,failed,expected", [
    (True, "completed", 0, None),
    (True, "input_required", 0, None),
    (True, "completed", 1, ("standard", "standard", "check_failed")),
    (True, "partial", 0, ("standard", "standard", "status_partial")),
    (True, "failed", 0, ("standard", "standard", "status_failed")),
    (False, None, 0, ("standard", "standard", "invalid_result")),
])
def test_escalation_from_light(valid, status, failed, expected):
    result = model_tiers.escalation(execution("light"), valid=valid, status=status, failed_checks=failed)
    assert (None if result is None else (result.complexity, result.tier, result.reason)) == expected


def test_escalation_climbs_and_stops_at_inherit():
    up = model_tiers.escalation(execution("standard", "standard"), valid=True, status="failed", failed_checks=0)
    assert (up.complexity, up.tier) == ("high", "inherit")
    assert model_tiers.escalation(execution("inherit", "high"), valid=False, status=None, failed_checks=3) is None
