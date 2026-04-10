"""
Tests for routing logic: _is_meta_query, _normalize_chat_history,
and sticky agent routing in route_and_load.

Queries are multilingual (EN, RU, DE, ES, FR) and depersonalized.
"""

import json
import pytest
from unittest.mock import AsyncMock, patch
from cachetools import TTLCache

from src.server import _is_meta_query, _normalize_chat_history
from src.engine.config import SESSION_CACHE_MAX_SIZE, SESSION_CACHE_TTL_SECONDS


# ---------------------------------------------------------------------------
# _is_meta_query
# ---------------------------------------------------------------------------

class TestIsMetaQuery:
    """Verify meta-query detection across languages."""

    # --- Should be detected as meta ---

    @pytest.mark.parametrize("query", [
        # English
        "hi",
        "hey",
        "hello",
        "test",
        "what can you do?",
        "What tools do you have?",
        "help me",
        "introduce yourself",
        "who are you",
        "what are you",
        # Russian
        "привет",
        "Привет!",
        "здравствуй",
        "кто ты",
        "кто ты?",
        "помоги",
        "что ты умеешь",
        "какие у тебя инструменты",
        "какие есть агенты",
    ])
    def test_meta_queries_detected(self, query):
        assert _is_meta_query(query) is True

    # Short queries (< 10 chars) are always meta regardless of language
    @pytest.mark.parametrize("query", [
        "ok",
        "да",
        "yes",
        "??",
        "ок",
        "oui",
        "ja",
        "sí",
    ])
    def test_short_queries_are_meta(self, query):
        assert _is_meta_query(query) is True

    # --- Should NOT be detected as meta ---

    @pytest.mark.parametrize("query", [
        # English — technical
        "Review and fix linter warnings in the authentication module",
        "Thorough code review: clean up unused imports and check test coverage",
        "Fix validation logic for user registration endpoint",
        "One-liner to archive only intact files from a storage pool preserving permissions",
        "Database corruption root cause analysis — what information to collect for a bug report",
        "Translate project documentation to English and create an architecture diagram",
        "Set up a split tunnel VPN configuration on a mobile device",
        # Russian — news/analysis
        "Какие главные новости сегодня в мире?",
        "Расскажи про последние события в регионе",
        # Russian — medical
        "У пациента 35 лет после физической нагрузки болит поясница уже неделю",
        # Russian — creative
        "Написать короткий рассказ про кота и его приключения",
        # Russian — parenting
        "Ребёнок 7 лет рассказывает всем семейные секреты, как с этим справиться",
        "Как объяснить ребёнку сложную тему простыми словами?",
        # Russian — technical
        "Проверь почему сервер перестал отвечать на запросы через API",
        "Оптимизация роутинга: кэширование решений по контексту запроса",
        "Проведи ревью изменений кода на текущей ветке",
        # Russian — travel
        "Как лучше всего провести отпуск с семьёй на побережье?",
        # Russian — health
        "Расскажи, как улучшить качество сна?",
        # Russian — philosophy
        "Что появилось первое — курица или яйцо?",
        # German — various topics
        "Wie kann ich die Leistung meiner Datenbank optimieren?",
        "Erkläre den Unterschied zwischen REST und GraphQL",
        "Schreibe eine kurze Geschichte über einen Roboter, der träumen lernt",
        "Welche Sehenswürdigkeiten sollte man in der Hauptstadt besuchen?",
        # Spanish — various topics
        "¿Cómo puedo mejorar el rendimiento de mi aplicación web?",
        "Explica la diferencia entre microservicios y monolitos",
        "Escribe un cuento corto sobre un viaje en el tiempo",
        "¿Cuáles son las mejores prácticas para la seguridad de APIs?",
        # French — various topics
        "Comment optimiser les requêtes SQL dans une base de données relationnelle?",
        "Explique la différence entre Docker et Kubernetes",
        "Écris une courte histoire sur un chat qui explore une ville abandonnée",
        "Quelles sont les meilleures pratiques pour le déploiement continu?",
    ])
    def test_real_queries_not_meta(self, query):
        assert _is_meta_query(query) is False

    # --- Known false positive: "help me with X" is caught by ^help\b ---
    def test_help_with_topic_is_meta_false_positive(self):
        # "help me with database optimization" SHOULD ideally NOT be meta,
        # but ^help\b matches it. Documenting current behavior.
        assert _is_meta_query("help me with database optimization") is True

    # --- Edge cases ---
    def test_question_about_project_not_meta(self):
        assert _is_meta_query("Что здесь интересного? Обзор проекта") is False

    def test_short_question_about_repo_not_meta(self):
        # 18 chars, doesn't match meta regex
        assert _is_meta_query("Что это за репо?") is False

    def test_german_greeting_not_detected(self):
        # German greetings are not in the regex — only EN/RU are supported
        assert _is_meta_query("Hallo, wie geht es dir?") is False

    def test_spanish_greeting_not_detected(self):
        assert _is_meta_query("Hola, ¿qué puedes hacer?") is False

    def test_french_greeting_not_detected(self):
        assert _is_meta_query("Bonjour, qui es-tu?") is False


# ---------------------------------------------------------------------------
# _normalize_chat_history
# ---------------------------------------------------------------------------

class TestNormalizeChatHistory:
    def test_none_returns_empty(self):
        assert _normalize_chat_history(None) == []

    def test_empty_string_returns_empty(self):
        assert _normalize_chat_history("") == []

    def test_whitespace_string_returns_empty(self):
        assert _normalize_chat_history("   ") == []

    def test_single_string_wrapped(self):
        assert _normalize_chat_history("hello") == ["hello"]

    def test_list_passthrough(self):
        assert _normalize_chat_history(["a", "b"]) == ["a", "b"]

    def test_list_filters_non_strings(self):
        assert _normalize_chat_history(["a", 42, None, "b"]) == ["a", "b"]

    def test_list_keeps_empty_strings(self):
        assert _normalize_chat_history(["a", "", "b"]) == ["a", "", "b"]


# ---------------------------------------------------------------------------
# Sticky routing integration tests
# ---------------------------------------------------------------------------

class TestStickyRouting:
    """Test sticky agent routing logic using mocked router and caches."""

    @pytest.fixture(autouse=True)
    def setup_mocks(self):
        """Patch router, caches, and enrichment for each test."""
        import src.server as srv

        self.srv = srv
        self.original_ctx_cache = srv.CONTEXT_HASH_CACHE
        self.original_session_cache = srv.SESSION_CACHE

        srv.CONTEXT_HASH_CACHE = TTLCache(maxsize=SESSION_CACHE_MAX_SIZE, ttl=SESSION_CACHE_TTL_SECONDS)
        srv.SESSION_CACHE = TTLCache(maxsize=SESSION_CACHE_MAX_SIZE, ttl=SESSION_CACHE_TTL_SECONDS)

        yield

        srv.CONTEXT_HASH_CACHE = self.original_ctx_cache
        srv.SESSION_CACHE = self.original_session_cache

    def _make_enrich_result(self, agent_name):
        """Helper: return a fake _load_and_enrich result."""
        prompt = f"system prompt for {agent_name}"
        ctx_hash = f"hash_{agent_name}"
        return (prompt, ctx_hash, ["skill-a"], ["implant-a"], "standard")

    @pytest.mark.asyncio
    async def test_no_sticky_cache_miss_returns_route_required(self):
        """Without context_hash, a cache miss should return ROUTE_REQUIRED."""
        with patch.object(self.srv.router, "lookup_cache", new_callable=AsyncMock, return_value=None), \
             patch.object(self.srv.router, "get_agent_catalog", return_value=[{"name": "universal_agent"}]):
            result = json.loads(await self.srv.route_and_load(
                "Erkläre den Unterschied zwischen REST und GraphQL"))
            assert result["status"] == "ROUTE_REQUIRED"
            assert "candidates" in result

    @pytest.mark.asyncio
    async def test_no_sticky_cache_hit_returns_success(self):
        """Without context_hash, a cache hit should load the agent and return SUCCESS."""
        from src.schemas.protocol import RouterDecision
        cached = RouterDecision(target_agent="daily_briefing", confidence=1.0,
                                reasoning="Cached", is_cached=True)

        with patch.object(self.srv.router, "lookup_cache", new_callable=AsyncMock, return_value=cached), \
             patch("src.server._load_and_enrich", new_callable=AsyncMock,
                   return_value=self._make_enrich_result("daily_briefing")), \
             patch.object(self.srv.router, "update_cache", new_callable=AsyncMock):
            result = json.loads(await self.srv.route_and_load(
                "Какие главные новости сегодня в мире?"))
            assert result["status"] == "SUCCESS"
            assert result["agent"] == "daily_briefing"

    @pytest.mark.asyncio
    async def test_no_sticky_meta_query_returns_universal(self):
        """Meta-queries without sticky context should go to universal_agent."""
        with patch.object(self.srv.router, "lookup_cache", new_callable=AsyncMock, return_value=None), \
             patch("src.server._load_and_enrich", new_callable=AsyncMock,
                   return_value=self._make_enrich_result("universal_agent")), \
             patch.object(self.srv.router, "update_cache", new_callable=AsyncMock):
            result = json.loads(await self.srv.route_and_load("привет"))
            assert result["status"] == "SUCCESS"
            assert result["agent"] == "universal_agent"

    @pytest.mark.asyncio
    async def test_sticky_keeps_agent_on_empty_cache(self):
        """With sticky agent and completely empty cache, keep current agent."""
        self.srv.CONTEXT_HASH_CACHE["prev_hash"] = "medical_expert"

        with patch.object(self.srv.router, "query_nearest",
                          new_callable=AsyncMock, return_value=None), \
             patch("src.server._load_and_enrich", new_callable=AsyncMock,
                   return_value=self._make_enrich_result("medical_expert")), \
             patch.object(self.srv.router, "update_cache", new_callable=AsyncMock) as mock_cache:
            result = json.loads(await self.srv.route_and_load(
                "Le patient a des douleurs lombaires depuis une semaine après un exercice physique",
                context_hash="prev_hash",
            ))
            assert result["status"] == "SUCCESS"
            assert result["agent"] == "medical_expert"
            # Should NOT cache unvalidated sticky decision
            mock_cache.assert_not_called()

    @pytest.mark.asyncio
    async def test_sticky_confirms_same_agent_from_cache(self):
        """When cache confirms the same agent (close distance), keep it and allow caching."""
        from src.schemas.protocol import RouterDecision
        self.srv.CONTEXT_HASH_CACHE["prev_hash"] = "sysadmin"
        cached = RouterDecision(target_agent="sysadmin", confidence=1.0,
                                reasoning="Cached", is_cached=True)

        with patch.object(self.srv.router, "query_nearest",
                          new_callable=AsyncMock, return_value=(cached, 0.01)), \
             patch.object(self.srv.router, "keyword_veto", return_value=None), \
             patch("src.server._load_and_enrich", new_callable=AsyncMock,
                   return_value=self._make_enrich_result("sysadmin")), \
             patch.object(self.srv.router, "update_cache", new_callable=AsyncMock) as mock_cache:
            result = json.loads(await self.srv.route_and_load(
                "Filesystem corruption root cause analysis — what data to collect",
                context_hash="prev_hash",
            ))
            assert result["agent"] == "sysadmin"
            mock_cache.assert_called_once()

    @pytest.mark.asyncio
    async def test_sticky_auto_switches_on_strong_signal(self):
        """Very strong competing signal (distance < 0.02) triggers auto-switch."""
        from src.schemas.protocol import RouterDecision
        self.srv.CONTEXT_HASH_CACHE["prev_hash"] = "software_engineer"
        cached = RouterDecision(target_agent="daily_briefing", confidence=1.0,
                                reasoning="Cached", is_cached=True)

        with patch.object(self.srv.router, "query_nearest",
                          new_callable=AsyncMock, return_value=(cached, 0.005)), \
             patch("src.server._load_and_enrich", new_callable=AsyncMock,
                   return_value=self._make_enrich_result("daily_briefing")), \
             patch.object(self.srv.router, "update_cache", new_callable=AsyncMock):
            result = json.loads(await self.srv.route_and_load(
                "¿Cuáles son las noticias más importantes de hoy?",
                context_hash="prev_hash",
            ))
            assert result["agent"] == "daily_briefing"

    @pytest.mark.asyncio
    async def test_sticky_keeps_on_weak_signal(self):
        """Weak competing signal for different agent (distance 0.02-0.05) keeps sticky."""
        from src.schemas.protocol import RouterDecision
        self.srv.CONTEXT_HASH_CACHE["prev_hash"] = "software_engineer"
        cached = RouterDecision(target_agent="daily_briefing", confidence=1.0,
                                reasoning="Cached", is_cached=True)

        with patch.object(self.srv.router, "query_nearest",
                          new_callable=AsyncMock, return_value=(cached, 0.04)), \
             patch("src.server._load_and_enrich", new_callable=AsyncMock,
                   return_value=self._make_enrich_result("software_engineer")), \
             patch.object(self.srv.router, "update_cache", new_callable=AsyncMock) as mock_cache:
            result = json.loads(await self.srv.route_and_load(
                "Füge jetzt Tests für diese Funktion hinzu",
                context_hash="prev_hash",
            ))
            assert result["agent"] == "software_engineer"
            mock_cache.assert_not_called()

    @pytest.mark.asyncio
    async def test_sticky_releases_on_topic_change(self):
        """When nearest match is far (>= 0.05), sticky releases to ROUTE_REQUIRED."""
        from src.schemas.protocol import RouterDecision
        self.srv.CONTEXT_HASH_CACHE["prev_hash"] = "software_engineer"
        far_match = RouterDecision(target_agent="daily_briefing", confidence=1.0,
                                   reasoning="Cached", is_cached=True)

        with patch.object(self.srv.router, "query_nearest",
                          new_callable=AsyncMock, return_value=(far_match, 0.15)), \
             patch.object(self.srv.router, "lookup_cache", new_callable=AsyncMock, return_value=None), \
             patch.object(self.srv.router, "get_agent_catalog", return_value=[{"name": "universal_agent"}]):
            result = json.loads(await self.srv.route_and_load(
                "Écris une courte histoire sur un chat qui explore une ville abandonnée",
                context_hash="prev_hash",
            ))
            assert result["status"] == "ROUTE_REQUIRED"

    @pytest.mark.asyncio
    async def test_sticky_meta_query_overrides_to_universal(self):
        """Meta-query always overrides sticky agent to universal_agent."""
        self.srv.CONTEXT_HASH_CACHE["prev_hash"] = "software_engineer"

        with patch("src.server._load_and_enrich", new_callable=AsyncMock,
                   return_value=self._make_enrich_result("universal_agent")), \
             patch.object(self.srv.router, "update_cache", new_callable=AsyncMock):
            result = json.loads(await self.srv.route_and_load(
                "hello",
                context_hash="prev_hash",
            ))
            assert result["agent"] == "universal_agent"

    @pytest.mark.asyncio
    async def test_sticky_releases_on_lookup_error(self):
        """When vector store query fails, release to ROUTE_REQUIRED instead of keeping sticky."""
        self.srv.CONTEXT_HASH_CACHE["prev_hash"] = "software_engineer"

        with patch.object(self.srv.router, "query_nearest",
                          new_callable=AsyncMock, side_effect=RuntimeError("Vector store connection lost")), \
             patch.object(self.srv.router, "get_agent_catalog", return_value=[{"name": "universal_agent"}]):
            result = json.loads(await self.srv.route_and_load(
                "Wie kann ich die Leistung meiner Datenbank optimieren?",
                context_hash="prev_hash",
            ))
            assert result["status"] == "ROUTE_REQUIRED"

    @pytest.mark.asyncio
    async def test_expired_context_hash_falls_through(self):
        """If context_hash is not in cache (expired), treat as non-sticky."""
        with patch.object(self.srv.router, "lookup_cache", new_callable=AsyncMock, return_value=None), \
             patch.object(self.srv.router, "get_agent_catalog", return_value=[{"name": "universal_agent"}]):
            result = json.loads(await self.srv.route_and_load(
                "Review code changes on the current branch",
                context_hash="expired_hash_not_in_cache",
            ))
            assert result["status"] == "ROUTE_REQUIRED"

    @pytest.mark.asyncio
    async def test_no_change_on_same_context_hash(self):
        """If enriched prompt produces the same hash, return NO_CHANGE."""
        self.srv.CONTEXT_HASH_CACHE["prev_hash"] = "software_engineer"

        with patch.object(self.srv.router, "query_nearest",
                          new_callable=AsyncMock, return_value=None), \
             patch("src.server._load_and_enrich", new_callable=AsyncMock,
                   return_value=("prompt", "prev_hash", ["s"], ["i"], "standard")), \
             patch.object(self.srv.router, "update_cache", new_callable=AsyncMock):
            result = json.loads(await self.srv.route_and_load(
                "Comment optimiser ce code?",
                context_hash="prev_hash",
            ))
            assert result["status"] == "NO_CHANGE"
            assert result["agent"] == "software_engineer"


# ---------------------------------------------------------------------------
# _load_and_enrich: preferred_implants support
# ---------------------------------------------------------------------------

class TestPreferredImplants:
    """Test preferred_implants tier promotion and forwarding in _load_and_enrich."""

    @pytest.fixture(autouse=True)
    def setup_caches(self):
        import src.server as srv
        self.srv = srv
        self.original_session_cache = srv.SESSION_CACHE
        self.original_ctx_cache = srv.CONTEXT_HASH_CACHE
        srv.SESSION_CACHE = TTLCache(maxsize=SESSION_CACHE_MAX_SIZE, ttl=SESSION_CACHE_TTL_SECONDS)
        srv.CONTEXT_HASH_CACHE = TTLCache(maxsize=SESSION_CACHE_MAX_SIZE, ttl=SESSION_CACHE_TTL_SECONDS)
        yield
        srv.SESSION_CACHE = self.original_session_cache
        srv.CONTEXT_HASH_CACHE = self.original_ctx_cache

    def _fake_enrichment(self, **kwargs):
        from src.engine.enrichment import EnrichmentResult
        return EnrichmentResult(prompt="enriched", skills_loaded=["s"], implants_loaded=["i"])

    @pytest.mark.asyncio
    async def test_tier_promoted_when_preferred_implants_present(self):
        """Lite tier should be promoted to standard when agent has preferred_implants."""
        metadata = {
            "preferred_skills": [],
            "preferred_implants": ["implant-chain-of-code"],
            "capabilities": [],
        }

        with patch("src.server.get_agent_metadata", return_value=metadata), \
             patch("src.server.load_agent_prompt", return_value="base prompt"), \
             patch("src.server.enrich_agent_prompt", new_callable=AsyncMock,
                   return_value=self._fake_enrichment()) as mock_enrich:
            # Short query → would infer "lite", but preferred_implants promotes to "standard"
            _, _, _, _, effective_tier = await self.srv._load_and_enrich(
                "math_scientist", "hi", [])
            assert effective_tier == "standard"

    @pytest.mark.asyncio
    async def test_tier_not_promoted_when_explicit(self):
        """Explicitly set tier should NOT be promoted even with preferred_implants."""
        metadata = {
            "preferred_skills": [],
            "preferred_implants": ["implant-chain-of-code"],
            "capabilities": [],
        }

        with patch("src.server.get_agent_metadata", return_value=metadata), \
             patch("src.server.load_agent_prompt", return_value="base prompt"), \
             patch("src.server.enrich_agent_prompt", new_callable=AsyncMock,
                   return_value=self._fake_enrichment()):
            _, _, _, _, effective_tier = await self.srv._load_and_enrich(
                "math_scientist", "hi", [], tier="lite")
            assert effective_tier == "lite"

    @pytest.mark.asyncio
    async def test_preferred_implants_forwarded_to_enrichment(self):
        """preferred_implants from metadata should be passed to enrich_agent_prompt."""
        metadata = {
            "preferred_skills": ["skill-math"],
            "preferred_implants": ["implant-chain-of-code", "implant-program-of-thoughts"],
            "capabilities": [],
        }

        with patch("src.server.get_agent_metadata", return_value=metadata), \
             patch("src.server.load_agent_prompt", return_value="base prompt"), \
             patch("src.server.enrich_agent_prompt", new_callable=AsyncMock,
                   return_value=self._fake_enrichment()) as mock_enrich:
            await self.srv._load_and_enrich("math_scientist", "solve x^2 = 4", [])
            mock_enrich.assert_called_once()
            call_kwargs = mock_enrich.call_args
            # preferred_implants is passed as a keyword argument
            assert call_kwargs.kwargs["preferred_implants"] == [
                "implant-chain-of-code", "implant-program-of-thoughts"
            ]

    @pytest.mark.asyncio
    async def test_empty_preferred_implants_no_promotion(self):
        """Empty preferred_implants should NOT trigger tier promotion on its own."""
        metadata = {
            "preferred_skills": [],
            "preferred_implants": [],
            "capabilities": [],
        }

        with patch("src.server.get_agent_metadata", return_value=metadata), \
             patch("src.server.load_agent_prompt", return_value="base prompt"), \
             patch("src.server.enrich_agent_prompt", new_callable=AsyncMock,
                   return_value=self._fake_enrichment()):
            _, _, _, _, effective_tier = await self.srv._load_and_enrich(
                "universal_agent", "hi", [])
            assert effective_tier == "lite"


# ---------------------------------------------------------------------------
# clear_session_cache clears both caches
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Keyword boosting: match_keywords and keyword_veto
# ---------------------------------------------------------------------------

class TestKeywordBoosting:
    """Tests for domain_keywords-based routing boost."""

    def _make_router_with_keywords(self, agent_keywords):
        """Create a SemanticRouter mock with pre-set _agent_keywords."""
        from src.engine.router import SemanticRouter
        r = object.__new__(SemanticRouter)
        r._agent_keywords = {
            name: [kw.lower() for kw in kws]
            for name, kws in agent_keywords.items()
        }
        return r

    # --- match_keywords ---

    def test_match_keywords_basic(self):
        r = self._make_router_with_keywords({
            "security_expert": ["vulnerability", "security audit", "xss", "sql injection"],
            "software_engineer": ["refactor", "debug", "implement"],
        })
        matches = r.match_keywords("Found a SQL injection vulnerability in the API")
        assert matches[0][0] == "security_expert"
        assert matches[0][1] >= 2

    def test_match_keywords_case_insensitive(self):
        r = self._make_router_with_keywords({
            "security_expert": ["owasp", "xss"],
        })
        matches = r.match_keywords("Check OWASP top 10 and XSS vectors")
        assert len(matches) == 1
        assert matches[0] == ("security_expert", 2)

    def test_match_keywords_no_match(self):
        r = self._make_router_with_keywords({
            "russian_lawyer": ["российское право", "закон рф"],
        })
        matches = r.match_keywords("How to bake a cake?")
        assert matches == []

    def test_match_keywords_multilingual_russian(self):
        r = self._make_router_with_keywords({
            "russian_lawyer": ["российское право", "закон рф", "гк рф", "нк рф"],
            "universal_agent": [],
        })
        matches = r.match_keywords("Анализ статьи ГК РФ про обязательства")
        assert matches[0][0] == "russian_lawyer"
        assert matches[0][1] >= 1

    def test_match_keywords_token_fallback(self):
        """Token-level matching: 'закон' (from 'закон рф') matches 'законодательству'."""
        r = self._make_router_with_keywords({
            "russian_lawyer": ["закон рф", "российское право"],
        })
        matches = r.match_keywords("Проверить на соответствие российскому законодательству")
        assert len(matches) == 1
        assert matches[0][0] == "russian_lawyer"
        # "закон" token (5 chars >= 4) is substring of "законодательству"
        assert matches[0][1] >= 1

    # --- keyword_veto ---

    def test_keyword_veto_confirms_cache(self):
        r = self._make_router_with_keywords({
            "russian_lawyer": ["закон рф", "гк рф"],
        })
        result = r.keyword_veto("Статья ГК РФ", "russian_lawyer")
        assert result is None

    def test_keyword_veto_overrides_cache(self):
        r = self._make_router_with_keywords({
            "russian_lawyer": ["закон рф", "гк рф", "нк рф"],
            "universal_agent": [],
        })
        result = r.keyword_veto("Анализ ГК РФ и НК РФ", "universal_agent")
        assert result == "russian_lawyer"

    def test_keyword_veto_returns_route_required(self):
        """When two agents have similar keyword hits, return ROUTE_REQUIRED."""
        r = self._make_router_with_keywords({
            "russian_lawyer": ["закон рф"],
            "kazakh_lawyer": ["закон рф"],  # same keyword in both
        })
        from src.engine.router import KEYWORD_VETO_ROUTE_REQUIRED
        result = r.keyword_veto("Анализ закон РФ", "universal_agent")
        # Both have 1 hit, ratio = 1.0 < 2.0 -> ambiguous
        assert result == KEYWORD_VETO_ROUTE_REQUIRED

    def test_keyword_veto_ignores_weak_signal(self):
        """No match at all -> None (trust cache)."""
        r = self._make_router_with_keywords({
            "russian_lawyer": ["российское право"],
        })
        result = r.keyword_veto("How to bake a cake?", "universal_agent")
        assert result is None

    def test_universal_agent_keywords_excluded(self):
        """universal_agent keywords are empty so it never wins by keywords."""
        r = self._make_router_with_keywords({
            "universal_agent": [],  # excluded at load time
            "product_manager": ["plan", "roadmap"],
        })
        matches = r.match_keywords("Help me plan the project")
        # Only product_manager should match (if it has "plan")
        agent_names = [m[0] for m in matches]
        assert "universal_agent" not in agent_names

    @pytest.mark.asyncio
    async def test_standard_routing_keyword_override(self):
        """Integration: cache returns universal_agent, keywords override to russian_lawyer."""
        import src.server as srv
        from src.schemas.protocol import RouterDecision

        cached = RouterDecision(
            target_agent="universal_agent",
            confidence=1.0,
            reasoning="Cached result",
            is_cached=True,
        )

        with patch.object(srv.router, "lookup_cache", new_callable=AsyncMock, return_value=cached), \
             patch.object(srv.router, "keyword_veto", return_value="russian_lawyer"), \
             patch.object(srv.router, "update_cache", new_callable=AsyncMock) as mock_cache, \
             patch("src.server._load_and_enrich", new_callable=AsyncMock, return_value=(
                 "prompt", "hash123", ["skill1"], ["implant1"], "standard"
             )), \
             patch("src.server._sample_with_agent", new_callable=AsyncMock, return_value=None):
            result = await srv.route_and_load("Проверить доверенность на соответствие российскому законодательству")
            data = json.loads(result)
            assert data["agent"] == "russian_lawyer"
            assert data["status"] in ("SUCCESS", "SUCCESS_SAMPLED")
            # Verify cache is updated with the overridden agent, not the original
            mock_cache.assert_called_once()
            assert mock_cache.call_args[1].get("agent_name", mock_cache.call_args[0][1]) == "russian_lawyer"

    @pytest.mark.asyncio
    async def test_sticky_autoswitch_respects_ambiguous_keyword_veto(self):
        """Regression: KEYWORD_VETO_ROUTE_REQUIRED from keyword_veto in auto-switch must
        release to ROUTE_REQUIRED, not silently proceed with the switch target."""
        import src.server as srv
        from src.schemas.protocol import RouterDecision
        from src.engine.router import KEYWORD_VETO_ROUTE_REQUIRED

        srv.CONTEXT_HASH_CACHE["prev_hash"] = "universal_agent"
        cached = RouterDecision(
            target_agent="software_engineer",
            confidence=1.0,
            reasoning="Cached",
            is_cached=True,
        )

        with patch.object(srv.router, "query_nearest",
                          new_callable=AsyncMock, return_value=(cached, 0.01)), \
             patch.object(srv.router, "keyword_veto", return_value=KEYWORD_VETO_ROUTE_REQUIRED), \
             patch.object(srv.router, "get_agent_catalog", return_value=[]):
            result = json.loads(await srv.route_and_load(
                "Анализ закон РФ",
                context_hash="prev_hash",
            ))
            assert result["status"] == "ROUTE_REQUIRED"


class TestClearSessionCache:
    @pytest.mark.asyncio
    async def test_clears_both_caches(self):
        import src.server as srv
        srv.SESSION_CACHE["key1"] = "val1"
        srv.CONTEXT_HASH_CACHE["hash1"] = "agent1"

        result = await srv.clear_session_cache()

        assert len(srv.SESSION_CACHE) == 0
        assert len(srv.CONTEXT_HASH_CACHE) == 0
        assert "cleared" in result.lower()
