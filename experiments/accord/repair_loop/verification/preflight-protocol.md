# Bounded repair comparison protocol

Frozen before the primary run. This is a small paired pilot on one repository defect, not an estimate across coding tasks.

## Question and task

Does Accord's contract IR improve observed successful completion or full execution time relative to the existing native Python SDK lifecycle on a stronger local model with bounded feedback? Reuse the exact source fingerprint, repair requirements, syntax gate and 46 behavior checks from `../shared_kv/workload/workload.json`. Do not patch the live `ir.py`, loosen tests, supply a golden repair or edit model output. Successful generated candidates stay in result artifacts.

Use installed Qwen3.8 27B Q4_K_M (GGUF SHA256 `f5f1dd8920d417aac2718b0bda3403da274301efdd6760b4f0f4b864ff2ad57d`) on the same M4 Max, with llama.cpp `b11429-d81235049`, dedicated loopback port 18189, one slot, context 16384, GPU layers 99, flash attention, batch/ubatch 512, eight CPU threads, f16 KV, no disk slot snapshots, cache RAM 0, no idle-slot cache, no context shifting, reasoning off. The hybrid model gets eight recurrent context checkpoints with minimum step 512. The larger model is a stronger candidate according to its publisher's coding positioning; this experiment measures its actual performance on the task.

## Fixed treatment and ordering

Four pairs, eight total episodes, no stopping based on early results. Ordered `(frontend, seed)`: Accord/101, Python/101, Python/202, Accord/202, Accord/303, Python/303, Python/404, Accord/404. The model does not see frontend labels. Fresh MCP identities vary by episode; paired seeds do not imply identical stochastic trajectories. Each episode uses a fresh runtime ledger, parent and two children, and a fresh cache handle. First model call is cold; later calls may reuse actual resident KV through the identical `SharedKV` service. Both frontends get caching. This experiment does not estimate a separate cache-on versus cache-off effect.

Temperature 0.2, top-p 1, top-k 0, min-p 0, repeat penalty 1, output allowance 1536 tokens per call. Same common source, model template, generic result schema, context preparation and checker in both arms. Accord uses `Contract.parse` and `Runtime`; Python uses native `JobSpec` and `PythonSDK`. Shared Python host glue implements retry, delegation and inference. No new IR operation is introduced.

## Bounded state machine

The parent receives five request units, transfers three to implementation and one to review, retaining one for adoption. The implementation child gets an initial attempt and at most two corrections. Each execution receives a freshly prepared MCP contract with the same stage task ID and a new attempt ID. Send `worker_context` unchanged with the source and task materials. Submit every raw result unchanged to the real MCP validator against the retained original contract before accepting any payload. No local substitute validator or manual polling gate is used.

For a failed attempt, give the implementation worker only its preceding output plus actual structural, syntax or behavior diagnostics. No host-generated repair hints or reference solution. A structurally invalid result can be correction material for that worker but cannot be adopted as a child result. On a changed candidate passing all 46 checks, stop implementation retries, accept its runtime-bound receipt and run an independent review child. Review has one request and may approve or reject. A validated approving review leads to one parent adoption request. Rejection, invalid/non-completed review, invalid/non-completed adoption, or exhausted implementation attempts is an episode failure. A partial status does not count as success.

The decoder enforces the supported completed/partial envelope consistency (completed means no remaining work; partial lists remaining work), without choosing code or verdict. This corrects the format ambiguity observed in the previous pilot. Other differences from that pilot include the stronger model, sampling parameters, correction loop, automatic MCP bridge, and model-specific cache settings. Do not attribute an old-versus-new success difference solely to model size.

## Outcomes and timing

Primary success is an accepted parent adoption following a fresh accepted changed implementation, all 46 behavior checks, and accepted independent approval. Count every started episode, including failures. Also report first-attempt fixes, implementation attempts and request totals. The existing checker materializes only statically admitted bounded code in an isolated temporary subprocess with its existing audit/resource restrictions.

Primary `full_elapsed_ms` is a host monotonic timer starting before per-episode directory/runtime initialization and ending after the final episode artifact is written. It includes MCP preparation, prompt rendering/tokenization, cold/warm model time, every failed attempt, structural validation, patch gates, behavior tests, review, adoption and artifact writes. No manual wait is subtracted. The batch driver additionally records its total wall time and one-time client initialization, model fingerprint verification, prefix calculation and baseline checks as setup. Server model loading is recorded separately from its log; report both per-episode service time and batch-plus-startup time. Do not charge startup selectively to one frontend. Network read timeouts are 240 seconds; the request count and output size are bounded, but a streaming response has no asserted hard end-to-end deadline.

Keep all raw responses, input prompts/token IDs, original prepared packages, actual MCP validations, host reports, generated patches, per-stage timings and runtime task snapshots. The summary reports successes/n, median full time, total full time and median model time for each frontend. Preserve unsuccessful and partial runs; do not replace a primary failure with an unreported rerun. Four pairs on one defect cannot establish population-level reliability or a small IR speed advantage.

## Cost and interpretation

Local inference incurs no provider API charge. Hardware, electricity, editor-agent usage and developer time are not priced. Token counts and elapsed time are directly measured. A tie or small timing difference supports no observed IR advantage for this workload; it does not prove that contract IR has no value for portability, audit or larger systems. Report those possible benefits as hypotheses unless measured separately.

## Sources

- [Ollama Qwen3.8 model page](https://ollama.com/library/qwen3.8), checked 2026-10-14: coding/agent positioning, 27B and controllable thinking.
- [llama.cpp server documentation](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md), checked 2026-10-14; actual flags verified against the pinned local binary's help and run log.
- Local model manifest, `/props`, GGUF digest and native server counters are the authority for the executed profile.
