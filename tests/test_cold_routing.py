"""Cold-router activation on a semantic-cache miss: shortlist and headless resolution (#734)."""

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import numpy as np
import pytest

import src.server as server
from src.engine import config, persona
from src.engine import router as router_module
from src.engine.router import ColdRanking, SemanticRouter
from src.schemas.protocol import PersonaResponse, RouterDecision

# Six agents with keyword hits in the real catalog: code_reviewer and software_engineer
# with two each, database_admin, devops_engineer, security_expert and sysadmin with one.
MANY_HITS = ("Debug this Python script, review the code for security vulnerabilities, optimize "
             "the SQL query and set up a docker container with a CI pipeline")
NO_HITS = "Plan a five-day trip to Kyoto in spring"
SPECIALISTS = [f"agent_{index:02d}" for index in range(11)]
CATALOG = [{"name": name, "display_name": name.title(), "role": f"Role of {name}",
            "trigger_command": f"/{name}"} for name in SPECIALISTS + ["universal_agent"]]


@pytest.fixture
def bundle(monkeypatch):
    async def build(agent, query, history, tier=None, selection=None):
        return SimpleNamespace(
            agent=agent, bundle_revision="b" * 64, scope=f"Scope of {agent}",
            persona_block=f"Role {agent}", rules_block="General rules", skills_block="Skills",
            implants_block="Implants", skills_loaded=[], implants_loaded=[], rules_loaded=[],
        )
    builder = AsyncMock(side_effect=build)
    monkeypatch.setattr(persona, "build_persona_bundle", builder)
    return builder


class FakeRouter:
    """The router surface route_persona uses, with a ranking chosen by the test."""

    def __init__(self, ranking=None, cached=None, veto=None):
        self.ranking = ranking
        self.lookup_cache = AsyncMock(return_value=cached)
        self.keyword_veto = Mock(return_value=veto)
        self.update_cache = AsyncMock()
        self.cold_ranking = AsyncMock(return_value=ranking)

    def get_agent_catalog(self):
        return [dict(entry) for entry in CATALOG]


def embedding_ranking(similarities, agents=None):
    """A ranking from embedded cards: agents in order with their similarities."""
    agents = agents or (SPECIALISTS + ["universal_agent"])[:len(similarities)]
    return ColdRanking(agents=list(agents), similarities=list(similarities))


CLEAR_LEAD = embedding_ranking([0.70, 0.60, 0.59, 0.58, 0.57, 0.40, 0.39, 0.38, 0.37, 0.36, 0.35, 0.10])
NARROW_LEAD = embedding_ranking([0.62, 0.61, 0.59, 0.58, 0.57, 0.40, 0.39, 0.38, 0.37, 0.36, 0.35, 0.10])


async def route(router, query="Draft the release notes", current=None):
    return json.loads(await persona.route_persona(router, query, [], current, lambda _q: False))


@pytest.fixture
def headless(monkeypatch):
    monkeypatch.setattr(config, "CLIENT_PROFILE", "headless")
    monkeypatch.setattr(config, "ROUTER_COLD_RESOLVE_MARGIN", 0.05)
    monkeypatch.setattr(config, "ROUTER_SHORTLIST_SIZE", 8)


# --- ac-1: the shortlist on every client --------------------------------------------------

@pytest.mark.asyncio
async def test_a_miss_with_keyword_signal_lists_a_capped_shortlist_with_universal_agent(bundle, monkeypatch):
    monkeypatch.setattr(server.router, "lookup_cache", AsyncMock(return_value=None))
    monkeypatch.setattr(config, "ROUTER_SHORTLIST_SIZE", 3)
    response = json.loads(await server.route_and_load(MANY_HITS, protocol_version=2))

    assert response["status"] == "ROUTE_REQUIRED"
    names = [candidate["name"] for candidate in response["candidates"]]
    assert names == ["code_reviewer", "software_engineer", "universal_agent"]
    assert set(response["candidates"][0]) == {"name", "display_name", "role"}
    total = len(server.router.get_agent_catalog())
    assert response["instruction"].endswith(persona.SHORTLIST_NOTE.format(shown=3, total=total))
    assert "list_agents shows all" in response["instruction"]
    bundle.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_miss_without_keyword_signal_lists_the_whole_catalog(bundle, monkeypatch):
    monkeypatch.setattr(server.router, "lookup_cache", AsyncMock(return_value=None))
    response = json.loads(await server.route_and_load(NO_HITS, protocol_version=2))

    assert response["status"] == "ROUTE_REQUIRED"
    assert len(response["candidates"]) == len(server.router.get_agent_catalog())
    assert response["instruction"] == persona.ROUTE_INSTRUCTION


@pytest.mark.asyncio
async def test_shortlist_size_zero_lists_the_whole_catalog(bundle, monkeypatch):
    monkeypatch.setattr(server.router, "lookup_cache", AsyncMock(return_value=None))
    monkeypatch.setattr(config, "ROUTER_SHORTLIST_SIZE", 0)
    response = json.loads(await server.route_and_load(MANY_HITS, protocol_version=2))

    assert len(response["candidates"]) == len(server.router.get_agent_catalog())
    assert response["instruction"] == persona.ROUTE_INSTRUCTION


@pytest.mark.asyncio
async def test_an_embedding_shortlist_keeps_universal_agent_in_its_last_place(bundle, monkeypatch):
    monkeypatch.setattr(config, "ROUTER_SHORTLIST_SIZE", 4)
    response = await route(FakeRouter(CLEAR_LEAD))

    assert [c["name"] for c in response["candidates"]] == SPECIALISTS[:3] + ["universal_agent"]
    assert response["replaces_activation_id"] is None


@pytest.mark.asyncio
async def test_universal_agent_ranked_inside_the_shortlist_keeps_its_rank(bundle, monkeypatch):
    monkeypatch.setattr(config, "ROUTER_SHORTLIST_SIZE", 4)
    ranking = embedding_ranking([0.7, 0.6, 0.5, 0.4], ["agent_03", "universal_agent", "agent_01", "agent_02"])
    response = await route(FakeRouter(ranking))

    assert [c["name"] for c in response["candidates"]] == ["agent_03", "universal_agent", "agent_01", "agent_02"]


@pytest.mark.asyncio
async def test_a_vetoed_cache_hit_keeps_the_whole_catalog_without_ranking(bundle):
    cached = RouterDecision(target_agent="agent_00", confidence=1, reasoning="cache")
    router = FakeRouter(CLEAR_LEAD, cached=cached, veto=router_module.KEYWORD_VETO_ROUTE_REQUIRED)
    response = await route(router)

    assert len(response["candidates"]) == len(CATALOG)
    router.cold_ranking.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failing_ranker_lists_the_whole_catalog(bundle):
    router = FakeRouter()
    router.cold_ranking = AsyncMock(side_effect=RuntimeError("ranker broke"))
    response = await route(router)

    assert response["status"] == "ROUTE_REQUIRED"
    assert len(response["candidates"]) == len(CATALOG)


# --- ac-2 and ac-3: headless resolution --------------------------------------------------

@pytest.mark.asyncio
async def test_headless_clear_leader_activates_with_alternatives_and_does_not_learn(bundle, headless):
    router = FakeRouter(CLEAR_LEAD)
    response = await route(router)

    assert response["status"] == "SUCCESS"
    assert response["persona"]["agent"] == "agent_00"
    assert response["persona_block"] == "Role agent_00"
    assert response["alternatives"] == [{"name": name, "role": f"Role of {name}"}
                                        for name in SPECIALISTS[1:4]]
    assert "candidates" not in response
    bundle.assert_awaited_once()
    router.update_cache.assert_not_awaited()
    PersonaResponse.model_validate(response)


@pytest.mark.asyncio
async def test_headless_leader_already_active_keeps_it_without_alternatives(bundle, headless):
    active = persona.PersonaDescriptor(
        agent="agent_00", activation_id="old", bundle_revision="a" * 64, scope="Scope",
        skills_loaded=[], implants_loaded=[], rules_loaded=[])
    response = await route(FakeRouter(CLEAR_LEAD), current=active)

    assert response["status"] == "NO_CHANGE"
    assert response["persona"] == active.model_dump()
    assert "alternatives" not in response
    bundle.assert_not_awaited()


@pytest.mark.asyncio
async def test_headless_below_the_margin_lists_the_shortlist(bundle, headless):
    response = await route(FakeRouter(NARROW_LEAD))

    assert response["status"] == "ROUTE_REQUIRED"
    names = [c["name"] for c in response["candidates"]]
    assert names == SPECIALISTS[:7] + ["universal_agent"]
    assert "alternatives" not in response
    bundle.assert_not_awaited()


@pytest.mark.asyncio
async def test_interactive_never_resolves_a_miss(bundle, monkeypatch):
    monkeypatch.setattr(config, "CLIENT_PROFILE", "interactive")
    monkeypatch.setattr(config, "ROUTER_COLD_RESOLVE_MARGIN", 0.05)
    response = await route(FakeRouter(CLEAR_LEAD))

    assert response["status"] == "ROUTE_REQUIRED"
    assert len(response["candidates"]) == config.ROUTER_SHORTLIST_SIZE
    bundle.assert_not_awaited()


@pytest.mark.asyncio
async def test_headless_keyword_ranking_never_resolves(bundle, headless):
    response = await route(FakeRouter(ColdRanking(agents=["agent_04", "agent_05"])))

    assert response["status"] == "ROUTE_REQUIRED"
    assert [c["name"] for c in response["candidates"]] == ["agent_04", "agent_05", "universal_agent"]
    bundle.assert_not_awaited()


@pytest.mark.asyncio
async def test_headless_margin_zero_turns_resolution_off(bundle, headless, monkeypatch):
    monkeypatch.setattr(config, "ROUTER_COLD_RESOLVE_MARGIN", 0.0)
    response = await route(FakeRouter(CLEAR_LEAD))

    assert response["status"] == "ROUTE_REQUIRED"
    bundle.assert_not_awaited()


@pytest.mark.asyncio
async def test_headless_failed_activation_falls_back_to_the_shortlist(bundle, headless):
    bundle.side_effect = RuntimeError("bundle failed")
    router = FakeRouter(CLEAR_LEAD)
    response = await route(router)

    assert response["status"] == "ROUTE_REQUIRED"
    assert len(response["candidates"]) == 8
    router.update_cache.assert_not_awaited()


@pytest.mark.asyncio
async def test_headless_cache_hit_is_unchanged_and_still_learns(bundle, headless):
    cached = RouterDecision(target_agent="agent_02", confidence=1, reasoning="cache")
    router = FakeRouter(CLEAR_LEAD, cached=cached)
    response = await route(router)

    assert response["persona"]["agent"] == "agent_02"
    assert "alternatives" not in response
    router.cold_ranking.assert_not_awaited()
    router.update_cache.assert_awaited_once()


def test_alternatives_are_optional_in_the_schema():
    plain = json.loads(PersonaResponse(status="ROUTE_REQUIRED", request_id="r").to_json())
    assert "alternatives" not in plain
    listed = PersonaResponse(status="SUCCESS", request_id="r", alternatives=[{"name": "a", "role": "b"}])
    assert json.loads(listed.to_json())["alternatives"] == [{"name": "a", "role": "b"}]


# --- The ranker: embedded catalog cards with a keyword fallback -------------------------

@pytest.fixture
def isolated_router(tmp_path, monkeypatch):
    """A SemanticRouter whose stores live in tmp_path, with a one-hot embedder per agent."""
    monkeypatch.setattr(router_module, "DATA_DIR", str(tmp_path))
    monkeypatch.setattr(router_module, "_ROUTER_MODEL_HASH_FILE", str(tmp_path / ".router_cache_model"))
    monkeypatch.setattr(router_module, "model_fingerprint", lambda: "model-a")
    made = SemanticRouter()
    index = {made.agent_card(name): position for position, name in enumerate(made.available_agents)}
    size = len(index)
    weights = {}

    def embed_texts(texts):
        vectors = np.zeros((len(texts), size), dtype=np.float32)
        for row, text in enumerate(texts):
            vectors[row, index[text]] = 1.0
        return vectors

    def embed_query(text):
        vector = np.full(size, 0.01, dtype=np.float32)
        for name, weight in weights.items():
            vector[made.available_agents.index(name)] = weight
        return vector

    monkeypatch.setattr(router_module, "embed_texts", embed_texts)
    monkeypatch.setattr(router_module, "embed_query", embed_query)
    return SimpleNamespace(router=made, weights=weights)


@pytest.mark.asyncio
async def test_without_a_loaded_model_the_ranking_is_keyword_hits_and_nothing_is_built(isolated_router, monkeypatch):
    monkeypatch.setattr(router_module, "is_loaded", lambda: False)
    start = Mock()
    monkeypatch.setattr(SemanticRouter, "_start_card_build", start)
    ranking = await isolated_router.router.cold_ranking(MANY_HITS)

    assert ranking.agents[:2] == ["code_reviewer", "software_engineer"]
    assert len(ranking.agents) == 6 and ranking.similarities is None and ranking.gap is None
    start.assert_not_called()


@pytest.mark.asyncio
async def test_unbuilt_cards_start_one_build_and_rank_by_keywords_meanwhile(isolated_router, monkeypatch):
    monkeypatch.setattr(router_module, "is_loaded", lambda: True)
    start = Mock()
    monkeypatch.setattr(SemanticRouter, "_start_card_build", start)
    first = await isolated_router.router.cold_ranking(NO_HITS)
    second = await isolated_router.router.cold_ranking(NO_HITS)

    assert first.agents == [] and second.agents == []
    start.assert_called_once()


@pytest.mark.asyncio
async def test_built_cards_rank_by_similarity_with_keyword_hits_breaking_ties(isolated_router, monkeypatch):
    monkeypatch.setattr(router_module, "is_loaded", lambda: True)
    router = isolated_router.router
    router._build_cards_in_background()  # what the started thread runs
    isolated_router.weights.update({"software_engineer": 0.9, "code_reviewer": 0.9, "lawyer": 0.5})
    ranking = await router.cold_ranking(MANY_HITS)

    # Both lead with the same similarity; the keyword hits tie at two as well, so the
    # embedding order alone cannot be asserted for them, only that both lead the lawyer.
    assert set(ranking.agents[:2]) == {"software_engineer", "code_reviewer"}
    assert ranking.agents[2] == "lawyer"
    assert len(ranking.agents) == len(router.available_agents)
    assert ranking.gap == pytest.approx(0.0, abs=1e-6)

    isolated_router.weights.clear()
    isolated_router.weights.update({"lawyer": 0.9, "data_analyst": 0.9})
    tie = await router.cold_ranking("add a regression test")  # "regression": data_analyst
    assert tie.agents[:2] == ["data_analyst", "lawyer"]


@pytest.mark.asyncio
async def test_cards_persist_and_a_changed_model_rebuilds_them(isolated_router, monkeypatch):
    monkeypatch.setattr(router_module, "is_loaded", lambda: True)
    isolated_router.router.build_agent_cards()
    start = Mock()
    monkeypatch.setattr(SemanticRouter, "_start_card_build", start)

    reloaded = SemanticRouter()
    assert reloaded.agent_cards_ready() is True
    start.assert_not_called()

    monkeypatch.setattr(router_module, "model_fingerprint", lambda: "model-b")
    stale = SemanticRouter()
    assert stale.agent_cards_ready() is False
    start.assert_called_once()


@pytest.mark.asyncio
async def test_a_failed_build_keeps_the_keyword_ranking_for_the_process(isolated_router, monkeypatch):
    monkeypatch.setattr(router_module, "is_loaded", lambda: True)
    monkeypatch.setattr(router_module, "embed_texts", Mock(side_effect=RuntimeError("no model")))
    router = isolated_router.router
    router._build_cards_in_background()

    assert router.agent_cards_ready() is False
    ranking = await router.cold_ranking(MANY_HITS)
    assert ranking.similarities is None and len(ranking.agents) == 6


@pytest.mark.asyncio
async def test_a_failed_query_embedding_falls_back_to_keywords(isolated_router, monkeypatch):
    monkeypatch.setattr(router_module, "is_loaded", lambda: True)
    router = isolated_router.router
    router._build_cards_in_background()
    monkeypatch.setattr(router_module, "embed_query", Mock(side_effect=RuntimeError("busy")))
    ranking = await router.cold_ranking(MANY_HITS)

    assert ranking.similarities is None and len(ranking.agents) == 6
