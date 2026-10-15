# Implementation validation

The mailbox extension has its own verification record below. The separate historical results cover the original coordinator and are not reused as evidence for the new tools or schema-3 migration. No cross-provider model trial or live deployment was performed.

Implementation baseline: `d7e34eb10cb2ab9f593ebf782a4538f677484493` (`origin/dev`). All 19 SHA-256 source fingerprints in the research manifest matched before edits. Work was performed only in the separate `codex/agent-cooperation-runtime` worktree. The live installation remained clean; no rollout, publication, paid/model evaluation or unrelated chat message was performed.

## Mailbox implementation checks

| Check | Actual result |
|---|---|
| Complete cooperation suite, tool annotations and remaining installer instruction checks after review fixes | 234 passed, 8 skipped in 100.51 seconds; exit 0 |
| Routing, memory migration, installer profiles, worker contracts and rules | 271 passed, 45 skipped in 77.90 seconds; exit 0 |
| Final compact worker audit | 176 packages, 44 profiles and four result types; all within 16,000 characters, range 13,821–15,399; 97 characters smaller than the pre-mailbox packages; no optional skill-selection changes |
| Independent review closure | Both reproduced defects fixed; no unresolved actionable findings within the reviewed scope |

The final runtime command was `python -m pytest tests/test_cooperation_identity.py tests/test_cooperation_instructions.py tests/test_cooperation_mailbox.py tests/test_cooperation_mcp.py tests/test_cooperation_models.py tests/test_cooperation_policy.py tests/test_cooperation_service.py tests/test_tool_annotations.py tests/test_installer_instructions.py tests/test_codex_instructions.py -q`. The instruction/migration command was `python -m pytest tests/test_cooperation_instructions.py tests/test_install_instructions.py tests/test_installer_profiles.py tests/test_protocol_migration.py tests/test_subagent.py tests/test_rules.py -q`. Both used `/Users/alexey.zhuchkov/Documents/Agents/.venv/bin/python` in the implementation worktree with `LANGFUSE_TRACING_ENABLED=false EMBEDDING_MODEL=intfloat/multilingual-e5-large`. Counts overlap; they are not a unique-test total. The complete regular suite was not repeated after the mailbox extension.

The MCP integration starts two independent initialized SDK sessions with separate HTTP clients against an isolated ASGI daemon and real persistent coordinator. They discover the tools, register separate linked checkouts and exchange a request, correlated reply and delivery acknowledgements. It checks exact send/ack retry, changed-payload conflict, recipient authentication, private inboxes, public-journal isolation, unchanged coordination revision, cursor boundaries and daemon bearer rejection. These are actual protocol clients, not provider model runs or a deployed service.

Mailbox tests additionally cover concurrent sends with one operation ID, durable receipts across restart, atomic acknowledgement batches, parent relay boundaries, foreign repositories, pending-message pagination and backpressure, cancelled/revoked identities, schema-1/2 migration, activity retention and projection rollback. Mail never renews heartbeats, clears quarantine, accepts agreements or transfers claims. The independent reviewer reproduced a receive-only listener being retired as unused and an unbounded cursor overflowing SQLite. Successful polling now records authenticated activity without changing liveness, and mailbox cursors are limited to `0..2**63-1`. Parser and real MCP regressions cover both cursor endpoints; the receive tool declares non-destructive, idempotent bookkeeping rather than a strictly read-only operation.

API, documentation and review worker envelopes, including revised results, passed validation against their unchanged parent-retained contracts. The parent inspected the changed source and executed the final suite; structural validation was not counted as proof of correctness. The reviewer independently checked both corrections, relay activity, rollback and exact schema export. The final offline [worker-size audit](instruction-size-messaging-results.json) uses the same fixed task as the earlier audit with component toggles enabled and no model calls.

## Mailbox documentation checks

The messaging documentation pass checked the live worktree definitions in `models.py`, `mailbox.py`, `store.py` and the MCP wrappers against the described send/receive/ack/reply workflow. It also read `src/daemon/clients.py` and `src/startup.py` to confirm that the existing generic stdio bridge reaches the installed HTTP authority. This was source inspection, not a live client or model-provider experiment.

`LANGFUSE_TRACING_ENABLED=false /Users/alexey.zhuchkov/Documents/Agents/.venv/bin/python -m pytest tests/test_cooperation_instructions.py -q` passed after the final messaging-guidance edits: **7 passed in 0.44 seconds**. A local-link check found **208 existing targets and no broken paths** across the seven changed Markdown files. These checks cover instruction propagation and document references; they do not substitute for the mailbox persistence, authorization, migration and transport tests recorded above. The earlier compact-worker audit below remains historical; the new audit records the final messaging rule.

## Recorded checks before the mailbox extension

| Check | Actual result |
|---|---|
| Focused cooperation and touched subsystem suite | 598 passed, 10 skipped in 60.68 seconds before the final review fixes |
| Final complete cooperation suite, including retention and migration | 136 passed in 84.84 seconds |
| Local daemon/process tests outside the sandbox | 115 passed, 1 skipped in 18.07 seconds |
| Full regular suite | 7,231 passed, 107 skipped, 19 deselected in 4,463.91 seconds (1:14:23); exit 0 |
| Compact worker audit | 176 packages (44 profiles × four result types), all within the 16,000-character default for the fixed recorded task; 13,918–15,496 characters, +1,498 each |
| Model behavior fixtures | 14 bilingual scenarios, 28 authored cases; no model trials run |
| Whitespace and offline cleanup CLI | `git diff --check` and `python -m src.cooperation.admin --help` passed |

Tests use the existing Python environment with imports resolved to the implementation worktree. `LANGFUSE_TRACING_ENABLED=false` and `EMBEDDING_MODEL=intfloat/multilingual-e5-large` select the existing cached model for broader repository tests. The test suite redirects derived state, daemon files, client configuration and notifications into its fixtures.

The final targeted command for that pre-mailbox revision was `python -m pytest tests/test_cooperation_service.py tests/test_cooperation_models.py tests/test_cooperation_policy.py tests/test_cooperation_identity.py tests/test_cooperation_mcp.py tests/test_cooperation_instructions.py -q`, using the existing absolute virtualenv interpreter. It reran all 136 cooperation tests after that revision's final code changes. The broader suite began before those last review fixes; the targeted run covers that original coordinator source only.

The first full run under the sandbox encountered local-process/loopback restrictions and was interrupted after review fixes were made. Its 11 recorded failures were in `test_daemon_peer.py` and `test_daemon_user_sync.py`; both suites passed together outside the sandbox. That interrupted run is not counted as a successful full-suite result.

## Deterministic scenario coverage before the mailbox extension

The [scenario matrix](evaluation.md) remains the semantic acceptance reference. Passing tests are executable evidence, not a claim that arbitrary external writers or hostile processes are fenced.

| Scenarios | Executed test evidence |
|---|---|
| C01, C02 | `test_cooperation_service.py`: separate spawned processes and a barrier race for the same file; exactly one grant; complete-set rollback on a busy resource |
| C03–C05 | Service tests: concurrent counterproposals, superseded acknowledgements, original receipt replay and changed-body idempotency conflict |
| C06, C07, C27 | Service tests: stale quarantine, restart epoch and fresh generation; process exit before/after transaction commit with claims/events/receipts all absent or present; old receipt replay |
| C07a, C11a, C11b | Service tests: offline owner cleanup refuses running coordinator/execution, absent authorization, stale/incomplete claims and oversized audit; cancellation invalidates capability without touching checkout files |
| C08 | Identity, service and real MCP tests: linked worktrees share repository identity, private paths remain independent, logical intent is visible and integration target is serialized |
| C09, C10 | `test_cooperation_identity.py`: symlink/escape, case alias, new-file sensitivity, external grant, submodule and preserved generic memory containment |
| C11–C13 | Identity/service/real MCP tests: foreign capability/root/relay refusal, child scope limits and explicit parent acknowledgement actor/subject |
| C14–C16 | Policy/service tests: cycle rejection, accepted-edge reopening protection, component isolation during reconciliation, dependency inheritance/removal, 600/1200-second aging and idle/blocked-time exclusion |
| C17, C30 | Service/policy tests: current writer never preempted, overdue checkpoint is reported, heavy execution refused without host lease admission |
| C18, C19 | Service/schema tests: bounded counterproposals, expiry/rejection, no ownership from silence, handoff refuses future checkpoints and binds existing checkpoint owner/digest |
| C20, C21 | Service tests: artifact digest/head/producer gates, changed acceptance invalidation, committed-gate provenance and explicit own-task cancellation |
| C22, C23, C26 | MCP/daemon/service tests: disabled/stdin unavailability, one lifecycle authority, namespace isolation, preserved claims and documented direct-shell limitation |
| C24, C25 | Service/model/MCP tests: expired cursors, independent snapshot pagination, strict malformed/oversize/duplicate/nonfinite/type input, closed output schemas and secret-free diagnostics |
| C28, C29 | Worker/instruction tests: exact flow selections remain unchanged, universal equivalent exclusion, mandatory fifth worker rule despite toggles, honest missing/oversized-rule errors |

`test_cooperation_mcp.py` executes the registered tools through a real isolated HTTP MCP daemon and persistent coordinator for both independent roots and a parent/child relay. Fake-service tests additionally isolate transport and lifecycle failure behavior. Model/response tests compare the public schema export to runtime generation and check closed nested shapes.

## Review before the mailbox extension

Prepared schema, host and instruction workers returned reports validated against their parent-retained Agents-Core contracts. The independent review report also passed structural validation; the parent checked its evidence and added reproducing tests. Structural validation alone was never counted as factual proof.

The independent review found nine defects across agreement completion, dependency provenance, checkpoint handoff, bounded diagnostics and queue fairness. The implementation now preserves declared-DAG acyclicity and committed gates, binds existing immutable checkpoint owner/digest, caps queue/cycle projections and validates mutation responses before commit. Completion runs after the final participant state is saved. Evidence acceptance preserves earned waiting time, while newly unblocked work receives a fresh ready sequence. Reconciliation quarantines every member of overlapping cyclic components, independently of diagnostic preview limits. Regression tests cover each defect. The final reviewer reran 12 focused scenarios successfully and reported no remaining actionable findings within that closure-review scope; its unchanged result passed the original contract validator.

The final specification check also added retirement of lost-response registrations. A separate closure review passed both retention/migration regression functions and three isolated probe groups covering activity rollback, parent relay, nonterminal agreement exclusion, retained claims and identity nonreuse. Concurrent first use versus collection was checked against SQLite's write-transaction serialization in source; no new multiprocess retention race was claimed. The revised review envelope passed the original contract validator.

## Rollout limits

No live daemon enablement/restart, native Windows/Linux runtime trial, paid model run or production concurrency benchmark was performed. Native filesystem case behavior was tested on macOS; platform branches remain covered by the repository's platform CI where applicable. A passing deterministic suite does not establish prompt reliability or token/latency overhead.

The existing generic stdio bridge forwards eligible clients to the installed HTTP daemon; a standalone stdio engine has no coordinator. The original report's assertion that no stdio forwarding existed was incorrect. No new bridge or live transport rollout is implied by this correction. The daemon has no priority-proof or heavy-execution admission adapter. It uses explicit checkpoint/parent-relay capability handling and treats unverified urgency as normal. Metadata coordination cannot prevent arbitrary shell writes, validate artifact contents or independently prove a host's execution-stop assertion. The mailbox does not wake models or call provider APIs, and messages/private receipts have no automatic deletion. See [runtime operations](runtime.md) for the trust boundary and owner recovery procedure.
