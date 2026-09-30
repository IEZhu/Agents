"""Persona protocol MCP contract and stateless persona activation regressions."""

import asyncio
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import src.server as server
from src.engine import config as engine_config
from src.engine import persona
from src.schemas.protocol import PersonaDescriptor, RouterDecision


def descriptor(agent="software_engineer", activation="old", revision="a" * 64):
    return PersonaDescriptor(
        agent=agent, activation_id=activation, bundle_revision=revision,
        scope="Implementation and debugging", skills_loaded=["skill-dev-clean-code"],
        implants_loaded=["IterBudget"], rules_loaded=["language-match"],
    )


@pytest.fixture
def bundle(monkeypatch):
    async def build(agent, query, history, tier=None):
        return SimpleNamespace(
            agent=agent, bundle_revision="b" * 64, scope=f"Scope of {agent}",
            persona_block=f"Role {agent}", rules_block="General rules",
            skills_block="Role skills", implants_block="Role implants",
            skills_loaded=["skill-dev-clean-code"], implants_loaded=["IterBudget"],
            rules_loaded=["language-match"], tier="standard",
        )
    builder = AsyncMock(side_effect=build)
    monkeypatch.setattr(persona, "build_persona_bundle", builder)
    monkeypatch.setattr(server.router, "update_cache", AsyncMock())
    return builder


@pytest.mark.asyncio
async def test_same_agent_keep_does_not_load_or_learn(bundle):
    active = descriptor()
    response = json.loads(await server.get_agent_context(
        active.agent, "Completely different words within this role", protocol_version=2,
        current_persona=active,
    ))
    assert response["status"] == "NO_CHANGE"
    assert response["persona"] == active.model_dump()
    assert "persona_block" not in response
    assert response["footer"] == persona.persona_footer(active)
    bundle.assert_not_awaited()
    server.router.update_cache.assert_not_awaited()


@pytest.mark.asyncio
async def test_success_complete_replaces_and_never_samples(bundle, monkeypatch):
    sample = AsyncMock()
    monkeypatch.setattr(server, "_sample_with_agent", sample)
    response = json.loads(await server.get_agent_context(
        "lawyer", "Read this fictional contract", protocol_version=2,
        current_persona=descriptor(),
    ))
    assert response["status"] == "SUCCESS"
    assert response["replaces_activation_id"] == "old"
    assert response["persona"]["agent"] == "lawyer"
    assert response["persona"]["activation_id"] != "old"
    assert all(response[f"{block}_block"] for block in ("persona", "rules", "skills", "implants"))
    assert "system_prompt" not in response
    assert "lawyer" in response["footer"]
    sample.assert_not_awaited()


@pytest.mark.asyncio
async def test_existing_agent_with_numeric_prefix_can_activate(bundle):
    response = json.loads(await server.get_agent_context(
        "3d_print_finder", "Find a printable replacement part", protocol_version=2,
    ))
    assert response["status"] == "SUCCESS"
    assert response["persona"]["agent"] == "3d_print_finder"


@pytest.mark.asyncio
async def test_restore_forces_complete_bundle_even_same_revision(bundle):
    active = descriptor(revision="b" * 64)
    response = json.loads(await server.get_agent_context(
        active.agent, "Restore instructions", protocol_version=2, current_persona=active,
        force_reload=True,
    ))
    assert response["status"] == "SUCCESS"
    assert response["persona"]["bundle_revision"] == active.bundle_revision
    assert response["persona"]["activation_id"] != active.activation_id
    server.router.update_cache.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("revision,status", [("a" * 64, "SUCCESS"), ("b" * 64, "NO_CHANGE")])
async def test_refresh_compares_actual_revision_without_routing(bundle, monkeypatch, revision, status):
    lookup = AsyncMock(side_effect=AssertionError("refresh must not route"))
    monkeypatch.setattr(server.router, "lookup_cache", lookup)
    response = json.loads(await server.refresh_persona_context("Need API skills", descriptor(revision=revision)))
    assert response["status"] == status
    assert response["persona"]["agent"] == "software_engineer"
    assert ("persona_block" in response) == (status == "SUCCESS")
    bundle.assert_awaited_once()
    lookup.assert_not_awaited()
    server.router.update_cache.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_bundle_does_not_activate_or_learn(bundle):
    bundle.side_effect = FileNotFoundError("Missing required rule")
    active = descriptor()
    before = active.model_dump()
    response = json.loads(await server.get_agent_context(
        "lawyer", "Switch", protocol_version=2, current_persona=active,
    ))
    assert response["status"] == "ERROR"
    assert "persona" not in response and "persona_block" not in response
    assert active.model_dump() == before
    server.router.update_cache.assert_not_awaited()


@pytest.mark.asyncio
async def test_parallel_dialogues_and_expired_session_cache_are_independent(bundle, monkeypatch):
    from cachetools import TTLCache
    monkeypatch.setattr(server, "SESSION_CACHE", TTLCache(1, 0))
    alice, bob = descriptor(activation="alice"), descriptor("lawyer", "bob")
    responses = await asyncio.gather(
        server.get_agent_context("literary_writer", "Switch", protocol_version=2, current_persona=alice),
        server.get_agent_context("lawyer", "Continue", protocol_version=2, current_persona=bob),
    )
    switched, kept = map(json.loads, responses)
    assert switched["replaces_activation_id"] == "alice"
    assert kept["persona"] == bob.model_dump()
    assert len(server.SESSION_CACHE) == 0
    bundle.assert_awaited_once()


@pytest.mark.asyncio
async def test_route_checks_keyword_veto_and_passes_history(bundle, monkeypatch):
    cached = RouterDecision(target_agent="software_engineer", confidence=1, reasoning="cache")
    monkeypatch.setattr(server.router, "lookup_cache", AsyncMock(return_value=cached))
    monkeypatch.setattr(server.router, "keyword_veto", Mock(return_value="lawyer"))
    response = json.loads(await server.route_and_load(
        "Налоги?", protocol_version=2, current_persona=descriptor(),
        chat_history="Fictional contract",
    ))
    assert response["persona"]["agent"] == "lawyer"
    server.router.lookup_cache.assert_awaited_once_with("Налоги?", {"history_text": "Fictional contract"})


@pytest.mark.asyncio
async def test_v2_route_uncertain_returns_candidates_preserving_activation(bundle, monkeypatch):
    monkeypatch.setattr(server.router, "lookup_cache", AsyncMock(return_value=None))
    response = json.loads(await server.route_and_load("SQL?", protocol_version=2, current_persona=descriptor()))
    assert response["status"] == "ROUTE_REQUIRED"
    assert response["candidates"]
    assert response["replaces_activation_id"] == "old"
    bundle.assert_not_awaited()


@pytest.mark.asyncio
async def test_malformed_descriptor_and_unknown_version_fail_without_load(bundle):
    malformed = descriptor().model_dump()
    malformed["bundle_revision"] = "not-a-sha256"
    for kwargs in [{"protocol_version": 2, "current_persona": malformed}, {"protocol_version": 3}]:
        response = json.loads(await server.get_agent_context("lawyer", "Switch", **kwargs))
        assert response["status"] == "ERROR"
    bundle.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("version", [1, 3])
async def test_removed_protocol_versions_return_error_without_routing_or_loading(bundle, monkeypatch, version):
    lookup = AsyncMock(side_effect=AssertionError("unsupported version must not route"))
    enrich = AsyncMock(side_effect=AssertionError("unsupported version must not enrich"))
    route, load = AsyncMock(), AsyncMock()
    monkeypatch.setattr(server.router, "lookup_cache", lookup)
    monkeypatch.setattr(server, "_load_and_enrich", enrich)
    monkeypatch.setattr(server, "route_persona", route)
    monkeypatch.setattr(server, "load_persona", load)
    responses = [
        json.loads(await server.route_and_load("Налоги?", protocol_version=version)),
        json.loads(await server.get_agent_context("lawyer", "Switch", protocol_version=version)),
    ]
    for response in responses:
        assert response["status"] == "ERROR"
        assert response["protocol_version"] == 2
        assert "protocol 1 was removed" in response["message"]
        assert "protocol_version=2" in response["message"]
        assert "persona" not in response and "system_prompt" not in response
    for dependency in (lookup, enrich, route, load, bundle, server.router.update_cache):
        dependency.assert_not_awaited()


@pytest.mark.asyncio
async def test_logging_checks_attribution_before_writing(monkeypatch):
    writer = Mock()
    monkeypatch.setattr(server, "HistoryWriter", writer)
    response = json.loads(await server.log_interaction("lawyer", "q", "r", persona=descriptor(), persona_action="keep"))
    assert response["status"] == "ERROR"
    writer.assert_not_called()


@pytest.mark.asyncio
async def test_logging_records_client_reported_activation(monkeypatch):
    writer = Mock()
    writer.return_value.append_entry.return_value = {"status": "written"}
    monkeypatch.setattr(server, "HistoryWriter", writer)
    monkeypatch.setattr(server, "is_langfuse_configured", lambda: False)
    active = descriptor()
    response = json.loads(await server.log_interaction(active.agent, "q", "r", persona=active, persona_action="keep"))
    assert response["persona"] == active.model_dump()
    assert response["persona_action"] == "keep"
    assert response["attribution"] == "client-reported"
    written_action = writer.return_value.append_entry.call_args.args[1]
    assert active.activation_id in written_action and active.bundle_revision in written_action


@pytest.mark.asyncio
async def test_logging_refuses_history_in_windows_directory(tmp_path, monkeypatch):
    """Regression: a stdio server started in C:\\Windows\\System32 wrote history.md there."""
    windows = tmp_path / "Windows"
    (windows / "System32").mkdir(parents=True)
    # The marker keeps the walk-up inside tmp_path; its directory is still refused.
    (windows / "CLAUDE.md").write_text("")
    monkeypatch.setattr(engine_config, "_windows_directory", lambda: windows.resolve())
    monkeypatch.delenv("AGENTS_CLIENT_REPO_ROOT", raising=False)
    monkeypatch.chdir(windows / "System32")
    engine_config._reset_client_repo_root_cache()
    try:
        response = json.loads(await server.log_interaction("software_engineer", "q", "r"))
    finally:
        engine_config._reset_client_repo_root_cache()
    assert response["status"] == "ERROR"
    assert response["message"].startswith("workspace_required: refusing")
    assert sorted(path.name for path in windows.rglob("*")) == ["CLAUDE.md", "System32"]


@pytest.mark.asyncio
async def test_slash_alias_uses_v2_direct_load_with_retrieval_hint(bundle, monkeypatch):
    lookup = AsyncMock(side_effect=AssertionError("explicit role must not route"))
    monkeypatch.setattr(server.router, "lookup_cache", lookup)
    result = await server.mcp.get_prompt("co_lawyer", {
        "query": "fictional contract",
        "current_persona": descriptor().model_dump_json(),
    })
    assert '"protocol_version": 2' in result.messages[0].content.text
    assert '"replaces_activation_id": "old"' in result.messages[0].content.text
    assert bundle.call_args.args[0] == "lawyer"
    assert bundle.call_args.args[1] == "/co_lawyer fictional contract"
    lookup.assert_not_awaited()


@pytest.mark.asyncio
async def test_ask_is_explicit_routing_and_passes_descriptor(bundle, monkeypatch):
    monkeypatch.setattr(server.router, "lookup_cache", AsyncMock(return_value=None))
    result = await server.mcp.get_prompt("ask", {
        "query": "SQL?",
        "current_persona": descriptor().model_dump_json(),
    })
    assert '"status": "ROUTE_REQUIRED"' in result.messages[0].content.text
    assert '"replaces_activation_id": "old"' in result.messages[0].content.text


@pytest.mark.asyncio
async def test_tool_schema_exposes_v2_descriptor_and_optional_defaults():
    tool_list = await server.mcp.list_tools()
    schemas = {tool.name: tool.inputSchema for tool in tool_list}
    for name in ("route_and_load", "get_agent_context"):
        properties = schemas[name]["properties"]
        assert properties["protocol_version"]["default"] == 2
        assert "context_hash" not in properties and "ctx" not in properties
    assert "current_persona" in schemas["get_agent_context"]["properties"]
    assert "force_reload" in schemas["get_agent_context"]["properties"]
    assert schemas["refresh_persona_context"]["required"] == ["query", "current_persona"]


@pytest.mark.asyncio
@pytest.mark.parametrize("command,retrieval_query", [
    ("lawyer", "fictional contract"),
    ("co_lawyer", "/co_lawyer fictional contract"),
])
async def test_agent_prompts_return_persona_bundles_by_default(bundle, command, retrieval_query, monkeypatch):
    enrich = AsyncMock(side_effect=AssertionError("prompts must not use per-query enrichment"))
    route = AsyncMock(side_effect=AssertionError("explicit role must not route"))
    monkeypatch.setattr(server, "_load_and_enrich", enrich)
    monkeypatch.setattr(server.router, "lookup_cache", route)

    result = await server.mcp.get_prompt(command, {"query": "fictional contract"})

    text = result.messages[0].content.text
    assert text.startswith("Requested persona:\n")
    assert '"status": "SUCCESS"' in text
    assert '"replaces_activation_id": null' in text
    assert "SYSTEM INSTRUCTIONS" not in text
    assert bundle.call_args.args[:2] == ("lawyer", retrieval_query)
    enrich.assert_not_awaited()
    route.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("query,cached_agent,selected_agent", [
    ("Explain a Python dictionary", "software_engineer", "software_engineer"),
    ("hi", None, "universal_agent"),
])
async def test_ask_routes_cached_or_meta_query_to_bundle(bundle, query, cached_agent, selected_agent, monkeypatch):
    cached = RouterDecision(target_agent=cached_agent, confidence=1, reasoning="cache") if cached_agent else None
    lookup = AsyncMock(return_value=cached)
    enrich = AsyncMock(side_effect=AssertionError("prompts must not use per-query enrichment"))
    monkeypatch.setattr(server.router, "lookup_cache", lookup)
    monkeypatch.setattr(server.router, "keyword_veto", Mock(return_value=None))
    monkeypatch.setattr(server, "_load_and_enrich", enrich)

    result = await server.mcp.get_prompt("ask", {"query": query})

    lookup.assert_awaited_once_with(query, {"history_text": ""})
    enrich.assert_not_awaited()
    assert bundle.call_args.args[0] == selected_agent
    text = result.messages[0].content.text
    assert text.startswith("Requested persona selection:\n")
    assert '"status": "SUCCESS"' in text
    assert text.count(f"User query: {query}") == 1


@pytest.mark.asyncio
async def test_ask_cache_miss_returns_route_required_candidates(bundle, monkeypatch):
    monkeypatch.setattr(server.router, "lookup_cache", AsyncMock(return_value=None))
    monkeypatch.setattr(server.router, "get_agent_catalog", Mock(return_value=[
        {"name": "software_engineer", "role": "Software implementation"},
    ]))
    enrich = AsyncMock()
    monkeypatch.setattr(server, "_load_and_enrich", enrich)

    result = await server.mcp.get_prompt("ask", {"query": "Explain a Python dictionary"})

    text = result.messages[0].content.text
    assert '"status": "ROUTE_REQUIRED"' in text
    assert '"software_engineer"' in text
    assert "protocol_version=2" in text
    enrich.assert_not_awaited()
    bundle.assert_not_awaited()


@pytest.mark.asyncio
async def test_legacy_client_call_with_context_hash_reaches_protocol_2_routing(monkeypatch):
    """Protocol 1 instructions omit protocol_version and send context_hash; the MCP
    boundary must ignore the unknown argument and route with protocol 2."""
    route = AsyncMock(return_value=json.dumps({"protocol_version": 2, "status": "ROUTE_REQUIRED"}))
    monkeypatch.setattr(server, "route_persona", route)

    _, result = await server.mcp.call_tool("route_and_load", {"query": "SQL?", "context_hash": "0123abcd"})

    assert json.loads(result["result"])["protocol_version"] == 2
    route.assert_awaited_once()
    assert route.await_args.args[1] == "SQL?"


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["ask", "lawyer", "co_lawyer"])
async def test_prompt_protocol_version_is_optional(command):
    prompts = {prompt.name: prompt for prompt in await server.mcp.list_prompts()}
    prompt = prompts[command]
    arguments = {argument.name: argument for argument in prompt.arguments}
    assert set(arguments) == {"query", "current_persona", "protocol_version"}
    assert arguments["query"].required
    assert not arguments["current_persona"].required
    assert not arguments["protocol_version"].required
    assert "Protocol 1" not in prompt.description


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["ask", "co_lawyer"])
async def test_prompt_accepts_explicit_protocol_2(command, monkeypatch):
    """Installed protocol 2 instructions tell clients to pass protocol_version=2."""
    result = json.dumps({"status": "NO_CHANGE"})
    monkeypatch.setattr(server, "load_persona", AsyncMock(return_value=result))
    monkeypatch.setattr(server, "route_persona", AsyncMock(return_value=result))

    prompt = await server.mcp.get_prompt(command, {"query": "fictional contract", "protocol_version": "2"})

    assert "NO_CHANGE" in prompt.messages[0].content.text


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["ask", "co_lawyer"])
@pytest.mark.parametrize("version", ["1", "3"])
async def test_prompt_rejects_other_protocol_versions_before_loading(command, version, monkeypatch):
    legacy, v2_load, v2_route, lookup = (AsyncMock() for _ in range(4))
    monkeypatch.setattr(server, "_load_and_enrich", legacy)
    monkeypatch.setattr(server, "load_persona", v2_load)
    monkeypatch.setattr(server, "route_persona", v2_route)
    monkeypatch.setattr(server.router, "lookup_cache", lookup)

    prompt = await server.mcp.get_prompt(command, {"query": "fictional contract", "protocol_version": version})

    assert "protocol_version=2 only" in prompt.messages[0].content.text
    for dependency in (legacy, v2_load, v2_route, lookup):
        dependency.assert_not_awaited()


@pytest.mark.asyncio
async def test_initialization_guidance_matches_success_payload(bundle):
    current = json.loads(await server.get_agent_context(
        "lawyer", "fictional contract", protocol_version=2,
    ))
    instructions = server.mcp._mcp_server.create_initialization_options().instructions
    section = instructions.split("Response statuses:\n", 1)[1].split("\n\n", 1)[0]

    # Check the field references delivered during MCP initialization against the
    # actual wire payload.
    success = next(line for line in section.splitlines() if line.startswith("- SUCCESS →"))
    fields = set(re.findall(r"`(\w+)`", success))
    assert fields <= current.keys()
    assert fields == {
        "persona", "persona_block", "rules_block", "skills_block", "implants_block",
        "replaces_activation_id", "footer",
    }
    for removed in ("Version 1", "SUCCESS_SAMPLED", "system_prompt", "context_hash"):
        assert removed not in instructions
