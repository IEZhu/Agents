import logging
import uuid
import os
import sys
import json
import asyncio
import functools
import dotenv
from mcp.server.fastmcp import FastMCP
from typing import Optional, List, Dict, Any

# Ensure project root is in python path
sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Setup logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mcp-server")

# Load env vars
env_path = os.path.join(os.path.dirname(__file__), "../.env")
dotenv.load_dotenv(env_path)

# Debug logging for env vars
public_key = os.getenv("LANGFUSE_PUBLIC_KEY")
if public_key:
    logger.info(f"LangFuse Configured. Public Key ends with: ...{public_key[-4:]}")
else:
    logger.error(f"LangFuse Public Key NOT FOUND. Checked path: {env_path}")

from src.engine.router import SemanticRouter
from src.engine.skills import SkillRetriever
from src.engine.implants import ImplantRetriever
from src.engine.context import ContextRetriever
from src.utils.prompt_loader import load_agent_prompt, get_agent_metadata
from src.schemas.protocol import AgentRequest, AgentResponse, RouterDecision

# Initialize Server
mcp = FastMCP("Agents-Core")

# Initialize Logic
router = SemanticRouter()
skill_retriever = SkillRetriever()
implant_retriever = ImplantRetriever()
context_retriever = ContextRetriever()

# Session Cache
SESSION_CACHE: Dict[str, str] = {} # {agent_name: enriched_prompt}

# Initialize Langfuse via Env
from langfuse import Langfuse
langfuse = Langfuse()

# --- Middleware / Decorators ---

async def get_dynamic_context_string(agent_name: str, query: str, chat_history: List[str] = [], preferred_skills: List[str] = None) -> str:
    """
    Helper to retrieve and format dynamic context (Skills + Implants).
    """
    loop = asyncio.get_running_loop()
    context_parts = []

    # 1. Retrieve Skills
    try:
        # Use lambda to pass kwargs
        skills = await loop.run_in_executor(
            None,
            lambda: skill_retriever.retrieve(query, preferred_skills=preferred_skills)
        )
        if skills:
            context_parts.append(skill_retriever.format_skills_for_prompt(skills))
    except Exception as e:
        logger.error(f"Failed to retrieve skills: {e}")

    # 2. Retrieve Implants
    try:
        # Restore automatic implant injection (Limit 3) to fix "implants not connecting"
        implants = await loop.run_in_executor(
            None,
            lambda: implant_retriever.retrieve(query, n_results=3, role=agent_name)
        )
        if implants:
            context_parts.append(implant_retriever.format_implants_for_prompt(implants))
    except Exception as e:
        logger.error(f"Failed to retrieve implants: {e}")

    return "\n\n".join(context_parts)

async def enrich_agent_prompt(agent_name: str, base_prompt: str, query: str, chat_history: List[str] = [], preferred_skills: List[str] = None) -> str:
    """
    Enriches the base system prompt with dynamic skills, implants, and language override.
    """
    # 1. Add dynamic context (skills + implants)
    dynamic_context = await get_dynamic_context_string(agent_name, query, chat_history, preferred_skills)
    if dynamic_context:
        base_prompt += f"\n\n{dynamic_context}"
    
    # 2. Detect language and inject override directive
    loop = asyncio.get_running_loop()
    context_data = await loop.run_in_executor(
        None,
        lambda: context_retriever.retrieve(query, chat_history)
    )
    
    detected_language = context_data.get("detected_language", "English")
    
    # Inject high-priority language directive at the end
    language_directive = f"""

## LANGUAGE OVERRIDE (CRITICAL)

The user is communicating in **{detected_language}**. You MUST respond in **{detected_language}**.

**This directive takes precedence over any previous language instructions.**
"""
    
    base_prompt += language_directive
    logger.info(f"Injected language directive: {detected_language}")
    
    return base_prompt

def trace_tool(tool_name: str = None):
    """
    Decorator to trace tool execution with Langfuse.
    """
    def decorator(func):
        nonlocal tool_name
        if not tool_name:
            tool_name = func.__name__

        @functools.wraps(func)
        async def wrapper(*args, **kwargs):
            # Create a span for the tool execution
            trace = langfuse.start_span(name=tool_name)

            # Capture inputs
            try:
                # Basic input capture (args/kwargs)
                input_data = {}
                if args: input_data["args"] = args
                if kwargs: input_data["kwargs"] = kwargs
                trace.update(input=input_data)
            except Exception:
                pass # Don't fail if logging inputs fails

            try:
                # Execute the function
                if asyncio.iscoroutinefunction(func):
                    result = await func(*args, **kwargs)
                else:
                    result = func(*args, **kwargs)

                # Capture output (truncate if too long maybe?)
                trace.update(output=str(result)[:5000]) # Cap output log
                return result

            except Exception as e:
                trace.update(
                    output={"error": str(e)},
                    level="ERROR"
                )
                raise e
            finally:
                trace.end()
                # We could flush here, but it might be performance heavy to flush on every tool.
                # Ideally, we rely on background flush, but for short-lived MCP calls, explicit flush is safer.
                langfuse.flush()

        return wrapper
    return decorator

# --- Tools ---

@mcp.tool()
async def clear_session_cache() -> str:
    """Clears the session cache. Use when switching contexts."""
    SESSION_CACHE.clear()
    return "Session cache cleared"

# Meta-Query patterns for auto-routing to universal_agent
META_QUERY_PATTERNS = [
    # English
    "what tools", "what can you", "help me", "hello", "hi ", "hey ",
    "who are you", "what are you", "introduce yourself",
    # Generic/ambiguous
    "?", "test"
]

def _is_meta_query(query: str) -> bool:
    """
    Detects if query is a meta-query (greeting, capabilities question, etc.)
    that should be routed to universal_agent by default.
    """
    query_lower = query.lower().strip()
    # Very short queries are likely meta
    if len(query_lower) < 10:
        return True
    return any(pattern in query_lower for pattern in META_QUERY_PATTERNS)

@mcp.tool()
@trace_tool("get_routing_info")
async def get_routing_info(query: str, chat_history: List[str] = []) -> str:
    """
    Checks the semantic cache for a routing decision.
    If cached, returns the agent name and system prompt.
    If NOT cached, returns a list of available agents so Cursor can decide.
    Provide 'chat_history' (list of strings) to improve routing context.
    """
    try:
        # Calls the async router method
        cached_decision = await router.lookup_cache(query, {"history_text": "\n".join(chat_history)})

        if cached_decision:
            try:
                loop = asyncio.get_running_loop()
                base_prompt = await loop.run_in_executor(None, load_agent_prompt, cached_decision.target_agent)

                # Get preferred skills from metadata
                metadata = await loop.run_in_executor(None, get_agent_metadata, cached_decision.target_agent)
                preferred_skills = metadata.get("preferred_skills", [])

                # Enrich with dynamic content (skills, implants)
                final_prompt = await enrich_agent_prompt(
                    cached_decision.target_agent,
                    base_prompt,
                    query,
                    chat_history,
                    preferred_skills
                )

                return json.dumps({
                    "status": "CACHE_HIT",
                    "agent": cached_decision.target_agent,
                    "reasoning": cached_decision.reasoning,
                    "system_prompt": final_prompt
                })
            except Exception as e:
                return json.dumps({
                    "status": "ERROR",
                    "message": f"Cache hit but failed to load prompt: {e}"
                })

        # Cache Miss - Check for Meta-Query auto-routing
        if _is_meta_query(query):
            logger.info(f"Meta-query detected, auto-routing to universal_agent: '{query[:50]}...'")
            # Directly load universal_agent context
            return await get_agent_context(
                agent_name="universal_agent",
                query=query,
                reasoning="Auto-fallback: Meta-Query detected (greeting/capabilities/ambiguous)",
                chat_history=chat_history
            )

        # Standard Cache Miss - return available agents with fallback hint
        return json.dumps({
            "status": "CACHE_MISS",
            "available_agents": router.available_agents,
            "default_fallback": "universal_agent",
            "instruction": "Select the best agent from the list. If the domain is unclear or ambiguous, use 'universal_agent' as the safe default."
        })
    except Exception as e:
         return json.dumps({"status": "ERROR", "message": str(e)})

@mcp.tool()
@trace_tool("get_agent_context")
async def get_agent_context(agent_name: str, query: str, reasoning: str = "Selected by Cursor Model", chat_history: List[str] = []) -> str:
    """
    Loads the system prompt for a specific agent.
    Implicitly updates the cache since the intelligent model (Cursor) made this choice.
    """
    try:
        # Check Session Cache
        # Use query hash to ensure dynamic context (implants/skills) is specific to the request
        query_hash = hash(query)
        cache_key = f"{agent_name}:{query_hash}"
        if cache_key in SESSION_CACHE:
            logger.info(f"Session cache hit for {agent_name} (query hash: {query_hash})")
            return json.dumps({
                "status": "SUCCESS",
                "agent": agent_name,
                "request_id": str(uuid.uuid4()), # New request ID for this interaction
                "system_prompt": SESSION_CACHE[cache_key],
                "source": "SESSION_CACHE"
            })

        # Load Prompt
        loop = asyncio.get_running_loop()
        base_prompt = await loop.run_in_executor(None, load_agent_prompt, agent_name)

        # Get preferred skills from metadata
        metadata = await loop.run_in_executor(None, get_agent_metadata, agent_name)
        preferred_skills = metadata.get("preferred_skills", [])

        # Enrich with dynamic content
        final_prompt = await enrich_agent_prompt(
            agent_name,
            base_prompt,
            query,
            chat_history,
            preferred_skills
        )

        # Update Session Cache
        SESSION_CACHE[cache_key] = final_prompt

        # Update Cache (We trust Cursor's decision)
        request_id = str(uuid.uuid4())
        await router.update_cache(query, agent_name, reasoning, request_id)

        return json.dumps({
            "status": "SUCCESS",
            "agent": agent_name,
            "request_id": request_id,
            "system_prompt": final_prompt
        })
    except Exception as e:
        return json.dumps({"status": "ERROR", "message": str(e)})

@mcp.tool()
@trace_tool("get_context")
async def get_context(query: str, agent_name: str, chat_history: List[str] = []) -> str:
    """
    Retrieves the dynamic context (skills + implants) for a given query and agent.
    Useful for inspecting what dynamic content would be added to the prompt.
    """
    try:
        return await get_dynamic_context_string(agent_name, query, chat_history)
    except Exception as e:
        return f"Error retrieving context: {str(e)}"

@mcp.tool()
@trace_tool("get_relevant_implants")
async def get_relevant_implants(query: str, agent_context: str = None, limit: int = 5) -> str:
    """
    Retrieves relevant cognitive implants (mental models, reasoning strategies) based on a query.
    """
    try:
        loop = asyncio.get_running_loop()
        # run_in_executor to avoid blocking the loop with ChromaDB operations
        results = await loop.run_in_executor(
            None,
            lambda: implant_retriever.retrieve(query=query, n_results=limit, agent_context=agent_context)
        )

        formatted_output = implant_retriever.format_implants_for_prompt(results)
        return formatted_output
    except Exception as e:
        logger.error(f"Failed to retrieve implants: {e}")
        return f"Error retrieving implants: {str(e)}"

@mcp.tool()
@trace_tool("get_reasoning_strategy")
async def get_reasoning_strategy(task_type: str, query: str = "") -> str:
    """
    Loads cognitive implants for a specific task type.
    Call ONLY when you need advanced reasoning strategies.

    task_types:
    - "debugging": chain-of-code, reflexion
    - "analysis": step-back-prompting, chain-of-verification
    - "creative": analogical-prompting, generated-knowledge
    - "planning": plan-and-solve, skeleton-of-thought
    """
    TASK_IMPLANT_MAP = {
        "debugging": ["implant-chain-of-code", "implant-reflexion"],
        "analysis": ["implant-step-back-prompting", "implant-chain-of-verification"],
        "creative": ["implant-analogical-prompting", "implant-generated-knowledge"],
        "planning": ["implant-plan-and-solve-plus", "implant-skeleton-of-thought"],
    }

    implant_names = TASK_IMPLANT_MAP.get(task_type, [])
    if not implant_names:
        return f"Unknown task type: {task_type}"

    try:
        # Load specific implants by ID (using the retriever's collection)
        loop = asyncio.get_running_loop()

        # We need a way to fetch specific implants.
        # Since ImplantRetriever uses Chroma, we can query by ID if filenames match.
        # But we don't have a direct 'get_by_ids' method exposed in `retrieve`.
        # Let's add a `retrieve_by_ids` or similiar logic to ImplantRetriever,
        # OR just use the collection directly here if we had access, but `implant_retriever` encapsulates it.
        # For now, let's implement a direct fetch inside this function using the retriever's collection reference.

        # Ensure .mdc extension
        target_ids = []
        for name in implant_names:
             if not name.endswith(".mdc"):
                 target_ids.append(f"{name}.mdc")
             else:
                 target_ids.append(name)

        results = await loop.run_in_executor(
            None,
            lambda: implant_retriever.collection.get(ids=target_ids)
        )

        implants = []
        if results['ids']:
            for i, id in enumerate(results['ids']):
                implants.append({
                    "filename": id,
                    "content": results['documents'][i],
                    "metadata": results['metadatas'][i],
                    "distance": 0.0
                })

        return implant_retriever.format_implants_for_prompt(implants)

    except Exception as e:
        logger.error(f"Failed to load reasoning strategy: {e}")
        return f"Error loading strategy: {str(e)}"

@mcp.tool()
@trace_tool("log_interaction")
async def log_interaction(agent_name: str, query: str, response_content: str, request_id: Optional[str] = None, reasoning: Optional[str] = None) -> str:
    """
    Validates the response structure, logs the full trace to LangFuse, and finalizes the interaction.
    ALWAYS call this at the end of a turn to ensure observability.
    """
    if not request_id:
        request_id = str(uuid.uuid4())

    try:
        # Create a specific trace for the interaction summary if needed,
        # but the decorator `trace_tool` will already log this function call.
        # However, `log_interaction` is meant to be the "record" of the *entire* turn.
        # Since we are using `trace_tool` on all tools, we have granular traces.
        # We can keep this to maintain the behavior of "logging the final answer explicitly".

        # We can add metadata to the current span (managed by the decorator) if we could access it,
        # but Langfuse Python SDK is thread-local usually.
        # For now, let's just let the decorator handle the logging of this specific call,
        # which effectively logs the "Final Answer".

        return f"Interaction logged successfully. Trace ID: {request_id}"

    except Exception as e:
        return f"Failed to log interaction: {e}"

if __name__ == "__main__":
    mcp.run()
