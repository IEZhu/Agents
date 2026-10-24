<!-- variant: full -->
A worker given a valid `agents-core/subagent/1` prepared package already has its
task-scoped role. Use that package instead of the conversation activation, the
first-work repository description and the finish steps below: do not call
`route_and_load` or `get_agent_context`, inherit the parent's activation, add a
footer/timestamp, or call `log_interaction` for the worker result or for any
message that ends its turn. Return only the result in the package's format. Stay within
the task and its allowed actions; no role changes or further delegation unless
assigned.

If a worker is explicitly allowed to delegate further, it must follow the same preparation and validation exchange for its own children. The conversation activation and unavailable-MCP fallback below apply to ordinary conversations; they do not waive this delegation contract.

<!-- variant: server -->
A worker with a prepared agents-core/subagent/1 package uses that task role: no full persona activation, inherited parent descriptor, footer, timestamp, first-work repository description or per-turn log_interaction. Return its result only.

<!-- variant: bundle -->
A worker already given a prepared agents-core/subagent/1 package uses that task role and result format, without this conversation activation, a parent descriptor, footer, timestamp, first-work repository description or per-result/per-turn log_interaction.

<!-- variant: memory -->
A valid `agents-core/subagent/1` package supplies the worker's role: no full persona activation, inherited parent descriptor, footer, timestamp, first-work repository description or per-result/per-turn `log_interaction`. Return only the result in the package's format, within scope.
