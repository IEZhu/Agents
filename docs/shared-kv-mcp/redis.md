# Redis snapshot backend

Status: implemented optional backend, disabled by default. Real Redis storage and native lifecycle acceptance are recorded in the [Redis live report](redis-live-report.md), separately from the earlier resident-only evaluation. This design uses ordinary Redis binary strings as opaque storage. It does not assume Redis understands attention tensors or makes llama.cpp slots concurrent.

## Chosen version 1 integration

Use the pinned llama.cpp slot save/restore interface through a private local spool. There is no direct Redis import API in this design. The sequence is engine snapshot file -> bounded chunks in Redis -> reconstructed private file -> engine restore. This adds filesystem I/O and copies, all of which count toward evaluation.

LMCache already has a Redis/Valkey L2 backend, but its documented installation stack is not a verified drop-in for our Mac/llama.cpp setup. Do not add LMCache as a mandatory dependency, switch to vLLM, or claim Metal support based on the Redis connector. A later backend can implement the same narrow store interface.

The prior 8B snapshot was 553,315,860 bytes (527.68 MiB). A single default Redis string is limited to 512 MB. Therefore, never use one SET for a whole snapshot or serialize it to a giant base64 JSON value. Sources are in [evidence](evidence.md).

## Narrow interface and configuration

Design an async SnapshotStore with `lookup(key)`, `load(manifest, destination)`, `save(key, source, manifest)`, `delete(key)` and `close()`. The service owns compatibility decisions; the store owns chunk I/O, manifest integrity and storage limits. Implement NullSnapshotStore and an in-memory fake before RedisSnapshotStore.

Redis is disabled unless the owner config enables it and provides the name of an environment variable holding the Redis URL. Never put credentials in a committed example, tool input or result. Use a dedicated local Redis instance/database for this feature; version 1 accepts only loopback endpoints. Do not change Redis server configuration, flush a database or start a user's Redis service automatically.

Suggested implementation dependencies are optional extras: declare httpx explicitly for the local HTTP backend (the inspected environment has 0.28.1), and redis-py only for the Redis extra. Verify the selected versions against the actual methods used and record them in the acceptance report. Base installation and disabled tools must import without either extra.

Defaults selected for version 1:
- Chunk size: 4 MiB, fixed within a manifest.
- Maximum snapshot: 1 GiB.
- Store budget for this installation namespace: 2 GiB including pending writes; this is an application admission cap, not a change to Redis maxmemory.
- Spool budget: 2 GiB of logical file bytes, reserved before save or reconstruct; native save must also have an enforced per-file bound as specified below.
- At most one snapshot store/load job; bounded pipeline of at most 8 chunks.
- Redis connect timeout: 1 second; individual I/O deadline: 5 seconds; whole snapshot operation: at most 30 seconds and the job's remaining deadline.
- Snapshot TTL: minimum of context remaining lifetime and 900 seconds.
- Chunk TTL: manifest TTL plus 60 seconds, allowing bounded cleanup after partial writes.
- Compression: off in version 1.

If no known free capacity can be admitted, skip snapshot creation and report a cache warning. Do not evict unrelated keys. Keep resident generation usable.

## Manifest and keys

Use a private persistent HMAC key generated in the service's private state directory, separate from transport and capability tokens. It authenticates our own snapshot metadata and bytes before they reach the native parser. A public SHA256 alone detects corruption but cannot establish who created a blob. Never use pickle, eval or executable deserialization.

Logical key components: protocol version, installation ID, canonical authorized source-root identity, workspace ID, owner_config_revision, stable model profile hash, content fingerprint and random context instance ID. Hash the canonical components for Redis key material. These fields are fixed by the service, not arbitrary client Redis keys. Generation-specific immutable chunk keys prevent partial replacement of an existing snapshot.

Example key layout (a design, not current repository behavior):

```text
agents-kv:v1:<installation>:<context-key>:manifest
agents-kv:v1:<installation>:<context-key>:<snapshot-uuid>:chunk:<index>
```

The manifest is small, bounded JSON. Require:
- schema/version, installation/workspace/source-root identities;
- stable profile hash, owner_config_revision, content fingerprint and context instance ID;
- snapshot UUID, creation/expiry timestamps;
- full byte length, chunk size, ordered chunk count;
- SHA256 per chunk and of the entire payload;
- engine saved token sequence hash and token count, plus its authoritative rendered-input identity and a bounded token-state sidecar digest/byte count;
- HMAC of canonical manifest fields including payload hashes.

Store the exact saved token sequence in a separate generation-specific binary sidecar (packed unsigned 32-bit little-endian IDs, maximum 1 MiB). Include its SHA256 and byte count in the signed manifest, upload it before manifest publication, and verify it before LCP checks after restore. A hash alone cannot reconstruct the sequence. Reject out-of-range IDs; the native snapshot remains a separate opaque payload. Sidecar bytes count toward TTL, capacity and cleanup.

Do not put the secret signing key or grant tokens in the manifest. Reject unknown versions, extra fields, inconsistent sizes/counts, expired entries, profile/context mismatch or invalid HMAC before allocating a large buffer or restoring native state. Bound manifest bytes to 64 KiB and chunks to 256.

## Exactly what gets saved

Version 1 stores a completed slot state after a successful normal generation, not a fabricated prefix-only tensor cache. The native slot may include that task and its generated suffix. Keep the corresponding exact token state alongside metadata so that a later request can compare the longest common prefix and let the engine discard the unrelated suffix.

Obtain generated IDs directly from the pinned native completion response (return_tokens); never retokenize output text to reconstruct state. The adapter must define saved_token_state from authoritative rendered input IDs and the native evaluated output-token subset. Before enabling snapshots, inspect the pinned engine source and demonstrate its exact rules for sampled-but-not-yet-evaluated final tokens, EOS/stop tokens, stop-string trimming and length termination. Record that mapping version in the stable profile. The owner probe must cover those finish cases and compare derived state length with native n_saved/n_restored, then verify reuse with controlled continuations against the saved sequence. Do not blindly append all returned IDs or truncate merely to make a count match. If exact mapping cannot be established, leave snapshot support unsupported and continue resident/cold operation. Save only when native counters and the full completed token state are internally consistent and the snapshot-roundtrip capability was demonstrated for this profile. The snapshot can contain task/output data as well as the shared prefix; scope it to this context's trust domain and never reuse it in a different context.

Keep one published snapshot per context instance. Equal text in two independently created contexts does not share a manifest, chunks or deletion scope. Do not run an extra hidden prefill merely to produce a smaller snapshot. A disabled-cache control neither reads nor writes Redis. Redis stores no generic answer cache; retained job results for idempotency remain in broker memory and are not confused with KV reuse.

## Enforce the native spool bound

A post-save size check cannot enforce a disk cap. For the POSIX version 1 backend, launch the owned engine through a small Python exec wrapper that sets soft and hard RLIMIT_FSIZE to max_snapshot_bytes, then os.execv into the configured engine. Do not use subprocess preexec_fn in the threaded daemon. This per-file OS limit applies before any native save; the model is read-only, stdout/stderr use bounded pipes, and the engine gets only one generated snapshot filename per serialized operation.

Before saving, reserve max_snapshot_bytes plus the bounded sidecar size against the spool budget, including existing owned partial files. If the reservation cannot fit, skip saving. No concurrent native save or reconstruction is permitted. Cleanup old owned partial spool files at startup before admitting snapshots. Restore writes are separately bounded by the verified manifest length. Never assert an OS limit that was not installed successfully: disable snapshots if the limit or wrapper cannot be used. Test with a fake child writing beyond the limit and verify actual bytes on disk remain capped. A native limit violation may kill the engine; retain an already valid completed answer with a cache warning, reap/fence that child, invalidate residency and restart only for a later explicit job. A truncated snapshot is never published.

The implementation authenticates run/lease inode identities with a private
installation key and retains an advisory lock in both supervisor and native
child through exec. Startup reclaims only a verified inactive run with the known
private layout. Snapshot admission scans the installation's bounded native
namespace; old live writers, legacy/unsafe state or exhausted scan bounds decline
optional snapshots. A directory name or PID alone never authorizes cleanup or
process termination. Normal cold/resident generation remains available.

## Atomic publish

1. Hold the inference scheduler's exclusive slot ownership through native snapshot save. No other generation or restore may change the slot.
2. Acquire the full spool reservation and verified OS write limit described above. Generate an unpredictable basename inside the private spool; never accept a path from a tool. Use private directory permissions and reject symlinks/nonregular files.
3. Call the pinned engine's save operation. Verify its reported byte/token counts against the file and known state. Refuse over-limit size before loading the file into memory.
4. Stream the file in bounded chunks, hashing as it is read. Reserve namespace capacity for the entire pending snapshot first. Do not allocate a second full snapshot in Python.
5. Write immutable generation-specific chunks with TTL. A failed chunk write leaves no published manifest. Best-effort delete written chunks; TTL handles residuals.
6. Sign and atomically publish the final manifest only after every chunk write succeeded.
7. Delete the previously published generation's manifest references/chunks after replacement; never delete the newly published generation by a broad pattern.
8. Release capacity reservation and delete the spool file in finally. Record save/store time and bytes even if publication fails.

The single broker is the writer in version 1. Do not claim distributed locking or cross-broker revocation semantics. On broker restart, reconcile only this installation's namespace using bounded SCAN, bounded manifest reads and recorded sizes; until accounting is rebuilt, do not admit new writes. Count pending orphan bytes conservatively until cleanup/TTL is confirmed. Redis maxmemory failures are a normal cache failure, not a reason to alter global server policy.

## Load and restore

1. At dispatch, validate context capability, live sources, stable profile and deadline before Redis access.
2. Read one bounded manifest, validate compatibility and HMAC, and reserve spool space.
3. Stream bounded chunks to a new private file. Verify order, chunk lengths/hashes and final size/hash; do not concatenate the whole snapshot in memory.
4. Under exclusive slot ownership, ask the engine to restore that private basename. No other caller can access this spool.
5. Treat native restored-count success as provisional. Only the next actual generation's cache counters establish a hit. Check reuse against the recorded token-state prefix.
6. If the blob is absent/expired/incompatible/corrupt, discard it and take the cold path without a model call having been consumed.
7. If restore failed after touching native state, erase the slot or restart/reap the owned engine before one cold generation. If the remaining deadline cannot cover recovery, fail the job.
8. Delete the reconstruction file in finally. Preserve a bounded warning code; never include payload bytes or credentials in errors.

A successful generation followed by a failed snapshot save remains a successful generation with a cache warning. Saving is optional and must not turn correct model output into a second model call. All awaited save/store time is included in the terminal job duration. If no snapshot deadline remains, skip storage.

## Deletion, expiry and restart

Release invalidates capabilities immediately and removes only matching manifests/chunks. Revoking one child stops that child but does not remove a context still owned by the parent. TTL bounds retained blobs after crashes; it is not immediate revocation. Redis keys never independently authorize use of a context.

Broker restart loses all public capabilities and job results. Restoring an old snapshot into a newly created context is not supported in version 1, even when content matches. Context instance IDs and capabilities are ephemeral. Old manifests remain bounded orphans for namespace cleanup/TTL and can never authorize a new context. An engine-only restart within the same broker can restore for the still-live context, subject to matching owner_config_revision, HMAC, identities/profile/content and expiry. Never resurrect old tokens from Redis.

Document the trust boundary: the local OS owner and service-owned Redis are trusted; a user who can read the HMAC key or bearer token is inside that trust domain. Do not expose Redis or the internal engine to a network.

## Evaluation decision

Compare resident-only versus resident-plus-Redis under controlled eviction/restart. Include save, upload, lookup, download, restore, first token and full completion time. Redis is useful only when avoided prefill exceeds these costs. A negative result should leave Redis disabled by default; do not change models, prompts or output limits between arms to manufacture a speedup.
