# Memory acceptance and retries (MEM-01)

`log_interaction` waits for its local memory write. It no longer acknowledges an
in-memory history queue that can drop writes. Langfuse remains best effort.

## Results and retries

| Result | Meaning and next action |
|---|---|
| `history.status: recorded`, `history.state: durably_accepted` | The complete journal entry and, when keyed, its retry receipt are committed. |
| `history.status: duplicate`, `history.state: durably_accepted` | An unkeyed legacy content duplicate already exists in the live deduplication window. |
| `history.status: error`, `history.state: rejected`, `error: idempotency_conflict` | The key was used with different content; the journal is unchanged. |
| `history.status: error`, `history.state: rejected`, `error: idempotency_collision` | The shortened entry ID collides with another entry; nothing is overwritten. Inspect the conflicting entry before submitting a new logical write. |
| `history.status: error`, `history.state: rejected`, `error: history_entry_conflict` | An unkeyed retry's ID matches a complete different entry. Inspect the journal; the existing entry is preserved. |
| `history.status: error`, `history.state: rejected`, `error: journal_symlink_unsupported` | The live journal is a symbolic link. Resolve its storage location before writing. |
| `history.status: error`, `history.state: rejected`, `error: history_busy`, `retryable: true` | The bounded memory worker queue could not admit this write. Retry later with the same payload and key. |
| `history.status: error`, `history.state: unknown`, `error: history_unwritable` | Persistence is uncertain. Retry the same payload with the same key; do not invent a new key. |
| `history.status: error`, `history.state: unknown`, `error: history_timeout`, `retryable: true` | The commit did not finish within 10 seconds. It may still complete; retry later with the same payload and key. |
| Protocol `ERROR` for workspace/key validation | Nothing was accepted by this call. Correct the scope or key before retrying. |

Generate a fresh UUID as `idempotency_key` for each new logical write, including
across conversations, and reuse it only on retries of that write. The API accepts
any nonblank key of at most 256 characters; recurring labels such as `turn-1`
are unsuitable because receipts never expire. `request_id` is only correlation. The server hashes
the normalized caller content, including query, answer, curated fields, files,
tags and persona attribution; generated timestamps and transport labels are not
part of that fingerprint. A retry returns the original **history receipt and
answer timestamp**. The request ID, PID, workspace report and telemetry status
describe the current invocation and may differ. Changing attribution under the
same key is a conflict; the older unkeyed attribution-repair behavior is retained.

Keys are scoped to the canonical, trusted Personal journal path and the
`log_interaction/v1` operation. The local owner and filesystem authorization are
the current trust boundary. Neither an agent name nor an arbitrary workspace
argument establishes ownership. Keyed calls reject unresolved/unauthorized
workspaces instead of redirecting to the fallback journal. Unkeyed Personal
clients retain their existing fallback behavior. Journal symlinks are refused
before writing because replacement would change the journal's physical identity.

Reads (including statistics, raw UI source and semantic indexing), receipt lookup
and Git export also refuse symbolic links and nonregular
files in the live journal, pending rotation and monthly archives. Local archive
paths are opened relative to the workspace root; the Python API's explicit
external `archive_dir` retains its separate trusted root, which must not itself
be a link. POSIX opens each descendant
directory and file without following links. Windows verifies the final opened
handle's path before reading. Parse-cache hits undergo the same file checks.
These checks rely on CPython and the filesystem reporting file identity and
name-surrogate reparse metadata correctly; they do not defend against a provider
that hides such metadata. CPython may omit the tag for an ordinary cloud-backed
file or directory, which is not by itself evidence of a link.
An unsafe artifact causes an explicit storage error rather than a partial
export or a successful acceptance based on another journal's receipt. Repair
the storage layout before retrying with the same idempotency key.

A regular project file named `history` (or `HISTORY` on a case-insensitive
filesystem) does not disable ordinary reads or export. Keyed acceptance still
fails closed when that path prevents inspection of its receipt archive.
It also prevents rotation from finishing: a pending journal remains recoverable,
but history UI deletion waits until the archive path is repaired. An unreadable
workspace is marked unavailable in the UI list and skipped in cross-workspace
search, leaving the other workspaces usable.

Receipts have **no automatic expiry**. Lookup includes the live file, a pending
rotation and every local monthly archive, independently of the legacy last-50
deduplication window. Deleting an entry through the history UI retains a compact
receipt comment without the deleted text; replay does not resurrect it. That
replayed receipt confirms the historical acceptance, not the entry's current
visibility. Removing
or manually rewriting journal storage, restoring an older backup, or moving the
workspace to a different canonical path ends that retry guarantee. Back up the
whole journal and archives together. Remote Git segments are not an idempotency
authority for another machine or workspace.

## Commit and recovery

The Markdown journal remains the single source of truth. The writer holds its
process mutex and stable cross-process sidecar lock, writes the old journal plus
the complete new entry and receipt into a sibling temporary file, flushes and
`fsync`s that file, atomically replaces the live file, then syncs its directory.
A crash before replacement leaves the previous journal intact; a crash after
replacement but before the response is resolved by a same-key retry. A retry also
syncs the file/directory before replaying a receipt. Rotation persists the archive
directory before discarding its recoverable pending source.

A dedicated memory worker keeps storage waits out of the daemon's general I/O
pool. At most one write runs and seven wait, leaving slots available under the
daemon's 32-request limit if storage stalls. This queue is admission control,
not durable acceptance: the tool waits up to 10 seconds for the actual commit.
A timeout or client disconnect does not cancel an admitted write. Real work
remains tracked for draining; at most eight memory requests can hold those slots.
After an ambiguous outcome, retry with the same key. These bounds provide failure
containment, not a measured throughput or latency guarantee. Live-journal
replacement uses private temporary files and preserves an existing journal's mode. Abrupt process
death can leave an unused temporary file, but it is never treated as accepted
memory. Temporary-file housekeeping is not part of this change.

For a Linux Docker deployment this requires a persistent local volume whose
filesystem and storage honor file and directory `fsync`, atomic same-filesystem
rename and file locks. A container's disposable writable layer, lost VDS disk,
storage corruption and unsynchronized network filesystems are outside this
guarantee. File sync alone does not persist directory entries; see the
[Linux fsync contract](https://man7.org/linux/man-pages/man2/fsync.2.html).
macOS uses ordinary `fsync`, not `F_FULLFSYNC`; acceptance therefore promises
process-crash recovery, not persistence through loss of drive power or volatile
drive caches. See the [Apple fsync contract](https://developer.apple.com/library/archive/documentation/System/Conceptual/ManPages_iPhoneOS/man2/fsync.2.html).
Windows retains process-crash recovery and its existing sidecar locking; this
implementation does not claim power-loss durability there because it does not
flush Windows directory handles. No new dependency or database migration is
required.

Acceptance does not mean **indexed** or **synchronized**. Those are downstream
states and are not asserted by this response. Semantic indexes rebuild from the
journal on demand. Personal Git export reconciles registered checkouts after
startup and on subsequent sync; a keyed retry also reschedules reconciliation
without marking the old entry fresh. A previously unregistered checkout still
needs registration/sharing approval. A crash before notification cannot erase the
accepted record. Existing sharing approvals, secret checks, per-machine segments, deletion
tombstones and local-copy precedence still apply. MEM-02 owns bounded background
processing, retry scheduling and backlog observability.

## Dependencies and release boundary

Existing trusted workspace resolution, journal readers and Personal Git
reconciliation are available and reused. A general authenticated owner/tenant
context and Hosted storage adapter (Stage 3 of the deployment plan) are not
implemented in this revision. Public Hosted writes must bind their scope to that
server-verified principal; a caller-provided owner string is not a substitute.
This change completes the Personal/Core acceptance primitive, not the public
multi-tenant deployment gate.

Atomic replacement copies the bounded live journal; keyed lookup currently
scans the complete local journal. It favors a small, reviewable storage contract
over an additional database. Large histories and burst concurrency need the
OVL/LOAD tasks' measurements before directory launch; no capacity or user-count
claim is made here. Retention compaction requires its own durable tombstone and
expiry contract before receipts can be removed safely.

Regression coverage includes real child-process exits before commit and after
commit/before response, cross-process and concurrent retries, changed payloads,
archive-only replay, deletion followed by retry, workspace refusal and immediate
I/O error reporting. The existing Personal/Git suite remains part of acceptance.
The Workspace bindings workflow runs the complete history, memory-acceptance,
artifact-safety, statistics and history-UI suites on Linux, macOS and Windows
with Python 3.11, plus Windows with Python 3.13. CI requires the artifact-safety
and workspace-isolation symlink cases to execute; missing Windows symlink
privilege fails that step. Local runs may skip those cases without the privilege;
POSIX-only cases report their skips. The remaining durability and replay checks
still run. The User sync workflow
separately includes the Personal/Git history tests on all three platforms.

Teams/Hosted contributions follow the `team` integration path in the
[release procedure](releases.md#procedure).
