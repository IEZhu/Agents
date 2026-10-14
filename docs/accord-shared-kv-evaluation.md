# Resident shared KV across delegated agents

Evaluation date: 2026-10-14. Baseline: `7a22a1c9561950f85b40bf0c2e6a92b36959af82`.

**Resident KV reuse worked for both Accord and ordinary Python. This pilot found no practical inference advantage attributable to Accord IR. It also failed to produce a successful code repair: all eight proposed implementations were rejected.** The result establishes cache feasibility and rejection behavior, not successful autonomous coding or general language equivalence.

## Implementation and actual task

The new [SharedKV adapter](../experiments/accord/shared_kv/cache.py) shares one resident llama.cpp slot among a parent and its two children. Opaque process-local handles bind an immutable engine/source profile, namespace and exact prefix token sequence. The host grants and revokes access explicitly; the adapter serializes full requests, bounds its handle registry, rejects mismatches and checks observed engine counters. It clears its residency assumption after dispatch failures. It neither stores answers nor saves/restores KV snapshots.

The host checks task scope and freshness before model dispatch, and rechecks accepted child dependencies before adoption. The adapter alone does not authenticate actors, attest a restarted server, detect source edits hidden by a dishonest host, or prove arbitrary caller-supplied text/ID equivalence. Its trusted renderer, tokenizer and exclusive slot ownership are part of the boundary. It provides serial reuse across logical agents, not simultaneous sibling execution, copy-on-write KV branches or cross-process tensor exchange.

The [real repository workload](../experiments/accord/shared_kv/workload/workload.json) targets `Contract.parse`: its initial UTF-8 encoding can leak `UnicodeEncodeError` before the JSON error handler. The bounded repair must normalize that error while preserving size limits, error precedence and valid inputs. The implementation child proposes the replacement source, the review child examines admitted candidate/host evidence, and the parent model decides whether to adopt. Host planning precedes these three real model calls; there is no model planning call.

Each model actor received a fresh Agents-Core package with unchanged worker_context and returned its own complete JSON envelope. The parent validated that result against its retained original contract before downstream use. Decoding grammar constrained shape and identity, leaving the source replacement, verdict and rationale to the model. This is grammar-assisted protocol execution, not a test of unrestricted JSON generation.

Only the frozen parse-input span may change. The AST materializer rejects arbitrary code before execution. Admitted candidates would run in an isolated subprocess against 46 frozen checks, including UTF-8/16/32 input, exact byte limits, duplicate keys, nonfinite numbers and surrogate handling. The unchanged baseline passes 42 checks and fails exactly four intended surrogate cases. No repaired snippet was supplied. The original repository source stays unchanged so earlier experiments remain reproducible.

## Fair comparison and environment

The [frozen protocol](../experiments/accord/shared_kv/protocol.md) crosses two factors:

- Accord: JSON `Contract` declarations interpreted by `Runtime`.
- Python: native `JobSpec` configuration and the existing independently implemented `PythonSDK`, with no contract parser/interpreter in its lifecycle.
- Either backend: the same shared adapter enabled or disabled, same source, prompts, checks, transport and three-request budget.

The Python baseline is the repository's ordinary Python lifecycle implementation, not the OpenAI client library. It shares low-level ledger/hash utilities and equivalent application policies with Accord. This compares declarative IR against native Python configuration under matched safeguards, not against an intentionally unprotected script. The job graph, cache grants, child handoff and dynamic review evidence still require shared Python host code; IR 0.1 does not express those relationships by itself.

Eight episodes ran in reverse-block order: Accord/shared, Python/shared, Accord/disabled, Python/disabled, Python/disabled, Accord/disabled, Python/shared, Accord/shared. There are two repetitions per cell and **one distinct task**. The first useful implementation call is cold in every episode; review and parent calls reuse its prefix only in shared cells. All 24 calls consumed actual request credits. No scored retries, primes or answer repairs occurred.

Hardware was the local Apple M4 Max system with 36 GiB unified memory, macOS 26.5.1 and Python 3.13.13. Inference used the existing Qwen3-8B Q4_K_M GGUF, SHA256 `d98cdcbd03e17ce47681435b5150e34c1417f50b5c0019dd560e4882c5745785`, verified against its bytes. Engine build: llama.cpp `b11429-d81235049`, commit `d81235049384534c167caea52b85a694f6103d14`.

The dedicated server used port 18188, one slot, context 16384, GPU layers 99, flash attention, batch/ubatch 512, eight CPU threads, f16 K/V defaults, `--cache-ram 0 --no-cache-idle-slots --no-context-shift --reasoning off`, and no slot-save path. Per-call settings were temperature 0, seed 42, top-k 0, top-p 1, min-p 0, repeat penalty 1 and a 1024-token output cap. Full rendered **text** was transmitted on every call. The pinned [native server documentation](https://github.com/ggml-org/llama.cpp/blob/d81235049384534c167caea52b85a694f6103d14/tools/server/README.md) describes the prefix-reuse interface; the observed `cache_n` counters, rather than a requested-cache flag, establish actual reuse here.

## Observed speed and work

Each latency below is the median of two episodes, summing the three model calls. These are **model-call durations, not full orchestration latency**.

| Lifecycle | KV disabled | Shared resident KV | Observed reduction | Accepted repairs |
|---|---:|---:|---:|---:|
| Accord IR | 87.62 s | 65.38 s | 25.4% | 0/4 |
| Python SDK | 94.77 s | 65.78 s | 30.6% | 0/4 |

The two shared-cell medians differ by 0.40 seconds, approximately 0.6%. This is not evidence of an IR speed advantage: identities and generated outputs differed, only two repetitions were run, and timing varied more substantially within each cell. Backend names were absent from prompts. The reverse order balances early/late position but does not completely counterbalance predecessors or eliminate machine-load effects.

| Lifecycle / mode | Input tokens, two episodes | Actually reused | Newly evaluated | Generated |
|---|---:|---:|---:|---:|
| Accord / shared | 28,609 | 13,659 | 14,950 | 2,122 |
| Python / shared | 28,319 | 13,658 | 14,661 | 2,292 |
| Accord / disabled | 28,601 | 0 | 28,601 | 2,082 |
| Python / disabled | 28,329 | 0 | 28,329 | 2,124 |

The registered common prefix was 3308 tokens. All eight subsequent shared calls hit: reviewers reused 3308 tokens; parent calls reused 3521–3522 where their full prompts shared a longer prefix. Every cold call reused zero. The verifier checks that observed reuse never extends beyond the exact common prefix of the preceding prompt/output token sequence and the new prompt, that reused plus evaluated equals full input, and that no truncation occurred.

Across shared episodes, **27,317 of 56,928 logical input tokens were reused: 48.0%**, including the first useful cold request in the denominator. This is direct evidence of avoided prefill work. Request payloads remained approximately 150–151 kB per two-episode cell: resident KV did not remove text transmission or output generation. Median client first-token latency across all six calls per cell was 5.15 s/4.31 s with sharing versus 15.33 s/15.40 s disabled for Accord/Python respectively. Those medians include each episode's initial cold call.

Whole orchestration medians were 126.09 s (Accord/shared), 112.46 s (Python/shared), 103.50 s (Accord/disabled), and 106.55 s (Python/disabled). **This interactive implementation did not demonstrate a full-wall-clock speedup.** Across all eight episodes, wall time was 897.20 s, model time 627.11 s and validation-bridge waiting 268.59 s. The bridge waiting includes 12.97 s measured inside actual MCP validation calls and substantial scheduling/tool-turn delay. Remaining active host work was approximately 1.50 s total. Local cache admission itself had cell medians of 0.22–0.48 ms.

Setup is separate: the server log reports model loaded at 4.857 s; two synthetic answer-42 calibration calls took 3.131 s together. Preparation of 23 packages took 3.392 s; the first package's preparation time was not captured. Model-file verification/render preparation and the controlling session's total effort were not fully timed. No new model weights were downloaded.

## Quality and protocol failures

There were **23 structurally valid primary envelopes out of 24**, but **zero admissible source replacements and zero accepted repairs out of eight**. Five proposals lacked the required final newline; three failed the single-block boundary. Inspection of every raw implementation response also confirms it starts with the unchanged defective input block: UTF-8 encoding still sits outside the exception handler. The model's claims of having fixed it were unsupported.

No rejected source was executed. Consequently the 46 behavior checks establish the baseline defect; they are **not 46 passing tests on generated fixes**. There were no materialized candidate modules to rerun. The review/adoption calls processed admission-failure evidence, not successful repaired code; all returned `reject`. One reviewer additionally claimed `completed` with nonempty `remaining_work`, failed MCP validation, and was withheld from the parent's validated child results. Both lifecycle implementations blocked adoption.

After the scored pilot, that reviewer received its actual diagnostics under a fresh attempt. The second attempt repeated the structural error. A third fresh attempt used a decoder that enforced the relation between completion status and remaining work, without choosing the verdict. It returned a structurally valid **partial** result. These two extra real model calls are retained under [protocol-correction](../experiments/accord/shared_kv/verification/protocol-correction/) and [protocol-correction-3](../experiments/accord/shared_kv/verification/protocol-correction-3/); neither replaces primary evidence, completes a repair, or enters the timing comparison. Including calibration, there were 28 local completion calls.

Thus the cache helped compute an unsuccessful workflow more cheaply. Quality improvement, successful review of a repair and successful parent adoption remain unestablished. Cache-dependent floating-point behavior and differing contract identities can change outputs even with greedy sampling, so this pilot also cannot establish quality preservation in general.

## Practical conclusion and evidence

Keep the resident-cache optimization as a **Python inference-service component** usable by both executors. Keep Accord IR useful for serializable task declarations, inspection, budget/scope constraints and bound validation receipts; this experiment supplies no reason to add engine-specific KV tensors to the portable IR or build a separate compiler. The measured compute benefit came from the same engine mechanism on both sides. The validation/rejection benefit was also available to native Python.

The local model endpoint incurred zero provider API charges. Electricity, hardware depreciation, GPU memory residency and the controlling Codex session were not priced. Avoided evaluated tokens are work counters, not measured monetary savings or reduced KV capacity.

Evidence and reproduction are in the [experiment README](../experiments/accord/shared_kv/README.md), [frozen run](../experiments/accord/shared_kv/results/qwen3-8b-m4max-20261014/), [recomputed analysis](../experiments/accord/shared_kv/results/qwen3-8b-m4max-20261014/analysis.json), and [offline verifier](../experiments/accord/shared_kv/analyze.py). Exact prompts, raw token IDs, responses, decoder schemas, original packages, actual MCP validations, task budgets, errors and server trace are retained. No save/restore route was called. Local SQLite ledgers are ignored; readable task records are retained in each episode JSON. Source freeze hashes still match. The existing experimental suite passed 69 unittest methods and the new cache/workflow suite passed 25: **94 methods passed**. These include 33 patch-rejection controls, expected baseline failures, cancellation/source changes before dispatch, child-receipt isolation, cache revocation/profile mismatch and deterministic request serialization.

The follow-up needed to assess useful completed work is a successful delegated repair under a frozen retry budget or a more capable model, followed by more tasks and repetitions. It must preserve the same cache capability for Python. The present data do not justify claiming Accord superior, or claiming it can never offer other benefits.
