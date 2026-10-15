# Ordered implementation plan

Execute these steps in order. The API and architecture are the contract; adjust this document if a necessary implementation change is discovered, explain why, and keep examples/tests consistent. Do not silently substitute a different feature.

## Step 0: establish the checkout and test environment

Read AGENTS.md, CLAUDE.md and this package. Confirm branch codex/shared-kv-mcp and the prepared base. The code is based on dev, not the research branch. Do not modify the live installation.

From the new worktree, the existing interpreter can be reused without editing its environment:

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/alexey.zhuchkov/Documents/Agents/.venv/bin/python -c 'import src; print(src.__file__)'
```

Expected module path is inside this worktree. Do not pip-install into the live installation's environment. Create a worktree-local virtual environment if new optional dependencies are needed. Never copy .env, client registrations or live data stores.

Read existing anchors before editing:
- Service lifecycle/health/auth/drain: src/daemon/app.py.
- Control drain counters: src/daemon/control.py.
- ClientContext and workspace resolution: src/daemon/workspaces.py.
- Main-checkout normalization: src/engine/config.py.
- Existing embedding executor: src/daemon/execution.py; keep it separate.
- MCP registration/annotations/strict worker argument rejection: src/server.py.
- Advertised schema compatibility: src/schemas/tool_args.py.
- HTTP bridge timeout: bridge/stdio.mjs.
- Fake runtime: tests/test_daemon.py; isolation: tests/conftest.py.

Exit: current paths and dependency availability recorded; no implementation claims yet.

## Step 1: strict models, configuration and pure registry

Create config/models/profile/context modules. Add the owner-config example's full field validation and disabled default. Define internal context/grant/job records and public schema version. Use secrets.token_urlsafe(32) or equivalent cryptographic randomness for capabilities, with digests retained internally.

Implement registry operations independently of MCP and engines. Use an injected clock for expiry and a deterministic token factory in tests only. Enforce byte/count limits before copying untrusted collections. Source-root IDs must resolve through owner configuration and bind to the existing ClientContext root. Separate actual source-root identity from normalized memory workspace identity.

Create a pure fake profile for tests. Do not hash a model or launch a child in unit tests. Implement request/output budget reservations and terminal result retention with explicit max_context_jobs and max_release_tombstones limits; repeat submit/cancel cannot evade admission limits. Decide exact error codes using api-contract.md, not ad hoc exception strings.

Exit: configuration, type-boundary, registry, source-boundary and budget tests pass. No runtime dependency on Redis or a model.

## Step 2: asynchronous job scheduler with a fake backend

Implement submit/result/cancel and a single FIFO worker. All accepted requests get a stable job_id and total deadline. A duplicated request_id cannot produce a second backend invocation. Result polling does not extend the deadline or alter budgets. A dropped polling connection does not cancel the job.

Use a fake backend with events/barriers to prove that active count never exceeds one. Test a fake engine that deliberately keeps running after its HTTP waiter is cancelled. The scheduler must keep the slot fenced until the fake process termination is confirmed. Test queued cancellation, revocation, expiry, source change after queue admission, full queue and shutdown with pending jobs.

Service progress must not depend on the default embedding executor. Integrate an explicit `begin_drain/resume/close` interface. Administrative drain prevents new submissions but lets accepted jobs finish; shutdown cancels/fences all remaining work with a bounded cleanup deadline.

Exit: tests demonstrate serialization and lifecycle behavior without wall-clock sleeps longer than short controlled waits and without any real child.

## Step 3: owned llama.cpp backend

Implement bounded async HTTP transport and process supervisor. Prefer httpx.AsyncClient with redirects disabled, environment proxies disabled and explicit connection/read/whole-operation timeouts. Build the endpoint from an owner-configured loopback port. The configured binary/model are local absolute paths, never tool arguments.

Keep process start idempotent under concurrent first requests. Startup failure leaves the optional service unavailable and regular MCP tools working. If the port is occupied, refuse it. Do not attach to or kill an existing model server.

Verify the selected binary supports the pinned native endpoints and flags. Use one slot, bounded context and output, explicit KV dtype, no context shifting, no automatic idle-slot snapshot behavior, and no hidden cache priming. Record the exact effective argv with secrets removed. Key-file location and snapshot spool are private service state. Add the POSIX child exec wrapper with enforced RLIMIT_FSIZE before enabling native snapshots; reserve spool capacity before writes as redis.md specifies.

Rendering/tokenization may use native helper endpoints. Verify that full-text completion input token counts match those derived by the service, including BOS/special-token behavior. An API assumption that fails this check is a backend compatibility failure, not a reason to change counters. Parse internal SSE incrementally to measure first output token and capture final native counters; return a bounded terminal result through MCP polling.

Add a local fake HTTP server test for malformed/oversized/trickled responses, redirects, missing counters, premature EOF, nonfinite metrics and stopped process. Keep snapshots disabled until Step 6.

Exit: backend protocol tests pass; process ownership and reaping are tested with a small fake executable, not a real model.

## Step 4: resident reuse and truthful accounting

Track stable profile, current process epoch, prior complete token state and resident context. Dispatch cold on first use, changed context, engine restart, uncertain previous state or cache_mode=off. Dispatch reuse only when the profile/context/epoch is eligible.

Check native counters for every completion. Reuse plus evaluated tokens must equal logical input. Verify no truncation, bounded generated tokens and reuse no greater than the actual common token prefix. A requested reuse with zero native hits is a miss. Unknown capability is not supported capability.

Add tests for two logical agents sharing a context, an unrelated context replacing residency, an explicitly revoked grant and identical text under a different source root or model profile. Existing historical SharedKV tests are ideas, not production integration tests.

Exit: deterministic tests distinguish cold, resident hit, partial hit, zero-hit miss and disabled. All error paths leave an explicit safe residency state.

## Step 5: MCP and daemon integration

Register the eight tools in api-contract.md, with exact annotation coverage:
local_inference_status, local_context_create, local_context_grant, local_context_revoke, local_generate, local_generation_result, local_generation_cancel, local_context_release.

Keep tools registered even when disabled so existing metadata tests remain deterministic. Disabled status is useful; other calls return disabled without loading heavy dependencies. Standalone stdio returns transport_unsupported for the new operations. Forward Context explicitly to workspace resolution.

FastMCP can discard unknown top-level fields before body validation. Extend the existing strict-dispatch pattern for these new mutation/request tools, with allowlists derived from their actual schema. Do not assume extra='forbid' inside a model catches data the framework already discarded. Keep advertised schemas compatible with clients that drop anyOf/$ref definitions; test tool JSON schemas as well as internal models.

Inject one LocalInferenceService from the shared daemon into the thin wrappers. Avoid creating a different module singleton per client. Add separate local inference readiness/pending fields; do not reuse embedding model_ready or inference_pending. Update Service drain/resume and Controller._drain to include accepted jobs and fencing. Close the service before a generic executor shutdown could wait forever.

Test the real ASGI wrapper with fake runtime/backend and temporary service state. Submit a long fake job, call status/persona-independent fake tools while it runs, disconnect a poll, reconnect, cancel, drain/resume and shut down.

Exit: all focused MCP/daemon/bridge contract tests pass, existing tools still work disabled, no production service restarted.

## Step 6: snapshot store and Redis

Implement NullSnapshotStore/fake store, the manifest/chunk protocol and native spool bridge exactly as redis.md. Add optional Redis dependency/import. First test read/write/expiry/missing chunks/corruption/HMAC/profile mismatch with a fake store. Then test actual Redis commands against a dedicated ephemeral test instance only when explicitly selected.

Complete snapshot operations inside the serialized engine transaction. Preserve a successful generated answer when best-effort storage fails. Never deserialize an unauthenticated or over-limit native state blob. Test a payload larger than 512 MiB using a streamed synthetic source and a fake backend; the normal unit suite must not allocate that much RAM.

Add an owner-run diagnostic command under scripts/dev/local_inference_probe.py. It must require explicit config and output destination, count its own model requests and save the private signed profile/result capability record described in architecture.md. It owns a separate process/unused port, performs explicit model calls, and proves saved-token mapping and the native write limit; it is not a read-only operation. It must never run during server import, normal status or test collection.

Exit: optional storage failure cannot break resident/cold generation; capacity/TTL/spool cleanup and engine fencing tests pass. Live snapshot compatibility remains a separately recorded capability.

## Step 7: docs, bounded live validation and report

Update public documentation only after code implements the contract: README MCP tool table, env.example, docs/shared-mcp-daemon.md, docs/README.md and tests/README.md. New optional inference changes the meaning of the initialization phrase 'server never samples an answer': qualify it narrowly so persona routing still returns instruction bundles and only explicit local generation tools run a model. Follow AGENTS.md's managed-template procedure if any shared routing instructions need changes; do not hand-edit only one restatement.

Create scripts/dev/bench_local_inference.py with the validation protocol. Add no automated long model run to the default suite. Use the existing dense local model for the first explicit live smoke if available. If missing, report live acceptance unperformed; do not download weights by surprise.

Compare direct Python calls and MCP calls through the same service; then cache off/resident/Redis under identical prompts. Report full client-observed time and success as well as model time. Do not mix failed and successful task times into a causal acceleration claim. Keep raw local prompts/results outside tracked docs unless deliberately sanitized; commit a compact report and hashes.

Exit: focused checks pass, live capabilities are supported/unsupported/unverified based on actual evidence, documentation matches implementation, and outstanding checks are explicit.

## Final implementation checklist

- [x] All default-disabled imports and tools are harmless and model-free.
- [x] No writes or branch changes in the live installation.
- [x] Explicit worktree source binding and freshness checks.
- [x] One real engine transaction at a time, including cancellation cleanup.
- [x] Job API stays within bridge deadlines and supports reconnect without duplicate dispatch.
- [x] No actor-label authorization; scoped capabilities and aggregate budgets enforced.
- [x] Cache hits come from native counters with token-prefix checks.
- [x] Engine restart and profile/source changes invalidate incompatible reuse.
- [x] Redis remains optional, bounded, authenticated and chunked.
- [x] Direct SDK and MCP use the same implementation.
- [x] Public docs and focused regressions are updated.
- [x] Real quality, speed and monetary conclusions do not exceed measurements.

The original handoff authorized preparation only. The user subsequently requested
execution of `flows/shared-kv-inference.md` in this worktree. Current milestones,
checks, live evidence and remaining acceptance are recorded in
[implementation-report.md](implementation-report.md).
