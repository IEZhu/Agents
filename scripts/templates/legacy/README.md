# Previously generated routing reminders

Exact copies of routing memory files that earlier installers wrote. The migration
helper compares an installed reminder with these bytes: a match is replaced with
the current `../memory-routing.md`, anything else is preserved as user-edited.
Do not edit these files.

Before changing `../memory-routing.md`, copy its current bytes here unchanged as
`memory-routing-v<N>.md`, using the next unused number. The helper reads only
files matching `memory-routing-*.md`; without that copy, installed reminders of
the previous version are reported as user-edited and are not migrated. Also add
the new copy where the tests list the existing ones
(`grep -rn memory-routing-v tests/`).

The concurrent artifact and cooperation branches both introduced a distinct
`memory-routing-v11.md`. Preserve every historical reminder byte-for-byte so
installations from either branch can migrate:

- `memory-routing-v11.md` retains the upstream reminder immediately before the persistent artifact-manifest instructions were added.
- `memory-routing-v12.md` retains the cooperation branch's original `memory-routing-v11.md`, with coordination guidance before MCP mailbox instructions were added.
- `memory-routing-v13.md` retains the artifact branch's current reminder before the cooperation instructions were merged.
- `memory-routing-v14.md` retains the cooperation branch's current reminder before the artifact instructions were merged.
- `memory-routing-v15.md` retains the reminder before the bounded `cooperation_wait` and request-tracking instructions were added.
- `memory-routing-v16.md` retains the reminder before it told the model to call `Agents-Core` rather than the denied `Agents-Core-Desktop` (#421).
- `memory-routing-v17.md` retains the reminder before it told the model to rate `complexity`, launch with `execution.spawn` and follow `escalation` (model tiers for delegated workers).
- `memory-routing-v18.md` retains the reminder before it told the model to pass `client` with `complexity`.

- `memory-routing-v19.md` retains the reminder before first-work repository bootstrap and turn-ending progress attribution were added.
- `memory-routing-v20.md` retains the reminder before MCP-unavailable first-work fallback and explicit ownership-consent delivery were aligned.

- `memory-routing-v21.md` retains the reminder before secret redaction in logged fields was made explicit.
- `memory-routing-v22.md` retains the reminder before the compact worker exchange (file receipts, plain-text results, validation by `contract_revision` and `result_ref`).
