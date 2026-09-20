---
name: Agents-Core persona continuity
description: Assess the active role locally; route only when another specialization is needed
type: feedback
---

Follow the Agents-Core Persona Protocol version 2 in the managed CLAUDE.md section.
Before each answer, silently assess whether the current role fits. On continuations,
confirmations, format changes, and tasks within its scope, keep the role without
routing, selecting another agent, or refreshing its bundle. Route or directly load
a known role only for a necessary switch. Restore lost instructions for a known
role without reselecting it; refresh only when extra skills are needed.

Replace only prior persona guidance and its skills/implants. Preserve conversation
facts, goals, constraints, permissions, and results. General rules remain in force.
Keep the applied descriptor and returned footer; never clear history or caches to
switch. If the server lacks version 2, follow the one-time v1 fallback in CLAUDE.md.

Compose the answer and saved footer, call log_interaction with that exact text
before the final answer, then deliver it; the final answer may end the tool loop.
