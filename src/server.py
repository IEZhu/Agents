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
from src.schemas.protocol import AgentRequest
from src.engine.config import REPO_ROOT

mcp = FastMCP(
    "Agents-Core",
    instructions=(
        "Agents-Core is a multi-agent routing system.\n"
        "Call `route_and_load(query)` to route any user query to the best specialist agent.\n\n"
        "Response statuses:\n"
        "- SUCCESS_SAMPLED → ready-made response from the agent. Display it to the user as-is.\n"
        "- SUCCESS → system_prompt is provided. Use it as context for your answer.\n"
        "- ROUTE_REQUIRED → pick the best agent from candidates, call get_agent_context(agent_name, query).\n"
        "- NO_CHANGE → context unchanged, continue.\n"
        "- ERROR → answer directly.\n\n"
        "Отвечай на русском языке (кроме блоков кода).\n"
        "В конце добавь: **Agent**: [name] · **Skills**: [skills]"
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

async def _load_and_enrich(agent_name: str, query: str, chat_history_list: List[str], tier: str | None = None) -> tuple[str, str]:
    """Shared helper: load prompt, enrich with skills/implants/capabilities.
    Returns (final_prompt, context_hash).
    """
    if tier is None:
        tier = infer_tier(query)

    query_hash = hash(query)
    cache_key = f"{agent_name}:{query_hash}:{tier}"
    if cache_key in SESSION_CACHE:
        prompt = SESSION_CACHE[cache_key]
        logger.info(f"Session cache hit for {agent_name} (tier={tier})")
        return prompt, _compute_context_hash(prompt)

    loop = asyncio.get_running_loop()
    base_prompt = await loop.run_in_executor(None, load_agent_prompt, agent_name)
    metadata = await loop.run_in_executor(None, get_agent_metadata, agent_name)
    preferred_skills = metadata.get("preferred_skills", [])
    capabilities = metadata.get("capabilities", [])

    final_prompt = await enrich_agent_prompt(
        agent_name, base_prompt, query, chat_history_list,
        preferred_skills, tier, capabilities
    )
    SESSION_CACHE[cache_key] = final_prompt
    ctx_hash = _compute_context_hash(final_prompt)
    CONTEXT_HASH_CACHE[ctx_hash] = agent_name
    return final_prompt, ctx_hash


async def _sample_with_agent(ctx: Context, system_prompt: str, query: str) -> str:
    """Use MCP sampling to generate a response with the agent's system prompt.

    The client (Claude Desktop / Claude Code) executes the LLM call —
    no API key needed on the server side. The systemPrompt is delivered
    as a proper system prompt field, not as tool result text.
    """
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
        return result.content.text
    elif isinstance(result.content, list):
        return "\n".join(
            block.text for block in result.content if hasattr(block, "text")
        )
    return str(result.content)


@mcp.tool()
@observe(name="route_and_load")
async def route_and_load(
    query: str,
    chat_history: Optional[List[str] | str] = None,
    context_hash: Optional[str] = None,
    ctx: Context | None = None,
) -> str:
    """
    Route a user query to the best specialist agent. Call this before answering.

    The tool selects the right agent via semantic routing, loads its system prompt
    enriched with skills and implants, and (if the client supports sampling)
    generates a ready-made response using the agent's persona.

    Response statuses:
    - SUCCESS_SAMPLED → `response` contains the agent's answer. Display it as-is.
    - SUCCESS → `system_prompt` is provided. Use it as context for your answer.
      Отвечай на русском (кроме кода). Append: **Agent**: [name] · **Skills**: [skills]
    - ROUTE_REQUIRED → pick the best agent from `candidates`, then call
      get_agent_context(agent_name, query).
    - NO_CHANGE → context unchanged (same context_hash).
    - ERROR → answer directly.

    Pass `context_hash` from a previous response to enable delta mode.
    """
    try:
        chat_history_list = _normalize_chat_history(chat_history)
        history_text = "\n".join(chat_history_list)
        request_id = str(uuid.uuid4())
        tier = infer_tier(query)

        # 1. Try semantic cache
        cached_decision = await router.lookup_cache(query, {"history_text": history_text})
        if cached_decision:
            agent_name = cached_decision.target_agent
            reasoning = cached_decision.reasoning
        elif _is_meta_query(query):
            agent_name = "universal_agent"
            reasoning = "Auto-fallback: meta-query detected"
            tier = "lite"
        else:
            # 2. Cache miss — return candidates for the calling LLM to decide
            candidates = router.get_agent_catalog()
            return json.dumps({
                "status": "ROUTE_REQUIRED",
                "request_id": request_id,
                "tier": tier,
                "candidates": candidates,
                "instruction": "Select the best agent from candidates based on the user query. Then call get_agent_context(agent_name, query) to load the agent prompt.",
            }, ensure_ascii=False)

        # 3. Cache hit or meta-query — load enriched prompt
        final_prompt, new_hash = await _load_and_enrich(agent_name, query, chat_history_list, tier)

        if context_hash and context_hash == new_hash:
            return json.dumps({
                "status": "NO_CHANGE",
                "agent": agent_name,
                "context_hash": new_hash,
                "request_id": request_id,
                "tier": tier,
            }, ensure_ascii=False)

        await router.update_cache(query, agent_name, reasoning, request_id)

        # Try sampling: generate response with agent's system prompt via client LLM
        if ctx:
            try:
                response = await _sample_with_agent(ctx, final_prompt, query)
                return json.dumps({
                    "status": "SUCCESS_SAMPLED",
                    "agent": agent_name,
                    "reasoning": reasoning,
                    "request_id": request_id,
                    "tier": tier,
                    "context_hash": new_hash,
                    "response": response,
                }, ensure_ascii=False)
            except Exception as sampling_err:
                logger.debug(f"Sampling not supported, falling back: {sampling_err}")

        # Fallback: return system_prompt for clients without sampling support
        return json.dumps({
            "status": "SUCCESS",
            "agent": agent_name,
            "reasoning": reasoning,
            "request_id": request_id,
            "tier": tier,
            "context_hash": new_hash,
            "system_prompt": final_prompt,
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "ERROR", "message": str(e)}, ensure_ascii=False)

@mcp.tool()
@observe(name="get_agent_context")
async def get_agent_context(agent_name: str, query: str, reasoning: str = "Selected by calling LLM", chat_history: Optional[List[str] | str] = None, ctx: Context | None = None) -> str:
    """
    Load a specific agent's system prompt. Call after route_and_load
    returned status=ROUTE_REQUIRED.

    Pick the best agent from the candidates list and pass its name here.
    If the client supports sampling, returns a ready-made response (SUCCESS_SAMPLED).
    Otherwise returns the system_prompt for you to use as context.
    Отвечай на русском языке (кроме кода).
    Append: **Agent**: [name] · **Skills**: [skills]
    """
    try:
        chat_history_list = _normalize_chat_history(chat_history)
        request_id = str(uuid.uuid4())

        final_prompt, ctx_hash = await _load_and_enrich(agent_name, query, chat_history_list)
        await router.update_cache(query, agent_name, reasoning, request_id)

        # Try sampling: generate response with agent's system prompt via client LLM
        if ctx:
            try:
                response = await _sample_with_agent(ctx, final_prompt, query)
                return json.dumps({
                    "status": "SUCCESS_SAMPLED",
                    "agent": agent_name,
                    "request_id": request_id,
                    "context_hash": ctx_hash,
                    "response": response,
                }, ensure_ascii=False)
            except Exception as sampling_err:
                logger.debug(f"Sampling not supported, falling back: {sampling_err}")

        # Fallback: return system_prompt for clients without sampling support
        return json.dumps({
            "status": "SUCCESS",
            "agent": agent_name,
            "request_id": request_id,
            "context_hash": ctx_hash,
            "system_prompt": final_prompt,
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "ERROR", "message": str(e)}, ensure_ascii=False)

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

        return implant_retriever.format_implants_for_prompt(implants)
    except Exception as e:
        logger.error(f"Failed to load implants: {e}")
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

        prompt, _ = await _load_and_enrich(agent_name, query, [])
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
                    prompt, _ = await _load_and_enrich(a_name, query, [])
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
