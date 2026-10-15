# Redis live acceptance and controlled-restart evaluation

The subsequent [resident deployment report](resident-deployment-report.md)
records fresh real TCP checks and the worktree archive/recovery incident. The
historical results below were recorded before the archive; their ignored raw
artifacts are currently unavailable. Fresh storage and HTTP reports have their
own retained evidence and do not change these historical denominators.

Recorded on 2026-10-15 in the authorized `codex/shared-kv-mcp` worktree, following the [initial implementation report](implementation-report.md). Production source and defaults were unchanged during this follow-up. The new opt-in storage module is `tests/test_local_inference_redis_live.py`. Deployment, commits, push and PR submission remain outside this run.

## Environment and isolation

The run used Redis server 7.4.11 and redis-py 7.0.0, with the existing Python 3.13.13 interpreter. Only the client was installed into the private evidence directory; the live installation's environment was not modified. The official `redis:7.4.11-alpine` image was pulled at digest `sha256:858f009f9709ce576febc734aa78b8f6d624b82571f9ddb6bda4377c833b3499`. The release tag is recorded in the [official image manifest](https://raw.githubusercontent.com/docker-library/official-images/master/library/redis); the digest and actual server/client versions are retained in `runtime-public.json`.

The task-owned password-authenticated container published only `127.0.0.1:49343`. It used a read-only root, dropped capabilities, no host mounts, a 1-GiB container memory cap and Redis `maxmemory` 768 MiB with `noeviction`. Persistence was disabled, so a Redis server restart intentionally lost all cached state. Native inference used the prior exact Qwen3-8B GGUF, pinned llama.cpp build 11429 (`d81235049384534c167caea52b85a694f6103d14`), profile `7a15e3bd65494166a2534274d730e4852cfaebac63810d3bbb5dd0b052d56164`, and private port 18194. Full binary/model hashes and host details remain in the initial report. No model was downloaded.

Private evidence is under `.shared-kv-evidence/2026-10-15/redis-live/` in this worktree. Its credential file is mode 0600 inside a mode-0700 directory; it is not a public artifact. Generated text, capabilities, native request/SSE payloads and local configs remain private. Docker actions checked the exact owned container ID and task label; no existing service or unrelated container was changed. No Redis database was flushed.

## Real storage acceptance

The production `RedisSnapshotStore` constructed its own binary redis-py client; none of the Redis commands were replaced with fakes.

| Check | Actual result |
|---|---|
| Standard live module, final selection | 17 passed, 1 separately gated large case skipped, 8.53 s |
| Explicit greater-than-512-MiB case | 1 passed, 17 deselected, 9.62 s |
| Existing fake store/coordinator plus default live selection | 58 passed, 1 module skipped, 2.33 s |
| Default module import with Redis imports and network connections forbidden | Intended module-level skip; base interpreter had no redis-py |
| Dedicated Redis after scoped storage teardown | `DBSIZE` 0 |

These are 18 distinct live storage cases; overlapping runs are not added together. They cover binary manifest/chunk/token-sidecar roundtrip, multi-batch pipelines, actual manifest expiry and data grace TTL, six binary-damage cases, five manifest-damage cases, signing identity, authenticated incompatible profile refusal before native parsing, scoped replacement/deletion, and reconstructed-store orphan accounting followed by expiry/capacity recovery.

The large case saved a sparse 536,870,913-byte source through 128 chunks of 4,194,304 bytes plus one one-byte chunk. The payload's streamed SHA256 matched both the manifest and restored file. Pipeline batches were bounded to eight chunks. The test never concatenated or read the complete file into memory, removed source/destination files, and confirmed its namespace empty. This is a synthetic binary storage test; the native lifecycle below separately proves compatible model state.

Exact argv, return codes and stdout are retained in `storage-test-runs.json`, `final-storage-test-run.json`, `large-storage-test-run.json` and their referenced stdout files. The unchanged prepared worker result passed validation against the parent's original contract; the parent independently inspected source and evidence.

## Native lifecycle and faults

`lifecycle-report.json` records 11 actual native completion attempts, 11 valid frozen JSON answers, no replay, zero acceptance errors, and confirmed owned backend shutdown. All 11 lifecycle assertions passed:

- A cold completion published a real 91,138,612-byte native snapshot through bounded Redis chunks.
- An owned engine restart restored the live context from Redis and reused 586 of 606 input tokens, evaluating 20. The epoch changed, and native slot restore reported reading 91,138,612 bytes and restoring 618 cached tokens.
- Equal text in an independently created context stayed cold and used distinct Redis identity.
- A damaged native chunk and a tampered manifest HMAC were each refused before native restore was called. Each request completed once, cold, with the corresponding cache warning.
- An actually expired Redis manifest produced one cold completion.
- Stopping the owned Redis container preserved one valid cold answer with `snapshot_unavailable`; no completion was replayed.
- Restarting the persistence-disabled server initially ran cold, repopulated the cache, then restored successfully after another engine restart with 586 tokens reused.
- A broker restart rejected old grants before native dispatch; a newly created equal-text context stayed cold despite the old snapshot remaining in Redis.
- Releasing the new context removed only its own keys and preserved the old context's manifest.
- An actual native save under a 16-MiB file-size limit failed against the normal 91-MB snapshot. The already completed answer remained valid with a cache warning, no truncated snapshot was published, and the owned child was fenced without replay.

The expiry test waits for real key expiry; fault injection modifies only owned keys. Native request counts come from the diagnostic backend and are checked to increase by exactly one per generated answer. `lifecycle.py`, native slot-action records, per-job counters, fences and private raw artifact digests preserve the acceptance procedure.

## Controlled benchmark

The full benchmark uses the unchanged production benchmark driver with all three arms (`off`, `resident`, `redis`), SDK and authenticated production FastMCP in-process ASGI, five counterbalanced repetitions, and identical frozen tasks/checkers. The cap is 384 output tokens, temperature 0, seed 42 and per-job timeout 180 seconds. Every shared-context between-request transition performs the same confirmed owned engine restart in all arms. Independent-context negative controls remain independent. Initial cold calls and unsuccessful checker outcomes remain in the denominator. Full arm time includes startup, contexts/grants, native inference, snapshot save/upload/download/restore, checking, forced restarts, release cleanup and owned close. No repair calls are made.

All 30 arms were performed: 330 planned and actual native completions, 330 operational successes, 270 strict-checker successes, zero operational errors, zero cache warnings, and no repair calls. Every arm/transport completed 55 requests and passed 45 checkers over five repetitions. Each task had one identical output SHA256 across all 30 arms. The implementation task's JavaScript `Math.max` expression failed a literal `max` expectation, and the reviewer task's empty array failed the required object schema: 30 failures each, retained without changing prompts/checkers. The parent task passed. These measured checker limits are unchanged from the initial evaluation.

| Transport / arm | Whole-arm samples | Requests / checker passes | Whole-arm p50 / p95 s | Input / evaluated / reused tokens | Output tokens |
|---|---:|---:|---:|---:|---:|
| SDK / off | 5 | 55 / 45 | 31.94 / 33.16 | 28505 / 28505 / 0 | 2615 |
| SDK / resident | 5 | 55 / 45 | 30.87 / 33.09 | 28505 / 28505 / 0 | 2615 |
| SDK / Redis | 5 | 55 / 45 | 31.19 / 33.69 | 28505 / 13955 / 14550 | 2615 |
| MCP / off | 5 | 55 / 45 | 31.82 / 34.87 | 28505 / 28505 / 0 | 2615 |
| MCP / resident | 5 | 55 / 45 | 35.66 / 39.52 | 28505 / 28505 / 0 | 2615 |
| MCP / Redis | 5 | 55 / 45 | 43.74 / 46.70 | 28505 / 13955 / 14550 | 2615 |

p95 is the largest of five full-arm observations. Redis recorded 50 `restored_hit` requests and 60 cold requests across both transports; its observed input-token reuse was 51.04% (29,100 / 57,010). Off and resident recorded zero reuse under the same forced restarts. All 11 task IDs had matching rendered input/token hashes across all 30 arms, including the independent-context negative controls. The source tasks/common context hashes match the initial benchmark. See the [66 per-task result rows](redis-benchmark-results.md) for the smaller submit-to-terminal boundary.

The median complete SDK arm was slightly slower with Redis than resident under eviction (31.19 versus 30.87 s); MCP Redis was also slower (43.74 versus 35.66 s). Redis SDK was slightly faster than SDK off, while MCP Redis was slower than MCP off. This run demonstrates restored prefill reuse, but does not establish consistent workflow acceleration or a useful latency break-even. Keep Redis optional and disabled by default. The SDK/MCP difference cannot be attributed to transport alone: this was an unisolated host with substantial memory pressure, five repetitions and changing system activity.

Median sums of measured job spans per Redis arm show the actual snapshot costs. Each column is independently aggregated from five per-arm sums and should not be added as if all medians came from one run:

| Transport | Native save ms | Store/upload ms | Lookup ms | Download ms | Native restore ms |
|---|---:|---:|---:|---:|---:|
| SDK | 415.03 | 3739.94 | 17.54 | 1927.52 | 46.99 |
| MCP | 427.00 | 5534.34 | 16.80 | 2092.89 | 52.00 |

Store-reported namespace bytes were zero at the end of all ten Redis arms after scoped release cleanup. Aggregate network transfer bytes and spool peak were not instrumented and remain null; the exact payload sizes above must not be relabeled as measured wire traffic or peak disk use. Largest sampled owned engine RSS was 5,704,560 KiB; Metal/unified GPU allocations were not separately measured. System swap used was already 26,980.81 MiB before the run and 28,386.38 MiB afterward. Raw OS memory samples remain in the private report.

The preceding six-arm smoke completed 12 requests and passed all 12 short JSON checkers; each Redis transport restored once with 586 tokens reused. It is a separate one-observation smoke and is not pooled into these quantiles. This follow-up made 353 actual completion attempts: smoke 12, lifecycle 11, full benchmark 330. Together with the initial 250 attempts, the project has 603 recorded actual attempts; initial failed/cancelled attempts remain included. Synthetic storage tests are excluded from model-call counts.

Source report SHA256: `b62aa321d77d26f9325096414ca9fa159bbd31bb041e5e7809d5e6a7ff2e599d`. `full-summary.json` independently reconciles all 990 native request/response/preparation artifact hashes, equal per-task rendering, exact budget charges, cleanup, counters and the unchanged 45-file prior source manifest. The unchanged benchmark invocation is preserved in `benchmark.py`: `--repetitions 5 --max-output-tokens 384 --timeout-seconds 180 --force-eviction`, with all default arms/transports. The URL is supplied privately through the configured environment-variable name.

## Final checks and cleanup

The exact task-labeled Redis container was stopped and removed after the completed benchmark. Both loopback ports 49343 and 18194 had no listener, and all owned native backends were closed. No unrelated service or container was changed. `cleanup-report.json` records identity verification and final port checks; only the test image cache, isolated client directory and private evidence remain.

The final model-free focused gate passed 570 tests with 11 skips in 15.61 s, exit 0, using the base interpreter without the isolated redis-py directory or live-test selectors. It covered all local-inference modules, portable disabled behavior, daemon/readiness/workspace forwarding, annotations and selected installer/protocol instructions. The live module intentionally skipped in that default selection. Exact stdout is retained in `final-focused-suite.log`; these overlapping checks must not be added to a unique-test total. The earlier regular repository suite was not rerun in this follow-up because all 45 previously accepted source/dependency files were unchanged.

Static checks passed for 44 changed/new Python files using Python 3.11 syntax, six private harnesses and 19 Markdown files' local links/fences. The follow-up source manifest covers 46 source/dependency files and preserves the original 45-file manifest. All 66 changed/new public files were checked for the actual private Redis URL/password with no matches. `git diff --check` was clean. Exact static results are retained in `static-checks.json`.

Independent read-only review reported no actionable findings. It recomputed all six benchmark summaries and 66 table rows, checked recorded literal outputs/budgets/counters, rehashed 1,320 referenced benchmark artifacts, reconciled lifecycle evidence, and verified both source manifests. The reviewer did not rerun models, Redis, Docker or pytest. Its unchanged result passed structural validation against the parent's retained original prepared contract; evidence was also assessed independently by the parent. Original contracts, unchanged worker results and validation records are preserved in this evidence directory.

## Limits

This acceptance is specific to the recorded local Redis/client versions, model profile and host. TLS/`rediss`, other Redis versions, persistent Redis crash recovery, Linux native execution and Windows live execution were not exercised. The local OS owner, HMAC key and Redis process are within the documented trust boundary. Redis remains optional and disabled by default. No cloud invoice, electricity or general model-quality benefit is inferred from avoided prefill tokens.
