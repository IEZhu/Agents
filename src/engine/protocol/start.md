<!-- variant: full -->
## Start before the first answer

When MCP is available and this conversation has no prior persona activation,
activate a persona before the first final answer, including a greeting or a
project-status question. Do this without waiting for the user to mention
Agents-Core, a role, the footer, or logging. Load an explicitly requested known
role with `get_agent_context`; otherwise call `route_and_load` with the user's
request, `protocol_version=2` and `current_persona=null`.

The Claude desktop app adds its own `Agents-Core-Desktop` server to its Code
sessions; Claude Code denies it there but may still list its tools. When both
`Agents-Core` and `Agents-Core-Desktop` are listed, make every call through
`Agents-Core`, the first one included. If a call is refused because
`Agents-Core-Desktop` is denied, repeat it through `Agents-Core`; never continue
without the persona or the log.

`ROUTE_REQUIRED` is an intermediate result, not an activated persona. Select the
best canonical candidate and call `get_agent_context` in the same turn; do not
finish with a candidate list or ask permission to complete the activation.
Discovery tools, repository memory, and workflow listings do not activate a
persona. Apply a valid `SUCCESS` as below, then compose the answer with its exact
footer and call `log_interaction` before sending it. If activation fails, report
that failure and follow the error or unavailable-MCP instructions below.

<!-- variant: server -->
Before the first final answer with MCP available and no prior activation, load an explicitly requested known role with get_agent_context, otherwise route_and_load with current_persona=null, even for greetings and project-status questions. Complete ROUTE_REQUIRED with get_agent_context in the same turn before answering; do not ask permission. Discovery and memory tools activate no persona. Report activation failures; follow the fallback below.

<!-- variant: memory -->
The following activation and logging steps apply to ordinary conversations.
Before the first final answer, when MCP is available and there is no prior persona
activation, load an explicitly requested known role with `get_agent_context`, or
call `route_and_load` with `protocol_version=2` and `current_persona=null`. Do not
wait for a reminder about Agents-Core, the footer, or logging. `ROUTE_REQUIRED` is
intermediate: select its best candidate and complete `get_agent_context` in the
same turn before answering. Discovery and memory tools do not activate a persona.
On activation failure, report it and follow the error or unavailable-MCP rules.
