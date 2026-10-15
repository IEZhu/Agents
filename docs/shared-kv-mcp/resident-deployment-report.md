# Resident service deployment and Redis acceptance

This is a historical checkpoint recorded before the persistent-artifact integration
PR. References to uncommitted code, the serving worktree and paused updates
describe that checkpoint, not the deployment state of a later merged revision.

On 2026-10-15 the existing Agents-Core service was switched to the implementation
in the authorized `codex/shared-kv-mcp` worktree. Local inference and resident
reuse are enabled for this owner's service. Redis remains disabled in production;
its live acceptance used isolated authenticated loopback containers. Package
defaults remain disabled. The implementation is an uncommitted working-tree diff,
not a merged release.

At final verification the daemon, its existing embedding model and dedicated
native backend were ready, with zero active/queued inference jobs. The daemon
retained the same boot throughout post-restart cancellation, successor generation
and final verification. Its native child belonged to the current daemon. All 162
pinned runtime files and 60 native bundle files matched their manifests. The
canonical checkout remained clean on `dev`.

## Client behavior

Clients explicitly create a bounded context, optionally capture allowed source
files, grant limited capabilities, submit a request UUID, poll its retained result
and release the context. One dedicated llama.cpp process serializes generation
and cache work through a bounded FIFO. Duplicate submissions retain one job and
one request-budget debit, including after a reconnect. The eight tools do not
redirect cloud reasoning, replace persona activation or change host delegation.

Both existing stdio entries were checked against the working service: the Node
bridge and original raw Python entry listed all eight tools, returned enabled
status and answered the existing `list_flows`. The registered raw Python entry
delegates through the shared-service bridge.

With `auto`, compatible requests in the same context reuse their exact native
token prefix. The working daemon evaluated 473 input tokens cold, then returned
`resident_hit` with 457 reused and 16 evaluated tokens out of 473. A separate
context containing equal text evaluated all 473 and reused zero. All three bounded
JSON answers passed. The sequence passed again after cancellation with a new
engine epoch. This establishes observed reuse and isolation for this profile,
not a general speedup or quality claim. `off` requires zero reuse and no snapshots.

Resident state is process-local. The explicit broker restart preserved enabled
resident configuration and runtime selection, but old-grant generation, old-owner
polling and release returned `context_unavailable`. Clients create new contexts.
Allowed worktree source capture also matched the source file's SHA256.

Redis is optional L2 storage for a context still owned by the current broker after
native residency is lost. Compatible authenticated snapshots can restore reuse.
Expired, absent, corrupted or unavailable snapshots fall back cold before one
generation. Corruption and transport faults carry warnings; a normal missing
manifest does not require one. Failed or ambiguous generation is never replayed
automatically. Redis does not make broker capabilities durable.

## Fresh checks

| Check | Actual result |
|---|---|
| Focused regression gate | 1,425 passed, 66 skipped, 3 deselected; 352.80 s; exit 0 |
| Runtime identity | 17 fresh-interpreter cases included: canonical catalogs/state, strict source binding, stdio dispatch and runner overlap guards |
| Private acceptance helpers | 34 static checks, 12 failure-path checks and 14 preflight checks passed |
| Fresh isolated owner probe | 9 actual native attempts; resident/snapshot supported; no errors; owned child closed |
| Real Redis storage | 18 passed, 18.89 s; actual 536,870,913-byte payload in 129 chunks |
| Real TCP MCP matrix | 13 distinct scenarios passed across 14 harness attempts; one failed assertion retained |
| Matrix native accounting | 30 dispatch attempts including the failed-assertion run; five audits confirm closed backends and zero pending jobs |
| Working daemon | Both stdio entries, two core sequences, revoke/sibling/cancel/release, source capture and restart invalidation passed |
| Final working state | Retrieval/native ready; resident/snapshot supported; no pending work; Redis off and Redis client absent from production interpreter |

The focused gate covers local inference, daemon/runtime identity, startup/stdio,
scheduler paths, scheduled sync, launch agents, installer profiles and protocol
migration. Counts overlap: the runtime file was included explicitly and by the
daemon glob. They are executions, not a sum of unique tests. An interrupted broad
run is not a passing gate. An earlier recovery gate found a missing autodev
protected-path entry; both control/protected lists and documentation were corrected
before the final gate. The full repository suite was not rerun at an immutable
final revision.

Isolated HTTP uses the production strict MCP handlers over actual TCP, omitting
retrieval startup to avoid a second embedding model. Tests against the actual
retrieval-ready daemon supply deployment coverage. Production native attempts were
not instrumented with the isolated audit counter and must not be added to its 30
attempts. Cancellation observed terminal cancellation and successful subsequent
work, without claiming every cancel landed after the first generated token.

## Redis modes and faults

| Scenario | Observed behavior |
|---|---|
| Disabled | Status and eight tools; no native startup |
| Cache off | Core completions, zero reuse, no snapshots |
| Resident | Cold, same-context hit, independent context cold and lifecycle checks |
| Redis enabled | Status and ordinary core behavior, including resident reuse |
| Valid saved snapshot | Restore and positive reuse after another context displaced residency |
| Corrupted owned chunk | Integrity refusal; one cold completion, warning and correct budget |
| Owned Redis stopped | One cold completion and transport warning; no replay |
| Actual TTL expiry | Owned keys actually expired; one cold completion and correct budget |
| Redis restart without persistence | Changed server run ID; lost snapshot gives one cold completion |

Status cases also checked authentication and absence of inference startup. Core
checks covered missing/invalid workspace, cross-workspace refusal, concurrent UUID
deduplication, reconnect/result retention, request conflict, budget refusal and
context isolation. Deterministic and live storage tests additionally cover HMAC,
chunk bounds, capacity, TTL accounting and orphan cleanup.

The first expiry assertion incorrectly required a warning for a normal missing
manifest. Production already returned the correct cold answer, zero reuse and one
budget debit. Only that assertion changed; fresh expiry/restart cases passed.
`http-matrix.json` retains the failed batch; `http-acceptance-final.json` reconciles
all attempts. Failures were not overwritten with success.

Redis 7.4.11 used official image digest
`sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499`,
with private redis-py 7.0.0 dependencies. Containers used authenticated loopback,
read-only roots, dropped capabilities, no host mounts, bounded memory, no eviction
and no persistence. Only this task's random-name, label/ID-verified containers were
stopped/restarted/removed. Both were removed; all isolated brokers/native children
were closed.

This finite matrix covers macOS, the selected dense Qwen3-8B GGUF and llama.cpp
build 11429 (`d81235049384534c167caea52b85a694f6103d14`). It does not certify every
model, engine, Redis deployment, platform or fault timing. The [earlier Redis
report](redis-live-report.md) retains its historical benchmark and quality outcomes;
no new broad performance benchmark was run.

## Installation and rollback

Canonical installation remains `/Users/alexey.zhuchkov/Documents/Agents`; this task
never edited, committed, reset or switched its tracked checkout. Runtime source is
`/Users/alexey.zhuchkov/.codex/worktrees/shared-kv-mcp/Agents`. Service directory,
port, workspace UUIDs, embedding/model state, catalogs and personal library retain
their identity.

Private `service.json.runtime_root` explicitly binds source to the canonical
installation. A persistent owner-only interpreter shim selects it for daemon
`serve` and verifies the full runtime manifest on every start. Other invocations
use the original Python interpreter. No `.env`, credentials or indexes were copied
into the worktree. A hash-verified native bundle lives in private service storage;
the existing GGUF remains in its model cache.

Automatic updates are explicitly paused and unscheduled while this unmerged
runtime is pinned. Do not delete, archive or edit runtime source in its worktree
while serving requests. Restore normal update settings when returning to an
integrated runtime. Canonical `dev` is behind its remote; this task did not repair
or switch it.

Exact original service/updater definitions and configuration are retained under
private state `deployments/resident-20261015-c5902368eb0a/`. Private
`deploy.py rollback` restores them and starts the original runtime without
overwriting history, tokens, workspace registries or indexes with old copies.
Rollback was reviewed and prepared, not executed live. Restart of the deployed
runtime was executed and passed.

## Concurrent token rotation and preservation limits

The final byte-preservation assertion failed and remains recorded. All 53 workspace
records were unchanged, but the token and 53 of 102 discovered client files changed
during acceptance. A migration journal at 09:11 local contains 109 exact token
replacements, including managed bridges/clients. Its token-before hash matches the
baseline and token-after hash matches current. For 51 discovered Codex files,
journal-before/after hashes exactly explain the difference. Another 49 discovered
files remained byte-identical.

Two Claude files also contain concurrently changing application metadata. Their
current top-level and per-project MCP entries match the applied rotation journal.
Baseline stored hashes only, so exact original whole-file preservation cannot be
established for those two files. No configuration was overwritten to force a pass.
The current rotated token was retained and subsequent service tests used it.

Graceful shutdown at 09:11:01 and relaunch at 09:11:05 align with this transaction:
`rotate_token` stops, applies replacements and starts in probation. The first core
test had completed before that restart. No rotation command was issued by this
task; its initiator is unknown. Evidence strongly supports rotation as the cause,
not an observed unhandled crash. The later 09:13 restart was this task's explicit
test; final verification retained that boot.

## Evidence recovery and retention

Before deployment, an `archive-cleanup` operation removed the worktree during a
regression run. Original service configuration was still unchanged. The branch was
restored at its original path from snapshot
`88c7e2198d71fcec8ab3a2414ae12f0f536acdae`; all 162 runtime files matched the
pre-removal SHA256 manifest. Persistent rollback files, native bundle and production
capability records survived.

Ignored historical raw evidence was absent from that snapshot. Earlier numbers
remain recorded historical results, with full raw artifacts unavailable after the
incident. Private helpers were recovered from this task's own tool transcripts
using data-only heredoc/literal-patch recovery. Fresh regression, owner probe,
HTTP/Redis and deployment checks have new evidence. The interrupted run is not a
passing gate.

Current private evidence is under
`.shared-kv-evidence/2026-10-15/resident-deploy/`; `deployment.json` identifies the
persistent backup. Fresh reports/raw HTTP artifacts and exact prepared-worker
contracts/results are also copied to owner-only checkpoints outside the removable
worktree. They contain capabilities and owner paths and remain private/ignored.
Worker results were structurally validated, then their source, checks and limits
were independently assessed.
