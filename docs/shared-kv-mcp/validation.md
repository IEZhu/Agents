# Validation and acceptance protocol

This file defines implementation acceptance. Results are recorded separately in [implementation-report.md](implementation-report.md), the follow-up [Redis live report](redis-live-report.md) and the [resident deployment report](resident-deployment-report.md); the existence of a test row is not evidence that it passed. Historical evidence remains separately identified in evidence.md.

## Deterministic acceptance matrix

Use pytest, fake clocks/events, temporary directories and injected transports. Tests cover observable behavior rather than private field layout. Suggested file names are new.

| ID | Test module | Required behavior |
|---|---|---|
| C01 | test_local_inference_config.py | Unset/disabled config starts no process, network, hashing or Redis; import works without optional extras |
| C02 | test_local_inference_config.py | Invalid owner types, ranges, unknown fields and paths yield optional-feature error without failing normal daemon readiness |
| C03 | test_local_inference_context.py | Forged actor label, wrong/missing capability and cross-workspace reuse are refused |
| C04 | test_local_inference_context.py | Same normalized repository workspace with two authorized worktree roots yields distinct captured context/cache identity |
| C05 | test_local_inference_context.py | Unconfigured source root, traversal, escaping/swap symlink, nonregular file including a FIFO with no writer rejected promptly without a blocked worker; oversized/invalid UTF-8 source rejected before model call |
| C06 | test_local_inference_context.py | Captured source change while queued or during generation invalidates publication and grants |
| C07 | test_local_inference_context.py | TTL, grant request cap and aggregate context/output budget never overspend; grant expiry during active inference suppresses output |
| C08 | test_local_inference_service.py | One active native transaction across multiple agents, including save/restore and process fencing |
| C09 | test_local_inference_service.py | Full FIFO queue returns busy without credit consumption; queued expiry/cancellation never dispatches |
| C10 | test_local_inference_service.py | Same ID/arguments/grant gives one job/call; conflicting or cross-grant reuse refused; submit/cancel loops hit the accepted-job cap; tombstones bounded |
| C11 | test_local_inference_service.py | Disconnected submit/poll does not duplicate or cancel work; explicit cancellation does |
| C12 | test_local_inference_service.py | Active timeout with continuing fake engine blocks next job until kill/reap; no unobserved background inference |
| C13 | test_local_inference_service.py | Revoke/release immediately prevents publication; queued requests rejected; active engine cleanup stays serialized |
| C14 | test_local_inference_service.py | Drain stops admission; resume reopens it; controller sees detached work; shutdown is bounded |
| C15 | test_local_inference_process.py | Port collision never kills foreign process; only owned child/key/spool removed; a closed owned listener's TIME_WAIT permits restart |
| C16 | test_local_inference_llama.py | Redirects, proxy env, malformed/trickled/oversized response, missing final SSE/counters fail safely; pinned partial/final slot sentinels are distinct; bounded startup health retries never dispatch a completion |
| C17 | test_local_inference_llama.py | Full rendered tokenization including BOS agrees with engine; boundary-token merges do not fake prefix matches |
| C18 | test_local_inference_cache.py | Cold, partial, resident hit and requested-but-zero-hit are distinct; counters sum correctly; hits cannot exceed LCP |
| C19 | test_local_inference_cache.py | Engine epoch, GGUF/template/KV profile, owner-config revision and source-root changes prevent incompatible hits; probe records are exact-profile bound |
| C20 | test_local_inference_cache.py | cache_mode=off bypasses all Redis I/O and native reuse, and clears broker residency assumptions |
| C21 | test_local_inference_redis.py | Atomic manifest-last publish, chunk/sidecar integrity/HMAC, TTL and capacity; token-state mapping covers stop/EOS/length and final-token evaluation |
| C22 | test_local_inference_redis.py | Missing/corrupt/incompatible chunks take cold path; partially failed native restore fences before next call |
| C23 | test_local_inference_redis.py | Snapshot save failure preserves successful completion and reports warning; no generation retry |
| C24 | test_local_inference_redis.py | >512 MiB stream splits without full-memory copy using sparse/fake streams; native child writes cannot exceed reserved spool/per-file cap |
| C25 | test_local_inference_redis.py | Namespace cleanup never touches unrelated keys; broker restart rejects old-context restore and counts orphans; equal independent contexts have separate keys/deletion |
| C26 | test_local_inference_mcp.py | All eight tool schemas, annotations, unknown-field rejection and Context forwarding match the public contract |
| C27 | test_local_inference_mcp.py | HTTP bearer/workspace errors, direct stdio refusal, disabled mode, long-job polling under bridge deadline |
| C28 | test_local_inference_mcp.py | Sentinel prompts, capabilities, credentials, snapshots and raw outputs never leak into debug/history/trace/error logs |
| C29 | test_local_inference_mcp.py | Existing persona/flow/history paths remain available while generation runs or optional backend fails |
| C30 | test_local_inference_service.py | Python API and MCP wrappers produce the same decisions for identical captured fake trajectories |
| C31 | test_local_inference_service.py | Proven pre-HTTP refusal refunds a reservation; ambiguous/attempted native dispatch retains its full charge |
| C32 | test_local_inference_service.py | Releasing unused contexts behind an active job does not grow an unbounded cleanup queue |
| C33 | test_local_inference_spool.py | Crash leftovers across engine directories cannot bypass spool admission; inactive owned runs are safely reclaimed and live/uncertain runs retained |
| C34 | test_local_inference_diagnostics.py | Capability reports keep missing counters unknown; benchmark checkers reject JSON float/bool substitutions for integer requirements |
| C35 | test_local_inference_service.py | Cancelling a file-I/O waiter retains admission until its physical thread completes, and detached reads remain visible to drain |

Add table entries if review finds a new behavior risk. Do not create 30 repetitive tests just to mirror these numbers: parameterized cases and meaningful concurrent scenarios are appropriate.

## Runnable commands and isolation

Before installing anything, verify an existing interpreter imports this worktree. The source audit confirmed the following interpreter is usable as of the recorded preparation, without loading a model:

```bash
PYTHONDONTWRITEBYTECODE=1 /Users/alexey.zhuchkov/Documents/Agents/.venv/bin/python -c 'import src; print(src.__file__)'
```

After new test files exist, a focused model-free command is:

```bash
LANGFUSE_TRACING_ENABLED=false PYTHONDONTWRITEBYTECODE=1 /Users/alexey.zhuchkov/Documents/Agents/.venv/bin/python -m pytest tests/test_local_inference_*.py tests/test_daemon.py tests/test_config_client_root.py tests/test_readiness.py tests/test_tool_annotations.py -q -p no:cacheprovider
```

The shell expands the glob; do not run this before the files exist and mistake a missing-file error for an implementation failure. If a worktree-local environment has been created, use its interpreter instead. Run from the new worktree. Follow tests/conftest.py isolation and tests/README.md; never symlink live data or copy credentials.

Run `node --test bridge/test.mjs` if bridge behavior or end-to-end bridge integration changes. Node 22+ is required by bridge/package.json. Do not launch the full repository suite or multiple model/embedding processes in parallel. The regular suite can load embeddings even when slow tests are excluded; choose focused checks first, then run the required broader regression once when ready.

## Live smoke gate

The real storage module `tests/test_local_inference_redis_live.py` is skipped before optional imports unless `AGENTS_RUN_REDIS_LIVE_TESTS=1`. Select a dedicated owner-controlled loopback Redis endpoint using `AGENTS_REDIS_LIVE_TEST_URL`; keep any credentials in the process environment, outside shell history and committed files. The interpreter must have the optional Redis extra available. No server is started by pytest, no database is flushed, and teardown deletes only random test-owned installation namespaces.

```bash
AGENTS_RUN_REDIS_LIVE_TESTS=1 PYTHONDONTWRITEBYTECODE=1 LANGFUSE_TRACING_ENABLED=false \
  .venv/bin/python -m pytest tests/test_local_inference_redis_live.py -q -p no:cacheprovider
```

The standard live selection runs 17 cases and skips the resource-intensive sparse-file case. To include the actual 536,870,913-byte save/load, also set `AGENTS_RUN_REDIS_LIVE_LARGE=1`. It streams through 129 chunks of at most 4 MiB, uses eight-command pipeline batches, compares streamed source/destination hashes, and removes its files and keys. Reserve sufficient Redis memory and private spool space before choosing that case. This synthetic binary payload proves real storage chunking; native model compatibility is a separate gate.

Live tests are opt-in. Record exact engine executable hash/build, GGUF hash/model architecture, effective context/KV settings, renderer/template identity, OS/hardware, feature configuration and time. Reuse an existing permitted local model; no silent downloads or cloud API calls.

Run the model only in a dedicated owned process on an unused port with private state. Never restart the shared installation to test a patch. Test a temporary daemon from the worktree with its own state, token and port, using fake retrieval if appropriate to avoid a second model load.

Minimum live sequence:
1. Cold request on dense model, with exact input/output/counter capture.
2. Different task with the same shared prefix and another grant; require positive observed reuse to mark resident capability supported.
3. Different context/profile or cache-off call; ensure no invalid hit.
4. Save/erase/restore and a related request; require positive native reuse to mark snapshot capability supported.
5. Engine restart with unchanged profile; resident state becomes cold, L2 restore is allowed only after validation.
6. Active cancellation followed by another job; verify owned process reaping and absence of overlapping work.
7. Redis outage/corrupt manifest; one cold fallback before generation, no duplicate completion.
8. Release and verify refusal of all old grants.

For an unsupported hybrid/recurrent profile, a negative result is expected evidence. Generation can still pass while caching remains unsupported. Record the reason rather than forcing support flags.

## Benchmark design

Three cache arms:
A. off: no resident reuse, no Redis.
B. resident: native resident reuse, no Redis.
C. Redis: same backend plus snapshot offload/restore.

Use exactly the same canonical tasks, common context, sampling settings, output caps, capability/budget checks and quality checker. Include initial cold calls in each workflow denominator. Counterbalance AB/BA and BC/CB order; record cold, warm and forced-eviction runs separately. Reset the controlled state between arms with our owned process/slot, not a global system cache flush.

For the first useful evaluation choose at least three task families: repeated questions on a fixed source snapshot; structured extraction from a long shared document; bounded implementation/reviewer/parent workflow. Include a no-shared-prefix negative control and a long-output control where decoding dominates. Start with a short smoke, then at least five repetitions per task/arm if resource budget allows; report actual denominators when fewer are possible. Do not change prompts after seeing answers in a scored run.

Use submit/result calls with long polling up to 20 seconds. Time from before context creation to receipt of terminal result, including source capture, cold model startup where applicable, submission, polling transport, unsuccessful attempts, validation, snapshot work and cleanup according to a declared boundary. Record model-only duration separately. Native first-token latency and client-visible final-result latency are different because MCP returns terminal text, not token streams.

For direct Python versus MCP overhead, call the same service methods in both paths with matching validated contexts. Any IR comparison is out of scope. Do not compare an unchecked Python script to a guarded MCP service.

Record:
- task success and checker failures; first-attempt and final success separately;
- total client-observed and service job durations; p50/p95 only with sample counts;
- first-token/model/prefill/queue/restore/save/store durations where available;
- logical input, newly evaluated input, reused input and output tokens;
- hit rate by requests and by tokens; startup/prime/setup calls explicitly;
- snapshot bytes transferred, spool peak, Redis namespace bytes;
- process RSS/system memory pressure and swap before and during the run;
- exact configuration, seeds/order, errors, cancelled/failed jobs and raw evidence digests.

## Reporting and acceptance

Functional pass requires the deterministic matrix and bounded live smoke for every capability claimed supported. A backend can be released with resident supported and Redis experimental/unsupported, provided documentation says so and no live result is invented.

A performance recommendation requires full task time to improve without a measured quality regression under the stated sample. Do not require a fabricated percentage to make the feature 'pass'. If Redis is slower, keep it optional/default-off and report the measured break-even. Local avoided tokens measure compute work; they are not a provider invoice or measured electricity savings.

Persist a compact report with one row per arm/task, exact denominators, failures and comparison boundaries. Keep private raw artifacts in an owner-selected durable directory, not a cleaned session scratchpad. Include hashes/provenance for reproducibility and paths only when they do not reveal secrets.
