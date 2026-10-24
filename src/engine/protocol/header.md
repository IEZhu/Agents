<!-- variant: server -->
Persona protocol 2: pass protocol_version=2 to route_and_load and get_agent_context.
If Agents-Core is listed, make every call there, the first too; retry Desktop denials there.

<!-- variant: memory -->
---
name: Agents-Core persona continuity
description: Assess the active role locally; route only when another specialization is needed
type: feedback
---

Follow the Agents-Core Persona Protocol in the managed CLAUDE.md section.
When both `Agents-Core` and `Agents-Core-Desktop` are listed, call `Agents-Core` from the first call on, and repeat a call refused for `Agents-Core-Desktop` through `Agents-Core`.
