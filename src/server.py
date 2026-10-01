import os
import sys

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

# Acquire the installation lease before loading application/configuration code.
# The bootstrap re-reads this file under the lease and holds it until server exit.
if __name__ == "__main__" and not globals().get("_agents_bootstrapped"):
    from src.startup import run_server
    run_server(__file__)
    raise SystemExit(0)

import atexit
import logging
import uuid
import re
import json
import asyncio
import datetime
import queue
import threading
import time
import dotenv
from src.utils.synchronized_cache import SynchronizedTTLCache as TTLCache
from src.engine.fingerprint import configuration_revision
from src import component_toggles
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.server import Context
from mcp.types import SamplingMessage, TextContent, ClientCapabilities, SamplingCapability
from typing import Optional, List

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
    resolve_profile,
    implant_retriever,
)
from src.engine.config import SESSION_CACHE_MAX_SIZE, SESSION_CACHE_TTL_SECONDS, get_client_repo_root
from src.utils.prompt_loader import load_agent_prompt, get_agent_metadata
from src.utils.debug_logger import debug_log
from src.memory.describer import RepoDescriber
from src.memory.history import HistoryReader, HistoryWriter
from src.daemon.workspaces import client_context, WorkspaceError, HistoryStores
from src import flow_persona
from src.flows import FlowCatalog, FlowError, execution_bundle
from src.user_flows import FlowLibrary
from src.schemas.protocol import PersonaDescriptor, PersonaAction
from src.engine.persona import load_persona, route_persona, parse_persona, error_response

PROTOCOL_VERSION = 2
UNSUPPORTED_PROTOCOL = (
    "Unsupported protocol_version; this server supports protocol_version=2 only "
    "(protocol 1 was removed). Refresh the client instructions with "
    "scripts/install_instructions.py."
)


def _check_prompt_protocol(protocol_version: Optional[str]) -> None:
    """Slash prompts accept the argument protocol 2 clients were told to pass."""
    if protocol_version not in (None, "", str(PROTOCOL_VERSION)):
        raise ValueError(UNSUPPORTED_PROTOCOL)

# Per-workspace HistoryStore cache (bounded LRU) — avoids reloading .npz from disk
# on every read_history call. HistoryStore.ensure_index() rebuilds a stale index
# from a content fingerprint.
_history_stores = HistoryStores()

mcp = FastMCP(
    "Agents-Core",
    instructions=(
        "Agents-Core uses persona protocol 2; pass protocol_version=2 to route_and_load "
        "and get_agent_context.\n"
        "Silently assess whether the active persona fits each request. On keep, "
        "do not route, enumerate agents, or enrich. Route only for initial selection or a needed "
        "specialization change, with protocol_version=2 and current_persona. Load an explicitly "
        "named role directly. Restore lost instructions with force_reload=True; refresh skills "
        "with refresh_persona_context. Preserve higher-priority instructions, the conversation "
        "and user constraints. Ignore stale or replayed activations. Never clear caches to "
        "switch personas. Except for the MCP-unavailable fallback below, compose the answer "
        "with the returned footer, call log_interaction "
        "with that answer (without any time line), the current user request verbatim as query, "
        "and the active descriptor/action, then send the final answer. When the footer lists the "
        "`answer-timestamp` rule and the call returned a `timestamp`, put that `timestamp` on its own "
        "first line followed by an empty line.\n\n"
        "Response statuses:\n"
        "- SUCCESS → validate the complete `persona` descriptor and `persona_block`, "
        "`rules_block`, `skills_block`, `implants_block`. Apply only if `replaces_activation_id` "
        "matches the current activation (null for initial load), replacing all four blocks, "
        "including empty blocks. Then save `persona` and `footer`.\n"
        "- ROUTE_REQUIRED → select from `candidates`, then call "
        "`get_agent_context(agent_name, query, protocol_version=2, current_persona=...)`; "
        "retain the previous activation until SUCCESS.\n"
        "- NO_CHANGE → keep the current blocks, descriptor and footer unchanged.\n"
        "- ERROR → keep the previous activation, report the failed operation and follow the "
        "response's `instruction`; for persona tools the requested bundle was not applied. "
        "Do not partially activate returned content.\n"
        "The server never samples an answer. The ask and agent slash prompts return the "
        "same bundles; pass current_persona as descriptor JSON when available.\n\n"
        "Respond in the same language as the user's query (auto-detect). "
        "Exceptions: code blocks, technical terms, tool/CLI output, and the footer "
        "labels `Agent`, `Skills`, `Implants`, `Rules` stay in English.\n"
        "Append the exact footer returned with the active bundle.\n"
        "HTTP memory tools require X-Agents-Workspace. On workspace_required or workspace_invalid, "
        "continue routing/persona, report unavailable project memory, and do not retry logging in a loop. "
        "For needs_summary preserve workspace_id, repo_path and repo_hash in write_repo_summary. "
        "Never replay an ambiguous write automatically; read the result first.\n"
        "For requested workflows, use list_flows and run_flow. Flows are built-in, "
        "personal (user:<id>, every repository) or per repository (repo:<id>); run_flow "
        "binds them to the caller's workspace and returns needs_execution. When the user "
        "asks to save, change, restore or delete a flow, use get_flow, save_flow and "
        "delete_flow; say which scope you used. To choose a flow's agent, skills, implants "
        "or rules, use set_flow_persona. Pass current_persona to run_flow and apply its "
        "persona_activation as a switch before executing. Execute the returned instructions in the current "
        "model session against repo_path, preserving user constraints. It does not "
        "perform the workflow or authorize additional actions. HTTP requires "
        "X-Agents-Workspace; never substitute the installation for a missing target.\n"
        "MCP-unavailable fallback: reuse a valid retained bundle's descriptor and exact "
        "footer. If none is retained, disclose manual fallback and omit the MCP footer and descriptor "
        "attribution; do not fabricate them. Skip unavailable logging."
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

# log_interaction writes its sinks (history.md, Langfuse) on these workers after
# it has answered. The workers are daemon threads, so a permanently blocked sink
# can never keep the process from exiting (a ThreadPoolExecutor is joined at
# interpreter exit). The queues are bounded: when a sink is stuck and its queue
# is full, new writes are dropped with an error in the server log.
LOG_DRAIN_TIMEOUT_SECONDS = 10.0
LOG_QUEUE_MAX = 256


class _SinkWorker:
    def __init__(self, name: str):
        self._queue: "queue.Queue" = queue.Queue(maxsize=LOG_QUEUE_MAX)
        self._idle = threading.Condition()
        self._unfinished = 0
        self.name = name
        self._thread = threading.Thread(target=self._run, name=f"log-{name}", daemon=True)
        self._thread.start()

    def submit(self, fn) -> bool:
        with self._idle:
            self._unfinished += 1
        try:
            self._queue.put_nowait(fn)
            return True
        except queue.Full:
            self._finished()
            logger.error("Log queue %s is full (%d); dropping a write", self.name, LOG_QUEUE_MAX)
            return False

    def _finished(self) -> None:
        with self._idle:
            self._unfinished -= 1
            self._idle.notify_all()

    def _run(self) -> None:
        while True:
            fn = self._queue.get()
            try:
                fn()
            except Exception as e:
                logger.error("Background log write failed: %s", e, exc_info=True)
            finally:
                self._finished()

    def drain(self, deadline: float) -> bool:
        with self._idle:
            while self._unfinished:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._idle.wait(remaining)
        return True


# Separate workers so a hanging Langfuse call can never delay the history write.
_history_worker = _SinkWorker("history")
_langfuse_worker = _SinkWorker("langfuse")


_drain_abandoned = False


def drain_pending_logs(timeout: float = LOG_DRAIN_TIMEOUT_SECONDS) -> bool:
    """Wait up to *timeout* seconds for queued log writes; True when none remain."""
    global _drain_abandoned
    if _drain_abandoned:
        return False
    deadline = time.monotonic() + timeout
    done = _history_worker.drain(deadline)
    done = _langfuse_worker.drain(deadline) and done
    if not done:
        logger.warning("Log writes still pending after %.0fs drain; abandoning them", timeout)
        # Later exit hooks must not wait for the same stuck sink again.
        _drain_abandoned = True
    return done


# Registered after langfuse.flush, so it runs first at exit (LIFO).
atexit.register(drain_pending_logs)


def _is_within(candidate: str, boundary: str) -> bool:
    """Return True iff *candidate* resolves inside *boundary* (inclusive).

    Uses ``os.path.commonpath`` so the check survives:

    * boundary == filesystem root (``"/"`` on POSIX) — naive
      ``startswith(boundary + os.sep)`` math would produce ``"//"`` and
      reject everything after ``rstrip(os.sep)``.
    * trailing-slash differences and path-normalization quirks.
    * Windows cross-drive paths (``ValueError`` from ``commonpath``).

    Mirrors the pattern in ``src/utils/prompt_loader.py:67``.
    """
    boundary_real = os.path.realpath(boundary)
    candidate_real = os.path.realpath(candidate)
    try:
        return os.path.commonpath([boundary_real, candidate_real]) == boundary_real
    except ValueError:
        # Different drives on Windows, or mixed absolute/relative — treat
        # as out-of-bounds.
        return False


# --- Tools ---

def _flow_library(ctx: Context | None) -> FlowLibrary:
    """Built-in plus personal flows; repo: flows need the caller's workspace."""
    try:
        root, error = client_context(ctx, allow_install_fallback=False).workspace_root(), None
    except (WorkspaceError, OSError, RuntimeError) as failure:
        root, error = None, str(failure)
    return FlowLibrary(FlowCatalog(), repo_root=root, repo_error=error)


def _flow_error(error: Exception) -> str:
    return json.dumps({"status": "error", "error": str(error)}, ensure_ascii=False)


@mcp.tool()
async def list_flows(scope: str = "all", ctx: Context | None = None) -> str:
    """List Markdown workflows: built-in, personal and this repository's.

    scope: all (default), builtin, user or repo. IDs: built-ins keep bare IDs
    (e.g. pr-review); personal flows are user:<id> (every repository) and
    repo:<id> (only the caller's repository). overridden_by marks a built-in
    replaced by a local copy; upstream_changed marks a copy whose built-in has
    changed since it was saved. No workspace is needed except for repo flows.
    """
    try:
        library = _flow_library(ctx)
        return json.dumps(await asyncio.to_thread(library.list, scope), ensure_ascii=False)
    except (FlowError, OSError, RuntimeError) as error:
        return _flow_error(error)


@mcp.tool()
async def get_flow(flow: str, version: Optional[str] = None, ctx: Context | None = None) -> str:
    """Read a flow's Markdown, revision and saved versions before editing it.

    flow: a bare ID or builtin:/user:/repo:<id>. version: an entry from history,
    to view or restore older text (restore = save_flow with that content).
    For a local copy of a built-in, upstream holds the current built-in text.
    """
    try:
        library = _flow_library(ctx)
        return json.dumps(await asyncio.to_thread(library.get, flow, version), ensure_ascii=False)
    except (FlowError, OSError, RuntimeError) as error:
        return _flow_error(error)


@mcp.tool()
async def save_flow(
    flow: str,
    content: str,
    scope: str = "user",
    expected_revision: Optional[str] = None,
    override: bool = False,
    ctx: Context | None = None,
) -> str:
    """Create or update a personal flow when the user asks to save or change one.

    Stored as Markdown in the installation's git-ignored flows/.user, never in a
    repository's working tree. scope: user (all repositories, default) or repo
    (only the caller's repository). content: the complete Markdown flow, starting
    with a "# Title", with steps and completion criteria; not a chat transcript.
    expected_revision: omit to create; to update, pass the revision from get_flow
    (a mismatch returns flow_conflict: reload and reapply instead of overwriting).
    Built-in flows are read-only: to change one for this user, save the same ID
    with override=true. Previous text stays in history.
    """
    try:
        library = _flow_library(ctx)
        result = await asyncio.to_thread(library.save, flow, content, scope=scope,
                                         expected_revision=expected_revision, override=override)
        return json.dumps(result, ensure_ascii=False)
    except (FlowError, OSError, RuntimeError) as error:
        return _flow_error(error)


@mcp.tool()
async def delete_flow(flow: str, expected_revision: str, ctx: Context | None = None) -> str:
    """Delete a personal flow (user:/repo:) on explicit request; its text stays in history.

    Deleting a local copy of a built-in makes the built-in effective again.
    """
    try:
        library = _flow_library(ctx)
        result = await asyncio.to_thread(library.delete, flow, expected_revision=expected_revision)
        return json.dumps(result, ensure_ascii=False)
    except (FlowError, OSError, RuntimeError) as error:
        return _flow_error(error)


@mcp.tool()
async def run_flow(
    flow: str,
    request: str = "",
    repo_path: Optional[str] = None,
    current_persona: PersonaDescriptor | None = None,
    ctx: Context | None = None,
) -> str:
    """Start a user-requested flow in the CALLER's repository.

    flow: a bare ID, ID.md, flows/ID.md, or builtin:/user:/repo:<id>. A bare ID
    resolves repo:, then user:, then builtin:. request carries the user's scope,
    PR/MR URL and constraints such as no-merge. repo_path defaults to the caller
    workspace; an override must be an existing directory within it.
    HTTP requires X-Agents-Workspace. Stdio uses AGENTS_CLIENT_REPO_ROOT as
    given, else the project inferred from CLAUDE_PROJECT_DIR or cwd; an inferred
    filesystem root or a directory inside the Windows directory is refused.

    Returns needs_execution with flow metadata, content, repo_path, workspace_id,
    request and instruction. Continue executing that content using client tools.
    When the flow names a persona (flow.persona), persona_activation is a protocol 2
    response for it: pass current_persona and apply it as a switch before executing.
    This tool only reads instructions: it does not run commands, edit files,
    create a background task, sample a model or claim the workflow is complete.
    Returns status=error for an invalid source or unavailable caller workspace.
    """
    try:
        client = client_context(ctx, allow_install_fallback=False)
        target = client.workspace_target(repo_path)
        library = FlowLibrary(FlowCatalog(), repo_root=client.workspace_root())
        loaded = await asyncio.to_thread(library.resolve, flow)
        bundle = execution_bundle(loaded, target, client.workspace_id, request)
        spec = loaded.metadata().get("persona")
        if spec:
            query = f"{loaded.title}\n{request}".strip()
            bundle["persona_activation"] = json.loads(await load_persona(
                router, spec["agent"], query, [], current_persona,
                reasoning=f"Persona of flow {loaded.id}",
                selection=flow_persona.selection(spec)))
        return json.dumps(bundle, ensure_ascii=False)
    except (FlowError, WorkspaceError, OSError, RuntimeError) as error:
        return _flow_error(error)


@mcp.tool()
async def set_flow_persona(
    flow: str,
    agent: Optional[str] = None,
    skills: Optional[List[str]] = None,
    implants: Optional[List[str]] = None,
    rules: Optional[List[str]] = None,
    reset: bool = False,
    ctx: Context | None = None,
) -> str:
    """Choose the agent, skills, implants and rules a flow runs with, for this user.

    Works for built-in, user: and repo: flows without copying their text; it
    replaces the persona in the flow's frontmatter. A list loads exactly those
    components (empty = none); an omitted list keeps the agent's own selection.
    agent omitted: run the flow without a persona. reset=true: drop this choice
    so the flow's frontmatter applies again. IDs come from list_agents and the
    component names in a persona footer.
    """
    try:
        library = _flow_library(ctx)
        persona = None
        if agent is not None:
            persona = {"agent": agent}
            for kind, values in (("skills", skills), ("implants", implants), ("rules", rules)):
                if values is not None:
                    persona[kind] = values
        elif any(values is not None for values in (skills, implants, rules)):
            raise FlowError("flow_invalid: components need an agent")
        result = await asyncio.to_thread(library.set_persona, flow, persona, reset=reset)
        return json.dumps(result, ensure_ascii=False)
    except (FlowError, OSError, RuntimeError) as error:
        return _flow_error(error)


@mcp.tool()
async def clear_session_cache() -> str:
    """Administrative cache reset. Not needed for persona switches; affects shared caches."""
    SESSION_CACHE.clear()
    return "Session cache cleared"

_META_QUERY_RE = re.compile(
    r"(?:h(?:ello|i|ey)|what tools(?: do you have)?|what can you(?: do)?|help(?: me)?"
    r"|who are you|what are you|introduce yourself|test"
    r"|привет|здравствуй(?:те)?|что (?:ты умеешь|можешь)|помоги|кто ты"
    r"|какие (?:у тебя|есть) (?:инструменты|агенты)"
    r"|ok|okay|yes|да|ок|oui|ja|sí|thanks|thank you|спасибо|continue|продолжи)",
    re.IGNORECASE,
)

def _is_meta_query(query: str) -> bool:
    query_stripped = query.strip().strip(".!?,;:… ")
    return not query_stripped or bool(_META_QUERY_RE.fullmatch(query_stripped))

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

async def _load_and_enrich(agent_name: str, query: str, chat_history_list: List[str], tier: str | None = None) -> tuple[str, list[str], list[str], list[str], str]:
    """Per-query enrichment for the evaluation harnesses: load and enrich one prompt.
    Returns (final_prompt, skills_loaded, implants_loaded, rules_loaded, effective_tier).
    """
    tier_explicit = tier is not None
    if tier is None:
        tier = infer_tier(query)

    loop = asyncio.get_running_loop()
    metadata = await loop.run_in_executor(None, get_agent_metadata, agent_name)
    core_skills = metadata.get("core_skills", []) or []
    preferred_skills = metadata.get("preferred_skills", []) or []
    capable_skills = metadata.get("capable_skills", []) or []
    preferred_implants = metadata.get("preferred_implants", []) or []

    # The classifier's own verdict, before any promotion. `None` when disabled,
    # which keeps every downstream layer on its legacy tier-derived budget.
    profile = resolve_profile(query)

    # Promote inferred tier to "standard" only when implants are declared.
    # `lite` keeps mandatory `core_skills` (loaded unconditionally below) but
    # skips the semantic skill pool and implants — exactly what short queries
    # benefit from. Implants always need the semantic pipeline, so promote.
    #
    # The promotion is SKIPPED when the intent classifier decided the task needs
    # no implants at all (converse/retrieve). Its rationale was that `lite` came
    # from a crude length rule which could not tell a greeting from a task, so any
    # agent declaring implants had to be rescued. A classifier that positively
    # identifies small talk supersedes that. Without this, `lite` is unreachable
    # in production — 43 of 43 agents declare `preferred_implants`, so every
    # lite decision was re-pinned to standard and the only surviving effect of
    # the whole lite half of the change was `suppress_persona_format`.
    # Gated on the MODE, not on `implant_budget == 0`. The budget is also zero for
    # `retrieve`, which is where `_detect_mode`'s no-lexicon fallback lands every
    # short query it cannot read — "Design a fault-tolerant event pipeline for 1M
    # events per second" and "Сделай ревью этого кода" both arrive there at
    # confidence 0.4. Waiving on the budget therefore stripped real work to zero
    # skills and zero implants on a guess. Only `converse`, which is positively
    # identified by `_is_pure_greeting`, may waive the promotion.
    classifier_waives_implants = profile is not None and profile.mode == "converse"
    if not tier_explicit and tier == "lite" and preferred_implants:
        if classifier_waives_implants:
            logger.info(
                "Tier kept at 'lite' for %s (intent=%s, confidence=%.2f)",
                agent_name, profile.mode, profile.confidence,
            )
        else:
            tier = "standard"
            logger.info(f"Tier promoted to 'standard' for {agent_name} (preferred implants declared)")

    if profile is not None and profile.tier != tier:
        profile = profile.with_tier(tier)

    query_hash = hash((query, configuration_revision()))
    # The cache key must carry the whole budget, not just the tier: once render
    # mode and pool size are decoupled from the tier, two profiles can share a
    # tier and still build different prompts. `cache_token` is colon-free so the
    # documented three-segment `agent:query_hash:X` shape survives.
    cache_key = f"{agent_name}:{query_hash}:{profile.cache_token if profile else tier}"
    if cache_key in SESSION_CACHE:
        cached = SESSION_CACHE[cache_key]
        cache_used = False
        if isinstance(cached, tuple):
            if len(cached) == 4:
                prompt, skills_loaded, implants_loaded, rules_loaded = cached
                cache_used = True
            elif len(cached) == 3:
                # Pre-rules cache shape — accept but treat rules as empty.
                prompt, skills_loaded, implants_loaded = cached
                rules_loaded = []
                cache_used = True
        if cache_used:
            logger.info(f"Session cache hit for {agent_name} (tier={tier})")
            debug_log("_load_and_enrich", "res", {"agent": agent_name, "tier": tier, "cache": "hit", "prompt_len": len(prompt)})
            return prompt, skills_loaded, implants_loaded, rules_loaded, tier
        # Unknown shape — log loudly and evict so the bug surfaces deterministically
        # instead of silently returning partial metadata.
        logger.warning(
            "SESSION_CACHE entry for %s has unexpected shape %r; evicting and refetching.",
            cache_key, type(cached).__name__ + (f"[{len(cached)}]" if isinstance(cached, tuple) else ""),
        )
        debug_log("_load_and_enrich", "cache_corrupt", {"agent": agent_name, "tier": tier, "shape": str(type(cached))})
        del SESSION_CACHE[cache_key]

    base_prompt = await loop.run_in_executor(None, load_agent_prompt, agent_name)

    enrichment = await enrich_agent_prompt(
        agent_name,
        base_prompt,
        query,
        chat_history_list,
        core_skills=core_skills,
        preferred_skills=preferred_skills,
        capable_skills=capable_skills,
        tier=tier,
        preferred_implants=preferred_implants,
        profile=profile,
    )
    final_prompt = enrichment.prompt
    SESSION_CACHE[cache_key] = (final_prompt, enrichment.skills_loaded, enrichment.implants_loaded, enrichment.rules_loaded)
    debug_log("_load_and_enrich", "res", {
        "agent": agent_name, "tier": tier, "cache": "miss",
        "task_mode": profile.mode if profile else None,
        "task_confidence": profile.confidence if profile else None,
        "depth_score": profile.depth_score if profile else None,
        "intent_signals": list(profile.signals) if profile else None,
        "prompt_len": len(final_prompt),
        "core_skills": core_skills,
        "preferred_skills": preferred_skills,
        "capable_skills": capable_skills,
        "preferred_implants": preferred_implants,
        "skills_loaded": enrichment.skills_loaded,
        "implants_loaded": enrichment.implants_loaded,
        "rules_loaded": enrichment.rules_loaded,
    })
    return final_prompt, enrichment.skills_loaded, enrichment.implants_loaded, enrichment.rules_loaded, tier


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


def _supports_sampling(ctx: Context | None) -> bool:
    if ctx is None or os.environ.get("AGENTS_TRANSPORT") == "http":
        return False
    try:
        return ctx.session.check_client_capability(
            ClientCapabilities(sampling=SamplingCapability())
        ) is True
    except (AttributeError, RuntimeError):
        return False


@mcp.tool()
@observe(name="route_and_load")
async def route_and_load(
    query: str,
    chat_history: Optional[List[str] | str] = None,
    protocol_version: int = PROTOCOL_VERSION,
    current_persona: PersonaDescriptor | None = None,
) -> str:
    """
    Route to a specialist. Call only for initial selection or a needed
    specialization change; keep the active role locally on continuations. Pass
    protocol_version=2 and current_persona. Returns separate instruction blocks.
    ROUTE_REQUIRED needs get_agent_context with the same version and descriptor.
    Explicit known roles can be loaded directly.
    """
    if protocol_version != PROTOCOL_VERSION:
        return error_response(UNSUPPORTED_PROTOCOL)
    return await route_persona(router, query, _normalize_chat_history(chat_history), current_persona, _is_meta_query)

@mcp.tool()
@observe(name="get_agent_context")
async def get_agent_context(
    agent_name: str, query: str, reasoning: str = "Selected by calling LLM",
    chat_history: Optional[List[str] | str] = None,
    protocol_version: int = PROTOCOL_VERSION, current_persona: PersonaDescriptor | None = None,
    force_reload: bool = False,
) -> str:
    """
    Load an explicitly chosen agent or a ROUTE_REQUIRED selection.
    Pass protocol_version=2 and current_persona. The same active agent
    returns NO_CHANGE without enrichment. Use force_reload=True to restore lost
    instructions; use refresh_persona_context to refresh the same role's skills.
    SUCCESS contains separate blocks and a descriptor; apply only when its
    replaces_activation_id matches your current activation. Preserve the dialogue.
    """
    if protocol_version != PROTOCOL_VERSION:
        return error_response(UNSUPPORTED_PROTOCOL)
    return await load_persona(
        router, agent_name, query, _normalize_chat_history(chat_history),
        current_persona, force_reload=force_reload, reasoning=reasoning,
    )

@mcp.tool()
async def refresh_persona_context(
    query: str, current_persona: PersonaDescriptor,
    chat_history: Optional[List[str] | str] = None,
) -> str:
    """Protocol 2: refresh the current role's complete bundle without choosing an agent.

    Call only when additional skills/implants are needed, never on ordinary
    continuations. Identical content returns NO_CHANGE; changed content returns
    a complete SUCCESS bundle to replace the current activation atomically.
    """
    try:
        current = parse_persona(current_persona)
        if current is None:
            raise ValueError("current_persona is required for refresh")
        return await load_persona(
            router, current.agent, query, _normalize_chat_history(chat_history),
            current, refresh=True,
        )
    except Exception as error:
        return error_response(error)


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

        off = component_toggles.disabled("implants")
        implants = [i for i in implants if i["filename"].removesuffix(".mdc") not in off]
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
    persona: PersonaDescriptor | None = None,
    persona_action: PersonaAction | None = None,
    ctx: Context | None = None,
) -> str:
    """End-of-turn logger. Compose the answer including its footer, call this tool
    with that exact response_content, then deliver the final answer. Pass the active persona descriptor and persona_action (keep/switch/refresh/restore).
    Pass the current user request verbatim as query, without paraphrasing or
    substituting a conversation summary.
    These are client-reported attribution, not proof of instruction compliance.

    Two sinks, independent of each other:

    * **history.md** — always written via ``HistoryWriter`` (append-only,
      content-hash deduped). Defaults: ``intent=query``, ``action="Agent: {agent_name}"``,
      ``outcome=response_content``. Pass ``intent``/``action``/``outcome``/``files``/``tags``
      to curate the entry; otherwise raw query/response are used.

    * **Langfuse** — generation trace recorded only when ``LANGFUSE_PUBLIC_KEY`` /
      ``LANGFUSE_SECRET_KEY`` are configured; otherwise the call is a no-op via
      ``langfuse_compat``.

    Returns at once, after validation and before either sink is written:
    ``{request_id, timestamp, langfuse: {status: "queued"}, history: {status: "queued"}}``
    plus the attribution. ``timestamp`` is the server's local time
    (``YYYY.MM.DD HH:MM:SS``); the final answer starts with it on its own line,
    and it is not part of ``response_content``. The sinks are written in the
    background with that timestamp; their failures go to the server log only
    and do not prevent each other. An unavailable workspace or invalid
    attribution writes nothing and returns a protocol ERROR without ``timestamp``.
    """
    try:
        client = client_context(ctx)
        root = client.require_root()
        active = parse_persona(persona)
        if active is not None and active.agent != agent_name:
            raise ValueError("agent_name does not match persona.agent")
        if persona_action is not None and active is None:
            raise ValueError("persona_action requires persona")
        if persona_action not in (None, "keep", "switch", "refresh", "restore"):
            raise ValueError("Invalid persona_action")
    except ValueError as error:
        return error_response(error, request_id, instruction=(
            "Nothing was logged. Keep the current activation; on workspace_required or "
            "workspace_invalid report unavailable logging, and do not retry logging in a loop."
        ))
    attribution = ({
        "persona": active.model_dump(), "persona_action": persona_action,
        "attribution": "client-reported",
    } if active else {})
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

    # Issued once; shown by the model in the answer and stored in both sinks.
    timestamp = datetime.datetime.now().strftime("%Y.%m.%d %H:%M:%S")

    # --- Langfuse trace (best-effort, skipped when keys absent) ---
    def _send_langfuse() -> None:
        if not is_langfuse_configured():
            return
        try:
            trace_id = langfuse.create_trace_id(seed=request_id)
            with langfuse.start_as_current_observation(
                as_type="span",
                name="agent_interaction",
                trace_context={"trace_id": trace_id},
                metadata={
                    "agent": agent_name, "source": "mcp-server",
                    "answer_timestamp": timestamp, **attribution,
                },
            ):
                with langfuse.start_as_current_observation(
                    as_type="generation",
                    name="response",
                    input=query[:2000],
                    metadata={
                        "agent": agent_name, "reasoning": reasoning or "",
                        "answer_timestamp": timestamp, **attribution,
                    },
                ) as gen:
                    gen.update(output=response_content[:5000])
            langfuse.flush()
        except Exception as e:
            logger.error("Langfuse logging failed: %s", e, exc_info=True)

    # --- History append (always; defaults to raw query/response) ---
    def _send_history() -> None:
        try:
            writer = HistoryWriter(str(root / "history.md"), str(root / "history"))
            eff_intent = (intent or query or "").strip()
            eff_action = (action or f"Agent: {agent_name}").strip()
            if active:
                eff_action += (
                    f"\nPersona (client-reported): {active.agent}; "
                    f"activation={active.activation_id}; revision={active.bundle_revision}; "
                    f"action={persona_action or 'unspecified'}"
                )
            eff_outcome = (outcome or response_content or "").strip()
            result = writer.append_entry(
                eff_intent, eff_action, eff_outcome, files, tags,
                {"answer_timestamp": timestamp},
            )
            if result.get("status") == "error":
                logger.error("History append failed: %s", result.get("error"))
        except Exception as e:
            logger.error("History append failed: %s", e, exc_info=True)

    # Both sinks are independent; the response does not wait for either.
    _langfuse_worker.submit(_send_langfuse)
    _history_worker.submit(_send_history)

    payload = {
        "request_id": request_id,
        "timestamp": timestamp,
        "langfuse": {"status": "queued"},
        "history": {"status": "queued"},
        **attribution,
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

    Builds a deterministic context bundle from the repo. When the client
    supports MCP sampling, asks the calling LLM to distill it into a
    structured summary and writes the result into the managed Repository
    Memory section of CLAUDE.md. Otherwise (or when sampling fails) it
    writes nothing and returns needs_summary; the summary is persisted only
    once write_repo_summary is called. Future Claude sessions read that
    section automatically and skip re-exploring the codebase.

    Returns JSON whose fields depend on status:
      refreshed: {status, action, path, hash, word_count, in_word_budget, summary_preview}
        (action: created, appended or replaced)
      up-to-date: {status, path, hash, word_count, in_word_budget, summary_preview}
      rejected, repo changed while sampling: {status, reason}
      rejected, sampled summary failed the sanity check:
        {status, reason, word_count, has_heading, summary_preview}
      needs_summary (no sampling, or sampling failed; nothing written):
        {status, workspace_id, repo_hash, repo_path, prompt, instruction}
      error: {status, error}
    Pass workspace_id, repo_path and repo_hash unchanged to write_repo_summary;
    the key names intentionally match its parameters.
    """
    try:
        client = client_context(ctx)
        repo_path = str(client.target(repo_path))

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
        if _supports_sampling(ctx):
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
            "workspace_id": client.workspace_id,
            "repo_hash": decision.current_hash,
            "repo_path": describer.repo_path,
            "prompt": prompt,
            "instruction": (
                "Sampling is not available or failed. Generate the repository overview by "
                "following the prompt above, then call write_repo_summary("
                f'summary=<your output>, repo_hash="{decision.current_hash}", '
                f'repo_path={json.dumps(repo_path)}, workspace_id={json.dumps(client.workspace_id)}'
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
    workspace_id: Optional[str] = None,
    ctx: Context | None = None,
) -> str:
    """Persist a repository summary after describe_repo returned status='needs_summary'.

    Call this with the summary you generated from the prompt and the
    repo_hash value from the describe_repo response. repo_path and
    workspace_id are optional over stdio; over HTTP pass both back from
    that response unchanged.

    Returns JSON whose fields depend on status:
      refreshed: {status, action, path, hash, word_count, in_word_budget, summary_preview}
      rejected, stale repo_hash: {status, reason}
      rejected, summary failed the sanity check:
        {status, reason, word_count, has_heading, summary_preview}
      error: {status, error}
    """
    try:
        client = client_context(ctx)
        client.require_root()
        if client.transport == "http" and (workspace_id != client.workspace_id or repo_path is None):
            raise WorkspaceError("workspace_invalid: pass the original workspace_id and repo_path from describe_repo")
        repo_path = str(client.target(repo_path))

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
    ctx: Context | None = None,
) -> str:
    """Read recent history entries or run a lazy semantic search.

    - Without ``query``: returns up to ``limit`` newest entries; ``since``
      (ISO8601 prefix) optionally filters for entries at or after that
      timestamp.
    - With ``query``: builds the vector index on first use (and rebuilds it
      when the content of history.md or the embedding configuration changes),
      then returns semantically nearest entries with cosine distance.

    Returns JSON:
      {entries: [...], total, mode}
      mode ∈ {"recency", "semantic"}.

    Entry shape depends on mode:
    - recency: {id, timestamp, intent, action, outcome, files, tags, metadata}.
    - semantic: {id, distance, document, timestamp, intent, tags}.
    """
    try:
        client = client_context(ctx)
        root = client.require_root()
        limit = max(1, min(limit, 500))
        query = (query or "").strip() or None
        debug_log("read_history", "req", {"limit": limit, "since": since, "query": query})
        loop = asyncio.get_running_loop()

        if query:
            def search():
                with _history_stores.acquire(client) as store:
                    return store.search(query, limit=limit)
            results = await loop.run_in_executor(None, search)
            payload = {"mode": "semantic", "total": len(results), "entries": results}
        else:
            reader = HistoryReader(str(root / "history.md"))
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
async def ask(
    query: str, current_persona: Optional[str] = None, protocol_version: Optional[str] = None,
) -> list:
    """Select a specialist and return a persona bundle.
    current_persona is the retained descriptor JSON, if any; protocol_version is optional (2).
    """
    try:
        _check_prompt_protocol(protocol_version)
        current = parse_persona(json.loads(current_persona)) if current_persona else None
        result = await route_persona(router, query, [], current, _is_meta_query)
        return [UserMessage(
            f"Requested persona selection:\n{result}\n\nUser query: {query}\n"
            "Apply successful blocks within existing instructions, preserving the dialogue. "
            "When no descriptor was supplied, this explicit command sets the role sequentially "
            "for this dialogue. Otherwise enforce the activation replacement check."
        )]
    except Exception as e:
        return [UserMessage(f"{query}\n\n(Routing error: {e})")]


def _build_retrieval_query(
    invoked_cmd: Optional[str],
    primary_trigger: str,
    query: str,
) -> str:
    """Build the query a slash prompt passes to persona-bundle skill/implant selection.

    For alias invocations (``invoked_cmd != primary_trigger``), prepend the
    invoked slash command so that alias-specific skill keywords — e.g.,
    ``/co_lawyer`` in ``skill-jurisdiction-co.mdc``'s ``keywords:`` list —
    participate in capable-skill retrieval. Without this, ``/co_lawyer``
    would route to the lawyer agent but miss the matching jurisdiction
    skill when the user's text is short or generic.

    For the primary ``trigger_command`` (and when ``invoked_cmd`` is ``None``
    or empty), return the user's query unchanged. Prepending the primary
    trigger would leak command-name signals (``audit``, ``review``,
    ``compare``) into tier inference and force ``deep`` tier on otherwise
    short queries.
    """
    if invoked_cmd and invoked_cmd != primary_trigger:
        return f"{invoked_cmd} {query}"
    return query


def _register_agent_prompts():
    """Register a /slash prompt for each agent's trigger_command and any aliases.

    Each agent's primary slash command comes from ``routing.trigger_command``.
    Agents may also declare ``routing.aliases``: a list of additional slash
    commands that route to the same agent (e.g., legacy per-country commands
    consolidated into a single multi-jurisdiction agent). Every entry is
    registered as its own MCP slash prompt.

    The retrieval query is built via :func:`_build_retrieval_query`, which
    prepends the invoked slash command only when an alias is used.
    """
    for agent_name in router.available_agents:
        meta = get_agent_metadata(agent_name)
        routing_meta = meta.get("routing", {})
        trigger = routing_meta.get("trigger_command", "")
        aliases = routing_meta.get("aliases", []) or []

        commands = [c for c in [trigger, *aliases] if c]
        if not commands:
            continue

        display_name = meta.get("identity", {}).get("display_name", agent_name)
        role = meta.get("identity", {}).get("role", "")

        def make_prompt(a_name, d_name, r, p_name, invoked_cmd, primary_trigger):
            async def agent_prompt(
                query: str, current_persona: Optional[str] = None, protocol_version: Optional[str] = None,
            ) -> list:
                retrieval_query = _build_retrieval_query(invoked_cmd, primary_trigger, query)
                try:
                    _check_prompt_protocol(protocol_version)
                    current = parse_persona(json.loads(current_persona)) if current_persona else None
                    result = await load_persona(router, a_name, retrieval_query, [], current)
                    return [UserMessage(
                        f"Requested persona:\n{result}\n\nUser query: {query}\n"
                        "Apply successful blocks within existing instructions, preserving the dialogue. "
                        "When no descriptor was supplied, this explicit command sets the role "
                        "sequentially for this dialogue. Otherwise enforce the activation replacement check."
                    )]
                except Exception as e:
                    return [UserMessage(f"{query}\n\n(Error loading {d_name}: {e})")]
            agent_prompt.__name__ = p_name
            agent_prompt.__doc__ = (
                f"{d_name} — {r}. Returns a persona bundle; pass current_persona "
                "as retained descriptor JSON."
            )
            return agent_prompt

        for cmd in commands:
            prompt_name = cmd.lstrip("/")
            mcp.prompt()(make_prompt(agent_name, display_name, role, prompt_name, cmd, trigger))


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
            "Do not call any other tools first. If it returns status "
            "`needs_summary`, nothing was written yet: generate the overview by "
            "following its `prompt`, then call `write_repo_summary` with that "
            "summary and the `repo_hash`, `repo_path` and `workspace_id` it "
            "returned, unchanged. Then report the final status, hash, word count, "
            "and the summary preview."
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


def _warmup_rules():
    """Pre-load the lenient rules cache at startup.

    ``get_rules()`` does sync filesystem I/O on its first call (parsing
    ``rules/rule-*.mdc``) and then serves that cache for the process lifetime.
    Only the per-query evaluation path (``_load_and_enrich`` via
    ``get_dynamic_context_string``) uses the cache, and it calls the loader on
    the event loop, so the warmup keeps its first request from blocking.
    Protocol 2 bundles do not use this cache: they re-read the rules in strict
    mode on every build, so rule edits apply to the next bundle. The lenient
    load skips invalid files and a failure here is only logged, so stdio starts
    even with an invalid rule set (unlike the daemon's strict warmup, which
    blocks readiness); each activation then returns ERROR.
    """
    try:
        from src.engine.rules import get_rules
        rules = get_rules()
        logger.info("Rules layer warmed up: %d rule(s) loaded", len(rules))
    except Exception as e:
        logger.warning("Rules layer warmup failed: %s", e, exc_info=True)


if __name__ == "__main__":
    _warmup_embedding_model()
    _warmup_rules()
    # Background self-update (Phase B): in a daemon thread WITHOUT blocking startup,
    # prepare the next update — fetch + build the new version's indexes in an
    # isolated git worktree and write a marker — so the next idle start activates
    # it via a fast move under startup.py's exclusive lease. Legacy mode already
    # ran synchronously there and starts no background thread. No-op unless on
    # the target branch. Started after warmup so reindex doesn't contend
    # for the model load. See src/self_update.py.
    from src.self_update import log_last_update, start_background_update
    log_last_update()
    start_background_update()
    try:
        mcp.run()
    finally:
        drain_pending_logs()
