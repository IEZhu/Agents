# Implementation plan

Status: retained implementation checklist from the research handoff. The
[delivery record](README.md) reports the resulting source and actual checks;
this plan does not establish completion. Baseline researched:
`d7e34eb10cb2ab9f593ebf782a4538f677484493`.

The mailbox extension adds `cooperation_send`, `cooperation_receive` and `cooperation_ack` to that original plan. The current [runtime guide](runtime.md#mailbox-workflow) defines their implemented workflow and limits. The existing generic stdio bridge forwards eligible clients to the installed HTTP authority; a standalone stdio engine still has no coordinator. This correction supersedes the original assumption that a new forwarding adapter was required. Model/provider interoperability is a shared MCP contract, not evidence of an executed cross-provider model trial.

## Preserve the boundary

Do not implement by editing the live installation. Re-read `AGENTS.md`, `CLAUDE.md`, `docs/session-playbook.md` and `tests/README.md`; inspect existing changes and worktrees. If the implementation base advanced, reconcile the source map before changing files. Do not touch configuration, journals, client registrations or generated indexes during a documentation change.

Use standard-library SQLite for the new journal unless an implementation review establishes a concrete blocker. No Redis, distributed consensus service, automatic worker launcher, chat scraper or background LLM is required. No runtime file-edit or Git integration command is executed by cooperation tools.

## Work packages and dependencies

| Package | Depends on | Deliverable | Acceptance |
|---|---|---|---|
| W1: schemas and transition model | None | Closed cooperation models, identity types, errors, protocol reference and fixtures | Invalid fields rejected; worker/1 and persona/2 remain byte-shape compatible |
| W2: identity and authority | W1 | Verified repository/worktree resolver, participant capabilities, parent relay and owner offline-cleanup authority | External worktrees, aliases, cross-repo handles and forged parents covered |
| W3: transactional journal | W1, W2 | SQLite store with migrations, revisions, receipts, claims, events, consistent snapshots and recovery | Multi-process race, crash and ambiguous retry tests pass |
| W4: cooperation policy | W3 | Negotiation, resource conflicts, all-or-none claims, DAG readiness, priority/aging, handoff and cancellation | Every policy scenario in evaluation.md has deterministic coverage |
| W5: MCP and host boundary | W4 | Original three coordination tools plus authenticated mailbox send/receive/ack, verified context, response schemas, explicit unavailability behavior and bounded reads | Registered MCP calls tested; existing HTTP/stdio-bridge authority shared; no spawning, file writes or unsolicited messaging |
| W6: instruction distribution | W5 | Draft rules refined/installed in source, template/restatements synchronized, docs linked | Bundle/worker budgets and all instruction propagation tests pass |
| W7: acceptance and rollout | W1-W6 | Recorded test results, model-eval fixtures, owner-facing usage/limitations | Runtime claims match actual evidence; enablement remains an explicit owner action |

W1/W2 and draft evaluation fixtures can be developed separately when their interfaces are agreed. W3 precedes W4; do not implement the journal by parallel edits to the same modules. Assign one owner to shared schema and integration files. Use the protocol being built as a review checklist, without pretending its tools already exist.

## Suggested source layout

This table records the planned responsibilities; use the delivered source and
delivery record to establish which requirements passed verification.

| Surface | Work |
|---|---|
| `src/cooperation/models.py` | Strict request/action/event/agreement/claim/artifact models and fixed bounds |
| `src/cooperation/identity.py` | Existing access context plus distinct verified physical checkout and Git common-directory identity |
| `src/cooperation/store.py` | SQLite schema, transactions, operation receipts, cursors, migrations, private location |
| `src/cooperation/policy.py` | Pure conflict, state transition, dependency and priority functions |
| `src/cooperation/service.py` | Capability checks, application of actions, snapshots and recovery |
| `src/cooperation/admin.py` | Explicitly authorized owner-only offline cancellation/release for lost independent-root capability |
| `src/server.py` | Thin tool wrappers, input parsing, output schemas, annotations and context propagation |
| `src/daemon/app.py` | One service lifecycle/epoch, if required; no server-global conversation persona |
| `src/daemon/workspaces.py`, `src/engine/config.py` | Reuse resolution; add narrowly scoped access support only where required, preserving memory behavior |
| `src/engine/subagent.py` | Mandatory worker-cooperation ID and universal-rule exclusion reason, context budget behavior |
| `rules/`, `scripts/templates/`, `CLAUDE.md`, `src/engine/persona.py` | Instruction distribution described in draft-instructions.md |
| `tests/test_cooperation_*.py` | New isolated tests, no live daemon, network or model |
| `evals/` | Small behavior dataset following the existing evaluation harness conventions |

Inspect `src/schedules/workers.py`, `socket_service.py`, `run_state.py`, `lease.py` and `heavy.py` as examples of run-scoped validation and controlled admission. Do not reuse a single-run WorkerService instance as the independent-session registry. Existing run/heavy locks remain authoritative in their own domains.

## Database and service constraints

Store `cooperation.sqlite3` in the installed daemon's private state directory, in a dedicated cooperation subdirectory. Select one installation-owned service authority; a worker worktree must never derive a separate journal from its own source path and claim it is shared. The MVP coordinator is served by the installed local daemon. Direct HTTP clients and clients using the existing `bridge/stdio.mjs` share that authority. Standalone stdio returns `store_unavailable` and uses the documented fallback. Do not start a second authority silently or create a database under the caller repository.

The schema should have tables for metadata/schema version/service epoch, participants, task attempts, resource claims, agreements/proposal revisions/acknowledgements, dependencies, artifacts/acceptance records, events and operation receipts. The schema-3 mailbox adds private messages and private send/ack receipts; public snapshots/events must not disclose their content. Mailbox acknowledgements record delivery only and never stand in for agreement acknowledgement, artifact acceptance, authorization or claims. Receive is non-destructive, retryable from cursor zero and authenticated to the recipient or its authorized direct parent relay. Persist messages and receipts across restart, enforce pending-inbox backpressure and state the absence of automatic mailbox cleanup. Normalize enough to impose uniqueness and foreign keys. Keep capability verifiers separate from public query projections.

Use short explicit write transactions (for example `BEGIN IMMEDIATE`) with foreign keys enabled and durable commit settings. No model/network calls inside a transaction. Use a bounded busy timeout and return `busy` after bounded retries. Read snapshot and event cursor from one read transaction. A successful tool response follows durable commit; an asynchronous history append is never the commit point.

Choose one documented local-filesystem SQLite journal mode; test crash recovery for it. Avoid network filesystems. A fresh per-call connection or a correctly serialized connection pool must support MCP concurrency; do not rely only on a Python dictionary lock. Never accept the process-local fallback of `file_lock` as proof of multi-process exclusion.

The daemon owns a single service epoch and initialization/migration lock; individual client registrations or stdio forwarders do not advance it. After an actual coordinator restart, reconcile the journal, advance the epoch once and quarantine prior claims. Migration failure returns a bounded error and leaves existing data intact; do not reset the store to recover.

Independent-root lost-capability recovery is deliberately offline in v1. Implement the owner administration command described in protocol.md section 9, separate from MCP and the daemon bearer credential. Require stopped coordinator, exclusive administration lock, verified OS owner, explicitly authorized target/revisions, stopped-execution evidence and inspected checkpoint. Its only state change is audited cancellation/release for that participant. Do not create an online capability-reset bypass. Parent recovery remains limited to registered children and requires the parent's valid capability.

Minimal observability: conflicts, stale revisions/generations, pending agreements, quarantined claims, dependency waits and idempotency replays. Show reasons and ownership in read snapshots. Do not log capabilities or full model prompts. Disablement stops new admission without deleting claims; existing participants must reconcile ownership before turning coordination off. Ordinary routing, history and stateless worker tools remain usable if coordination fails.

## Resolve physical worktree access without broadening permission

`ClientContext.workspace_target` enforces containment under its root; an external Codex worktree may be outside a memory-normalized root. Add a cooperation-specific resolver that separately validates the actual checkout against the caller's authorized MCP roots or an explicit owner-registered checkout access grant. Then verify its Git common-directory membership. Git membership alone grants no filesystem access.

For HTTP, retain the authorized physical checkout during workspace registration or add an owner-authorized registration mapping; do not trust a new model-supplied path merely because it shares a common directory. Test external linked worktrees, symlink escapes, submodules, and missing roots. Do not weaken `workspace_target` or change existing history root normalization.

MVP reads expose only coordination metadata for authorized participants in the verified repository. An artifact's path in another checkout remains a reference: it does not allow the receiving session to open it. Parent relaying is explicit. A new endpoint must not turn a daemon bearer token into automatic ownership of every participant.

## Schema work the implementing model must complete

The protocol fixes semantics, actions and common envelopes; W1 must publish full JSON Schemas for each action payload before W3. Use the examples as fixtures, not schemas. Include explicit field definitions for:
- Proposal terms: participant and parent roles, resource sets, task-attempt bindings, dependencies, artifacts and checks, integrator, priority provenance, checkpoint, deadline and completion conditions.
- Acknowledgement: proposal ID/revision/digest, participant generation and acceptance mode (self or authorized parent relay).
- Acquisition/release/handoff: complete resource set, old/new owners, expected generation and claim revisions, release-ready checkpoint.
- Artifact acceptance: producer/consumer task attempts, digest/revision, checks and acceptance decision.
- Recovery: service epoch, previous generation, participant/parent capability authority, host termination or release evidence, and reconciliation result. Independent-root capability loss uses the separately specified offline owner cleanup, not this online action.

No field may allow a peer to mutate a worker contract or silently change the root user's requirements. Persist the original task-attempt binding. Changed assignment scope requires a new prepared worker attempt and sidecar attachment before proceeding.

## Validation commands and reporting

Run commands from the implementation worktree with an interpreter whose `src` import resolves there. The repository test guide governs setup. Run only one heavy test/embedding process at a time.

Start with the new cooperation tests and focused tests for touched surfaces. Existing likely suites include `tests/test_subagent.py`, `tests/test_subagent_mcp.py`, `tests/test_rules.py`, `tests/test_persona_protocol.py`, `tests/test_persona_bundle.py`, `tests/test_config_client_root.py`, `tests/test_daemon.py`, `tests/test_install_instructions.py`, `tests/test_installer_instructions.py`, `tests/test_codex_instructions.py` and `tests/test_protocol_migration.py`. Verify names against the current tree before running; do not silently skip missing tests.

After focused checks pass, run the regular suite for this multi-subsystem runtime change:
```bash
LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/ -q
git diff --check
```
Review all new files because ordinary `git diff` omits untracked content. Check local links, schemas, examples, English prose and generated instruction consistency. Do not install/restart the shared daemon to validate documentation.

Report deterministic tests separately from prompt/model trials. Paid evaluations and production rollout require separate authorization. For PR submission follow `flows/pr-review.md`, with an English description and target `dev`; the specification does not itself request publication.

## Completion gates

Implementation is complete only when:
- Both parent/worker and independent-root examples execute against the real new tool layer in isolated tests.
- Negotiation, ordering, claims and recovery satisfy all deterministic scenarios.
- Older worker/1 outputs and persona/2 descriptors are unchanged.
- Every instruction distribution surface agrees and compact worker preparation remains bounded.
- Unsupported hosts, nonparticipants and arbitrary shell writes have accurately stated limitations.
- The release documents enabled/disabled behavior, state location, retention and recovery, with actual results attached.

If a host cannot safely carry capabilities or maintain background heartbeats, deliver parent-relay/checkpoint mode explicitly. Do not downgrade into an unauthenticated participant-ID-only service.
