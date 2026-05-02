import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any, Tuple

from src.engine.config import (
    ROUTER_SIMILARITY_THRESHOLD, AGENTS_DIR, DATA_DIR, EMBEDDING_MODEL,
    KEYWORD_OVERRIDE_MIN_HITS, KEYWORD_UNIQUENESS_RATIO,
)
from src.engine.embedder import embed_query
from src.engine.vector_store import NumpyVectorStore
from src.schemas.protocol import RouterDecision, AgentRequest
from src.utils.langfuse_compat import observe
from src.utils.prompt_loader import split_frontmatter

logger = logging.getLogger(__name__)

ROUTER_CACHE_MAX_SIZE = 500
KEYWORD_VETO_ROUTE_REQUIRED = "__ROUTE_REQUIRED__"
_ROUTER_MODEL_HASH_FILE = os.path.join(DATA_DIR, ".router_cache_model")
# Substring in the ValueError raised by NumpyVectorStore.query() when the
# query vector and stored vectors disagree on dimensionality. Matched in
# query_nearest() to trigger lazy self-heal.
_DIM_MISMATCH_ERROR_FRAGMENT = "Dimension mismatch"


class SemanticRouter:
    def __init__(self):
        self.store = NumpyVectorStore(name="router_cache", data_dir=DATA_DIR)
        self._invalidate_on_model_change()

        self.available_agents = self._scan_agents()
        self._agent_keywords: Dict[str, List[str]] = {}
        self._agent_descriptions = self._load_agent_descriptions()

    def _invalidate_on_model_change(self):
        """Clear router cache when the model name changes or the marker's
        recorded dim drifts from the on-disk vector dim.

        Marker format: ``<model>|<dim>`` (current) or ``<model>`` (legacy,
        accepted on read and upgraded on next save). A name match alone does
        not guarantee correctness — the cache may have been written by a
        different model whose marker was later overwritten — so we also
        compare the marker's dim against the actual stored dim. Reading the
        npz dim is cheap (already loaded by NumpyVectorStore), so this check
        runs without loading the embedder. Queries against a stale cache
        that still slips past this check self-heal in ``query_nearest``.
        """
        stored_model, stored_dim = self._read_marker()
        cache_dim = self.store.dim()

        name_changed = stored_model != EMBEDDING_MODEL
        dim_drifted = (
            stored_dim is not None
            and cache_dim is not None
            and stored_dim != cache_dim
        )

        if name_changed or dim_drifted:
            if self.store.count() > 0:
                logger.info(
                    "Clearing router cache (model: %r → %r, dim_drifted=%s)",
                    stored_model or "<none>", EMBEDDING_MODEL, dim_drifted,
                )
                self.store.clear()
                self.store.save()
            self._write_marker(EMBEDDING_MODEL, None)

    @staticmethod
    def _read_marker() -> Tuple[str, Optional[int]]:
        """Parse the marker file. Returns (model_name, dim). dim is None for
        legacy markers, missing files, or unparseable values."""
        try:
            if not os.path.exists(_ROUTER_MODEL_HASH_FILE):
                return "", None
            with open(_ROUTER_MODEL_HASH_FILE, "r") as f:
                raw = f.read().strip()
        except OSError:
            return "", None
        if "|" not in raw:
            return raw, None
        name, _, dim_str = raw.partition("|")
        try:
            return name, int(dim_str)
        except ValueError:
            return name, None

    @staticmethod
    def _write_marker(model_name: str, dim: Optional[int]) -> None:
        """Persist (model_name, dim) to the marker. Empty caches write only
        the model name so the legacy format is the natural rest state."""
        os.makedirs(DATA_DIR, exist_ok=True)
        payload = f"{model_name}|{dim}" if dim is not None else model_name
        with open(_ROUTER_MODEL_HASH_FILE, "w") as f:
            f.write(payload)

    def _wipe_and_remark(self) -> None:
        """Drop all cached entries and reset the marker. Used by the lazy
        self-heal path in ``query_nearest`` when a query exposes a dim
        mismatch the init-time check could not detect."""
        self.store.clear()
        self.store.save()
        self._write_marker(EMBEDDING_MODEL, None)

    def _scan_agents(self) -> List[str]:
        """
        Dynamically scans the agents directory for available agents.
        """
        agents_dir = AGENTS_DIR
        if not os.path.exists(agents_dir):
            logger.warning(f"Agents directory not found at {agents_dir}. Fallback to universal_agent.")
            return ["universal_agent"]

        agents = []
        try:
            for entry in os.scandir(agents_dir):
                if entry.is_dir() and not entry.name.startswith(".") and entry.name != "common":
                    # Check if it has a system_prompt.mdc
                    if os.path.exists(os.path.join(entry.path, "system_prompt.mdc")):
                        agents.append(entry.name)
        except Exception as e:
            logger.error(f"Error scanning agents: {e}")

        # Ensure common agents are present if scan fails or directory is structured differently
        if not agents:
            # Fallback to minimal agent if scan completely fails
            logger.warning("Agent scan returned empty. Falling back to universal_agent only.")
            return ["universal_agent"]

        return sorted(agents)

    def _load_agent_descriptions(self) -> Dict[str, Dict[str, str]]:
        """Load display_name and role for each agent from frontmatter."""
        import yaml
        descriptions = {}
        for name in self.available_agents:
            path = os.path.join(AGENTS_DIR, name, "system_prompt.mdc")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                fm_str, _ = split_frontmatter(content)
                if fm_str is not None:
                    meta = yaml.safe_load(fm_str) or {}
                    identity = meta.get("identity", {})
                    routing = meta.get("routing", {})
                    descriptions[name] = {
                        "display_name": identity.get("display_name", name),
                        "role": identity.get("role", ""),
                        "trigger_command": routing.get("trigger_command", ""),
                    }
                    # Load domain_keywords (exclude universal_agent — its generic
                    # keywords like "general", "plan" should never outcompete specialists)
                    if name != "universal_agent":
                        raw_kw = routing.get("domain_keywords", [])
                        self._agent_keywords[name] = [
                            kw.lower() for kw in raw_kw if isinstance(kw, str)
                        ]
                    else:
                        self._agent_keywords[name] = []
                    continue
            except Exception as e:
                logger.warning(f"Failed to load metadata for {name}: {e}")
            descriptions[name] = {"display_name": name, "role": "", "trigger_command": ""}
            self._agent_keywords[name] = []
        return descriptions

    def get_agent_catalog(self) -> List[Dict[str, str]]:
        """Returns agent list with descriptions for candidate selection."""
        return [
            {"name": name, **self._agent_descriptions.get(name, {"display_name": name, "role": ""})}
            for name in self.available_agents
        ]

    @staticmethod
    def _is_significant_token(token: str) -> bool:
        """A token is significant for fallback matching if it's either
        long enough (>= 4 chars) or a short abbreviation/acronym containing
        at least one letter (e.g. "рф", "ip", "3d", "ai"). Single-char
        tokens and pure-digit tokens are excluded."""
        if len(token) >= 4:
            return True
        return len(token) >= 2 and token.isalnum() and any(c.isalpha() for c in token)

    @staticmethod
    def _token_in_query(token: str, query_lower: str) -> bool:
        """Check if token appears in query. Short tokens (< 4 chars) require
        word-boundary matching to avoid false positives like 'ux' inside
        'auxiliary'. Longer tokens use plain substring matching to support
        inflected forms."""
        if len(token) >= 4:
            return token in query_lower
        # Short token: require word boundary (space, start/end of string, or punctuation)
        import re
        return bool(re.search(r'(?<!\w)' + re.escape(token) + r'(?!\w)', query_lower))

    def match_keywords(self, query: str) -> list[tuple[str, int]]:
        """Match query against each agent's domain_keywords.

        For each keyword, tries exact substring match first. If that fails,
        extracts significant tokens (>= 4 chars or short alphanumeric
        abbreviations like "рф", "ip", "3d") and requires ALL of them to
        appear in the query. Short tokens (< 4 chars) use word-boundary
        matching to prevent false positives (e.g. "ux" inside "auxiliary").

        Returns [(agent_name, hit_count), ...] sorted by hits descending,
        filtered to agents with at least 1 hit.
        """
        query_lower = query.lower()
        matches: list[tuple[str, int]] = []
        for agent_name, keywords in self._agent_keywords.items():
            hits = 0
            for kw in keywords:
                # Short keywords (< 4 chars) use word-boundary matching to prevent
                # false positives like "ux" inside "auxiliary".
                if self._token_in_query(kw, query_lower):
                    hits += 1
                    continue
                # Token fallback: ALL significant tokens must match.
                tokens = [t for t in kw.split() if self._is_significant_token(t)]
                if tokens and all(self._token_in_query(t, query_lower) for t in tokens):
                    hits += 1
            if hits > 0:
                matches.append((agent_name, hits))
        matches.sort(key=lambda x: x[1], reverse=True)
        return matches

    def keyword_veto(self, query: str, cached_agent: str) -> Optional[str]:
        """Check if domain_keywords contradict the cached agent decision.

        Returns:
            None                        — keywords confirm or don't contradict the cache
            agent_name                  — keywords strongly indicate a different agent (override)
            KEYWORD_VETO_ROUTE_REQUIRED — keywords point elsewhere but ambiguously
        """
        matches = self.match_keywords(query)
        if not matches:
            return None

        top_agent, top_hits = matches[0]

        # If cached_agent is tied with the top (same hit count), confirm it —
        # don't let alphabetical sort order determine the outcome.
        if top_agent == cached_agent:
            return None
        cached_hits = next((h for a, h in matches if a == cached_agent), 0)
        if cached_hits == top_hits:
            return None

        if top_hits < KEYWORD_OVERRIDE_MIN_HITS:
            return None

        second_hits = matches[1][1] if len(matches) > 1 else 0

        if second_hits == 0 or (top_hits / max(second_hits, 1)) >= KEYWORD_UNIQUENESS_RATIO:
            return top_agent
        return KEYWORD_VETO_ROUTE_REQUIRED

    async def query_nearest(
        self, query: str, context: Dict[str, Any] = None
    ) -> Optional[tuple[RouterDecision, float]]:
        """
        Returns the nearest cached entry as (decision, distance), or None if
        the cache has no entries. No threshold is applied — the caller decides
        what to do with the distance.

        Raises on errors so callers can distinguish empty cache from
        lookup failure and handle each appropriately.
        """
        if self.store.count() == 0:
            return None

        history_text = context.get("history_text", "") if context else ""
        semantic_query = f"{history_text[-200:]}\n{query}" if history_text else query

        loop = asyncio.get_running_loop()
        query_emb = await loop.run_in_executor(None, embed_query, semantic_query)
        try:
            results = await loop.run_in_executor(
                None, lambda: self.store.query(query_embedding=query_emb, n_results=1)
            )
        except ValueError as e:
            # Self-heal a stale cache whose vectors disagree with the current
            # embedder's dim. The init-time marker check catches drift the
            # codebase wrote, but a marker and npz that agree with each other
            # but not with the embedder (e.g. cache restored from a backup
            # taken under a different model) only surface here. Wipe, log,
            # and let the query miss — the next update will repopulate.
            if _DIM_MISMATCH_ERROR_FRAGMENT in str(e):
                logger.warning(
                    "Router cache dim mismatch at query time, wiping: %s", e
                )
                await loop.run_in_executor(None, self._wipe_and_remark)
                return None
            raise

        if results.ids and results.distances and len(results.distances) > 0:
            distance = results.distances[0]
            metadata = results.metadatas[0]
            decision = RouterDecision(
                target_agent=metadata["target_agent"],
                confidence=1.0,
                reasoning=f"Cached result (distance: {distance:.4f})",
                is_cached=True,
            )
            return decision, distance
        return None

    async def lookup_cache_with_distance(
        self, query: str, context: Dict[str, Any] = None
    ) -> Optional[tuple[RouterDecision, float]]:
        """Returns (decision, distance) only if distance passes the similarity threshold."""
        try:
            result = await self.query_nearest(query, context)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("Vector store lookup failed: %s", e, exc_info=True)
            return None
        if result and result[1] < (1 - ROUTER_SIMILARITY_THRESHOLD):
            return result
        return None

    async def lookup_cache(self, query: str, context: Dict[str, Any] = None) -> Optional[RouterDecision]:
        """Only checks the cache with threshold. Returns None if miss."""
        result = await self.lookup_cache_with_distance(query, context)
        return result[0] if result else None

    async def update_cache(self, query: str, agent_name: str, reasoning: str, request_id: str):
        """
        Manually adds an entry to the cache.
        """
        loop = asyncio.get_running_loop()
        try:
            query_emb = await loop.run_in_executor(None, embed_query, query)

            def _mutate_and_save():
                self.store.add(
                    ids=[request_id],
                    embeddings=query_emb.reshape(1, -1),
                    documents=[query],
                    metadatas=[{
                        "target_agent": agent_name,
                        "reasoning": reasoning,
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }],
                )
                self.store.trim(ROUTER_CACHE_MAX_SIZE)
                self.store.save()
                # Keep the marker in sync with cache contents so the next
                # startup can detect drift without loading the embedder.
                self._write_marker(EMBEDDING_MODEL, self.store.dim())

            await loop.run_in_executor(None, _mutate_and_save)
        except Exception as e:
            logger.error("Failed to update cache: %s", e, exc_info=True)

    @observe(name="route_request")
    async def route(self, request: AgentRequest) -> Optional[RouterDecision]:
        """
        Routing logic: Cache lookup only.
        Returns None on cache miss — caller should return ROUTE_REQUIRED to the client LLM.
        """
        cached = await self.lookup_cache(request.query, request.context)
        return cached
