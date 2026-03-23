import logging
import os
import json
import asyncio
from datetime import datetime, timezone
from src.utils.langfuse_compat import observe

logger = logging.getLogger(__name__)
from typing import List, Optional, Dict, Any

from src.schemas.protocol import RouterDecision, AgentRequest
from src.engine.config import REPO_ROOT, ROUTER_SIMILARITY_THRESHOLD
from src.engine.chroma import get_chroma_client, get_embedding_fn

class SemanticRouter:
    def __init__(self):
        self.chroma_client = get_chroma_client()
        self.embedding_fn = get_embedding_fn()

        self.collection = self.chroma_client.get_or_create_collection(
            name="router_cache",
            embedding_function=self.embedding_fn,
            metadata={"hnsw:space": "cosine"}
        )

        self.available_agents = self._scan_agents()
        self._agent_descriptions = self._load_agent_descriptions()

    def _scan_agents(self) -> List[str]:
        """
        Dynamically scans the .cursor/agents directory for available agents.
        """
        agents_dir = os.path.join(REPO_ROOT, ".cursor", "agents")
        if not os.path.exists(agents_dir):
            logger.warning(f"Agents directory not found at {agents_dir}. Fallback to empty list.")
            return []

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
            path = os.path.join(REPO_ROOT, ".cursor", "agents", name, "system_prompt.mdc")
            try:
                with open(path, "r", encoding="utf-8") as f:
                    content = f.read()
                if content.startswith("---"):
                    parts = content.split("---", 2)
                    if len(parts) >= 2:
                        meta = yaml.safe_load(parts[1]) or {}
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

    async def lookup_cache(self, query: str, context: Dict[str, Any] = None) -> Optional[RouterDecision]:
        """
        Only checks the cache. Returns None if miss.
        """
        history_text = context.get("history_text", "") if context else ""

        if history_text:
             # Create a context-aware query for semantic search
             semantic_query = f"{history_text[-200:]}\n{query}" # Last 200 chars of context
        else:
             semantic_query = query

        # ChromaDB query is blocking, run in executor
        loop = asyncio.get_running_loop()
        try:
            results = await loop.run_in_executor(
                None,
                lambda: self.collection.query(
                    query_texts=[semantic_query],
                    n_results=1
                )
            )
        except Exception as e:
            logger.error(f"ChromaDB lookup failed: {e}")
            return None

        if results['ids'] and results['distances'] and len(results['distances'][0]) > 0:
            distance = results['distances'][0][0]
            if distance < (1 - ROUTER_SIMILARITY_THRESHOLD):
                metadata = results['metadatas'][0][0]
                return RouterDecision(
                    target_agent=metadata['target_agent'],
                    confidence=1.0,
                    reasoning=f"Cached result (distance: {distance:.4f})",
                    is_cached=True
                )
        return None

    async def update_cache(self, query: str, agent_name: str, reasoning: str, request_id: str):
        """
        Manually adds an entry to the cache.
        """
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None,
                lambda: self.collection.add(
                    documents=[query],
                    metadatas=[{
                        "target_agent": agent_name,
                        "reasoning": reasoning,
                        "timestamp": datetime.now(timezone.utc).isoformat()
                    }],
                    ids=[request_id]
                )
            )
        except Exception as e:
            logger.error(f"Failed to update cache: {e}")

    @observe(name="route_request")
    async def route(self, request: AgentRequest) -> Optional[RouterDecision]:
        """
        Routing logic: Cache lookup only.
        Returns None on cache miss — caller should return ROUTE_REQUIRED to the client LLM.
        """
        cached = await self.lookup_cache(request.query, request.context)
        return cached
