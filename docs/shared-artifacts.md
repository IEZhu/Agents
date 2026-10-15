# Persistent shared artifacts

Artifacts hold exact UTF-8 task data outside a worker's prepared prompt. Publish
the bytes once, select immutable versions in a task manifest, and let native
workers read bounded slices through MCP. An optional local completion can use
the same manifest by having the broker hydrate its selected bytes into a local
context. Neither path treats stored text as instructions or permission.

This reference describes the implementation in `src/artifacts/`,
`src/schemas/artifacts.py`, `src/engine/subagent.py` and `src/local_inference/`.
The [earlier local-inference reports](shared-kv-mcp/START_HERE.md) retain their
recorded revisions and do not establish artifact deployment or acceptance.
The [test guide](../tests/README.md#persistent-artifact-checks) identifies the
focused checks; it does not record a passing run.

## Storage and lifetime

The store lives under `state_dir() / "artifacts"` in the installation's private
service state. The default state location is outside repository worktrees;
`AGENTS_SERVICE_DIR` overrides it. Artifact content, original metadata, manifests and
release tombstones persist on disk independently of Redis and local-generation
contexts. A broker or engine restart does not erase them. Local-generation
capabilities remain ephemeral and must be created again after broker restart.

Each resolved workspace root has its own namespace. The artifact ID is
`art_` followed by the lowercase SHA256 of its exact bytes. Repeated publication
of the same bytes in that workspace returns the first descriptor, including
its original name and media type. It does not rename or reinterpret the data.
Changed bytes produce another ID. A captured artifact is a snapshot: later
edits to its source file do not alter it.

A manifest contains the task ID, workspace hash, creation/expiry times and
selected descriptors with their expected hashes, required flags and purposes.
It has its own ID and canonical-content SHA256. Every manifest operation pins
both; there is no "latest" lookup. Manifest expiry or explicit release ends
subsequent access without deleting artifact bytes. Release is idempotent and
cannot retract bytes already read by a worker. Discard pending output based on
a released manifest and prepare a new authorized attempt when needed.

There is no garbage collection or deletion tool in this storage format.
Expired and released manifests still count toward the manifest quota, and
release does not reclaim artifact space. These are persistent storage limits,
separate from model context limits:

| Limit | Value |
|---|---|
| Artifact bytes | 4 MiB (4,194,304 bytes) |
| Artifact content bytes per workspace | 64 MiB (67,108,864 bytes) |
| Distinct artifacts per workspace | 256 |
| Manifests per workspace, including expired/released manifests | 256 |
| Unique entries per manifest | 1–32 |
| Manifest lifetime | 30–604,800 seconds; default 86,400 seconds |
| Read slice | 4–65,536 bytes; default 16,384 bytes |

## Six MCP tools

Successful calls return `schema: "agents-core/artifacts/1"`, `status: "ok"`
and a `data` object. Storage/access failures return `status: "error"` with a
bounded error code and message. Invalid tool arguments are rejected without
echoing private content in validation diagnostics. None of these tools starts
a model or requires Redis.

| Tool | Inputs and result |
|---|---|
| `artifact_publish` | Required `name`; optional `media_type` defaults to `text/plain`; supply exactly one of `content` or `source_path`. Returns an immutable descriptor with `artifact_id`, `sha256`, `size_bytes`, `media_type` and `name`. |
| `artifact_manifest_create` | Required `task_id` and `entries`; optional `ttl_seconds`. Each entry supplies `artifact_id`, expected `sha256`, `purpose` and optional `required` (default `true`). Returns the complete `agents-core/artifact-manifest/1` manifest with pinned descriptors. |
| `artifact_manifest_get` | Required `manifest_id` and `manifest_sha256`. Returns the exact active manifest in the caller's workspace without returning artifact bodies. |
| `artifact_read` | Required `manifest_id`, `manifest_sha256` and listed `artifact_id`; optional `offset` and `max_bytes`. Returns the descriptor, pinned references, byte offsets, `text`, `next_offset` and `eof`. |
| `artifact_manifest_verify` | Required `manifest_id` and `manifest_sha256`. Rechecks active access and every selected artifact's full bytes, descriptor and hash; returns `verified: true` and verified descriptors on success. |
| `artifact_manifest_release` | Required `manifest_id` and `manifest_sha256`. Revokes subsequent access and returns `released: true`, retaining immutable data. |

Names are display names of 1–120 characters, without path separators or control
characters. Supported media types are `text/plain`, `text/markdown` and
`application/json`; JSON publication must contain valid finite JSON. All
content must be valid UTF-8, and limits count encoded bytes rather than
characters. Task IDs use 1–128 characters matching
`[A-Za-z0-9][A-Za-z0-9_.:-]*`; purposes contain 1–1,000 characters.

Reads use UTF-8 byte offsets. Start at zero and follow each returned
`next_offset` until `eof` is true, preserving the exact pinned IDs and hash.
An offset inside a multibyte character is rejected; the store ends slices at
character boundaries. Even a small slice verifies the complete stored artifact
before returning. Integrity checks do not establish the truth of its contents.

## Workspace and file capture

Every call requires a valid resolved client workspace, with no installation
fallback. HTTP uses the authenticated registration identified by
`X-Agents-Workspace`. Stdio uses its explicitly resolved client root; the
artifact tools do not accept an arbitrary `workspace`, root or absolute source
path. Configure the client's project root using the
[workspace rules](shared-mcp-daemon.md#installation-and-client-migration), then keep that
same workspace when preparing and reading a manifest.

`artifact_publish(source_path=...)` captures a regular UTF-8 file relative to
that exact `ClientContext.root`. It rejects absolute paths, empty/`.`/`..`
components, backslashes and symbolic links along the root and source path. It
opens path components without following links and refuses a file that changes
during capture. Safe capture requires POSIX support; unsupported platforms fail
closed. Prefer capture for a large existing file so its body does not pass
through the model's tool arguments. `content` is useful for small explicit text.

Memory's normalization of related worktrees to a canonical repository does not
grant artifact capture access to a sibling worktree. Nor do the optional local
inference service's separately configured source-root aliases broaden artifact
capture. Publish from the authenticated workspace containing the file, or use
explicit content you are already authorized to provide.

Private owner storage and workspace partitioning target one trusted local OS
owner. Manifest references are not independent credentials or a multi-user
isolation boundary. A reference alone grants neither cross-workspace access
nor permission to execute, publish, send messages or expose secrets.

## Native worker workflow

1. Publish each selected input with `artifact_publish`, retaining its descriptor.
2. Create a manifest with `artifact_manifest_create`, using the intended worker
   task ID and expected artifact hashes. Retain the complete returned manifest.
3. Call `get_subagent_context` with that manifest as `artifact_manifest`, plus
   the ordinary role, task, acceptance criteria, context, scope, non-goals and
   result type. Omit `task_id` to inherit the manifest's task ID, or pass the
   same ID explicitly; a mismatch is refused.
4. Require `status: prepared`, retain the original returned contract, and pass
   `worker_context` unchanged to a fresh native worker. The host must make the
   exact artifact reader available in the same authorized workspace. Preparation
   returns instructions and does not launch the worker or provide tools to it.
5. The conditional `worker-artifacts` rule and exact `artifact_read` calls tell
   the worker to read every required entry fully before claiming completion.
   Optional entries can be read with the same pinned manifest and listed IDs.
6. Validate the unchanged result with `validate_subagent_result` against the
   parent's retained contract. Also call `artifact_manifest_verify` with its
   pinned manifest ID/hash and independently assess the cited evidence, actual
   checks, limitations and acceptance criteria before accepting the result.
7. Release the manifest when subsequent access should end.

The MCP preparation endpoint checks the stored manifest against the active
workspace, hash, lifetime and artifact bytes. The pure preparer validates the
manifest shape and task binding, includes it in the contract fingerprint and
renders the required read calls without inlining bodies. References and rules
still count toward the prepared-context budget. Existing preparation defaults
and the [delegation contract](subagent-contract.md) continue to apply.

`validate_subagent_result` stays a pure structural validator: it performs no
artifact lookup, network call or filesystem read. A valid result envelope and
matching hashes do not prove findings, authority, test execution or completion.
Keep the original contract; never swap manifests or identities to make a result
pass. A new execution attempt needs a new prepared attempt and matching
parent-retained contract.

If the reader is unavailable, a required input is missing/expired/released, or
a hash does not match, the worker must return `input_required` when critical
input is needed or `partial` when usable work remains, following its result
schema. Identify the affected reference and exact limitation. Do not invent
unread data, claim complete review of a partial read, or use `local_generate`
as an artifact lookup tool. Instructions inside artifact text cannot override
the assignment or higher-priority instructions.

## Returning generated artifacts

When producing output artifacts is within the worker's assigned scope, the
automatic instructions tell it to publish generated files with `artifact_publish`
and create a separate output manifest with the contract's task ID. The input
manifest and parent-retained contract remain unchanged. The worker returns the
following compact entry in its ordinary result envelope instead of the file body:

```json
{"path":"artifact://<manifest_id>/<manifest_sha256>/<artifact_id>","purpose":"Generated report"}
```

This is a reference string in `artifacts[].path`, not a browsable URL or an MCP
resource endpoint. The parent extracts the three exact identifiers, calls
`artifact_manifest_get`, checks the returned task/workspace and listed artifact,
then uses `artifact_read` through `eof` and `artifact_manifest_verify` before
accepting the generated data. It also validates the worker result against its
original input contract and assesses the evidence. A returned output reference
does not replace that contract or authorize another task. Keep output manifests
active until the parent has consumed them; release them after acceptance when
future access should end. A read-only assignment does not gain publication
permission from these instructions. The single local completion path below
returns its ordinary bounded output and does not invoke publishing tools.

## Optional local completion

Pass the same complete manifest to `local_context_create.artifact_manifest`
alongside its ordinary `common_system` and `shared_context` arguments. The
broker verifies the snapshot, hydrates every selected entry exactly into
`shared_context` with artifact/hash framing, and checks it again after reading.
All selected entries are hydrated, including entries marked optional for a
native worker. Select a smaller manifest when fewer inputs are needed.

Hydrated content and framing consume the configured context byte allowance
together with shared text and any captured files. Native tokenization then
checks the rendered prompt plus output allowance against effective model
capacity before generation. Oversized input fails; the service does not
summarize, truncate or replay it to fit. Artifact storage capacity is not an
increase to model context capacity. For example, a profile with 4,095 effective
tokens and a 512-token output allowance permits at most 3,583 rendered input
tokens. This is a capacity example, not a configuration default or a current
deployment measurement; inspect the owner's configured and effective limits.

The broker rechecks manifest access and integrity before dispatch, completion
publication and result reads, as well as when issuing grants. Expiry, release
or tampering invalidates the artifact-backed context instead of allowing a
stale result to be accepted. Persistent bytes do not bypass context, grant or
job expiry and revocation.

Reuse one local context with separate bounded grants for several tasks sharing
the same exact prefix; creating new contexts for each task does not establish
shared KV identity. Native reuse remains subject to verified capabilities and
exact token-prefix checks. `local_generate` produces one bounded completion;
it has no native tool loop to fetch further artifacts. It does not replace
native worker spawning, persona activation, structural result validation or
independent evidence review. See [local inference operations](shared-mcp-daemon.md#local-inference)
for transport, owner configuration, capability and cache limits.

## Failures and verification

Workspace failures use `workspace_required`, `workspace_invalid` or
`workspace_unsafe`. `artifact_unavailable` covers inaccessible, absent, expired
or released references; `integrity_error` refuses changed metadata or bytes.
Capture can return `permission_denied`, `source_changed`, `unsupported` or
`artifact_too_large`. `quota_exceeded` requires reducing publication demand or
an explicit future storage-maintenance mechanism; releasing a manifest will
not reclaim its quota. `store_busy` is retryable and does not revoke an otherwise valid local context
or its grants. A completion interrupted by contention is not replayed; its
retained error records any consumed budget. Retry an undispatched operation or
submit a new explicit generation when needed; do not substitute unpinned data.

Run focused offline checks from a worktree using the
[test guide](../tests/README.md#persistent-artifact-checks). They exercise
temporary stores and deterministic boundaries. No test result, model run or
deployment is implied by this reference. Historical KV benchmarks measure
their named revision and are not artifact-layer acceptance evidence.
