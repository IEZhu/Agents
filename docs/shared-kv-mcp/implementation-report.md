# Shared local inference implementation report

This is a historical checkpoint recorded before the persistent-artifact integration
PR. References to uncommitted code, the serving worktree and paused updates
describe that checkpoint, not the deployment state of a later merged revision.

The later [resident deployment report](resident-deployment-report.md) records a
worktree archive/recovery incident and fresh service acceptance. The original
results below were recorded before that incident. Their ignored raw artifacts
were not retained by the Git snapshot and are currently unavailable; recovered
source and fresh checks have separate provenance.

The optional local inference implementation is complete on `codex/shared-kv-mcp`, based at `0268879f`, within the supported scope below. Package defaults remain disabled; the later owner-authorized deployment enables resident reuse with production Redis off, as recorded in the [resident deployment report](resident-deployment-report.md). The implementation has not been committed, pushed or submitted as a PR. The reviewable working-tree diff and private evidence remain in the authorized worktree. Native resident/snapshot compatibility passed live acceptance. The subsequent [Redis live report](redis-live-report.md) records real-server storage, native fault acceptance and the controlled-restart evaluation; the original resident-only runs below retain their historical boundaries.

## Implemented milestones

| Milestone | Result |
|---|---|
| 0 Checkout/environment | Specified worktree and existing interpreter used; live installation inspected, never edited or switched. |
| 1 Configuration/registry | Strict owner JSON, bounded collections, explicit workspace/source-root identity, safe file capture, separate owner/access/child capabilities, expiry/revocation and aggregate budgets. |
| 2 FIFO scheduler | One transaction includes rendering, freshness, generation, snapshots and fencing; submit/result/cancel, reconnect, UUID deduplication, bounded retention/drain/shutdown. |
| 3 Native backend | Exact binary/GGUF hashing, pinned owned loopback process, private key/isolated environment, bounded HTTP/SSE, native counters, confirmed reap and native file-size enforcement. |
| 4 Resident cache | Exact token-prefix bounds, profile/epoch/context isolation, cold/hit/partial/miss accounting and cache-off. |
| 5 MCP/daemon | Eight strict production FastMCP tools, structured results/annotations, explicit HTTP workspace forwarding, direct stdio refusal and separate readiness/pending counts. |
| 6 Snapshots | Lazy optional Redis, bounded binary chunks/sidecars, signed manifest-last publish, TTL/capacity/orphan accounting, verified restore, best-effort save and crash-safe native spool leases. |
| 7 Docs/evaluation | Public references, explicit owner probe/benchmark and live native/lifecycle evidence; final measured rows below. |

Enabled native execution targets POSIX macOS/Linux; ordinary/default-disabled paths remain usable elsewhere. Windows was not live-tested. Native snapshot support and live Redis acceptance are separate capabilities.

The installed MCP run_flow refused the target outside its registered workspace. The requested local flow was read and executed directly; no client registration changed. Neither CodeGraph nor TokenSave was initialized in the target, so ordinary source inspection was used.

## Changed files

- New `src/local_inference/`: config, models, profile, context, backend, render, metrics, service, process, child_exec, llama, capabilities, redis_store, snapshots, tools and package initialization.
- Integration: `src/server.py`, `src/daemon/app.py`, `src/daemon/control.py`, `pyproject.toml`, `uv.lock`.
- Owner tools: `scripts/dev/local_inference_probe.py`, `local_inference_diagnostics.py`, `bench_local_inference.py`.
- Tests: `tests/test_local_inference_*.py`, shared fake backend, portable `test_optional_inference_disabled.py`, collection isolation and protocol/installer expectations.
- Public references: README, env.example, daemon/test/document maps and this handoff's architecture, API, Redis, validation, evidence and implementation checklist.
- Managed routing: source template first, regenerated CLAUDE.md section, server initialization instructions, persona APPLY_INSTRUCTION and memory reminder. Prior reminder preserved as `scripts/templates/legacy/memory-routing-v10.md`.

The private `source-sha256.json` manifest covers changed source/tests/dependency files. It is not a committed revision or a hash of this evolving report.

## Checks and independent review

| Check | Actual result |
|---|---|
| Baseline daemon/metadata | 12 passed, 1.63 s |
| Configuration/registry/profile | 109 passed, 0.77 s |
| Scheduler review fixes | 49 passed, 1.66 s; then 3 narrow checks, 0.54 s |
| Native HTTP/process boundary | 71 passed, 4.97 s, including actual tiny-child RLIMIT_FSIZE enforcement |
| Resident accounting | 17 passed, 0.51 s |
| Daemon integration | 12 passed, 1.47 s |
| Strict MCP/annotations/prepared-worker regressions | 68 passed, 1.76 s |
| Snapshot store/coordinator | 46 passed, 2.31 s; sparse 536,870,913-byte streaming fixture |
| Snapshot transaction integration | 45 passed, 1.83 s |
| Initial combined gate | 424 passed, 11.37 s |
| Owner diagnostics/checkers | 21 passed, 1.17 s |
| Process/spool/fake Redis recovery | 83 passed, 7.33 s |
| Combined focused review gate | 578 passed, 2 skipped, 34.20 s |
| Independent narrow gate | 32 passed, 0.69 s, before subsequent live-protocol fixes |
| Native partial-slot fix | 104 passed, 2.51 s |
| Socket/default-spool wiring regressions | 104 passed, 5.88 s |
| Startup-health race plus affected native/scheduler/cache/diagnostics | 165 passed, 8.21 s |
| Final current-source combined focused gate | 587 passed, 2 skipped, 14.75 s, exit 0 |
| Final independent critical-regression gate | 58 passed, 1.00 s |
| Existing stdio/HTTP bridge contracts | 15 passed, 1883.84 ms, exit 0 |
| Static/dependency checks | 43 Python files parse with Python 3.11 syntax; 17 changed Markdown files have valid local links/fences; offline lock resolves 110 packages; git diff --check clean |
| Regular repository suite | 7273 passed, 106 skipped, 19 deselected, 3228.17 s (53:48), exit 0 |

These overlapping counts must not be added into a count of unique tests. The regular suite used an already cached multilingual MiniLM, isolated FastEmbed cache, offline HF/Transformers settings, tracing off and no bytecode/pytest cache writes. Diagnostics were excluded while their worker finished them and checked separately. Fixes landed during the broad run; focused gates validate later source. The full suite was not rerun at an immutable final revision. Exact tail/environment: `regular-suite-tail.txt` and `focused-suite-tail.txt` in the private evidence directory.

Prepared author results were structurally validated and their actual source/evidence inspected. Independent review identified reservation refunds, bounded cleanup, crash-spool accounting and strict-checker defects, fixed with regressions. Final validated review completion is recorded below; structural validity alone does not prove correctness.

Live checks exposed native assumptions missed by synthetic fixtures:

- Partial SSE frames use `id_slot=-1`; only finals identify slot 0. Parser and fixtures now enforce that distinction with existing token/counter/epoch checks. See pinned [partial/final construction](https://raw.githubusercontent.com/ggml-org/llama.cpp/d81235049384534c167caea52b85a694f6103d14/tools/server/server-context.cpp) and [base sentinel](https://raw.githubusercontent.com/ggml-org/llama.cpp/d81235049384534c167caea52b85a694f6103d14/tools/server/server-task.h).
- The exclusive port probe uses SO_REUSEADDR to tolerate the old owned listener's TIME_WAIT. A real live-listener refusal test still passes; SO_REUSEPORT is never used.
- Exec acknowledgement can precede HTTP readiness. Only owned-startup health checks retry refused connections/connect timeout/503 under the total deadline and repeated child-liveness checks. Completions are never replayed.

## Live host and exact profile

The initial runs below used the existing GGUF/binary on private port 18194. No model/package download, cloud-model call, installation, shared-daemon restart or foreign-process termination occurred in that phase. The later Redis follow-up pulled one isolated test server image and installed its optional client only into the private worktree evidence directory; see the separate live report.

| Field | Observed value |
|---|---|
| Host | Apple M4 Max; 38,654,705,664 bytes RAM (36 GiB) |
| OS | macOS 26.5.1, build 25F80 |
| Initial swap | 25,600 MiB total; 24,896.25 MiB used; 703.75 MiB free |
| GGUF | Version 3; architecture qwen3; Qwen3 8B Awq Compatible Instruct; file-type enum 15; 399 tensors |
| GGUF SHA256 | `d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785` |
| Executable SHA256 | `1bf5602a2be50097c44282a6ecc63234d94ee8f96d39fd45fed6eff4e74b6b41` |
| Engine | Build 11429, commit `d81235049384534c167caea52b85a694f6103d14` |
| Stable profile | `7a15e3bd65494166a2534274d730e4852cfaebac63810d3bbb5dd0b052d56164` |
| Template identity | `ed5c740fa03b0d4a14e773650b594a58e45ec652d52818ba0d4f41639484c465` |
| Tokenizer identity | `79842dce9fbe31be6b21f8a46d02004dcba8424c6a3947e9affdc050df02884e` |
| Renderer | agents-core/messages/1; whole native template/tokenization with special-token parsing |
| Settings | One slot; context 4096, one-token headroom; K/V f16; flash attention on; GPU layers 99; threads/batch threads 4; reasoning/speculation/context shift off |
| Native caches | Automatic RAM/idle snapshots and cache-reuse relocation disabled; explicit broker reuse only |

Private `live-host-before.json`, `gguf-metadata.json`, `observed-native-argv.json` and probe profile preserve these observations. The argv came from the exact benchmark-owned process; key/spool paths are redacted. Benchmark times include RSS/swap instrumentation. High pre-existing swap is a condition of this single-host run, not a measured cache-induced memory change.

## Native capability and lifecycle acceptance

`probe-report-2.json` passed with nine actual completion attempts, no errors and confirmed owned close:

| Case | Observed acceptance |
|---|---|
| Cold then related task | Cold 544 evaluated / 0 reused; related task 545 input = 11 evaluated + 534 reused |
| EOS snapshot | 545 saved/restored and reused on continuation; original input 544 |
| Stop-word snapshot | 548 saved/restored and reused; original input 547 |
| Length-limit snapshot | 557 saved/restored and reused; final sampled token correctly excluded |
| Incomplete UTF-8 | Incomplete authoritative output IDs; snapshot refused |
| Native file limit | Production wrapper stopped a synthetic writer at exactly 4096 bytes |

Native snapshots were 80,373,156 / 80,815,572 / 82,142,820 bytes. Payload SHA256, slot replies, exact token/input hashes and private raw request/SSE evidence remain local. This proves native snapshot compatibility for this profile, independently of Redis. Signed capability publication occurred after confirmed close.

`lifecycle-report-4.json` passed in 7954.02 ms with three model attempts. Cancellation followed an observed native token; the old child was reaped before the next job. The next job had a new epoch, unchanged profile and zero reuse. Duplicate submit kept one job; cache-off reused zero; revoked and released grants were refused. Both recorded fences confirmed no live child afterward; final close succeeded. The ambiguous cancelled dispatch retained its full 128-token reservation.

Failed attempts are preserved: first probe one native attempt before the slot mismatch; lifecycle attempts 1/2/3 made 0/0/1 attempts while identifying port/startup races. Missing counters remain unknown. The service did not automatically retry failed generations.

The short SDK/MCP smoke passed eight strict JSON checkers from eight requests. Each transport ran identical off/resident tasks. Resident reused 586 of 1212 total input tokens per transport; off reused zero. All engines closed. Both Redis arms were explicitly unperformed (redis_disabled). One sample per arm is not a performance recommendation.

## Full benchmark and final gates

The full run completed 220/220 requests operationally, with 180/220 strict-checker passes and no repair calls. Each row below contains five whole-arm observations and 55 requests, including the cold requests and all checker failures. Full arm time includes setup/startup and cleanup; p95 is the maximum of these five samples. Exact per-arm/task rows are in [benchmark-results.md](benchmark-results.md).

| Transport / arm | Checker | Whole-arm p50 / p95 s | Input / evaluated / reused | Output | Hit requests |
|---|---:|---:|---:|---:|---:|
| SDK / off | 45/55 | 29.15 / 43.25 | 28505 / 28505 / 0 | 2615 | 0/55 |
| SDK / resident | 45/55 | 23.76 / 40.37 | 28505 / 13955 / 14550 | 2615 | 25/55 |
| MCP / off | 45/55 | 30.25 / 40.37 | 28505 / 28505 / 0 | 2615 | 0/55 |
| MCP / resident | 45/55 | 21.08 / 23.05 | 28505 / 13955 / 14550 | 2615 | 25/55 |

Resident reuse avoided evaluating 51.04% of logical input tokens in each transport. All four combinations produced identical per-task output hashes and identical checker outcomes. Questions, extraction, independent-context and long-output controls passed all their checks. The implementation task returned Math.max where the literal expected max (the prompt did not fix a language); the reviewer returned an empty array instead of the required findings object. Both failed every repetition in every combination. These failures remain in the denominators and are not interpreted as successful code review. Parent acceptance passed.

Measured whole-arm medians were lower with resident reuse in this experiment. This is evidence for this fixed workload/profile on this host; five samples, high pre-existing swap, unisolated host activity and ASGI-only MCP do not establish general speed or transport overhead. No separate quality superiority is claimed. The largest measured engine ps RSS ranged from 5,698,496 to 5,702,624 KiB; GPU/unified allocations were not separately measured. Private native timings, per-request spans, budgets and 0.5-second memory samples remain in benchmark-report.json. Spool peak and Redis transfer counters are unmeasured, not zero.

The forced-eviction control completed eight requests and passed 8/8 checkers. The same owned restart was applied to both off/resident arms and both transports. All reuse was zero; resident resumed cold with a changed epoch. All owned processes closed. Its single observation per arm is kept separate in eviction-report.json. Ten full-run and two eviction Redis arms were explicitly unperformed (redis_disabled), with zero model calls; no cold run is relabeled as Redis.

Across the initial live reports there were 250 actual native completion attempts: probes 1+9, lifecycle attempts 0+0+1+3, short smoke 8, full benchmark 220 and eviction control 8. The failed and cancelled attempts are included. Deterministic fake calls and the synthetic file-limit writer are excluded. The separate Redis live report records subsequent attempts without changing these historical counts.

The final combined gate passed 587 cases with 2 skips in 14.75 s, exit 0. It included every local-inference module plus portable disabled, daemon, MCP, protocol, installer, workspace-root and readiness regressions under offline settings. Exact stdout is retained as final-focused-suite.log. Independent final current-source regressions passed 58 cases in 1.00 s. The exact prepared review result passed original-contract structural validation (valid: true), reported no remaining actionable findings and independently reconciled all 45 source hashes and the live report metrics. Its scope and unperformed checks are explicit. The unchanged envelope, original contracts and test stdout are retained as final-review-result.json, original-worker-contracts.json and independent-review-tests.log in the private evidence directory.

## Reproduction, boundaries and limitations

Private durable root: `.shared-kv-evidence/2026-10-15/` in this worktree. Owner configs are separate from tracked examples. Raw prompts/outputs/capabilities are not copied into tracked docs; inspect ignored evidence before publishing it.

Owner commands require absolute config/state/output paths; benchmark additionally requires absolute workspace. The probe used cap 32/deadline 180 s. The predeclared full run uses benchmark-config.json, cap 384, temperature 0, seed 42, deadline 180 s, five repetitions and both transports. The higher cap was selected before scoring to fit long outputs. Tasks/checkers were never revised after seeing answers.

The principal executed commands, from the authorized worktree root, were:

```bash
PYTHONDONTWRITEBYTECODE=1 LANGFUSE_TRACING_ENABLED=false \
  /Users/alexey.zhuchkov/Documents/Agents/.venv/bin/python scripts/dev/local_inference_probe.py \
  --config "$PWD/.shared-kv-evidence/2026-10-15/owner-config.json" \
  --state-dir "$PWD/.shared-kv-evidence/2026-10-15/owner-state" \
  --output "$PWD/.shared-kv-evidence/2026-10-15/probe-report-2.json" \
  --max-output-tokens 32 --timeout-seconds 180

PYTHONDONTWRITEBYTECODE=1 LANGFUSE_TRACING_ENABLED=false \
  /Users/alexey.zhuchkov/Documents/Agents/.venv/bin/python scripts/dev/bench_local_inference.py \
  --config "$PWD/.shared-kv-evidence/2026-10-15/benchmark-config.json" \
  --state-dir "$PWD/.shared-kv-evidence/2026-10-15/owner-state" \
  --output "$PWD/.shared-kv-evidence/2026-10-15/benchmark-report.json" \
  --workspace "$PWD" --repetitions 5 --max-output-tokens 384 --timeout-seconds 180
```

These output files were reported at the original checkpoint; their ignored raw artifacts are currently unavailable, as noted above. A script refusing to overwrite an existing destination does not establish that a historical report is still present. Select a new output filename for a rerun. The eviction control used the benchmark command with owner-config.json, --smoke --force-eviction and cap 32; its report was separate. The original SDK lifecycle references were the private lifecycle.py harness and lifecycle-report-4.json. See the [reconciliation report](reconciliation-report.md) for the later source comparison and its evidence limits.

The full arm boundary includes setup/model startup, contexts/grants, submit/poll/check, failed outcomes, release cleanup and owned close. Workflow clocks exclude one-time arm setup, so full arm time is primary. MCP is authenticated in-process ASGI with production strict FastMCP; sockets/stdio bridge/retrieval startup are excluded. SDK uses the same service and authorization. No repair calls occur; first/final success denominators are identical. The frozen literal JSON checker can reject semantically valid implementation synonyms. Unmeasured spool peak/Redis transfer bytes remain null.

Extras declare httpx >=0.28.1,<0.29 and redis-py >=7.0.0,<7.1. No package was installed during the initial implementation; redis-py 7.0.0 was subsequently installed only into the isolated Redis live evidence directory. Lock entries were checked against authoritative [redis metadata](https://pypi.org/pypi/redis/7.0.0/json) and [async-timeout metadata](https://pypi.org/pypi/async-timeout/5.0.1/json), including SHA256 and the Python <3.11.3 marker. Offline uv lock --check passed without downloads.

Real Redis was unperformed in the initial phase because no server/client had been selected. The requested follow-up subsequently ran actual storage, outage/corruption/restart, native restore and controlled-restart comparisons using a dedicated loopback container. See the [Redis live report](redis-live-report.md) for results, denominators and remaining measurement limits. No cloud invoice, electricity, broad-quality or cross-host performance claim is made.

Live installation retains its pre-existing dev branch and was not repaired/switched/deployed. Its updater expects its configured update branch (default main); that pre-existing choice is separate from this worktree. Publication/deployment requires a separate request; this flow leaves a local reviewable diff.
