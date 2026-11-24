# >>> Agents-Core Routing Protocol (managed by init_repo) >>>

# Agents-Core Persona Protocol — version 2

Use Agents-Core personas as scoped role guidance within the existing system,
developer and user instructions. Keep the conversation in the current model.
Its tools return instructions and contracts, never the answer, and launch no worker;
`local_context_create` describes optional local inference.

## Delegated worker mode

Delegate to any AI worker (subagent, reviewer, verifier) only through
`get_subagent_context` before spawning and `validate_subagent_result` (`valid: true`)
before accepting its output; first read both descriptions, which hold the procedure
and the artifact rules. If either is unavailable, work in the parent and say so,
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
On `keep`, reuse that footer; do not infer component lists. When the server
instructions or a `SUCCESS` say that this client's Stop hook logs each turn, skip
every `log_interaction` step of this protocol; everything else holds. Otherwise,
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

# <<< Agents-Core Routing Protocol (managed by init_repo) <<<

## Repository notes

For contribution instructions, read [AGENTS.md](AGENTS.md). Never edit, commit
or switch branches in the live installation checkout the daemon runs from; work
in a linked worktree cut from `origin/dev` and open the PR against `dev`
(AGENTS.md, "Make changes"). The
[documentation map](docs/README.md) distinguishes current references from plans
and recorded results. To refresh both human documentation and AI instructions,
execute [flows/documentation-refresh.md](flows/documentation-refresh.md).
Keep reusable model task instructions in `flows/`; see the
[workflow catalog and authoring guide](flows/README.md).
`list_flows()` discovers built-in, personal (`user:`) and repository (`repo:`)
workflows; `run_flow(...)` returns their instructions bound to the caller's
repository; a bare ID resolves `repo:`, then `user:`, then the built-in.
`get_flow`/`save_flow`/`delete_flow` manage personal and repository flows and
`set_flow_persona` chooses a flow's agent and components (`src/flow_persona.py`),
stored in the git-ignored `flows/.user` (`src/user_flows.py`; the daemon's `/ui`
editor is `src/daemon/flows_ui.py`). When `run_flow` returns `persona_activation`,
apply it as a switch, then continue executing the `needs_execution` bundle
against its `repo_path` with the current model's tools.
The loader is in `src/flows.py`; installation source paths and target paths
have different roles and must not be interchanged.
For PR/MR review, follow [flows/pr-review.md](flows/pr-review.md), including
description updates, bot review cycles, thread replies, and merge conditions.

The committed managed section is `scripts/templates/routing-protocol-core.md`
(protocol 2, the only protocol), generated with the MCP instructions, the
`SUCCESS` instruction and the memory reminder from the section files in
`src/engine/protocol/`; the procedures of delegation, artifacts, cooperation and
local inference are not in those always-on surfaces but in the descriptions of the
tools they name (`TOOL_PARTS` in `src/engine/protocol_text.py`, #734), which the
surfaces only trigger. Do not edit the template or the managed section by hand.
The global installer does not modify this tracked checkout file; after editing a
section, run `.venv/bin/python -m src.engine.protocol_text --write`, which also
refreshes the managed section here ([README.md](README.md#persona-continuity)).
Keep these repository notes outside the markers so future protocol changes
preserve them.

- `src/server.py` exposes the MCP tools. Protocol handlers and complete bundle
  assembly live in `src/engine/persona.py` and `src/engine/persona_bundle.py`;
  shared response schemas live in `src/schemas/protocol.py`. Over stdio the
  persona tools, `log_interaction` and `describe_repo` return their JSON object
  once instead of inside `{"result": <text>}` (`SINGLE_ENCODED_TOOLS`), and a
  semantic-cache miss is ranked by `SemanticRouter.cold_ranking`
  (`src/engine/router.py`) into a shortlist, or under the headless profile into a
  direct load ([docs/routing_flow.md](docs/routing_flow.md#a-cache-miss)).
- `src/client_instructions.py` decides when a client already holds the managed
  section (`holds_current_section`: only when every checkout the session may run
  in holds the current section in its `AGENTS.md`; Claude Code and every other
  client keep the full text), so the MCP initialization answers a Codex session
  with the pointer instead of the full instructions and a persona `SUCCESS` sends
  the section pointer; the headless profile (`AGENTS_CLIENT_PROFILE`, #734) sends
  that pointer without the check (`_instruction` in `src/engine/persona.py`). It
  also words the warnings about outdated sections;
  `tests/test_instruction_pointers.py` checks it.
- Agent metadata declares core, preferred and capable skills, plus preferred
  implants. General rules are versioned bundle content, governed by
  `src/engine/rules.py`; the configuration can disable them with `RULES_ENABLED=0`,
  and the web UI switches single rules, skills and implants off for the
  installation (`src/component_toggles.py`).
- The separator rule of the plain-text worker result has one source too:
  `SEPARATORS` in `src/engine/subagent_text.py`, from which `separator_rule`
  composes the sentences that both the rendered package and the block between the
  markers of `docs/subagent-contract.md` state. Edit the source, never the block,
  then run `.venv/bin/python -m src.engine.subagent_contract_text --write`
  (`--check` reports a stale block). The conditions in those sentences are not
  generated: the labelled examples that follow them in the package are executed by
  `tests/test_subagent_exchange.py` instead (#596).
- `describe_repo()` reads repository context without sampling or writing.
  `write_repo_summary(...)` explicitly persists the managed summary and refuses
  the running installation/source root before creating a lock or cache. Follow
  the description's `instruction` before persisting. `read_history(...)`
  retrieves existing entries; `log_interaction(...)` records turn attribution.
- `read_shared_memory(...)` serves what the AI clients keep in their native
  memory on this machine (#369): `src/memory/native.py` reads the places the
  web UI's Memory tab also shows, `src/memory/shared.py` skips secret-bearing
  files, keeps each fact's sources and builds the semantic index in the
  background ([docs/shared-memory.md](docs/shared-memory.md)).
- `claim_record(...)` and `claim_lookup(...)` keep verdicts about factual claims
  per workspace in `data/claims/claims.jsonl` (`src/claims/`). A claim's verdicts
  run through the exact two-state Bayes filter of `src/claims/model.py`, with one
  hidden reading per family of verifiers that fail together; its accuracies and
  volatilities are priors, not measurements
  ([docs/validated-claims.md](docs/validated-claims.md)). `src/estimation/` holds
  the Kalman filter behind the warmup hint and the ledger's recent acceptance rate.
- Preserve unrelated Repository Memory sections and user instructions when
  updating managed routing sections. See [docs/routing_flow.md](docs/routing_flow.md)
  for the current contracts, compatibility behavior and migration procedure.
- Run `LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/ -q` for
  the regular suite; include slow tests with `-m ''` when required. See
  [tests/README.md](tests/README.md) for environment setup, focused checks and
  worktree isolation. Run only one heavy test or embedding process at a time.

# >>> Agents-Core Repository Memory (managed by describe_repo) >>>

# Repository: Agents
> Auto-generated by `describe_repo` on 2026-11-19T16:48:55+00:00. Hash: 07b11e08.
> To update: `describe_repo(force_refresh=True)`, then `write_repo_summary(...)` persists it.

### Project Identity

Agents-Core is one local Model Context Protocol server that gives an AI coding assistant a task-relevant persona, Markdown workflows it executes with its own tools, bounded validated delegated workers and a memory of each project; the calling model does the work, the server supplies context and contracts. Python 3.11+ (`requires-python = ">=3.11"`), distribution `agents-private`, version read from the root `VERSION` file, setuptools with `pip`/`venv`, and both `requirements.txt` and `uv.lock` committed; the tool surface is FastMCP from `mcp`. Most of the product is data: 46 personas under `agents/*/system_prompt.mdc`, 89 skills, 57 implants, 8 always-on rules plus 6 worker rules in `rules/subagents/`, and 21 built-in flows. Clients: Claude Code, Codex, Cursor, Antigravity, Claude Desktop.

### Tech Stack

- `mcp==1.30.0` — MCP/JSON-RPC server and FastMCP tool registration; exact pin.
- `onnxruntime>=1.24.4`, `huggingface-hub>=0.23,<2.0`, `tokenizers>=0.22,<1.0` — direct ONNX embedding inference (`src/engine/onnx_embedding.py`) instead of sentence-transformers, keeping FastEmbed 0.8.1's catalog and numerical conventions rather than its code: the tree carries `embedding_catalog.json`, `FASTEMBED_LICENSE` and `FASTEMBED_NOTICE`, and no vendored FastEmbed source. First setup downloads about 1.1 GB.
- `numpy>=1.24` — the vector store is NumPy (`src/engine/vector_store.py`), not ChromaDB.
- `pydantic>=2.0` (schemas), `PyYAML>=6.0` (frontmatter), `cryptography==50.0.2` (DSSE signing), `uvicorn==0.48.0` (shared service), `langfuse>=3.0` (optional tracing, a no-op without keys), plus `cachetools>=5.3`, `python-dotenv`, `langdetect>=1.0`, `anthropic>=0.18.0`, `pdf2image>=1.16.0` and `Pillow>=13.0.0` (OCR).
- Extras: `thread-status` (`claude-agent-sdk==0.2.168`), `signing-tests` (`model-signing==1.1.1`), `local-inference` (`httpx`), `local-inference-redis` (adds `redis>=7.0.0,<7.1`), `evals` (`datasets`, `jinja2`, `openai`, `anthropic`).

### Entry Points

| Path | Purpose |
|---|---|
| `src/server.py` | 34 `@mcp.tool` handlers; `src/artifacts/tools.py` (6) and `src/local_inference/tools.py` (8) register theirs on the same instance, 48 in all |
| `src/startup.py`, `src/runtime_paths.py`, `src/stdio_runtime.py` | stdio launch; readers and content paths bind before any engine import. `src/main.py` is a legacy demo CLI |
| `src/daemon/__main__.py` | shared local service: `python -m src.daemon`, with `update`, `recover`, `flows-ui`, `migrate` |
| `src/schedules/`, `src/user_sync/`, `src/context_compaction/`, `src/instruction_signing/` `__main__.py` | subsystem command lines |
| `src/reindex.py`, `src/model_migration.py`, `src/update_channel.py`, `src/uninstall.py` | index rebuild, model switch, update channel, removal |
| `install.sh`, `scripts/init_repo.sh`, `scripts/init_repo.bat`, `scripts/install_instructions.py` | install, setup, instructions-only refresh |
| `CONTRIBUTING.md`, `AGENTS.md`, `CLAUDE.md` | the short index GitHub shows, the contributor instructions it points to, and the managed protocol plus repository notes |
| `plugins/agents-core/`, `plugins/agents-core-context-compaction/` | the plugin registering this server, and the optional compaction mod |
| `scripts/check_docs.py`, `scripts/validate_agents.py`, `scripts/check_skill_sources.py` | documentation, persona-metadata and skill-source checks, none loading a model |
| `.github/workflows/` | 14 workflows, led by shared-runtime, documentation-quality, instruction-signing, user-sync |

### Module Map

- `src/engine/` — retrieval and protocol core: `persona.py`/`persona_bundle.py` (stateless protocol-2 handlers, all-or-nothing bundles), `router.py`, `enrichment.py`, the embedding stack (`onnx_embedding.py`, `vector_store.py`, `fingerprint.py`, `readiness.py`), the delegation stack (`subagent*.py`, where `subagent_exchange.py` holds the private package and stored contract, plus `model_tiers.py`), and `protocol_text.py` over `protocol/`, the 20 section files every protocol surface comes from.
- `src/schemas/` — `protocol.py` (persona responses), `subagent.py` (result models and JSON depth/node limits), `artifacts.py`, `tool_args.py`.
- `src/memory/` — the four memory tools; `config.py:22-23` owns this block's markers.
- `src/daemon/` — the shared service, its control CLI and `mcp_proxy/`; importing it never loads the engine. Its web UI has tabs for agents, flows, implants, skills, rules, history and memory.
- `src/artifacts/`, `src/cooperation/` — pinned text artifacts for workers; the mailbox, requests and agreements between sessions.
- `src/instruction_signing/`, `src/local_inference/`, `src/schedules/`, `src/user_sync/`, `src/context_compaction/` — local authority and trust (enforcement still planned), optional disabled-by-default completions, scheduled runs and the autodev runner, personal library sync through a private git repository, opt-in client compaction.
- Easy to miss: `src/utils/` and `src/mcp_servers/` (a bundled `document_ocr/` server, a `_template/`). Top-level modules cover flows (`flows.py`, `user_flows.py`, `flow_execution.py`, `flow_persona.py`), the personal library (`user_rules.py`, `rule_sources.py`, `component_toggles.py`, `component_catalog.py`) and client integration (`client_instructions.py`, `client_paths.py`, `mcp_registry.py`, `mcp_secrets.py`).
- `agents/`, `skills/`, `implants/`, `rules/`, `flows/` — the content library; `agents/common/agent-schema.json` validates persona frontmatter alone. `docs/README.md` maps the documentation in seven goal sections, `docs/install.md` holds setup, per-client configuration and updates, `docs/configuration.md` the environment variables, `docs/architecture.md` the source layout, `docs/memory.md` the repository memory, `docs/development.md` the vulnerability gate and debug logging, `docs/mcp-tools.md` the public tools and `docs/features.md` the client matrix.

### Conventions

- Components are Markdown with YAML frontmatter and an `.mdc` extension. A persona folder holds `system_prompt.mdc` and `subagent.mdc` (46 of each); only `agents/universal_agent/` adds a `meta_prompt.md`.
- Conventional commits: `type(scope): subject`, imperative, body wrapped at 72, one change per commit. New work uses a `codex/<slug>` branch unless the user or flow names another; issue implementation requires `claude/issue-<number>-<short-slug>`.
- Every committed document is in English.
- Every record listed under "Plans, research and recorded results" in `docs/README.md` opens with a `**Status:**` line: what the document is, its date, how much is implemented, and, where a maintained reference exists, where current behavior is documented (recorded measurements of something never adopted leave that out). That line is the only place for it: the map lists those documents by kind and carries no status column. The two generated eval reports (`evals/reports/baseline.md`, `evals/reports/implants_preferred_ab.md`) are exempt because their next run overwrites the file, so the map notes their provenance instead. Reference documents, and the reports under `docs/agent-cooperation/` and `docs/shared-kv-mcp/`, still carry the older plain `Status:` line or none; converting them is not done.
- The protocol text has one source, the 20 section files in `src/engine/protocol/`, which `src/engine/protocol_text.py` composes into every surface: `scripts/templates/routing-protocol-core.md` (the managed section of `CLAUDE.md` and `AGENTS.md`), the MCP initialization instructions, the `SUCCESS` instruction, the client pointers and `memory-routing.md`. Never hand-edit a generated surface: change the section, then `python3 -m src.engine.protocol_text --write` (`--check` reports stale ones). This memory block has its own markers; keep repository notes outside both marker pairs.
- The worker result's separator rule has one source too, `SEPARATORS` in `src/engine/subagent_text.py`, regenerated into `docs/subagent-contract.md` by `.venv/bin/python -m src.engine.subagent_contract_text --write`.
- A handler returning a JSON string is wrapped by FastMCP as `{"result": <text>}` and counted twice-escaped; one annotated with a response model exposes structured content instead. `src/result_size.py:16-17` sets `INLINE_LIMIT = 50_000` and `RESULT_SIZE_CEILING = 500_000`, so result size is a design constraint either way.

### Key Workflows

- `install.sh` — clone or fast-forward `~/.agents-core`, then `scripts/init_repo.sh --yes`: venv, dependencies, indexes, client registration, managed instructions, hooks.
- `LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/ -q` — the regular suite; a command-line `-m ''` overrides `addopts` and adds slow tests.
- `.venv/bin/python scripts/check_docs.py` — offline navigation and the public tool index; 186 tracked Markdown files, 0 errors at this revision. CI runs the same script and fails on stale badge data.
- `python3 -m src.engine.protocol_text --write` regenerates every protocol surface; `-m src.reindex` rebuilds the stores.
- `python3 scripts/bump_plugin_version.py --base origin/main` — the next calendar version into `VERSION` and the plugin manifest, before a promotion. It reads the *working tree's* `VERSION`, so run it inside the worktree being promoted.
- `.venv/bin/python -m src.daemon update|recover|flows-ui|migrate` — update or recover the service checkout, open the web UI, migrate its state.
- `flows/*.md` through `list_flows()` and `run_flow()` — pr-review, pr-queue, documentation-refresh, thread-close, issue-agent, autodev, review-config-rollout, legal-reference-refresh, consistency-audit.

### Architecture Patterns

Protocol 2 is stateless on the server. `route_and_load` answers `ROUTE_REQUIRED` only when it cannot pick an agent itself; on a cache hit or a meta query it returns `SUCCESS` or `NO_CHANGE` directly, so a caller follows the status it got. A `SUCCESS` carries a descriptor plus four blocks replacing the previous four; a `NO_CHANGE` carries the descriptor and footer without them. Activation state lives in the caller's dialogue, and `replaces_activation_id` (null on a first activation) lets a stale reply be ignored.

Retrieval is local and lazy. `readiness.py` warms the ONNX model and the NumPy stores in one daemon thread. Discovery tools are ungated; retrieval tools wait at most `WARMUP_WAIT_SECONDS` (20 by default), then answer `warming_up` rather than hanging. `fingerprint.py` mixes the embedding contract into derived indexes, so a model change invalidates them.

Delegation is a contract, not a prompt. `get_subagent_context` writes a task-scoped package to a private 0600 file and keeps the contract by revision, returning a receipt whose `launch` text the host feeds to a fresh native worker; `validate_subagent_result` checks the worker's exact bytes against it, structurally only, under depth, node and size limits. Shared inputs travel as `src/artifacts/` manifests pinned into the contract. No MCP tool spawns a worker: the host owns execution and limits, and in-process generation exists only in the disabled-by-default `src/local_inference/`, though the autodev runner starts CLI processes through `src/schedules/worker_launch.py`.

### Test Strategy

`pytest` with `addopts = ["-m", "not slow"]` across 251 tracked `tests/test_*.py` files, but the `slow` marker covers only four selections: `tests/test_startup_handshake.py:16` module-wide and three tests in `tests/test_routing.py`. Retriever and isolation checks stay in the default selection and still build the stores and re-embed them, so the suite is not model-free: keep `EMBEDDING_MODEL` cached and go offline with `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`. `tests/conftest.py` pins the model from `.env`, isolates the data and service directories, and fails a test that would reach the real notification path or re-exec the process; fake binaries are per-test fixtures in `tests/fixtures/`. Run with `LANGFUSE_TRACING_ENABLED=false`, one heavy test at a time. `tests/ci_shard.py` splits one selection across Windows jobs. Node covers `bridge/test.mjs`, `tests/mcp_metadata_guard.test.mjs`, `tests/flows_ui_page_harness.mjs` and the compaction mod's tests. `tests/README.md` maps each subsystem to its files.

### Gotchas

- The root `README.md` is an overview of about 190 lines: what the project is, the client matrix, the one-command install, a first call and pointers. Setup, configuration, architecture, repository memory and the development reference live in `docs/install.md`, `docs/configuration.md`, `docs/architecture.md`, `docs/memory.md` and `docs/development.md`. A section moved out of the README keeps its heading text, because other documents link those anchors.
- `scripts/check_docs.py` takes its file list from `git ls-files`, so a new document is invisible to it until the file is staged. Stage new documentation before trusting a local run; CI sees the committed tree and will find what the local run skipped.
- Never edit, commit, reset or switch branches in the checkout the daemon runs from (default `~/.agents-core`); reading it is fine. Cut a linked worktree from `origin/dev` with `--no-track` and open the PR against `dev`.
- `requirements.txt` declares `langdetect>=1.0` but not `openai`, which lives in the `evals` extra and is needed by `tests/test_openrouter_provider.py` and `tests/test_prompt_ab.py`. Neither file declares `markdown-it-py`, which `scripts/check_docs.py` imports: `.github/workflows/documentation-quality.yml` installs it as `markdown-it-py>=3,<5`.
- A script that reads the working tree answers about that tree. The shared checkout can lag `origin/dev` by hundreds of commits, and `scripts/bump_plugin_version.py --check` then reports a version problem that does not exist on the release ref.
- Live runtime state is split, and a worktree is not exempt. Worker packages, session locks and indexes sit in the *installation* root's `data/` (`src/engine/config.py:22`), which defaults to the running source tree but is configurable. Losing it loses the stored contracts, after which `validate_subagent_result` answers `contract_unknown`. The artifact store sits in the service-state directory (`AGENTS_SERVICE_DIR`). `git status --short --ignored` says whether a worktree still has none, because `git worktree remove` deletes what it finds.
- A Windows run collects fewer tests by design: `tests/conftest.py:65` drops every `tests/test_local_inference_*` file on non-POSIX platforms.
- The personal flow library lives in the installation's git-ignored `flows/.user`. The groups that sync are `common`, `personas`, `history`, `components` and `mcp` (`KIND_GROUPS` in `src/user_sync/scope.py:43`), and per-repository groups under them sync as well once portable.
- `RULES_ENABLED=0` disables the rules layer; the web UI switches single components off for the installation (`src/component_toggles.py`).
- Installation source and target paths have different roles in the flow loader and must not be interchanged.

### Glossary

| Term | Definition |
|---|---|
| Persona | An identity prompt plus selected skills, implants and rules |
| Bundle | The four blocks one activation returns, replacing the previous four |
| Descriptor | The `persona` object the caller echoes back |
| Skill / Implant / Rule | A method in `skills/`, a reasoning pattern in `implants/`, an always-on constraint in `rules/` |
| Flow | A Markdown procedure in `flows/` the calling model executes |
| Prepared package | A task-scoped assignment plus its server-retained contract |
| `contract_revision` | Ties a worker's output to that stored contract |
| Artifact manifest | Pinned hashes of immutable text shared across workers |

# <<< Agents-Core Repository Memory (managed by describe_repo) <<<
