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

from src.engine.router import SemanticRouter
from src.engine.enrichment import (
    enrich_agent_prompt,
    infer_tier,
    implant_retriever,
)
from src.engine.config import SESSION_CACHE_MAX_SIZE, SESSION_CACHE_TTL_SECONDS
from src.utils.prompt_loader import load_agent_prompt, get_agent_metadata
from src.utils.debug_logger import debug_log

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

from src.utils.langfuse_compat import observe, get_langfuse
langfuse = get_langfuse()
atexit.register(langfuse.flush)

# --- Tools ---

@mcp.tool()
async def clear_session_cache() -> str:
    """Clears the session cache. Use when switching contexts."""
    SESSION_CACHE.clear()
    return "Session cache cleared"

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

CONTEXT_HASH_CACHE: TTLCache = TTLCache(maxsize=64, ttl=SESSION_CACHE_TTL_SECONDS)

def _compute_context_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:16]

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
    capabilities = metadata.get("capabilities", [])

    # Promote tier to at least "standard" when agent declares skills/capabilities,
    # but only if tier was inferred (not explicitly set by the caller)
    if not tier_explicit and tier == "lite" and (preferred_skills or capabilities):
        tier = "standard"
        logger.info(f"Tier promoted to 'standard' for {agent_name} (has preferred_skills or capabilities)")

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
        preferred_skills, tier, capabilities
    )
    final_prompt = enrichment.prompt
    SESSION_CACHE[cache_key] = (final_prompt, enrichment.skills_loaded, enrichment.implants_loaded)
    ctx_hash = _compute_context_hash(final_prompt)
    CONTEXT_HASH_CACHE[ctx_hash] = agent_name
    debug_log("_load_and_enrich", "res", {
        "agent": agent_name, "tier": tier, "cache": "miss",
        "prompt_len": len(final_prompt), "preferred_skills": preferred_skills,
        "capabilities": capabilities,
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

        # 1. Try semantic cache
        explicit_tier = None  # only set for intentional overrides (e.g. meta-query)
        cached_decision = await router.lookup_cache(query, {"history_text": history_text})
        if cached_decision:
            agent_name = cached_decision.target_agent
            reasoning = cached_decision.reasoning
        elif _is_meta_query(query):
            agent_name = "universal_agent"
            reasoning = "Auto-fallback: meta-query detected"
            explicit_tier = "lite"
        else:
            # 2. Cache miss — return candidates for the calling LLM to decide
            candidates = router.get_agent_catalog()
            result = {
                "status": "ROUTE_REQUIRED",
                "request_id": request_id,
                "tier": tier,
                "candidates": candidates,
                "instruction": (
                    "CRITICAL: You MUST call get_agent_context(agent_name, query) RIGHT NOW as your ONLY next action. "
                    "Do NOT call any other tools. Do NOT use Agent, Bash, Read, Grep, or any tool in parallel. "
                    "Do NOT explore the codebase. Do NOT answer the user. "
                    "FIRST pick the single best agent from candidates, THEN call ONLY: "
                    "get_agent_context(agent_name=\"<chosen>\", query=\"<original query>\"). "
                    "Wait for its response. Only THEN proceed with the user's request."
                ),
            }
            debug_log("route_and_load", "res", result)
            return json.dumps(result, ensure_ascii=False)

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
      debugging  → chain-of-code, reflexion
      analysis   → step-back-prompting, chain-of-verification
      creative   → analogical-prompting, generated-knowledge
      planning   → plan-and-solve, skeleton-of-thought
    """
    TASK_IMPLANT_MAP = {
        "debugging": ["implant-chain-of-code", "implant-reflexion"],
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
                lambda: implant_retriever.collection.get(ids=target_ids),
            )
            implants = [
                {
                    "filename": results["ids"][i],
                    "content": results["documents"][i],
                    "metadata": results["metadatas"][i],
                    "distance": 0.0,
                }
                for i in range(len(results["ids"]))
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
async def log_interaction(agent_name: str, query: str, response_content: str, request_id: Optional[str] = None, reasoning: Optional[str] = None) -> str:
    """
    Logs the full agent interaction turn to LangFuse as a generation trace.
    ALWAYS call this at the end of a turn to ensure observability.
    """
    if not request_id:
        request_id = str(uuid.uuid4())

    try:
        trace_id = langfuse.create_trace_id(seed=request_id)
        
        with langfuse.start_as_current_observation(
            as_type="span",
            name="agent_interaction",
            trace_context={"trace_id": trace_id},
            metadata={"agent": agent_name, "source": "mcp-server"}
        ) as trace:
            with langfuse.start_as_current_observation(
                as_type="generation",
                name="response",
                input=query[:2000],
                metadata={"agent": agent_name, "reasoning": reasoning or ""}
            ) as gen:
                gen.update(output=response_content[:5000])
                
        langfuse.flush()
        return f"Interaction logged. Trace ID: {request_id}"
    except Exception as e:
        logger.error(f"Failed to log interaction: {e}")
        return f"Logging failed (non-critical): {e}"

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

if __name__ == "__main__":
    mcp.run()
