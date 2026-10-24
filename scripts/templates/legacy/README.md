# Previously generated routing reminders

Exact copies of routing memory files that earlier installers wrote. The migration
helper compares an installed reminder with these bytes: a match is replaced with
the current `../memory-routing.md`, anything else is preserved as user-edited.
Do not edit these files.

`../memory-routing.md` is generated from `src/engine/protocol/`. When
`python -m src.engine.protocol_text --write` replaces it, the command saves the
released reminder (the one at the merge base with `origin/dev` or `dev`) here
unchanged as `memory-routing-v<N>.md`, using the next unused number, and prints its
name and source; it does so on every run, so a reminder released after the branch
last changed its own is kept too, and `--check` fails while one is missing. When git
or a `dev` base is unavailable, the released reminder cannot be determined: the
command keeps no copy, since the working-tree reminder may never have been
installed, prints a warning, and `--check` does not report the directory. Fetch
`dev` and run the command again. The helper reads only
files matching `memory-routing-*.md`; without that copy, installed reminders of
the previous version are reported as user-edited and are not migrated. Also add
the new copy where the tests list the existing ones
(`grep -rn memory-routing-v tests/`).

Only the reminder at the merge base is checked. When another pull request
changes the reminder and lands on `dev` after this branch's merge base, the
reminder released by that merge is neither the merge-base one nor the rendered
one, and nothing keeps it until the branch is merged with or rebased onto the
current `dev`, which moves the merge base. Update the branch from `dev` before
the last `--write`, so the reminder installations run from `dev` is kept too.

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
- `memory-routing-v23.md` retains the reminder before it was rendered from the protocol source in `src/engine/protocol/` (#431), which unified its delegation, artifact, cooperation and first-work wording with the MCP instructions.
