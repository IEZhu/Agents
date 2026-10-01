---
name: Agents-Core persona continuity
description: Assess the active role locally; route only when another specialization is needed
type: feedback
---

Follow the Agents-Core Persona Protocol in the managed CLAUDE.md section.
Before each answer, silently assess whether the current role fits. On continuations,
confirmations, format changes, and tasks within its scope, keep the role without
routing, selecting another agent, or refreshing its bundle. Route or directly load
a known role only for a necessary switch. Restore lost instructions for a known
role without reselecting it; refresh only when extra skills are needed.

On each successful activation, replace all four persona, rules, skills, and implants
blocks, including empty blocks; changed or removed rules supersede old rules.
Preserve higher-priority instructions, conversation facts, goals, constraints,
permissions, and results.
Keep the applied descriptor and returned footer; never clear history or caches to
switch.

Except for the unavailable-MCP fallback below, compose the answer plus saved
footer. Before delivering it, call
`log_interaction(agent_name, query, response_content, persona=..., persona_action=...)`:
use the active specialist's canonical name as `agent_name`, the current user
request verbatim as `query`, and the complete composed answer plus footer as
`response_content`. Pass the active descriptor and the actual keep/switch/refresh/restore
action. The call returns at once; when the footer's `Rules` list includes
`answer-timestamp` and it returns a `timestamp`, deliver the answer with that
`timestamp` as its first line followed by an empty line (it is not part of
`response_content`), otherwise with no time line. The final answer may end the tool loop.

When MCP is unavailable, follow the explicit fallback in CLAUDE.md: keep valid
retained attribution when available, skip unavailable logging, and label manual
roles as manual without inventing a descriptor or MCP footer. Activate a manually
loaded role through MCP before resuming normal attribution after recovery.
