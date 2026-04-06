"""
Tests for routing logic: _is_meta_query, _normalize_chat_history,
and sticky agent routing in route_and_load.

Queries are multilingual (EN, RU, DE, ES, FR) and depersonalized.
"""

import json
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from cachetools import TTLCache

from src.server import _is_meta_query, _normalize_chat_history


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

        srv.CONTEXT_HASH_CACHE = TTLCache(maxsize=128, ttl=600)
        srv.SESSION_CACHE = TTLCache(maxsize=128, ttl=600)

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

        with patch.object(self.srv.router, "_query_nearest",
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

        with patch.object(self.srv.router, "_query_nearest",
                          new_callable=AsyncMock, return_value=(cached, 0.01)), \
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

        with patch.object(self.srv.router, "_query_nearest",
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

        with patch.object(self.srv.router, "_query_nearest",
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

        with patch.object(self.srv.router, "_query_nearest",
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

        with patch.object(self.srv.router, "_query_nearest",
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
# clear_session_cache clears both caches
# ---------------------------------------------------------------------------

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
