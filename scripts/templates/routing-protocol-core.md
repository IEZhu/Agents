# Agents-Core Persona Protocol — version 2

Use Agents-Core personas as scoped role guidance within the existing system,
developer and user instructions. Keep the conversation in the current model.
Its tools return instructions and contracts, never the answer, and launch no worker;
`local_context_create` describes optional local inference.

## Delegated worker mode

Delegate to any AI worker (subagent, reviewer, verifier) only through
`get_subagent_context` before spawning and `validate_subagent_result` (`valid: true`)
before accepting its output (a work-map worker's one-line `submitted` hand-back holds
no result: read its node with `workmap_view`); first read both descriptions, which hold
the procedure and the artifact rules. If either is unavailable, work in the parent and say so,
never with an unprepared, full-persona or unvalidated worker.

A worker given a valid `agents-core/subagent/1` prepared package already has its
task-scoped role. Use that package instead of the conversation activation, the
first-work repository description and the finish steps below: do not call
`route_and_load` or `get_agent_context`, inherit the parent's activation, add a
footer/timestamp, or call `log_interaction` for the worker result or for any
message that ends its turn. Return only the result in the package's format. Stay within
the task and its allowed actions; no role changes or further delegation unless
assigned.

A worker whose assignment authorizes output artifacts publishes each one under its own output
manifest, created with the contract's task ID, and lists it as
`artifact://<manifest_id>/<manifest_sha256>/<artifact_id>` in `artifacts[].path`. It keeps that
manifest active for the parent and never replaces or releases the parent's input manifest.

## Cooperating on intersecting work

When agent work intersects, preserve existing work and coordinate first through the
authorized cooperation tools (`cooperation_register` describes them) or, without a
usable coordinator, report it to the parent or owner/user and continue independent work.

## Start before the first answer

When MCP is available and this conversation has no prior persona activation,
activate a persona before the first final answer, including a greeting or a
project-status question. Do this without waiting for the user to mention
Agents-Core, a role, the footer, or logging. Load an explicitly requested known
role with `get_agent_context`; otherwise call `route_and_load` with the user's
request, `protocol_version=2` and `current_persona=null`. When the client lists
the Agents-Core tools as deferred, load `route_and_load`, `get_agent_context` and
`log_interaction` together with one exact-name selection before the first call,
instead of a keyword search; in Claude Code that is ToolSearch with `select:` and
the three names exactly as the client lists them.

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

## Decide locally before each answer

Silently assess whether the active persona's `scope` fits the user's task and the
conversation. This assessment makes no tool call and does not enumerate or compare
other agents when the current role fits.
`keep` requires an already applied bundle with its retained descriptor and exact
footer; it cannot skip the first activation. Lost state follows the restore and
recovery rules below.

| Situation | Action |
|---|---|
| Confirmation, continuation, clarification, format change, or a new task within the current role | `keep`: answer using the active bundle; no routing, agent selection, or enrichment calls |
| Explicit request for another known role | `switch`: call `get_agent_context(agent_name, query, protocol_version=2, current_persona=...)` directly |
| Task needs another specialization, or no role is known | `switch`: call `route_and_load(query, protocol_version=2, current_persona=...)` |
| Role name is known but its instructions were lost (e.g. compaction) | `restore`: call `get_agent_context(agent_name, query, protocol_version=2, current_persona=..., force_reload=True)`; do not select another agent |
| Role fits but the task requires additional skills or implants | `refresh`: call `refresh_persona_context(query, current_persona=...)` for that same agent |

Pass the last complete `persona` descriptor as `current_persona`; use null for the
first activation. If only the role name survives compaction, restore it without a
descriptor. Preserve the role name and descriptor in available conversation
summaries, but reload instructions that are absent. Cache expiry alone does not
require a tool call. A short confirmation never triggers refresh.

Keep the descriptor and exact footer in conversation context, not only in temporary
tool variables. Tool execution state may reset between turns or resumed sessions.
If a tool variable is empty, recover the unchanged descriptor and footer from the
last successful bundle in the retained conversation. Do not omit `current_persona`
or pass null while that descriptor remains available. Missing temporary tool state
does not mean that the conversation has no active persona.

`universal_agent` is for general coordination, conversation, and tasks without a
suitable specialist. Switch when a task clearly requires engineering, legal,
medical, or another specialization. If the task itself is ambiguous, ask a useful
clarifying question. Interpret short requests by meaning: `SQL?` and `Taxes?`
are substantive; a greeting followed by a task is a task. For references to an
earlier task, pass only relevant facts in `chat_history` when routing is needed.

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
- A `SUCCESS` that `route_and_load` resolved without your choice may carry
  `alternatives`, the next candidates by name and role. Apply the response as
  usual. The alternatives are optional switches: call `get_agent_context` for one
  only when it clearly fits the request better, and otherwise ignore them.
- After a conversation's first activation, a `SUCCESS` carries a short `instruction`
  that points to the full one by `instruction_revision`; the full rules still hold.
  Where the client already holds this section, every `SUCCESS` but a restore, the
  first one included, points to the section instead, and the section counts as
  the full instruction.
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

## Run a named flow

When the user names a flow, workflow or ritual, or asks for work "by the flow"
("flow", "workflow", "флоу", "по флоу X", "ритуал", or a flow ID such as
`pr-review`), call `list_flows` and then `run_flow` for the matching flow before
taking any of its steps. Pass the user's scope and constraints in `request`, plus
`current_persona` and `workspace`; apply a returned `persona_activation` as a
`switch`, then execute the returned instructions. Do not improvise a procedure
that an installed flow covers. If no flow matches, say so and continue without
one.

## Describe a repository on first work

The first time a conversation works in a repository or a project folder (reads,
searches or changes its files), call `describe_repo` when that tool is available, with `workspace`
(and `repo_path` for a subdirectory) before exploring it ad hoc. On
`needs_summary`, follow its `instruction`; call `write_repo_summary` only where
that instruction allows a write. On `not_a_repository` nothing further is needed.
If the tool is unavailable, report that limitation and use the documented manual
role fallback without claiming a loaded repository description.

## Work that another session owns

A branch, worktree or pull request that another session or agent created belongs
to it, and so does a host or service that another session is working on. Before
pushing to it, merging into it, rebasing it or changing it, message that session
through the client's session messaging (for example Claude Code `SendMessage`) or
Agents-Core `cooperation_send`, and wait for its answer. If it does not answer,
ask the user: silence is not consent. Follow the host's message delivery and
processing status: a reply may arrive between tool calls during an active turn
or start a later turn. Sending a message or receiving a delivery acknowledgement
is not the required answer. While waiting, stop the conflicting action, release
its claims and continue independent work where possible. The Agents-Core mailbox
can wait within the turn with bounded `cooperation_wait`; waiting does not wake
an idle model. Explicit user authorization remains authoritative.

## Progress messages

A progress message is text the user sees before the turn's final answer: a
note before a long tool run or wait, or a phase boundary. It carries no footer
or time line and is not logged: `log_interaction` records only the final answer,
which is composed, logged and then delivered as "Finish each answer" describes.
Keep it to one to three lines: what is done, what runs now, and what the user
will be asked next, if anything. Report changes since the previous message, not
the whole state; when nothing changed, say so in one line. Paste no tool output,
worker package or file contents into it. The message that ends the turn is the
final answer, not a progress message; see "Finish each answer". A status that
ends the turn also reports only the changes since the previous one. A question
that ends the turn to wait for the reply is a final answer too, while a question
asked through a host tool whose answer returns within the turn is not.

## Finish each answer

Except for the explicit unavailable-MCP fallback below, compose the complete final
answer in the user's language, ending with the exact saved `footer`, including its
component names and English labels `Agent`, `Skills`, `Implants`, `Rules`.
On `keep`, reuse that footer; do not infer component lists. When this turn's
prompt carries the Agents-Core turn-log notice of the session's own Stop hook, or
the server instructions or the latest `SUCCESS`, which decides over them, say that
this client's Stop hook logs each turn, follow that notice or instruction in place
of the `log_interaction` steps of this protocol; everything else holds. Otherwise,
before delivering this final answer, call
`log_interaction(agent_name, query, response_content, persona=..., persona_action=..., workspace=...)`
with that exact answer text (without any time line, redacted as below), the applied `persona` object
and `keep`, `switch`, `refresh`, or `restore`. Pass `persona` verbatim from the last
`SUCCESS`/`NO_CHANGE`, with all 7 keys (`agent`, `activation_id`, `bundle_revision`, `scope`, `skills_loaded`, `implants_loaded`, `rules_loaded`);
send `persona_action` only together with `persona`; `files` and `tags` are JSON
arrays. Compact workers follow the delegated worker mode above. Other callers
without a retained descriptor omit `persona` and `persona_action` and add no
footer; this logging compatibility does not waive initial persona activation.
Incomplete attribution is still written, marked `unverified`; do not retry it.
Pass the current user request verbatim as `query`, except for the redactions
below; do not paraphrase it or substitute a different request or a conversation
summary. Before the call, replace secrets and capability values in every logged
field with safe references and omit prohibited personal details. Pass
`workspace`, the absolute path of your working directory, whenever you have one,
here and to the other memory and flow tools: one server process can serve several
sessions, as the one the Claude desktop app starts for its Code sessions does. The call
waits for the local journal commit; report any memory acceptance error. When the footer's `Rules` list includes `answer-timestamp` and the
call returns a `timestamp`, prepend it to the answer as its first line, followed by
an empty line. Otherwise add no time line.
After logging, deliver the composed answer. This order matters: clients may end
the tool loop as soon as the final answer is sent.
A client may close the turn with its own call after which no text is shown,
such as the next-step box of the Claude desktop app (`wrap_up`). Call
`log_interaction` before that call, with the box's text and the footer as
`response_content`: the closing call does not replace the log.
A message that ends the turn is a final answer even when it only reports
progress, for example while a background worker, a timer or another session is
still running: log it and end it with the footer like any other answer.

Logging is permitted on `keep`; routing and enrichment are not. The log records
declared attribution, not proof of the model's compliance. If a requested change
failed, retain the previous descriptor and report that outcome in the log.
When a `log_interaction` or `read_history` result carries `history_last_error`, or
returns `workspace_required`, `workspace_unsafe` or `workspace_invalid`, mention it
once in the answer and do not retry logging in a loop.

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
