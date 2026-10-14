# Accord real-model inference evaluation

Measured on 2026-10-14. **Direct token-ID input and actual KV-state reuse both work on the tested model. The large performance gain comes from reusing the KV prefix, and ordinary text requests obtain it too.** These results support an optional prepared-input/cache layer in a Python runtime; they do not justify a separate programming language or compiler.

## Model, task and controls

The real model was **Qwen3 8B, Q4_K Medium**, already cached locally, GGUF SHA256 `d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785`. The official llama.cpp build was **b11429**, commit `d81235049384534c167caea52b85a694f6103d14`, on an **Apple M4 Max with 36 GiB unified memory, macOS 26.5.1**. The server requested Metal offload of all model layers, one slot, an 8192-token context, f16 KV, flash attention and 512-token batches. Global prompt caching, context shifting and reasoning were disabled. The exact command, template, effective settings and profile are in [setup.json](../experiments/accord/inference/results/qwen3-8b-m4max-20261014/setup.json).

The [frozen protocol](../experiments/accord/inference/protocol.md) predates scoring. Twelve questions use actual repository source at `7b81e3e8b748f7cfff0c6b712740ff313bd5f091`: four contract diagnostics, four state transitions and four bounded code-edit choices. The shared source context is 16,984 UTF-8 bytes. Complete rendered inputs contain **3,780–3,848 tokens**, with an exactly shared **3,752-token prefix**. Expected answers were independently checked against code before inference; they are excluded from model messages. This tests repository reasoning and strict output correctness, not end-to-end autonomous code editing.

The primary design crosses text/IDs with empty/restored KV, three repetitions per task: **144 scored requests**. All use the same native `/completion` endpoint and greedy settings (temperature 0, seed 42, 128-token cap), without schema-constrained decoding or answer repair. Model weights remain loaded in every arm; “cold” means empty **prompt KV**, not cold process startup. Three synthetic requests warm the engine before scoring; prefix priming and snapshot setup are recorded separately.

Full messages are rendered once by the model template. The engine's tokenizer uses `add_special=true, parse_special=true`; integer input is checked elementwise against that sequence. The pinned [completion/tokenizer source](https://github.com/ggml-org/llama.cpp/blob/d81235049384534c167caea52b85a694f6103d14/tools/server/server-context.cpp) and [native token handling](https://github.com/ggml-org/llama.cpp/blob/d81235049384534c167caea52b85a694f6103d14/tools/server/server-common.cpp) were inspected to verify those paths. Every recorded response echoes the same full prompt and accounts for its complete input length. This is actual integer dispatch to a real model, not tokenization followed by conversion back to text before dispatch.

For every warm request, the host clears the slot, restores the same saved state and sends the complete prompt. Every cold request clears the slot and disables reuse. All warm responses report `timings.cache_n=3752`; all cold responses report zero. `cache_n + prompt_n` equals the full token count for every response, with no truncation. Final `tokens_cached` is deliberately not used as the reuse counter. The native [server API documentation](https://github.com/ggml-org/llama.cpp/blob/d81235049384534c167caea52b85a694f6103d14/tools/server/README.md) documents integer prompts, prefix caching, snapshots and the numerical limits of cache-dependent batching.

## Latency

Medians over 36 requests per cell, milliseconds. Request TTFT includes request serialization/transport and ends at the first generated token. Operational timing additionally includes profile/artifact validation and, for warm requests, snapshot restoration. Test-only cache erasure is excluded from both cells. One-time setup is accounted separately below.

| Mode | Request TTFT | Operational TTFT | Operational full response | Strict correct |
|---|---:|---:|---:|---:|
| Text, empty cache | 15667.6 | 15667.6 | 16111.6 | 21/36 |
| Token IDs, empty cache | 15495.7 | 15501.6 | 15997.6 | 21/36 |
| Text, restored KV | 318.0 | 773.3 | 1032.1 | 21/36 |
| Token IDs, restored KV | 220.2 | 445.5 | 754.5 | 21/36 |

The median paired reduction in operational time to the first token for **text → text with KV** is **20.49×**. The comparison includes the restore overhead; it does not attribute this gain to token-ID transport. Full-response gains are smaller because generating the answer still costs time. All individual timings and ranges remain in the [raw trials](../experiments/accord/inference/results/qwen3-8b-m4max-20261014/trials.jsonl) and [summary](../experiments/accord/inference/results/qwen3-8b-m4max-20261014/summary.json).

The primary rotation balances positions but retains a cyclic immediate-predecessor pattern. Small text/ID differences can therefore be influenced by kernel/graph/buffer reuse. After noticing that limitation during the run, a separate [format-order protocol](../experiments/accord/inference/format-control-protocol.md) was frozen before its own calls. It uses 48 warm requests, both pair directions for every task, ABBA/BAAB orders and twelve separately recorded preconditioning calls. No primary records or questions were changed.

| Format control | Paired requests | Text/ID request TTFT ratio | Text minus ID TTFT | Text/ID operational full-response ratio |
|---|---:|---:|---:|---:|
| all | 24 | 1.129× | 27.12 ms | 1.006× |
| text_first | 12 | 1.072× | 17.05 ms | 0.964× |
| ids_first | 12 | 1.353× | 62.30 ms | 1.074× |

The extra control reduces the apparent warm full-response advantage from a primary paired ratio of 1.334× to **1.006×**: roughly 0.6%, with the direction reversing in one order subgroup. IDs save a median paired **27.12 ms to the first token** in this control, but a material full-response benefit is not established. The primary cold full-response ratio is likewise only 1.008×. This is why the substantial cache gain should not be credited to token-ID transport.

Ratios greater than one favor IDs; less than one favor text. These are descriptive local ratios, not statistical significance or a universal tokenizer-speed guarantee. The control's 12 preconditioning calls cost 12.06 seconds and are excluded from its steady-state table. See its [raw evidence and summary](../experiments/accord/inference/results/qwen3-8b-format-control-20261014/summary.json).

## Work, transport and cost

Medians per primary request. CPU seconds include warm restoration but exclude test-only erasure; they do not measure GPU energy.

| Mode | Evaluated input tokens | Reused input tokens | Generated tokens | HTTP request body bytes | Process CPU seconds |
|---|---:|---:|---:|---:|---:|
| Text, empty cache | 3,809 | 0 | 7 | 18,439 | 0.91 |
| Token IDs, empty cache | 3,809 | 0 | 7 | 17,661 | 0.89 |
| Text, restored KV | 57 | 3,752 | 7 | 18,438 | 0.56 |
| Token IDs, restored KV | 57 | 3,752 | 7 | 17,660 | 0.49 |

Both text and ID requests contain the **same logical prompt tokens**. Across 36 requests per representation, the cold arm evaluates 137,202 prompt tokens; the warm arm reuses 135,072 (98.45%) and evaluates 2,130, plus a one-time 3,752-token prime. Passing IDs does not itself reduce context length or model prefill. All primary calls generated 1,212 tokens in total, including terminal tokens recorded by the engine.

Rendering all 12 prompts took 66.09 ms; preparing their IDs via the local tokenizer endpoint took 110.15 ms. That setup is not hidden inside per-request timings. The HTTP byte comparison includes the complete JSON request and its settings; profile metadata remains host-side, so these are not sizes of a portable cross-agent envelope. The prepared artifact and complete source text are both retained.

The saved prefix occupies **553,315,860 bytes (527.68 MiB)**. Priming took 9.939 s, saving 536.20 ms and hashing the saved file 432.75 ms. Every warm restore is included in operational timing. This controlled disk handoff is deliberately more expensive than an already resident shared prefix; repeated file access also benefits from the operating system's filesystem cache. Snapshot bytes were not committed.

Aggregated measured service time for 36 requests of each format, including common rendering, applicable token preparation and warm prefix prime/save/hash:

| Input format | Empty KV total | Restored KV total including setup | Estimated cache break-even requests |
|---|---:|---:|---:|
| text | 579.05 s | 54.37 s | 0.74 |
| ids | 575.03 s | 42.81 s | 0.72 |

Break-even divides measured one-time incremental cache setup by the observed mean per-request operational saving. It is a local estimate for this long shared prefix; it excludes process/model startup common to both arms. It is not a production-load forecast. Peak sampled process RSS was 0.65 GiB; process RSS does not equal peak unified GPU memory.

**Measured provider inference charges: $0 in every arm**, because all inference ran locally. Electricity, energy consumption, hardware depreciation and the cost of the orchestration chat were not measured. Consequently this experiment establishes less repeated model work and lower local latency, not a quantified currency saving or hosted-API discount.

## Quality and exact identity

The scorer accepts one strict JSON object with the single `answer` key, exact value and exact type. Duplicate keys, extra prose/fields, wrong types and wrong values fail. No generated code is executed. Repetitions are timing samples: there are **12 distinct quality tasks**, not 144 independent examples.

| Primary paired contrast | Exact text | Exact generated IDs | Correct → wrong | Wrong → correct |
|---|---:|---:|---:|---:|
| text_cold → ids_cold | 36/36 | 36/36 | 0 | 0 |
| text_warm → ids_warm | 36/36 | 36/36 | 0 | 0 |
| text_cold → text_warm | 36/36 | 36/36 | 0 | 0 |
| ids_cold → ids_warm | 36/36 | 36/36 | 0 | 0 |

Per-task primary results below include all four cells and three repetitions. Incorrect answers remain in the evidence; neither text nor ID format automatically makes the model understand the source correctly.

| Task | Expected | Observed complete output(s) | Correct runs |
|---|---|---|---:|
| `closed_contract_extra_field` | `{"answer": "unknown or missing contract fields"}` | `{"answer":"unknown or missing contract fields"}` | 12/12 |
| `duplicate_key_diagnostic` | `{"answer": "duplicate_key"}` | `{"answer":"duplicate_key"}` | 12/12 |
| `contract_hash_excluded_field` | `{"answer": "contract_revision"}` | `{"answer": "contract_revision"}` | 12/12 |
| `completed_report_inconsistent` | `{"answer": "completed_incomplete"}` | `{"answer":"completed_incomplete"}` | 12/12 |
| `identical_resource_write_stales_task` | `{"answer": "stale input: spec"}` | `{"answer":"stale input: spec"}` | 12/12 |
| `child_transfer_and_attempt_replay` | `{"answer": 3}` | `{"answer": 3}` | 12/12 |
| `integer_validator_return` | `{"answer": false}` | `{"answer": true}` | 0/12 |
| `opaque_recovery_missing_receipt` | `{"answer": "outcome_unknown"}` | `{"answer": "confirmed"}` | 0/12 |
| `integer_type_guard_regression` | `{"answer": true}` | `{"answer": {"type": "number", "value": 1.0}}` | 0/12 |
| `digest_only_freshness_regression` | `{"answer": false}` | `{"answer": true}` | 0/12 |
| `utf8_guard_repair` | `{"answer": "A"}` | `{"answer": "A"}` | 12/12 |
| `receipt_coverage_repair` | `{"answer": "C"}` | `{"answer": "A"}` | 0/12 |

The additional format control has 14/24 correct text responses and 14/24 correct ID responses, with 24/24 identical paired output-token sequences. Observed equality on this small deterministic workload does not prove general quality equivalence. Different prompt/decode batching can alter floating-point logits even with greedy generation, as the engine documentation warns.

## Implication for Accord

Keep portable messages/source references as the authoritative task input. A matching backend may prepare the full token sequence once and attach its model/tokenizer/template/settings profile. A cache handle then refers to engine-owned state for an exact prefix. The optimization must retain a full-input fallback when the cached state is unavailable; the empty-cache cells demonstrate execution of that complete input.

The useful exchange object is an immutable input description plus optional prepared IDs and an engine cache reference. **A plain Python adapter already delivers the measured behavior.** The task-contract IR can refer to such artifacts later, but adding compiler syntax would not create the observed speedup. A cache reference is preferable to routinely transporting a roughly 528 MiB snapshot between agents sharing an engine. Cross-machine transfer economics and interoperability were not measured.

This is a trusted, same-process/same-profile snapshot experiment. The file digest is recorded at creation; per-request validation checks sidecar profile/prefix metadata rather than rehashing snapshot bytes. Six negative controls reject altered IDs, wrong model/template/source profiles and incompatible cache metadata before dispatch. They do not authenticate arbitrary swapped blobs or establish cross-model cache compatibility, tenancy isolation or a security boundary. A production backend needs those mechanisms separately.

The surrounding coding host's native subagent messages were not changed to a token-ID protocol. The experiment demonstrates real token/KV exchange at the local inference-engine boundary. It does not transfer hidden reasoning, portable model state or semantic intent by merely encoding text as integers.

## Evidence and reproduction

Use the [experiment README](../experiments/accord/inference/README.md) for pinned engine/model fingerprints and commands. [setup.json](../experiments/accord/inference/results/qwen3-8b-m4max-20261014/setup.json) records the pre-scoring plan, exact prompts/IDs, warmups, prime, snapshot and effective profile. The offline verifier recomputes every score, matrix position, token invariant and aggregate from raw responses. Independent design/workload/review exchanges are retained under [verification](../experiments/accord/inference/verification/); their protocol validation is structural, and their actual checks and limitations are recorded separately. The independent reviewer inspected source goldens, mocked paths and the first 17 real rows, while the parent verified the complete finished evidence. [Final evidence checks](../experiments/accord/inference/verification/final-evidence-checks.json) pass for all 144 primary and 48 control requests, plus 12 control preconditioning calls. The saved snapshot digest remained unchanged after all trials. The dedicated server was stopped and its port was confirmed closed.

`python3 -m unittest discover -s experiments/accord/tests -v` passed **69 tests**, including nine new inference boundary tests. The frozen scorer additionally passes 23 response controls and 12 expected-answer cases. No production code or task-contract semantics changed in this experiment.

This pilot covers one quantized model, one engine build, one device, one long common prefix and a bounded workload. The active desktop is not an isolated benchmark appliance. It provides a concrete feasibility test and a measured local performance result, not a claim of model-independent speed, language superiority or financial return.
