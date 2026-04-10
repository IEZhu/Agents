import asyncio
import logging
import os
from datetime import datetime, timezone
from typing import List, Optional, Dict, Any

from src.engine.config import ROUTER_SIMILARITY_THRESHOLD, AGENTS_DIR, DATA_DIR, EMBEDDING_MODEL
from src.engine.embedder import embed_query
from src.engine.vector_store import NumpyVectorStore
from src.schemas.protocol import RouterDecision, AgentRequest
from src.utils.langfuse_compat import observe
from src.utils.prompt_loader import split_frontmatter

logger = logging.getLogger(__name__)

ROUTER_CACHE_MAX_SIZE = 500
_ROUTER_MODEL_HASH_FILE = os.path.join(DATA_DIR, ".router_cache_model")


class SemanticRouter:
    def __init__(self):
        self.store = NumpyVectorStore(name="router_cache", data_dir=DATA_DIR)
        self._invalidate_on_model_change()

        self.available_agents = self._scan_agents()
        self._agent_descriptions = self._load_agent_descriptions()

    def _invalidate_on_model_change(self):
        """Clear router cache when the embedding model changes."""
        stored_model = ""
        try:
            if os.path.exists(_ROUTER_MODEL_HASH_FILE):
                with open(_ROUTER_MODEL_HASH_FILE, "r") as f:
                    stored_model = f.read().strip()
        except Exception:
            pass

        if stored_model != EMBEDDING_MODEL:
            if self.store.count() > 0:
                logger.info(
                    "Embedding model changed (%s → %s), clearing router cache",
                    stored_model or "<none>", EMBEDDING_MODEL,
                )
                self.store.clear()
                self.store.save()
            os.makedirs(DATA_DIR, exist_ok=True)
            with open(_ROUTER_MODEL_HASH_FILE, "w") as f:
                f.write(EMBEDDING_MODEL)

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
                    continue
            except Exception as e:
                logger.warning(f"Failed to load metadata for {name}: {e}")
            descriptions[name] = {"display_name": name, "role": "", "trigger_command": ""}
        return descriptions

    def get_agent_catalog(self) -> List[Dict[str, str]]:
        """Returns agent list with descriptions for candidate selection."""
        return [
            {"name": name, **self._agent_descriptions.get(name, {"display_name": name, "role": ""})}
            for name in self.available_agents
        ]

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
        results = await loop.run_in_executor(
            None, lambda: self.store.query(query_embedding=query_emb, n_results=1)
        )

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
