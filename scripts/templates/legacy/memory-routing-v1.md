---
name: Agents-Core routing is mandatory
description: ALWAYS call route_and_load() before answering any query — no exceptions, no judgment calls
type: feedback
---

Always call `route_and_load(query)` from Agents-Core MCP before responding to any user query.

**Why:** The user explicitly configured this as a BLOCKING REQUIREMENT in CLAUDE.md. The instruction is unconditional — not at the assistant's discretion. There was an incident where routing was skipped "for efficiency" and the user flagged it.

**How to apply:** Before generating any response — call `route_and_load(query)` via the Agents-Core MCP server, then handle the response status per the routing protocol in CLAUDE.md. No exceptions, even if the task seems straightforward.
