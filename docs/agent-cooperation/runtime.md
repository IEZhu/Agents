# Cooperation runtime operations

## Enablement and authority

The coordinator is disabled by default. The installation owner can set `AGENTS_COOPERATION_ENABLED=1` for the installed daemon as part of a separately authorized rollout. No live enablement, client registration or daemon restart was performed during implementation. All cooperating clients must reach that same installed authority. Direct HTTP clients and clients forwarded by the existing `bridge/stdio.mjs` use the same tools and journal. `src/startup.py` also delegates eligible stdio sessions to that bridge. A standalone stdio server without the bridge returns `store_unavailable`; it does not create a second journal.

The mailbox protocol is independent of the client or model provider: an authorized compatible MCP client supplies the same requests and handles the same responses. It neither calls provider APIs nor depends on native host messaging. This is source-level interoperability, not a claim of live testing across provider models. The existing installed bridge needs its private configuration and recorded Node executable; missing bridge prerequisites or an explicit standalone configuration do not provide a shared coordinator.

State is `<installed service state>/cooperation/cooperation.sqlite3`, outside Git. The daemon owns one OS administration lock for its complete coordinator lifetime and advances the persisted epoch once per start. SQLite uses local-filesystem `DELETE` journaling, `synchronous=FULL`, foreign keys, per-call connections and short `BEGIN IMMEDIATE` writes. A bounded busy timeout returns `busy`; migration/schema errors preserve the existing database. Network filesystems are not supported.

The owner controls `AGENTS_COOPERATION_RETENTION_DAYS` (integer, default 30, minimum 30). Invalid configuration makes only coordination unavailable. Active/nonterminal agreement receipts and events are not collected. The maintenance method `CooperationService.collect_terminal()` removes eligible terminal action receipts and a contiguous terminal event prefix; participant/generation tombstones and immutable proposal/checkpoint history remain. The enabled daemon runs it once on readiness and then hourly, retries a failed pass at the next interval, pauses new passes while draining, and joins in-flight collection before closing the coordinator. It is an owner-side maintenance API, not a peer mutation. Public reads expose `minimum_cursor`; a cursor below it returns `cursor_expired` and requires a fresh snapshot. Mailbox messages and their private send/ack receipts are separate and are not automatically deleted, including acknowledged messages; this is a storage-retention limitation.

If the registration response is lost, register a fresh task attempt rather than guessing the missing capability; delegated work also needs its fresh prepared contract. Maintenance retires an unused registration only after the retention window, with no recorded authenticated activity, incoming mail, claims, children or nonterminal agreement references. It invalidates the credential and preserves the participant/task-attempt tombstone. A durable internal activity marker survives receipt collection. When migrating schema-1 journals, every existing participant is conservatively treated as used, because absence of historical activity cannot be proved. Schema 3 adds the private mailbox and receipts and accepts migration from schemas 1 and 2. These migrations never release established claims.

Disablement does not delete state or free claims. Existing owners must checkpoint/reconcile and release before coordination is turned off. In-process `service.enabled=False` stops new admission while allowing checkpoint, recovery, release and cancellation. If the process is stopped, a later start quarantines outstanding work before admission resumes.

## Workspace and capabilities

The existing owner workspace-registration boundary records physical checkout grants separately in `checkout-access.json`, before memory normalizes a linked worktree to its main checkout. MCP request arguments never create grants. An external linked checkout must already have an explicit grant. Existing memory `workspace_target` containment is unchanged. Callers sharing an existing workspace UUID share its explicitly registered physical grants; this is not a new hostile-tenant ACL.

Without an explicit MCP roots list, `CLAUDE_PROJECT_DIR` and authenticated `hook` registrations retain the resolved named directory as their physical grant; only `cwd` implies marker-based project discovery. A supplied roots list permits marker expansion only when one of its roots contains that marker. `AGENTS_CLIENT_REPO_ROOT` keeps its named physical grant. Memory may still normalize the project root independently; that normalization grants no additional cooperation access.

Removed physical checkouts are ignored on reads and pruned during owner registration, so normal worktree cleanup does not invalidate remaining grants or exhaust the 50-checkout limit. Existing paths that have been retargeted through symlinks, malformed records and non-directory grants still fail closed. Optional grant failures leave ordinary memory registration usable. Cooperation requires an explicit valid grant, including for the main checkout; an existing memory workspace without a grant must be registered again through the owner boundary. Missing grants never authorize a replacement path elsewhere.

Repository and checkout handles use verified canonical filesystem identity and installation namespace. Git remote equality does not join domains. Symlinks, case aliases and nonexisting filenames on case-insensitive filesystems are normalized; escapes, traversal, nested repositories and foreign handles are rejected. Registration and validation do not fetch artifact paths or URLs.

A participant capability is opaque and returned once. Only its verifier is stored; snapshots, events and receipts omit capabilities. Keep it in an authorized host/session channel, never in prompts shared with peers or artifact text. A parent creates a separate limited child capability or relays under its own capability with `subject_id`; every relay is recorded as such. Public IDs, contract hashes, persona names and the daemon bearer token do not substitute for participant authority.

## Mailbox workflow

Register each execution instance and retain its own returned capability privately. Registration defaults to `communication_mode="mcp"`; `native`, `parent_relay` and `checkpoint` remain supported declarations. Discover participant IDs with `cooperation_read`, then exchange only messages authorized by the task and host. Discoverability alone is not permission to contact an unrelated chat.

All three mailbox tools accept `request` and optional `workspace`, as the other cooperation tools do. Every request includes `protocol="agents-core/cooperation/1"`, `repository_id`, `participant_id` and that participant's `capability`. An optional `subject_id` lets an authenticated parent act only for its direct registered child in `parent_relay` communication mode. A child with its own capability acts directly. IDs and the daemon bearer token cannot read another participant's inbox or impersonate its sender.

1. Call `cooperation_send` with a stable `operation_id`, `recipient_id`, `kind` (`notice`, `request` or `response`; default `notice`) and a nonblank `body` of at most 4,000 characters. Optional `correlation_id` groups related messages. Success means the message was durably stored for that recipient, not that a model read or accepted it.
2. The recipient calls `cooperation_receive`. By default it returns pending messages after `after_cursor=0`, up to `limit=20` (maximum 50). It is non-destructive and returns ordered `messages`, `next_cursor` and `has_more`. Use the cursor to traverse that poll's pages; poll from zero again to retry all still-pending deliveries, including earlier pages. `include_acknowledged=true` also returns retained acknowledged messages.
3. After handling a delivery, call `cooperation_ack` with a stable `operation_id` and 1–50 unique `message_ids` from the recipient's inbox. This records delivery acknowledgement only. Agreement acknowledgement remains `cooperation_apply` with `action.kind="acknowledge"`; artifact acceptance remains `accept_artifact`.
4. Reply through `cooperation_send` as the recipient, address the original sender and set `reply_to` to the original `message_id`. The server checks that sender and recipient reverse the referenced pair. Reply content still needs independent verification and authorization before it affects work.

Each message records the effective `sender_id`, `recipient_id`, actual authenticated `actor_id`, server cursor/time, optional reply/correlation references and acknowledgement actor/time. Parent relays preserve the actual actor. Message bodies and mailbox receipts are excluded from `cooperation_read` and public events. They are stored in the private local database; capabilities are not protection against the trusted OS owner or a process with unrestricted access to that user's files.

Send and ack persist private idempotency receipts across restart. They share one operation-ID namespace per repository and actor, separate from `cooperation_apply` receipts. Retry exactly the same request after an ambiguous timeout; changing the payload or switching between send and ack under the same mailbox actor/`operation_id` returns `idempotency_conflict`. An acknowledgement does not delete the message. An inbox with 1,000 pending messages refuses another send with `mailbox_full`; the recipient must acknowledge deliveries before new messages can be accepted. There is no automatic message or mailbox-receipt collection.

Mailbox calls do not require a journal revision or participant generation and do not acquire claims, renew a heartbeat, clear quarantine, change priorities or validate delegated results. Successful inbox polling records usage only to prevent retirement as an unused registration; it is not evidence of execution liveness. A suspect participant can communicate during recovery; a cancelled participant with a still-valid capability can receive and acknowledge but cannot send new messages. New sends to cancelled recipients are also refused. No model is launched or woken automatically: poll at useful checkpoints and bound any waiting. Silence, successful send and delivery acknowledgement never constitute consent, agreement, artifact acceptance or permission to act.

## Actions and checks

| Action | Preconditions and result |
|---|---|
| `declare_intent` | Bounded resources, original scope, consumer-owned dependencies and priority provenance; rejects duplicate dependencies without mutation and preserves gates from committed agreements |
| `propose` | Explicit parties and original task bindings, resource owners, one integrator, artifacts, checks, dependencies, priority source, checkpoint and termination terms |
| `acknowledge` | Current proposal revision/digest and participant generation; final acknowledgement atomically commits compatible terms |
| `reject` | A required party explicitly rejects the current proposal |
| `acquire` | Ready work, original/agreed scope, compatible complete resource set and current claims; no partial grant |
| `heartbeat` | Current actor/generation; reports overdue checkpoints without stealing ownership or extending the bounded work unit |
| `checkpoint` | Immutable new checkpoint ID, producer-bound artifact revisions/digests, actual checks and limitations; release readiness is explicit |
| `release` | Current release-ready checkpoint and exact claim revisions; a previous work unit's checkpoint cannot release a new unit |
| `handoff` | Current task/generation bindings for every required agreement participant, old owner release-ready checkpoint accepted by the new owner, exact owner/digest and claim revisions, ready destination and atomic transfer |
| `accept_artifact` | Exact producer task/attempt, artifact digest/head, consumer binding and passing consumer checks; changed artifacts invalidate old acceptance |
| `cancel` | Acts only on the subject's task or nonterminal cooperation agreement; an original receipt still replays, while a new cancellation cannot overwrite a terminal outcome; held claims remain quarantined pending stopped-execution reconciliation |
| `recover` | Current service epoch, valid owner/registered parent, previous generation, execution evidence, inspected checkpoint and complete claim set |

For `cooperation_apply`, `operation_id` is unique per repository and actor within the action-receipt namespace; mailbox send/ack use their own shared namespace. Identical retries return the original durable receipt even after revisions advance. A changed body under the same ID returns `idempotency_conflict`. After an ambiguous timeout, retry exactly the same request or read current state; do not invent another ID. Response schemas are validated before committing the receipt.

Committed agreement dependencies retain provenance and cannot be removed by an individual intent update. The declared graph remains acyclic even after artifact acceptance, preventing artifact replacement from reopening a cycle. Reconciliation freezes every member of a cyclic strongly connected component, including overlapping cycles, while leaving unrelated work schedulable. Cancellation of a producer does not fabricate its consumer's artifact acceptance.

Read events use `cursor`/`next_cursor`. Snapshot arrays use `offset`/`next_snapshot_offset`, independently, with `limit` 1–100, exact array counts and truncation flags. Return the first page's `snapshot_time` on subsequent requests to keep time-derived queue aging and ordering fixed. Each page is consistent at its returned `revision`/`snapshot_cursor`; if revisions or service epochs change between pages, restart snapshot pagination without the old time before treating it as one complete view. Reconciliation transitions, including liveness expiry, proposal expiry, dependency quarantine and restart, advance the revision and emit coordinator-owned `reconcile` events. Mutation responses return at most 100 ready entries with total count/truncation; use paginated read for the full queue.

Cycle diagnostics return one witness per cyclic component, limited to 100 previews and 50 nodes per preview. `dependency_cycles_count` reports the component total and `dependency_cycles_truncated` marks either limit. A truncated preview is a partial path, not a fabricated closed cycle; paginated `dependencies` snapshots retain the full graph. Rejected mutations similarly mark a shortened `cycle` with `cycle_truncated`. Diagnostic limits never limit which participants or claims are quarantined.

## Priority and host limitations

Priority uses background/normal/urgent ranks, 600-second eligible-wait aging, reverse-DAG dependency inheritance and server-ready-sequence/task-ID ties. Idle, running and dependency-blocked time does not accrue new aging. A blocked-to-ready transition receives the current server revision as its queue sequence; continuous eligible waiting preserves its sequence and earned age. Inheritance is recomputed and disappears when gates are accepted, cancelled or replaced. A running writer is nonpreemptive.

An `authorized_user` field in model-supplied JSON is not proof. The service accepts an owner/host `priority_verifier` adapter; without one, self-asserted urgency ranks normal. Authorized parent relay can inherit its parent's verified base priority. The current daemon installs no priority-proof adapter and no heavy-execution adapter. Heavy claims therefore return `execution_lease_required`, preserving the existing execution-lease boundary. These are explicit host integration limits, not permission bypasses.

Heartbeat metadata has a 120-second soft-stale threshold; hosts may use 30-second heartbeats or checkpoint mode. The model is not promised a background timer. Checkpoint interval defaults/maxes at 300 seconds; acknowledgement timeout defaults/maxes at 120 seconds. Negotiation permits the initial proposal and one counterproposal per required party. Recovery of a suspended agreement requires new bindings and fresh acknowledgements.

Artifact acceptance records the consumer's declared checks. The service does not read artifacts, validate code or prove that a stopped-execution assertion is truthful. The trusted local host/owner supplies that evidence. It never kills processes, resets files or protects against arbitrary writes by nonparticipants. Snapshot reads must use a pinned revision before supporting final verification.

## Lost independent-root capability

The offline command is deliberately absent from MCP:

```text
python -m src.cooperation.admin --directory <private-cooperation-directory> \
  --repository-id <repository-id> --participant-id <participant-id> \
  --expected-generation <generation> \
  --claims-json '[{"claim_id":"...","expected_revision":42}]' \
  --execution-pid <confirmed-execution-pid> --checkpoint-id <inspected-checkpoint> \
  --inspection '<observed checkpoint and dirty state>' --reason '<owner reason>' \
  --owner-authorized
```

This requires explicit authorization from the human installation owner for that exact cleanup. It checks private state ownership, the exclusive administration lock (coordinator must be stopped), that the named PID is absent, inspection and exact claim revisions. Unknown/inaccessible/running execution state is refused. OS PID absence alone cannot establish a historical execution identity: the owner attests that binding and the inspected state. The atomic audited operation cancels the participant, invalidates its verifier and releases only its enumerated claims. It does not fetch/reset artifacts, resurrect an old identity or grant a replacement claim. A new root registers afresh after restart; survivors reconcile the new epoch.

## Fallback and rollout

Ordinary persona, history and stateless worker tools remain usable when coordination is unavailable. A parent coordinates its own workers through authorized native channels with one writer and unchanged result validation. Independent roots isolate worktrees and pause conflicting integration/ref actions. Missing channels, silence and expiry never imply consent or authorize cross-chat messages.

No paid model trials were run. The [authored bilingual fixtures](../../evals/datasets/cooperation.jsonl), [original instruction-size audit](instruction-size-results.json) and [mailbox instruction-size audit](instruction-size-messaging-results.json) support a later authorized rollout; none measures model reliability, token overhead or production safety.
