<!-- variant: full -->
## Compatibility and unavailable servers

The server supports only this protocol. If the `route_and_load` or
`get_agent_context` schema lacks `protocol_version` or `current_persona`, the
server predates it: explain once that the server must be updated, answer without
claiming an activated persona, and stop retrying unsupported calls. Do not treat
an ordinary loading error as protocol incompatibility.

When MCP is unavailable, this fallback overrides the footer and logging steps
above only where their required state or tool is unavailable:

- If a valid MCP bundle is retained, keep its role and descriptor. Reuse its exact
  footer when available, state that MCP is unavailable, and skip logging while
  `log_interaction` is unavailable. Do not invent a lost footer or component list.
- If no valid bundle is retained and repository files are available, load the
  known or appropriate `agents/<name>/system_prompt.mdc` manually. Attribute the
  answer to that role as a manual fallback, with MCP attribution unavailable.
  Omit the MCP footer and descriptor-based logging; do not fabricate a
  descriptor, activation ID, revision, footer, or loaded-component list. If no
  prompt is accessible either, answer without claiming an activated persona.
- When MCP becomes available again, a retained bundle with its descriptor and
  exact footer follows the normal local keep/switch assessment. A manually loaded
  role, or a retained role missing its descriptor or exact footer, requires a real
  activation: call `get_agent_context` for its known name with `protocol_version=2`
  and `force_reload=True`, passing the last real descriptor if retained, otherwise
  null. If no role name is known, perform initial routing. Resume normal footer
  and logging requirements only after a successful MCP activation; never turn
  the manual fallback into a synthetic past activation or log it retroactively
  as though MCP had supplied it.

<!-- variant: server -->
MCP-unavailable fallback: reuse a valid retained bundle's descriptor and exact footer. If none is retained, disclose manual fallback and omit the MCP footer and descriptor attribution; do not fabricate them. Skip unavailable logging.

<!-- variant: memory -->
When MCP is unavailable, follow the explicit fallback in CLAUDE.md: keep valid
retained attribution when available, skip unavailable logging, and label manual
roles as manual without inventing a descriptor or MCP footer. Activate a manually
loaded role through MCP before resuming normal attribution after recovery.
