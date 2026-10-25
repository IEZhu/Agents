<!-- variant: full -->
## Apply a complete response

- `ROUTE_REQUIRED`: select the best canonical agent from `candidates`, then call
  `get_agent_context` with `protocol_version=2` and the same `current_persona`
  before answering. The route alone has not loaded a persona.
- `SUCCESS`: validate the complete descriptor and all four blocks. The response's
  `replaces_activation_id` must match the active activation, or be null when none
  exists. Ignore a stale response for another activation. A repeated response with
  an already applied `activation_id` is a no-op.
- On every `SUCCESS`, replace all four previously applied blocks with the returned
  `persona_block`, `rules_block`, `skills_block`, and `implants_block`, including
  any empty blocks. This applies to switches, restores, and refreshes: changed or
  removed rules supersede the previous rules instead of accumulating alongside
  them. Previous role-specific methods, output formats, and style cease to apply.
  Preserve higher-priority instructions, conversation history, facts, goals,
  constraints, user permissions, and tool results.
- Save `persona` and the exact returned `footer` only after successful application.
- After a conversation's first activation, a `SUCCESS` carries a short `instruction`
  that points to the full one by `instruction_revision`; the full rules still hold.
  Restore with `force_reload=True` when the full instruction is gone from your
  context. A first activation may carry `warnings` about outdated managed routing
  sections: mention them once to the user.
- `NO_CHANGE`: retain the active instructions, descriptor, and footer. It does not
  certify that source files are current. A refresh checks the bundle revision.
- `ERROR`, missing blocks, or invalid descriptors: keep the previous state and
  report the failed operation without claiming that the requested role loaded.

This is logical replacement of active role instructions. MCP cannot physically
delete previous messages from the client's context. Do not clear history, memory,
or any server cache to switch roles. A tool result is not a system message.

Explicit agent slash commands load that agent directly; `/ask` explicitly requests
routing. Both return the same bundles: pass `current_persona` as descriptor JSON
when available and apply the same replacement rules. Omitting the descriptor lets
the explicit command set the role sequentially for the current conversation; do
not run competing activations in the background.

<!-- variant: server -->
Response statuses:
- SUCCESS → validate the complete `persona` descriptor and `persona_block`, `rules_block`, `skills_block`, `implants_block`. Apply only if `replaces_activation_id` matches the current activation (null for initial load), replacing all four blocks, including empty blocks. Then save `persona` and `footer`.
- ROUTE_REQUIRED → select from `candidates`, then call `get_agent_context(agent_name, query, protocol_version=2, current_persona=...)`; this is an intermediate result, not an activated persona; retain the previous activation until SUCCESS.
- NO_CHANGE → keep the current blocks, descriptor and footer unchanged.
- ERROR → keep the previous activation, report the failed operation and follow the response's `instruction`; for persona tools the requested bundle was not applied. Do not partially activate returned content.

<!-- variant: server-prompts -->
The ask and agent slash prompts return the same bundles; pass current_persona as descriptor JSON when available.

<!-- variant: bundle -->
For ordinary conversation activations: Apply this complete persona bundle only if replaces_activation_id matches your current activation (null for initial load). Ignore replayed or stale activations. On SUCCESS, replace all four previous blocks with persona_block, rules_block, skills_block, and implants_block, including empty blocks. Changed or removed rules supersede previous rules on switches, restores, and refreshes. Preserve higher-priority instructions, conversation, facts, goals, constraints, user permissions, and tool results. This is a logical replacement, not deletion of transcript messages.

<!-- variant: bundle-pointer -->
A later SUCCESS of this conversation carries a short instruction that points to this one by its instruction_revision, and these rules still hold; force_reload=True returns this text when it is gone from your context. A first activation may carry warnings about outdated managed routing sections: mention them once to the user.

<!-- variant: memory -->
On each successful activation, replace all four persona, rules, skills, and implants
blocks, including empty blocks; changed or removed rules supersede old rules.
Preserve higher-priority instructions, conversation facts, goals, constraints,
permissions, and results.
Keep the applied descriptor and returned footer; never clear history or caches to
switch.
