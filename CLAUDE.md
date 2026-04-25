# CRITICAL: Agents-Core Routing Protocol

IMPORTANT: These instructions OVERRIDE any default behavior. You MUST follow them exactly.

## BLOCKING REQUIREMENT — MCP Routing

**Before answering ANY user query, you MUST call `route_and_load(query)` from the Agents-Core MCP server.** This is not optional. Do NOT answer directly — always route first.

This applies to ALL queries: coding, research, questions, documentation, debugging — everything.

### Routing Flow

1. **ALWAYS call `route_and_load(query)` FIRST** with the user's message.
   - On multi-turn: pass previous `context_hash` for delta mode.

2. **Handle the response based on status:**
   - `SUCCESS_SAMPLED` → Display `response` to the user as-is.
   - `SUCCESS` → Use `system_prompt` as context for your answer.
   - `ROUTE_REQUIRED` → **STOP all other actions.** Do NOT call any other tools in parallel.
     Pick best agent from `candidates`, call `get_agent_context(agent_name, query)` as your ONLY next action.
     Wait for its response before doing anything else.
   - `NO_CHANGE` → Context unchanged. Keep current persona.
   - `ERROR` → Answer directly (only in this case).

3. **Post-flight (after EVERY response):**
   - Respond in the same language as the user's query (auto-detect). Exceptions: code blocks, technical terms, and tool/CLI output stay in English.
   - Append at the end: **Agent**: [name] · **Skills**: [skills] · **Implants**: [implants] · **Rules**: [rules]
   - Call `log_interaction(agent_name, query, response_content)` — this **always** appends an entry to `history.md` and (if Langfuse is configured) sends a generation trace. For meaningful turns (decisions, fixes, refactors, new features), additionally pass `intent=`, `action=`, `outcome=`, `files=[...]`, `tags=[...]` to curate the entry; otherwise the raw query/response are used.

4. **Repository memory (first session per repo):**
   - On the first session in an unfamiliar repo, call `describe_repo()` once to bootstrap the managed Repository Memory section in CLAUDE.md. Subsequent calls are no-ops unless `force_refresh=True` or the repo manifest changes.

## Available MCP Tools

| Tool | Purpose |
|---|---|
| `route_and_load(query)` | **MUST call first** — routes to best specialist agent |
| `get_agent_context(agent_name, query)` | Load a specific agent (after ROUTE_REQUIRED) |
| `load_implants(task_type)` | Load reasoning strategies (debugging/analysis/creative/planning) |
| `list_agents()` | List all available agents |
| `log_interaction(agent_name, query, response_content, intent?, action?, outcome?, files?, tags?)` | End-of-turn logger — **always** appends to `history.md` (deduped) and, if Langfuse is configured, records a generation trace. Curate the history entry with the optional `intent`/`action`/`outcome` params. |
| `clear_session_cache()` | Clear routing cache (use when switching contexts) |
| `describe_repo(force_refresh=False)` | One-shot repo bootstrap — writes summary into CLAUDE.md |
| `read_history(limit?, since?, query?)` | Recent entries or lazy semantic recall over history |

## Environment

- MCP server: `Agents-Core` (stdio transport, Python/FastMCP)
- Agents: `agents/[name]/system_prompt.mdc`
- Skills: `skills/skill-*.mdc`
- Implants: `implants/implant-*.mdc`
- Capabilities: `agents/capabilities/registry.yaml`
- Config: `.env` (LANGFUSE_* optional, ANTHROPIC_API_KEY for document OCR)

## Fallback (if MCP is unavailable)

If `route_and_load` fails or Agents-Core MCP is not connected:
1. Read `agents/` to find the right agent directory
2. Read `agents/[name]/system_prompt.mdc`
3. Follow the prompt manually

---

## Enrichment layers (order in every system prompt)

1. **Base agent system_prompt** — agent persona from `agents/<name>/system_prompt.mdc`.
2. **Rules** (`rules/rule-*.mdc`) — **always-on, universal, no semantic retrieval, no opt-out**. Loaded by `src/engine/rules.py`. Architectural invariant: rules apply to every agent without exception. Per-agent guidance belongs in `skills/`. Toggle via `RULES_ENABLED=0`.
3. **Skills** (`skills/skill-*.mdc`) — semantic retrieval + per-agent opt-in via `preferred_skills` / `capabilities`. Caveman-style output compression lives here as `skill-caveman-tokenomics`, opt-in via the `concise-output` capability.
4. **Capability Directives** — terse one-liners from `agents/capabilities/registry.yaml`.
5. **Implants** (`implants/implant-*.mdc`) — semantic retrieval, cognitive reasoning patterns.

## Repository Structure

```
src/
  server.py            — MCP server: route_and_load(), get_agent_context(), clear_session_cache()
  engine/
    router.py          — SemanticRouter: cache lookup, keyword matching, agent catalog
    vector_store.py    — NumpyVectorStore: numpy-based cosine similarity store
    embedder.py        — FastEmbed wrapper (model configurable via EMBEDDING_MODEL env var)
    config.py          — Thresholds, paths, env-based configuration (RULES_ENABLED, RULES_DIR)
    enrichment.py      — Prompt enrichment with rules/skills/implants by tier (lite/standard/deep)
    rules.py           — Universal always-on rules layer (no retrieval, no opt-out)
    skills.py          — Skill retrieval from vector store
    implants.py        — Implant retrieval from vector store
    capabilities.py    — Capability -> skill resolution via registry.yaml
    language.py        — Language detection (langdetect, 24 languages)
    context.py         — Context management
  utils/
    prompt_loader.py   — Frontmatter parsing, agent metadata, @import resolution
    debug_logger.py    — JSON debug logging (AGENTS_DEBUG=1)
    langfuse_compat.py — Optional Langfuse observability
  schemas/
    protocol.py        — RouterDecision, AgentRequest, AgentResponse
agents/
  [name]/system_prompt.mdc — Agent persona with YAML frontmatter (identity, routing, skills)
  common/agent-schema.json — Frontmatter JSON schema
  capabilities/registry.yaml — Capability -> skill mapping (incl. concise-output)
rules/rule-*.mdc           — Universal always-on directives (accuracy, honesty, language, sycophancy)
skills/skill-*.mdc         — Compiled skill prompts (incl. skill-caveman-tokenomics)
implants/implant-*.mdc     — Cognitive reasoning implants
tests/
  test_routing.py      — Routing logic, sticky routing, keyword boosting tests
  test_vector_store.py — NumpyVectorStore correctness tests
  test_language.py     — Language detection tests
  test_rules.py        — Rules layer: parsing, priority, invariant (no opt-out fields)
```

## Routing Flow (Internal)

```
Query -> route_and_load()
  |-- Sticky agent? -> query_nearest() -> distance-based decisions
  |     |-- d < 0.02 + keyword check: auto-switch (validate with keywords)
  |     |-- d < 0.05 & same agent + keyword check: confirm or override
  |     |-- d >= 0.05: ROUTE_REQUIRED (topic change)
  |     +-- else: keep sticky (stability)
  +-- No sticky -> lookup_cache() (threshold: d < 0.05)
        |-- Hit + keyword_veto() confirms -> SUCCESS
        |-- Hit + keyword_veto() overrides -> use keyword winner
        |-- Hit + keyword_veto() ambiguous -> ROUTE_REQUIRED
        |-- Meta-query -> universal_agent (lite tier)
        +-- Miss -> ROUTE_REQUIRED (LLM picks from candidates)
```

## Key Thresholds (config.py)

| Constant | Default | Purpose |
|----------|---------|---------|
| `ROUTER_SIMILARITY_THRESHOLD` | 0.95 | Cache hit if cosine distance < 0.05 |
| `STICKY_SWITCH_THRESHOLD` | 0.02 | Auto-switch only for near-duplicate queries |
| `KEYWORD_OVERRIDE_MIN_HITS` | 1 | Min keyword hits to consider cache override |
| `KEYWORD_UNIQUENESS_RATIO` | 2.0 | Top agent must have >= 2x hits vs second-best |
| `SKILLS_RELEVANCE_THRESHOLD` | 0.75 | Cosine distance cutoff for skill retrieval |
| `IMPLANTS_RELEVANCE_THRESHOLD` | 0.85 | Cosine distance cutoff for implant retrieval |
| `SESSION_CACHE_MAX_SIZE` | 128 | Max enriched prompt cache entries |
| `SESSION_CACHE_TTL_SECONDS` | 600 | Prompt cache TTL (10 min) |
| `ROUTER_CACHE_MAX_SIZE` | 500 | Max routing decisions in vector store (router.py) |

## Cache Storage (data/)

- `router_cache.npz` + `.json` — Semantic routing cache (500 entries max, atomic writes)
- `skills_store.npz` + `.json` — Skills vector store
- `implants_store.npz` + `.json` — Implants vector store
- `.router_cache_model` — Embedding model hash (auto-invalidates cache on model change)

## Debug Logging

Set `AGENTS_DEBUG=1` in `.env` -> JSON logs written to `logs/{date}/` per call.
