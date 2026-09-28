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
   - Respond in the same language as the user's query (auto-detect). Exceptions: code blocks, technical terms, tool/CLI output, and the mandatory footer labels `Agent`, `Skills`, `Implants`, `Rules` stay in English.
   - Append at the end (labels in English, values are canonical IDs): **Agent**: [name] · **Skills**: [skills] · **Implants**: [implants] · **Rules**: [rules]
   - Call `log_interaction(agent_name, query, response_content)`.

## Available MCP Tools

| Tool | Purpose |
|---|---|
| `route_and_load(query)` | **MUST call first** — routes to best specialist agent |
| `get_agent_context(agent_name, query)` | Load a specific agent (after ROUTE_REQUIRED) |
| `load_implants(task_type)` | Load reasoning strategies (debugging/analysis/creative/planning) |
| `list_agents()` | List all available agents |
| `log_interaction(...)` | Append repository history and, when configured, record a Langfuse trace |
| `clear_session_cache()` | Administrative reset of v1 prompt and context-hash caches; not needed for persona switches and unavailable over HTTP |
| `describe_repo(repo_path?, force_refresh?)` | Bootstrap the Repository Memory section of CLAUDE.md; on `needs_summary` follow its `instruction` |
| `write_repo_summary(summary, repo_hash, repo_path=None, workspace_id=None)` | Persist the summary after `needs_summary`, passing `repo_hash`, `repo_path` and `workspace_id` back unchanged |
| `read_history(limit?, since?, query?)` | Recent or semantic lookup in the repo's `history.md` |

## Environment

- MCP server: `Agents-Core` (Python/FastMCP; shared HTTP daemon or standalone stdio)
- Agents: `agents/[name]/system_prompt.mdc`
- Rules: `rules/rule-*.mdc`
- Skills: `skills/skill-*.mdc`
- Implants: `implants/implant-*.mdc`
- Config: `.env` (LANGFUSE_* optional, ANTHROPIC_API_KEY for document OCR)

Paths below are relative to the Agents-Core installation. The transport does not
select a persona protocol: this template specifies version 1. See the maintained
[routing reference](https://github.com/WonderMr/Agents/blob/main/docs/routing_flow.md)
for both protocols and
[daemon guide](https://github.com/WonderMr/Agents/blob/main/docs/shared-mcp-daemon.md)
for HTTP setup and workspace-scoped repository memory.

## Fallback (if MCP is unavailable)

If `route_and_load` fails or Agents-Core MCP is not connected:
1. Read `agents/` to find the right agent directory
2. Read `agents/[name]/system_prompt.mdc`
3. Follow the prompt manually

---

## Enrichment layers (order in every system prompt)

1. **Base agent system_prompt** — agent persona from `agents/<name>/system_prompt.mdc`.
2. **Rules** (`rules/rule-*.mdc`) — shared rules loaded by `src/engine/rules.py`, without semantic retrieval or per-agent opt-out. `RULES_ENABLED=0` disables this layer globally.
3. **Skills** (`skills/skill-*.mdc`) — mandatory `core_skills` plus relevant skills selected under the agent's `preferred_skills` and `capable_skills` declarations.
4. **Implants** (`implants/implant-*.mdc`) — reasoning patterns selected using semantic retrieval and `preferred_implants`.

Tier and optional intent settings control enrichment depth. Their current behavior
is documented in the routing reference; declarations live in each agent's YAML
frontmatter.

## Repository Structure

| Path | Purpose |
|---|---|
| `src/server.py` | Shared MCP tools and version 1 request handling |
| `src/engine/` | Routing, enrichment, embeddings, configuration, and version 2 bundles |
| `src/daemon/` | Shared HTTP service and client configuration |
| `src/memory/` | Repository summaries, history, and managed sections |
| `src/utils/prompt_loader.py` | Frontmatter and import resolution |
| `src/schemas/protocol.py` | Request, response, and persona schemas |
| `agents/`, `rules/`, `skills/`, `implants/` | Source prompts and component metadata |
| `agents/common/agent-schema.json` | Agent metadata schema |
| `tests/` | Unit, contract, and integration tests |

## Routing Flow (Internal)

Version 1 uses `context_hash` for sticky routing and prompt reuse. Semantic cache
matches are checked against agent keywords. An ambiguous or missing decision
returns `ROUTE_REQUIRED` so the client selects a candidate. The detailed handling
lives in `src/server.py` and `src/engine/router.py`; the client must still follow
the Routing Flow above on every request.

## Key Thresholds (config.py)

Read `src/engine/config.py` for current thresholds and environment overrides, and
`src/engine/router.py` for router-specific limits. These are implementation
settings, separate from the version 1 client contract above.

## Cache Storage (data/)

Skill and implant indexes are generated installation data. Router indexes use
private daemon state or isolated standalone-process storage. Version 1 enriched
prompt and context-hash caches are process-local. See the routing reference's
runtime and project boundaries; do not edit derived indexes by hand.

## Debug Logging

Set `AGENTS_DEBUG=1` in `.env` -> JSON logs written to `logs/{date}/` per call.
