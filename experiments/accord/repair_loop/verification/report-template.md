# Accord: stronger-model repair with bounded feedback

<!-- Fill measured outcomes only after the fixed pilot has completed. -->

{{LEAD}}

## Observed outcomes

{{TABLE}}

These are four executions per frontend on one narrow defect, not four distinct tasks. Seeds are paired and execution order is counterbalanced. Fresh worker identity strings differ between episodes; identical seeds do not produce an identical prompt or guarantee the same sampling trajectory. Frontend labels are not in model prompts. The live success-rate difference is descriptive, not causal evidence that the IR changes model ability.

{{EPISODES}}

{{CORRECTIONS}}

Full episode time starts before runtime/ledger creation and includes preparation of every worker contract, rendering/tokenization, every unsuccessful proposal, actual MCP validation, candidate checks, independent review, parent adoption and final episode artifact writing. There is no manual validation wait subtraction. Comparing failure latency with successful completion latency is not a speedup comparison.

{{TIMING}}

## Task, model and bounded loop

The workload and checker are unchanged from the [earlier shared-KV experiment](accord-shared-kv-evaluation.md): normalize raw surrogate-containing Python text to `AccordError("invalid contract JSON")` in `Contract.parse`, preserve the exact byte-limit diagnostic and existing valid input forms, and change only the frozen source span. A candidate must pass the same syntax gate and all 46 behavior checks before independent review and parent adoption. Generated full candidates and patches are saved beside their actual host reports. The baseline `experiments/accord/ir.py` remains unchanged for reproducibility.

The installed [Qwen3.8 27B](https://ollama.com/library/qwen3.8) Q4_K_M model replaces Qwen3-8B. The model GGUF SHA256 is `f5f1dd8920d417aac2718b0bda3403da274301efdd6760b4f0f4b864ff2ad57d`; the local model configuration identifies 27.3B parameters and the `qwen35` architecture. The dedicated llama.cpp build is `b11429-d81235049` on Apple M4 Max with 36 GiB RAM. Context is 16384, one slot, f16 KV, eight recurrent checkpoints at minimum step 512, temperature 0.2, output limit 1536, reasoning off. Full server settings, model template and executable source digests are recorded in `freeze.json` and the [protocol](../experiments/accord/repair_loop/protocol.md).

The parent has five request units: implementation receives three, reviewer one, parent retains one. Implementation stops at its first accepted repair or after three proposals. Corrections receive only the preceding output and actual diagnostics. Every execution receives a fresh prepared Agents-Core contract; the real loopback MCP server validates its raw output unchanged against the retained original contract. The decoder permits consistent completed/partial envelopes without selecting a repair or verdict. Here, independent review means a separately prepared worker and inference call using the same model and supplied implementation/host evidence. It is not a separate model, a new test suite or a blind assessment. A review rejection or non-completed stage is a failure. Neither code nor envelope fields are repaired by the host.

Compared with the previous 8B pilot, this changes the model, sampling parameters, output allowance, correction loop, status grammar, automatic MCP transport and model-specific cache configuration. An improved observed result therefore cannot be assigned solely to model size.

## Separating the IR from model variability

Accord uses `Contract.parse` and `Runtime`; the comparison uses native `JobSpec` and `PythonSDK`. Both share the same bounded Python host loop, model service, source context, checker, validation protocol, cache grants and acceptance conditions. `PythonSDK` here is the independent experimental lifecycle implementation in this repository, not an OpenAI provider SDK. It shares low-level hashing/storage helpers with Accord, but does not interpret the contract IR.

{{REPLAY}}

This control is an offline replay of retained results and original validation receipts. It creates no new model answers and performs no fresh MCP validation. It reruns real runtime acceptance and candidate checks. Its timings are not included in live model latencies or success denominators.

The correction state machine and reviewer flow remain Python host code in both arms; the IR does not encode them. The measured workload therefore offers no demonstrated justification for a separate language or compiler. A Python runtime with explicit contracts, conserved budgets, immutable result identity and checked handoffs covers the tested behavior. Portability and cross-language audit are possible reasons for an IR, but this pilot does not measure those benefits.

## Cache, environment and cost

{{CACHE}}

The machine remained in ordinary use. A system-wide snapshot during the first episode showed 39,971 MiB of swap used, and generation throughput varied during the run. There is no pre-inference memory baseline, so that snapshot is not attributed solely to the model. Small timing differences must not be interpreted as frontend overhead or a stable hardware benchmark. The run did not stop unrelated applications.

Local model inference made no provider API calls. Hardware, energy, editor-agent usage and developer effort were not priced. The reported request counts, token counts and elapsed times are measurements, not dollar-cost estimates.

## Verification and preserved failures

Before primary inference, 117 unittest methods passed: 69 existing Accord tests, 25 shared-cache/workload tests and 23 new transport/loop tests. Loop tests exercise both runtimes; success reports in those unit tests are explicitly simulated, while the primary pilot's behavior reports come from real generated candidates. Independent worker preparation/result/validation records are under `verification/`.

An initial infrastructure attempt completed eight failed episode records without invoking the model. Mandatory worker context required 13,344 characters, exceeding the configured 12,000. It is preserved under `results/preflight-context-rejected-20261014`, together with its original runner/protocol. Before any model answer, the limit was raised to the standard 16,000 and fresh `repair-r2-` task identities were used. These preparation failures are reported separately from model-quality observations; they have not been overwritten.

{{VERIFICATION}}

The [experiment README](../experiments/accord/repair_loop/README.md) documents live execution and offline replay. The [primary evidence directory](../experiments/accord/repair_loop/results/qwen38-27b-m4max-20261014/) contains original packages, prompts/token IDs, raw model responses, real MCP decisions, generated candidates, behavior reports, runtime snapshots and measured summaries. Captured validation receipts are trusted local evidence, not cryptographically authenticated attestations.
