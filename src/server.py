import atexit
import hashlib
import logging
import uuid
import os
import sys
import re
import json
import asyncio
import dotenv
from cachetools import TTLCache
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Context
from mcp.types import SamplingMessage, TextContent
from typing import Optional, List

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-server")

# Load env vars
env_path = os.path.join(os.path.dirname(__file__), "../.env")
dotenv.load_dotenv(env_path)

# Langfuse is optional — server works without keys

from src.engine.router import SemanticRouter, KEYWORD_VETO_ROUTE_REQUIRED
from src.engine.enrichment import (
    enrich_agent_prompt,
    infer_tier,
    implant_retriever,
)
from src.engine.config import SESSION_CACHE_MAX_SIZE, SESSION_CACHE_TTL_SECONDS, STICKY_SWITCH_THRESHOLD, ROUTER_SIMILARITY_THRESHOLD, REPO_ROOT
from src.utils.prompt_loader import load_agent_prompt, get_agent_metadata
from src.utils.debug_logger import debug_log
from src.memory.describer import RepoDescriber
from src.memory.history import HistoryReader, HistoryStore, HistoryWriter

# Cached instance — avoids reloading .npz from disk on every read_history call.
# HistoryStore.ensure_index() handles mtime-based staleness internally.
_history_store = HistoryStore()

mcp = FastMCP(
    "Agents-Core",
    instructions=(
        "CRITICAL: Agents-Core is a multi-agent routing system.\n"
        "You MUST call `route_and_load(query)` BEFORE answering ANY user query.\n"
        "This is a BLOCKING REQUIREMENT — do NOT answer without routing first.\n\n"
        "Response statuses:\n"
        "- SUCCESS_SAMPLED → display `response` as-is (ready-made agent answer).\n"
        "- SUCCESS → use `system_prompt` as context for your answer.\n"
        "- ROUTE_REQUIRED → pick best agent from `candidates`, call `get_agent_context(agent_name, query)`.\n"
        "- NO_CHANGE → context unchanged, continue.\n"
        "- ERROR → answer directly (only fallback).\n\n"
        "Respond in the same language as the user's query (auto-detect). "
        "Exceptions: code blocks, technical terms, and tool/CLI output stay in English.\n"
        "Append at the end: **Agent**: [name] · **Skills**: [skills] · **Implants**: [implants]"
    ),
)

router = SemanticRouter()

SESSION_CACHE: TTLCache = TTLCache(
    maxsize=SESSION_CACHE_MAX_SIZE,
    ttl=SESSION_CACHE_TTL_SECONDS,
)

from src.utils.langfuse_compat import observe, get_langfuse, is_langfuse_configured
langfuse = get_langfuse()
atexit.register(langfuse.flush)

# --- Tools ---

@mcp.tool()
async def clear_session_cache() -> str:
    """Clears the session cache and sticky agent mappings. Use when switching contexts."""
    SESSION_CACHE.clear()
    CONTEXT_HASH_CACHE.clear()
    return "Session cache and sticky agent mappings cleared"

_META_QUERY_RE = re.compile(
    r"^("
    # English greetings and meta
    r"h(ello|i|ey)\b|what (tools|can you)|help\b|who are you|what are you|introduce yourself"
    r"|test\b"
    # Russian greetings and meta
    r"|привет|здравствуй|что (ты умеешь|можешь)|помоги|кто ты|какие (у тебя|есть) (инструменты|агенты)"
    r")",
    re.IGNORECASE,
)

def _is_meta_query(query: str) -> bool:
    query_stripped = query.strip()
    if len(query_stripped) < 10:
        return True
    return bool(_META_QUERY_RE.search(query_stripped))

def _normalize_chat_history(chat_history: Optional[List[str] | str]) -> List[str]:
    """
    Accept both the documented list payload and a legacy/invalid empty string.
    This keeps MCP tools resilient when callers serialize "no history" as "".
    """
    if chat_history is None:
        return []

    if isinstance(chat_history, str):
        normalized = chat_history.strip()
        return [normalized] if normalized else []

    return [entry for entry in chat_history if isinstance(entry, str)]

CONTEXT_HASH_CACHE: TTLCache = TTLCache(maxsize=SESSION_CACHE_MAX_SIZE, ttl=SESSION_CACHE_TTL_SECONDS)

def _compute_context_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]

_ROUTE_REQUIRED_INSTRUCTION = (
    "CRITICAL: You MUST call get_agent_context(agent_name, query) RIGHT NOW as your ONLY next action. "
    "Do NOT call any other tools. Do NOT use Agent, Bash, Read, Grep, or any tool in parallel. "
    "Do NOT explore the codebase. Do NOT answer the user. "
    "FIRST pick the single best agent from candidates, THEN call ONLY: "
    "get_agent_context(agent_name=\"<chosen>\", query=\"<original query>\"). "
    "Wait for its response. Only THEN proceed with the user's request."
)

def _build_route_required(request_id: str, tier: str, candidates: list) -> str:
    """Build a ROUTE_REQUIRED JSON response. Single source of truth for this payload."""
    result = {
        "status": "ROUTE_REQUIRED",
        "request_id": request_id,
        "tier": tier,
        "candidates": candidates,
        "instruction": _ROUTE_REQUIRED_INSTRUCTION,
    }
    debug_log("route_and_load", "res", result)
    return json.dumps(result, ensure_ascii=False)

async def _load_and_enrich(agent_name: str, query: str, chat_history_list: List[str], tier: str | None = None) -> tuple[str, str, list[str], list[str], str]:
    """Shared helper: load prompt, enrich with skills/implants/capabilities.
    Returns (final_prompt, context_hash, skills_loaded, implants_loaded, effective_tier).
    """
    tier_explicit = tier is not None
    if tier is None:
        tier = infer_tier(query)

    loop = asyncio.get_running_loop()
    metadata = await loop.run_in_executor(None, get_agent_metadata, agent_name)
    preferred_skills = metadata.get("preferred_skills", [])
    preferred_implants = metadata.get("preferred_implants", [])
    capabilities = metadata.get("capabilities", [])

    # Promote tier to at least "standard" when agent declares skills/capabilities/implants,
    # but only if tier was inferred (not explicitly set by the caller)
    if not tier_explicit and tier == "lite" and (preferred_skills or capabilities or preferred_implants):
        tier = "standard"
        logger.info(f"Tier promoted to 'standard' for {agent_name} (has preferred_skills, capabilities, or preferred_implants)")

    query_hash = hash(query)
    cache_key = f"{agent_name}:{query_hash}:{tier}"
    if cache_key in SESSION_CACHE:
        cached = SESSION_CACHE[cache_key]
        if isinstance(cached, tuple):
            prompt, skills_loaded, implants_loaded = cached
        else:
            prompt, skills_loaded, implants_loaded = cached, [], []
        logger.info(f"Session cache hit for {agent_name} (tier={tier})")
        debug_log("_load_and_enrich", "res", {"agent": agent_name, "tier": tier, "cache": "hit", "prompt_len": len(prompt)})
        return prompt, _compute_context_hash(prompt), skills_loaded, implants_loaded, tier

    base_prompt = await loop.run_in_executor(None, load_agent_prompt, agent_name)

    enrichment = await enrich_agent_prompt(
        agent_name, base_prompt, query, chat_history_list,
        preferred_skills, tier,
        capabilities=capabilities,
        preferred_implants=preferred_implants,
    )
    final_prompt = enrichment.prompt
    SESSION_CACHE[cache_key] = (final_prompt, enrichment.skills_loaded, enrichment.implants_loaded)
    ctx_hash = _compute_context_hash(final_prompt)
    CONTEXT_HASH_CACHE[ctx_hash] = agent_name
    debug_log("_load_and_enrich", "res", {
        "agent": agent_name, "tier": tier, "cache": "miss",
        "prompt_len": len(final_prompt), "preferred_skills": preferred_skills,
        "preferred_implants": preferred_implants, "capabilities": capabilities,
        "skills_loaded": enrichment.skills_loaded,
        "implants_loaded": enrichment.implants_loaded,
    })
    return final_prompt, ctx_hash, enrichment.skills_loaded, enrichment.implants_loaded, tier


async def _sample_with_agent(ctx: Context, system_prompt: str, query: str) -> str:
    """Use MCP sampling to generate a response with the agent's system prompt.

    The client (Claude Desktop / Claude Code) executes the LLM call —
    no API key needed on the server side. The systemPrompt is delivered
    as a proper system prompt field, not as tool result text.
    """
    debug_log("_sample_with_agent", "req", {"query": query, "system_prompt_len": len(system_prompt)})
    result = await ctx.session.create_message(
        messages=[SamplingMessage(
            role="user",
            content=TextContent(type="text", text=query),
        )],
        system_prompt=system_prompt,
        max_tokens=4096,
    )
    # Extract text from the sampling result
    if hasattr(result.content, "text"):
        text = result.content.text
    elif isinstance(result.content, list):
        text = "\n".join(
            block.text for block in result.content if hasattr(block, "text")
        )
    else:
        text = str(result.content)
    debug_log("_sample_with_agent", "res", {"response_len": len(text), "response_preview": text[:500]})
    return text


@mcp.tool()
@observe(name="route_and_load")
async def route_and_load(
    query: str,
    chat_history: Optional[List[str] | str] = None,
    context_hash: Optional[str] = None,
    ctx: Context | None = None,
) -> str:
    """
    Route a user query to the best specialist agent. Call this BEFORE answering ANY query.

    Response statuses:
    - SUCCESS_SAMPLED → Display `response` to the user as-is. Do not modify.
    - SUCCESS → Use `system_prompt` as context for your answer.
    - ROUTE_REQUIRED → You MUST immediately call get_agent_context(agent_name, query)
      with the best agent from `candidates`. Do NOT answer without completing this step.
    - NO_CHANGE → Context unchanged, continue with current persona.
    - ERROR → Answer directly (only fallback).

    Respond in the same language as the user's query (auto-detect).
    Exceptions: code blocks, technical terms, and tool/CLI output stay in English.
    Append at the end: **Agent**: [name] · **Skills**: [skills] · **Implants**: [implants]
    Pass `context_hash` from a previous response to enable delta mode.
    When context_hash maps to a previously loaded agent, sticky routing is activated:
    the router prefers keeping the current agent unless a very strong semantic signal
    (distance < STICKY_SWITCH_THRESHOLD) suggests a different one.
    """
    try:
        chat_history_list = _normalize_chat_history(chat_history)
        history_text = "\n".join(chat_history_list)
        request_id = str(uuid.uuid4())
        tier = infer_tier(query)
        debug_log("route_and_load", "req", {
            "query": query, "tier": tier, "context_hash": context_hash,
            "history_len": len(chat_history_list), "request_id": request_id,
        })

        # 1. Sticky agent: if we already have an active agent, prefer keeping it
        explicit_tier = None  # only set for intentional overrides (e.g. meta-query)
        should_cache = True  # skip caching for unvalidated sticky decisions
        sticky_agent = CONTEXT_HASH_CACHE.get(context_hash) if context_hash else None

        if sticky_agent:
            logger.info(f"Sticky agent active: {sticky_agent} (hash={context_hash})")
            # Meta-queries always override sticky state
            if _is_meta_query(query):
                agent_name = "universal_agent"
                reasoning = "Auto-fallback: meta-query overrides sticky agent"
                explicit_tier = "lite"
            else:
                # Use unfiltered nearest match to distinguish "empty cache" from "topic change".
                # query_nearest raises on vector store errors — release to ROUTE_REQUIRED on failure.
                lookup_failed = False
                try:
                    nearest = await router.query_nearest(query, {"history_text": history_text})
                except asyncio.CancelledError:
                    raise
                except Exception as e:
                    logger.error(f"Sticky lookup failed, releasing to ROUTE_REQUIRED: {e}")
                    debug_log("route_and_load", "sticky", {"action": "release", "reason": "lookup_error", "from": sticky_agent, "error": str(e)})
                    nearest = None
                    lookup_failed = True

                distance_threshold = 1 - ROUTER_SIMILARITY_THRESHOLD

                if lookup_failed:
                    # DB error — release to ROUTE_REQUIRED so LLM can decide
                    return _build_route_required(request_id, tier, router.get_agent_catalog())
                elif nearest is None:
                    # Cache is genuinely empty — keep current agent, don't cache
                    agent_name = sticky_agent
                    reasoning = "Sticky: cache empty, keeping current agent"
                    should_cache = False
                    debug_log("route_and_load", "sticky", {"action": "keep", "reason": "empty_cache", "agent": agent_name})
                elif nearest[1] < STICKY_SWITCH_THRESHOLD and nearest[0].target_agent != sticky_agent:
                    # Very strong signal for a different agent — validate with keywords
                    switch_target = nearest[0].target_agent
                    kw_veto = router.keyword_veto(query, switch_target)
                    if kw_veto == KEYWORD_VETO_ROUTE_REQUIRED:
                        debug_log("route_and_load", "sticky", {"action": "release", "reason": "keyword_ambiguous_autoswitch", "from": sticky_agent, "switch_target": switch_target, "distance": nearest[1]})
                        return _build_route_required(request_id, tier, router.get_agent_catalog())
                    elif kw_veto and kw_veto != switch_target:
                        agent_name = kw_veto
                        reasoning = f"Keyword override (auto-switch): {switch_target} -> {kw_veto} (d={nearest[1]:.4f})"
                        logger.info(f"Keyword override in auto-switch: {sticky_agent} → {agent_name} (cache suggested {switch_target}, d={nearest[1]:.4f})")
                        debug_log("route_and_load", "sticky", {"action": "keyword_override", "from": sticky_agent, "to": agent_name, "cache_target": switch_target, "distance": nearest[1]})
                    else:
                        agent_name = switch_target
                        reasoning = f"Auto-switch from {sticky_agent}: strong signal (d={nearest[1]:.4f})"
                        logger.info(f"Sticky auto-switch: {sticky_agent} → {agent_name} (d={nearest[1]:.4f})")
                        debug_log("route_and_load", "sticky", {"action": "switch", "from": sticky_agent, "to": agent_name, "distance": nearest[1]})
                elif nearest[1] < distance_threshold and nearest[0].target_agent == sticky_agent:
                    # Cache confirms the same agent — but check keywords
                    kw_veto = router.keyword_veto(query, sticky_agent)
                    if kw_veto and kw_veto != KEYWORD_VETO_ROUTE_REQUIRED:
                        agent_name = kw_veto
                        reasoning = f"Keyword override (sticky): {sticky_agent} -> {kw_veto} (d={nearest[1]:.4f})"
                        logger.info("Keyword override in sticky: %s -> %s", sticky_agent, kw_veto)
                        debug_log("route_and_load", "sticky", {"action": "keyword_override", "from": sticky_agent, "to": kw_veto, "distance": nearest[1]})
                    elif kw_veto == KEYWORD_VETO_ROUTE_REQUIRED:
                        debug_log("route_and_load", "sticky", {"action": "release", "reason": "keyword_ambiguous", "from": sticky_agent, "distance": nearest[1]})
                        return _build_route_required(request_id, tier, router.get_agent_catalog())
                    else:
                        agent_name = sticky_agent
                        reasoning = f"Sticky: confirmed by cache (d={nearest[1]:.4f})"
                        debug_log("route_and_load", "sticky", {"action": "keep", "reason": "same_agent", "agent": agent_name, "distance": nearest[1]})
                elif nearest[1] >= distance_threshold:
                    # Query is far from anything cached — likely a topic change.
                    # Go straight to ROUTE_REQUIRED so the LLM can pick the right agent.
                    debug_log("route_and_load", "sticky", {"action": "release", "reason": "topic_change", "from": sticky_agent, "distance": nearest[1]})
                    return _build_route_required(request_id, tier, router.get_agent_catalog())
                else:
                    # Close match for a different agent, but not strong enough to auto-switch.
                    # Keep sticky agent for stability, don't cache.
                    agent_name = sticky_agent
                    reasoning = f"Sticky: kept despite competing signal for {nearest[0].target_agent} (d={nearest[1]:.4f})"
                    should_cache = False
                    debug_log("route_and_load", "sticky", {"action": "keep", "reason": "weak_signal", "agent": agent_name, "competing": nearest[0].target_agent, "distance": nearest[1]})
        else:
            # 2. No sticky agent — standard routing (cache → keyword veto → meta-query → ROUTE_REQUIRED)
            cached_decision = await router.lookup_cache(query, {"history_text": history_text})
            if cached_decision:
                veto = router.keyword_veto(query, cached_decision.target_agent)
                if veto is None:
                    agent_name = cached_decision.target_agent
                    reasoning = cached_decision.reasoning
                elif veto == KEYWORD_VETO_ROUTE_REQUIRED:
                    logger.info("Keyword veto: ambiguous override for cached %s", cached_decision.target_agent)
                    debug_log("route_and_load", "keyword_veto", {
                        "action": "route_required", "cached_agent": cached_decision.target_agent, "query": query,
                    })
                    return _build_route_required(request_id, tier, router.get_agent_catalog())
                else:
                    agent_name = veto
                    reasoning = f"Keyword override: {cached_decision.target_agent} -> {veto}"
                    logger.info("Keyword override: %s -> %s for query: %.80s", cached_decision.target_agent, veto, query)
                    debug_log("route_and_load", "keyword_veto", {
                        "action": "override", "from": cached_decision.target_agent, "to": veto, "query": query,
                    })
            elif _is_meta_query(query):
                agent_name = "universal_agent"
                reasoning = "Auto-fallback: meta-query detected"
                explicit_tier = "lite"
            else:
                # 3. Cache miss — return candidates for the calling LLM to decide
                return _build_route_required(request_id, tier, router.get_agent_catalog())

        # 3. Cache hit or meta-query — load enriched prompt
        # Pass explicit_tier only for meta-queries (preserves "lite"); None lets _load_and_enrich infer + promote
        final_prompt, new_hash, skills_loaded, implants_loaded, tier = await _load_and_enrich(agent_name, query, chat_history_list, explicit_tier)

        if context_hash and context_hash == new_hash:
            result = {
                "status": "NO_CHANGE",
                "agent": agent_name,
                "context_hash": new_hash,
                "request_id": request_id,
                "tier": tier,
                "skills_loaded": skills_loaded,
                "implants_loaded": implants_loaded,
            }
            debug_log("route_and_load", "res", result)
            return json.dumps(result, ensure_ascii=False)

        if should_cache:
            await router.update_cache(query, agent_name, reasoning, request_id)

        # Try sampling: generate response with agent's system prompt via client LLM
        if ctx:
            try:
                response = await _sample_with_agent(ctx, final_prompt, query)
                result = {
                    "status": "SUCCESS_SAMPLED",
                    "agent": agent_name,
                    "reasoning": reasoning,
                    "request_id": request_id,
                    "tier": tier,
                    "context_hash": new_hash,
                    "response": response,
                    "skills_loaded": skills_loaded,
                    "implants_loaded": implants_loaded,
                }
                debug_log("route_and_load", "res", result)
                return json.dumps(result, ensure_ascii=False)
            except Exception as sampling_err:
                logger.debug(f"Sampling not supported, falling back: {sampling_err}")
                debug_log("route_and_load", "sampling_error", {"error": str(sampling_err)})

        # Fallback: return system_prompt for clients without sampling support
        result = {
            "status": "SUCCESS",
            "agent": agent_name,
            "reasoning": reasoning,
            "request_id": request_id,
            "tier": tier,
            "context_hash": new_hash,
            "system_prompt": final_prompt,
            "skills_loaded": skills_loaded,
            "implants_loaded": implants_loaded,
        }
        debug_log("route_and_load", "res", result)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        result = {"status": "ERROR", "message": str(e)}
        debug_log("route_and_load", "error", result)
        return json.dumps(result, ensure_ascii=False)

@mcp.tool()
@observe(name="get_agent_context")
async def get_agent_context(agent_name: str, query: str, reasoning: str = "Selected by calling LLM", chat_history: Optional[List[str] | str] = None, ctx: Context | None = None) -> str:
    """
    Load a specific agent's system prompt. Call after route_and_load
    returned status=ROUTE_REQUIRED.

    Pick the best agent from the candidates list and pass its name here.
    If the client supports sampling, returns a ready-made response (SUCCESS_SAMPLED).
    Otherwise returns the system_prompt for you to use as context.
    Respond in the same language as the user's query (auto-detect).
    Exceptions: code blocks, technical terms, and tool/CLI output stay in English.
    Append at the end: **Agent**: [name] · **Skills**: [skills] · **Implants**: [implants]
    """
    try:
        chat_history_list = _normalize_chat_history(chat_history)
        request_id = str(uuid.uuid4())
        debug_log("get_agent_context", "req", {"agent_name": agent_name, "query": query, "reasoning": reasoning})

        final_prompt, ctx_hash, skills_loaded, implants_loaded, _ = await _load_and_enrich(agent_name, query, chat_history_list)
        await router.update_cache(query, agent_name, reasoning, request_id)

        # Try sampling: generate response with agent's system prompt via client LLM
        if ctx:
            try:
                response = await _sample_with_agent(ctx, final_prompt, query)
                result = {
                    "status": "SUCCESS_SAMPLED",
                    "agent": agent_name,
                    "request_id": request_id,
                    "context_hash": ctx_hash,
                    "response": response,
                    "skills_loaded": skills_loaded,
                    "implants_loaded": implants_loaded,
                }
                debug_log("get_agent_context", "res", result)
                return json.dumps(result, ensure_ascii=False)
            except Exception as sampling_err:
                logger.debug(f"Sampling not supported, falling back: {sampling_err}")
                debug_log("get_agent_context", "sampling_error", {"error": str(sampling_err)})

        # Fallback: return system_prompt for clients without sampling support
        result = {
            "status": "SUCCESS",
            "agent": agent_name,
            "request_id": request_id,
            "context_hash": ctx_hash,
            "system_prompt": final_prompt,
            "skills_loaded": skills_loaded,
            "implants_loaded": implants_loaded,
        }
        debug_log("get_agent_context", "res", result)
        return json.dumps(result, ensure_ascii=False)
    except Exception as e:
        result = {"status": "ERROR", "message": str(e)}
        debug_log("get_agent_context", "error", result)
        return json.dumps(result, ensure_ascii=False)

@mcp.tool()
@observe(name="load_implants")
async def load_implants(
    query: str = "",
    task_type: Optional[str] = None,
    limit: int = 5,
) -> str:
    """
    Load cognitive implants (mental models, reasoning strategies).
    Two modes — provide ONE of:
      - task_type: predefined bundle (debugging | analysis | creative | planning)
      - query: free-form semantic search across all implants

    task_type bundles:
      debugging  → chain-of-code, reflexion, react
      analysis   → step-back-prompting, chain-of-verification
      creative   → analogical-prompting, generated-knowledge
      planning   → plan-and-solve, skeleton-of-thought
    """
    TASK_IMPLANT_MAP = {
        "debugging": ["implant-chain-of-code", "implant-reflexion", "implant-react"],
        "analysis": ["implant-step-back-prompting", "implant-chain-of-verification"],
        "creative": ["implant-analogical-prompting", "implant-generated-knowledge"],
        "planning": ["implant-plan-and-solve-plus", "implant-skeleton-of-thought"],
    }

    loop = asyncio.get_running_loop()
    debug_log("load_implants", "req", {"query": query, "task_type": task_type, "limit": limit})

    try:
        if task_type:
            implant_names = TASK_IMPLANT_MAP.get(task_type)
            if not implant_names:
                return f"Unknown task_type: {task_type}. Valid: {', '.join(TASK_IMPLANT_MAP)}"

            target_ids = [
                f"{n}.mdc" if not n.endswith(".mdc") else n
                for n in implant_names
            ]
            results = await loop.run_in_executor(
                None,
                lambda: implant_retriever.store.get(ids=target_ids),
            )
            implants = [
                {
                    "filename": results.ids[i],
                    "content": results.documents[i],
                    "metadata": results.metadatas[i],
                    "distance": 0.0,
                }
                for i in range(len(results.ids))
            ]
        else:
            if not query:
                return "Provide either 'query' or 'task_type'."
            implants = await loop.run_in_executor(
                None,
                lambda: implant_retriever.retrieve(query=query, n_results=limit),
            )

        result = implant_retriever.format_implants_for_prompt(implants)
        debug_log("load_implants", "res", {"implant_count": len(implants), "result_len": len(result)})
        return result
    except Exception as e:
        logger.error(f"Failed to load implants: {e}")
        debug_log("load_implants", "error", {"error": str(e)})
        return f"Error loading implants: {str(e)}"

@mcp.tool()
async def list_agents(include_metadata: bool = True) -> str:
    """
    Returns the list of all available agents with optional metadata
    (display_name, role, trigger_command).
    Use this as a fallback when route_and_load is unavailable,
    or to present agent options to the user.
    """
    agents = router.available_agents
    if not include_metadata:
        return json.dumps({"agents": agents}, ensure_ascii=False)

    loop = asyncio.get_running_loop()
    catalog = []
    for name in agents:
        meta = await loop.run_in_executor(None, get_agent_metadata, name)
        identity = meta.get("identity", {})
        routing = meta.get("routing", {})
        catalog.append({
            "name": name,
            "display_name": identity.get("display_name", name),
            "role": identity.get("role", ""),
            "trigger_command": routing.get("trigger_command", ""),
        })

    return json.dumps({"agents": catalog}, ensure_ascii=False, indent=2)

@mcp.tool()
async def log_interaction(
    agent_name: str,
    query: str,
    response_content: str,
    request_id: Optional[str] = None,
    reasoning: Optional[str] = None,
    intent: Optional[str] = None,
    action: Optional[str] = None,
    outcome: Optional[str] = None,
    files: Optional[List[str]] = None,
    tags: Optional[List[str]] = None,
) -> str:
    """End-of-turn logger. ALWAYS call at the end of every turn.

    Two sinks, independent of each other:

    * **history.md** — always written via ``HistoryWriter`` (append-only,
      content-hash deduped). Defaults: ``intent=query``, ``action="Agent: {agent_name}"``,
      ``outcome=response_content``. Pass ``intent``/``action``/``outcome``/``files``/``tags``
      to curate the entry; otherwise raw query/response are used.

    * **Langfuse** — generation trace recorded only when ``LANGFUSE_PUBLIC_KEY`` /
      ``LANGFUSE_SECRET_KEY`` are configured; otherwise the call is a no-op via
      ``langfuse_compat``.

    Returns JSON: ``{request_id, langfuse: {status, trace_id?, error?},
    history: {status, entry_id?, path?, error?}}``. A failure in one sink does
    not prevent the other.
    """
    if not request_id:
        request_id = str(uuid.uuid4())

    debug_log("log_interaction", "req", {
        "agent_name": agent_name,
        "request_id": request_id,
        "query_len": len(query or ""),
        "response_len": len(response_content or ""),
        "curated": bool(intent or action or outcome or files or tags),
        "files": files or [],
        "tags": tags or [],
    })

    loop = asyncio.get_running_loop()

    # --- Langfuse trace (best-effort, skipped when keys absent) ---
    # Run the sync Langfuse SDK off the event loop so a slow network
    # round-trip doesn't stall the MCP handler.
    def _send_langfuse() -> dict:
        if not is_langfuse_configured():
            return {"status": "skipped"}
        try:
            trace_id = langfuse.create_trace_id(seed=request_id)
            with langfuse.start_as_current_observation(
                as_type="span",
                name="agent_interaction",
                trace_context={"trace_id": trace_id},
                metadata={"agent": agent_name, "source": "mcp-server"},
            ):
                with langfuse.start_as_current_observation(
                    as_type="generation",
                    name="response",
                    input=query[:2000],
                    metadata={"agent": agent_name, "reasoning": reasoning or ""},
                ) as gen:
                    gen.update(output=response_content[:5000])
            langfuse.flush()
            return {"status": "logged", "trace_id": trace_id}
        except Exception as e:
            logger.error("Langfuse logging failed: %s", e, exc_info=True)
            return {"status": "error", "error": str(e)}

    # --- History append (always; defaults to raw query/response) ---
    def _send_history() -> dict:
        try:
            writer = HistoryWriter()
            eff_intent = (intent or query or "").strip()
            eff_action = (action or f"Agent: {agent_name}").strip()
            eff_outcome = (outcome or response_content or "").strip()
            return writer.append_entry(
                eff_intent, eff_action, eff_outcome, files, tags, None
            )
        except Exception as e:
            logger.error("History append failed: %s", e, exc_info=True)
            return {"status": "error", "error": str(e)}

    # Both sinks are independent — run them concurrently.
    # Bound Langfuse to 10s so a hanging SDK doesn't block the tool response.
    langfuse_future = loop.run_in_executor(None, _send_langfuse)
    history_future = loop.run_in_executor(None, _send_history)
    try:
        langfuse_payload = await asyncio.wait_for(langfuse_future, timeout=10.0)
    except asyncio.TimeoutError:
        langfuse_payload = {"status": "error", "error": "timeout (10s)"}
    # History is the critical sink — no timeout, so we never report a false
    # failure while the thread silently succeeds in the background.
    history_payload = await history_future

    payload = {
        "request_id": request_id,
        "langfuse": langfuse_payload,
        "history": history_payload,
    }
    debug_log("log_interaction", "res", payload)
    return json.dumps(payload, ensure_ascii=False)

# --- Memory tools (describe + history) ---

@mcp.tool()
@observe(name="describe_repo")
async def describe_repo(
    ctx: Context | None = None,
    repo_path: Optional[str] = None,
    force_refresh: bool = False,
) -> str:
    """One-shot repo bootstrap.

    Builds a deterministic context bundle from the repo, asks the calling
    LLM (via MCP sampling) to distill it into a structured summary, and
    writes the result into the managed Repository Memory section of
    CLAUDE.md. Future Claude sessions read that section automatically and
    skip re-exploring the codebase.

    Returns JSON: {status, path, hash, word_count, in_word_budget, summary_preview}.
    status ∈ {"refreshed", "up-to-date", "rejected", "error"}.
    """
    try:
        # Restrict repo_path to REPO_ROOT to prevent arbitrary filesystem access.
        # Resolve relative paths against REPO_ROOT (not CWD) for stable behavior.
        if repo_path is not None:
            if not os.path.isabs(repo_path):
                repo_path = os.path.join(REPO_ROOT, repo_path)
            resolved = os.path.realpath(repo_path)
            repo_root_resolved = os.path.realpath(REPO_ROOT) + os.sep
            if resolved != repo_root_resolved.rstrip(os.sep) and not resolved.startswith(repo_root_resolved):
                payload = {
                    "status": "error",
                    "error": f"repo_path must be within {REPO_ROOT}",
                }
                return json.dumps(payload, ensure_ascii=False)
            if not os.path.isdir(resolved):
                payload = {
                    "status": "error",
                    "error": f"repo_path is not an existing directory: {repo_path}",
                }
                return json.dumps(payload, ensure_ascii=False)
            # Use the canonicalized path for all downstream work so symlink
            # TOCTOU can't bypass the sandbox after validation.
            repo_path = resolved

        debug_log("describe_repo", "req", {
            "repo_path": repo_path,
            "force_refresh": force_refresh,
        })
        loop = asyncio.get_running_loop()
        describer = RepoDescriber(repo_path=repo_path)

        decision = await loop.run_in_executor(None, describer.plan, force_refresh)
        if not decision.needs_refresh:
            payload = describer.up_to_date_response(decision)
            debug_log("describe_repo", "res", payload)
            return json.dumps(payload, ensure_ascii=False)

        prompt = await loop.run_in_executor(None, describer.build_prompt)

        # Try MCP sampling if context is available.
        if ctx is not None:
            try:
                summary = await _sample_with_agent(ctx, prompt, "Generate the repository overview.")
                payload = await loop.run_in_executor(
                    None, describer.write_summary, summary, decision.current_hash
                )
                debug_log("describe_repo", "res", payload)
                return json.dumps(payload, ensure_ascii=False)
            except Exception as sampling_err:
                logger.debug("describe_repo: sampling unavailable, returning prompt: %s", sampling_err)
                debug_log("describe_repo", "sampling_fallback", {"error": str(sampling_err)})

        # Fallback: return the prompt for the calling model to process.
        payload = {
            "status": "needs_summary",
            "repo_hash": decision.current_hash,
            "repo_path": describer.repo_path,
            "prompt": prompt,
            "instruction": (
                "Sampling is not available. Generate the repository overview by "
                "following the prompt above, then call write_repo_summary("
                f'summary=<your output>, repo_hash="{decision.current_hash}"'
                ") to persist it."
            ),
        }
        debug_log("describe_repo", "res", payload)
        return json.dumps(payload, ensure_ascii=False)
    except Exception as e:
        logger.error("describe_repo failed: %s", e, exc_info=True)
        payload = {"status": "error", "error": str(e)}
        debug_log("describe_repo", "error", payload)
        return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
@observe(name="write_repo_summary")
async def write_repo_summary(
    summary: str,
    repo_hash: str,
    repo_path: Optional[str] = None,
) -> str:
    """Persist a repository summary after describe_repo returned status='needs_summary'.

    Call this with the summary you generated from the prompt and the
    repo_hash value from the describe_repo response.

    Returns JSON: {status, path, hash, word_count, in_word_budget, summary_preview}.
    status ∈ {"refreshed", "rejected", "error"}.
    """
    try:
        if repo_path is not None:
            if not os.path.isabs(repo_path):
                repo_path = os.path.join(REPO_ROOT, repo_path)
            resolved = os.path.realpath(repo_path)
            repo_root_resolved = os.path.realpath(REPO_ROOT) + os.sep
            if resolved != repo_root_resolved.rstrip(os.sep) and not resolved.startswith(repo_root_resolved):
                payload = {
                    "status": "error",
                    "error": f"repo_path must be within {REPO_ROOT}",
                }
                return json.dumps(payload, ensure_ascii=False)
            if not os.path.isdir(resolved):
                payload = {
                    "status": "error",
                    "error": f"repo_path is not an existing directory: {repo_path}",
                }
                return json.dumps(payload, ensure_ascii=False)
            repo_path = resolved

        debug_log("write_repo_summary", "req", {
            "repo_path": repo_path,
            "repo_hash": repo_hash,
            "summary_len": len(summary),
        })
        loop = asyncio.get_running_loop()
        describer = RepoDescriber(repo_path=repo_path)
        payload = await loop.run_in_executor(
            None, describer.write_summary, summary, repo_hash
        )
        debug_log("write_repo_summary", "res", payload)
        return json.dumps(payload, ensure_ascii=False)
    except Exception as e:
        logger.error("write_repo_summary failed: %s", e, exc_info=True)
        payload = {"status": "error", "error": str(e)}
        debug_log("write_repo_summary", "error", payload)
        return json.dumps(payload, ensure_ascii=False)


@mcp.tool()
@observe(name="read_history")
async def read_history(
    limit: int = 20,
    since: Optional[str] = None,
    query: Optional[str] = None,
) -> str:
    """Read recent history entries or run a lazy semantic search.

    - Without ``query``: returns up to ``limit`` newest entries; ``since``
      (ISO8601 prefix) optionally filters for entries at or after that
      timestamp.
    - With ``query``: builds the vector index on first use (or refreshes
      it if history.md is newer than the stored embeddings), then returns
      semantically nearest entries with cosine distance.

    Returns JSON:
      {entries: [...], total, mode}
      mode ∈ {"recency", "semantic"}.

    Entry shape depends on mode:
    - recency: {id, timestamp, intent, action, outcome, files, tags, metadata}.
    - semantic: {id, distance, document, timestamp, intent, tags}.
    """
    try:
        limit = max(1, min(limit, 500))
        query = (query or "").strip() or None
        debug_log("read_history", "req", {"limit": limit, "since": since, "query": query})
        loop = asyncio.get_running_loop()

        if query:
            results = await loop.run_in_executor(
                None,
                lambda: _history_store.search(query, limit=limit),
            )
            payload = {"mode": "semantic", "total": len(results), "entries": results}
        else:
            reader = HistoryReader()
            entries = await loop.run_in_executor(
                None,
                lambda: reader.read_recent(limit=limit, since=since),
            )
            payload = {
                "mode": "recency",
                "total": len(entries),
                "entries": [e.to_dict() for e in entries],
            }
        debug_log("read_history", "res", {"mode": payload["mode"], "total": payload["total"]})
        return json.dumps(payload, ensure_ascii=False)
    except Exception as e:
        logger.error("read_history failed: %s", e, exc_info=True)
        payload = {"status": "error", "error": str(e)}
        debug_log("read_history", "error", payload)
        return json.dumps(payload, ensure_ascii=False)


# --- MCP Prompts (slash commands for Claude Desktop) ---

from mcp.server.fastmcp.prompts.base import UserMessage

@mcp.prompt()
async def ask(query: str) -> list:
    """Route any query through Agents — auto-selects the best specialist agent"""
    try:
        cached = await router.lookup_cache(query, {"history_text": ""})
        if cached:
            agent_name = cached.target_agent
        elif _is_meta_query(query):
            agent_name = "universal_agent"
        else:
            # Cache miss — return candidates as fallback
            candidates = router.get_agent_catalog()
            lines = [f"- **{c['name']}**: {c.get('role', '')}" for c in candidates]
            return [UserMessage(
                f"Pick the best agent for my query and call `get_agent_context(agent_name, query)`.\n"
                f"Agents:\n" + "\n".join(lines) + f"\n\nQuery: {query}"
            )]

        prompt, _, _, _, _ = await _load_and_enrich(agent_name, query, [])
        return [UserMessage(
            f"SYSTEM INSTRUCTIONS (MANDATORY — follow exactly):\n\n"
            f"{prompt}\n\n"
            f"---\n"
            f"USER QUERY: {query}"
        )]
    except Exception as e:
        return [UserMessage(f"{query}\n\n(Routing error: {e})")]


def _register_agent_prompts():
    """Dynamically register a /slash prompt for each agent's trigger_command."""
    for agent_name in router.available_agents:
        meta = get_agent_metadata(agent_name)
        trigger = meta.get("routing", {}).get("trigger_command", "")
        if not trigger:
            continue

        prompt_name = trigger.lstrip("/")
        display_name = meta.get("identity", {}).get("display_name", agent_name)
        role = meta.get("identity", {}).get("role", "")

        def make_prompt(a_name, d_name, r):
            async def agent_prompt(query: str) -> list:
                try:
                    prompt, _, _, _, _ = await _load_and_enrich(a_name, query, [])
                    return [UserMessage(
                        f"SYSTEM INSTRUCTIONS (MANDATORY — follow exactly):\n\n"
                        f"{prompt}\n\n"
                        f"---\n"
                        f"USER QUERY: {query}"
                    )]
                except Exception as e:
                    return [UserMessage(f"{query}\n\n(Error loading {d_name}: {e})")]
            agent_prompt.__name__ = prompt_name
            agent_prompt.__doc__ = f"{d_name} — {r}"
            return agent_prompt

        mcp.prompt()(make_prompt(agent_name, display_name, role))


_register_agent_prompts()


def _register_memory_prompts():
    """Register the slash prompt that drives the repository memory tool.

    The tool name ``describe_repo`` already occupies that Python identifier
    in this module, so the prompt function is defined under a distinct local
    name and renamed via ``__name__`` before being passed to ``mcp.prompt()``
    — same trick used in ``_register_agent_prompts``.
    """

    def _truthy(arg: str) -> bool:
        return arg.strip().lower() in ("1", "true", "force", "yes", "y", "on")

    async def describe_cmd(force: str = "") -> list:
        force_arg = "True" if _truthy(force) else "False"
        return [UserMessage(
            "Call the `describe_repo("
            f"force_refresh={force_arg})` MCP tool now as your only next action. "
            "Then report the resulting status, hash, word count, and the summary "
            "preview. Do not call any other tools first."
        )]
    describe_cmd.__name__ = "describe_repo"
    describe_cmd.__doc__ = (
        "Bootstrap or refresh the Repository Memory section in CLAUDE.md. "
        "Optional arg: `force=true` to regenerate even if the repo hash is unchanged."
    )
    mcp.prompt()(describe_cmd)


_register_memory_prompts()


def _warmup_embedding_model():
    """Warm up the embedding model so the first MCP request doesn't pay the
    cold-start cost (model load can take several seconds for large models
    like multilingual-e5-large and may exceed client timeouts)."""
    try:
        from src.engine.embedder import embed_texts, embed_query
        embed_texts(["warmup"])
        embed_query("warmup")
        logger.info("Embedding model warmed up")
    except Exception as e:
        logger.warning("Embedding model warmup failed: %s", e, exc_info=True)


if __name__ == "__main__":
    _warmup_embedding_model()
    mcp.run()
