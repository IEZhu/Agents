"""Versioned MCP contract and stateless persona activation regressions."""

import asyncio
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import src.server as server
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
        current_persona=descriptor(), ctx=Mock(),
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
async def test_parallel_dialogues_and_expired_legacy_cache_are_independent(bundle, monkeypatch):
    from cachetools import TTLCache
    monkeypatch.setattr(server, "CONTEXT_HASH_CACHE", TTLCache(1, 0))
    monkeypatch.setattr(server, "SESSION_CACHE", TTLCache(1, 0))
    alice, bob = descriptor(activation="alice"), descriptor("lawyer", "bob")
    responses = await asyncio.gather(
        server.get_agent_context("literary_writer", "Switch", protocol_version=2, current_persona=alice),
        server.get_agent_context("lawyer", "Continue", protocol_version=2, current_persona=bob),
    )
    switched, kept = map(json.loads, responses)
    assert switched["replaces_activation_id"] == "alice"
    assert kept["persona"] == bob.model_dump()
    assert len(server.SESSION_CACHE) == len(server.CONTEXT_HASH_CACHE) == 0
    bundle.assert_awaited_once()


@pytest.mark.asyncio
async def test_v2_route_ignores_sticky_and_checks_keyword_veto(bundle, monkeypatch):
    cached = RouterDecision(target_agent="software_engineer", confidence=1, reasoning="cache")
    monkeypatch.setattr(server.router, "lookup_cache", AsyncMock(return_value=cached))
    monkeypatch.setattr(server.router, "keyword_veto", Mock(return_value="lawyer"))
    response = json.loads(await server.route_and_load(
        "Налоги?", protocol_version=2, current_persona=descriptor(),
        context_hash="irrelevant", chat_history="Fictional contract",
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
@pytest.mark.parametrize("supported", [False, True])
async def test_v1_sampling_requires_advertised_capability(monkeypatch, supported):
    monkeypatch.setattr(server, "_load_and_enrich", AsyncMock(return_value=("prompt", "hash", [], [], [], "lite")))
    monkeypatch.setattr(server.router, "update_cache", AsyncMock())
    sample = AsyncMock(return_value="sampled")
    monkeypatch.setattr(server, "_sample_with_agent", sample)
    context = Mock()
    context.session.check_client_capability.return_value = supported
    response = json.loads(await server.get_agent_context("universal_agent", "hi", ctx=context))
    assert response["status"] == ("SUCCESS_SAMPLED" if supported else "SUCCESS")
    assert sample.await_count == int(supported)
    assert "protocol_version" not in response


@pytest.mark.asyncio
async def test_slash_alias_uses_v2_direct_load_with_retrieval_hint(bundle, monkeypatch):
    lookup = AsyncMock(side_effect=AssertionError("explicit role must not route"))
    monkeypatch.setattr(server.router, "lookup_cache", lookup)
    result = await server.mcp.get_prompt("co_lawyer", {
        "query": "fictional contract", "protocol_version": "2",
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
        "query": "SQL?", "protocol_version": "2",
        "current_persona": descriptor().model_dump_json(),
    })
    assert '"status": "ROUTE_REQUIRED"' in result.messages[0].content.text
    assert '"replaces_activation_id": "old"' in result.messages[0].content.text


@pytest.mark.asyncio
async def test_tool_schema_exposes_v2_descriptor_and_optional_defaults():
    tool_list = await server.mcp.list_tools()
    schemas = {tool.name: tool.inputSchema for tool in tool_list}
    assert schemas["route_and_load"]["properties"]["protocol_version"]["default"] == 1
    assert "current_persona" in schemas["get_agent_context"]["properties"]
    assert "force_reload" in schemas["get_agent_context"]["properties"]
    assert schemas["refresh_persona_context"]["required"] == ["query", "current_persona"]


@pytest.mark.asyncio
@pytest.mark.parametrize("command,retrieval_query", [
    ("lawyer", "fictional contract"),
    ("co_lawyer", "/co_lawyer fictional contract"),
])
async def test_agent_prompts_default_to_legacy_loading(command, retrieval_query, monkeypatch):
    legacy = AsyncMock(return_value=("Legacy role text", "hash", [], [], [], "standard"))
    v2 = AsyncMock()
    route = AsyncMock()
    monkeypatch.setattr(server, "_load_and_enrich", legacy)
    monkeypatch.setattr(server, "load_persona", v2)
    monkeypatch.setattr(server.router, "lookup_cache", route)

    result = await server.mcp.get_prompt(command, {"query": "fictional contract"})

    legacy.assert_awaited_once_with("lawyer", retrieval_query, [])
    v2.assert_not_awaited()
    route.assert_not_awaited()
    assert result.messages[0].content.text == (
        "SYSTEM INSTRUCTIONS (MANDATORY — follow exactly):\n\n"
        "Legacy role text\n\n---\nUSER QUERY: fictional contract"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("query,cached_agent,selected_agent", [
    ("Explain a Python dictionary", "software_engineer", "software_engineer"),
    ("hi", None, "universal_agent"),
])
async def test_ask_defaults_to_legacy_cached_or_meta_prompt(query, cached_agent, selected_agent, monkeypatch):
    cached = SimpleNamespace(target_agent=cached_agent) if cached_agent else None
    lookup = AsyncMock(return_value=cached)
    legacy = AsyncMock(return_value=("Legacy role text", "hash", [], [], [], "standard"))
    v2 = AsyncMock()
    monkeypatch.setattr(server.router, "lookup_cache", lookup)
    monkeypatch.setattr(server, "_load_and_enrich", legacy)
    monkeypatch.setattr(server, "route_persona", v2)

    # Supplying v2-only state cannot implicitly opt an existing prompt into v2.
    result = await server.mcp.get_prompt("ask", {"query": query, "current_persona": "unused in v1"})

    lookup.assert_awaited_once_with(query, {"history_text": ""})
    legacy.assert_awaited_once_with(selected_agent, query, [])
    v2.assert_not_awaited()
    text = result.messages[0].content.text
    assert text.startswith("SYSTEM INSTRUCTIONS (MANDATORY — follow exactly):\n\nLegacy role text")
    assert text.endswith(f"USER QUERY: {query}")
    assert "protocol 2" not in text


@pytest.mark.asyncio
async def test_ask_default_cache_miss_keeps_legacy_selection_request(monkeypatch):
    monkeypatch.setattr(server.router, "lookup_cache", AsyncMock(return_value=None))
    monkeypatch.setattr(server.router, "get_agent_catalog", Mock(return_value=[
        {"name": "software_engineer", "role": "Software implementation"},
    ]))
    legacy, v2 = AsyncMock(), AsyncMock()
    monkeypatch.setattr(server, "_load_and_enrich", legacy)
    monkeypatch.setattr(server, "route_persona", v2)

    result = await server.mcp.get_prompt("ask", {"query": "Explain a Python dictionary"})

    text = result.messages[0].content.text
    assert "get_agent_context(agent_name, query)" in text
    assert "**software_engineer**: Software implementation" in text
    assert "protocol_version" not in text
    legacy.assert_not_awaited()
    v2.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["ask", "lawyer", "co_lawyer"])
async def test_prompt_version_is_optional_and_discoverable(command):
    prompts = {prompt.name: prompt for prompt in await server.mcp.list_prompts()}
    prompt = prompts[command]
    arguments = {argument.name: argument for argument in prompt.arguments}
    assert arguments["query"].required
    assert not arguments["protocol_version"].required
    assert not arguments["current_persona"].required
    assert "Protocol 1 is the default" in prompt.description
    assert "protocol_version=2" in prompt.description


@pytest.mark.asyncio
@pytest.mark.parametrize("command", ["ask", "co_lawyer"])
async def test_prompt_rejects_unsupported_version_before_loading(command, monkeypatch):
    legacy, v2_load, v2_route, lookup = (AsyncMock() for _ in range(4))
    monkeypatch.setattr(server, "_load_and_enrich", legacy)
    monkeypatch.setattr(server, "load_persona", v2_load)
    monkeypatch.setattr(server, "route_persona", v2_route)
    monkeypatch.setattr(server.router, "lookup_cache", lookup)

    result = await server.mcp.get_prompt(command, {"query": "fictional contract", "protocol_version": "3"})

    assert "protocol_version must be 1 or 2" in result.messages[0].content.text
    for dependency in (legacy, v2_load, v2_route, lookup):
        dependency.assert_not_awaited()


@pytest.mark.asyncio
async def test_initialization_guidance_matches_each_version_success_payload(bundle, monkeypatch):
    monkeypatch.setattr(server, "_load_and_enrich", AsyncMock(
        return_value=("Legacy role text", "hash", [], [], [], "standard"),
    ))
    legacy = json.loads(await server.get_agent_context("lawyer", "fictional contract"))
    current = json.loads(await server.get_agent_context(
        "lawyer", "fictional contract", protocol_version=2,
    ))
    instructions = server.mcp._mcp_server.create_initialization_options().instructions
    v2_section = instructions.split("Version 2 response statuses:\n", 1)[1].split(
        "Version 1 (default API):", 1,
    )[0]
    v1_section = instructions.split("Version 1 response statuses:\n", 1)[1]

    # Check the field references delivered during MCP initialization against the
    # actual wire payloads: v2 clients must not be told to read v1's system_prompt.
    advertised = []
    for section, response in ((v1_section, legacy), (v2_section, current)):
        success = next(line for line in section.splitlines() if line.startswith("- SUCCESS →"))
        fields = set(re.findall(r"`(\w+)`", success))
        assert fields <= response.keys()
        advertised.append(fields)
    assert advertised[0] == {"system_prompt"}
    assert advertised[1] == {
        "persona", "persona_block", "rules_block", "skills_block", "implants_block",
        "replaces_activation_id", "footer",
    }
    assert "`system_prompt`" not in v2_section
