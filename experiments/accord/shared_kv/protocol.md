# Resident shared-KV delegation pilot (frozen before scored inference)

## Question

Does a resident prefix cache shared by a parent and two delegated model workers help a real code repair, and is any benefit specific to Accord IR rather than an ordinary Python SDK using the same inference service?

## Workload and execution

One distinct repository defect: `Contract.parse` leaks `UnicodeEncodeError` before its JSON exception handler. Freeze baseline commit `7a22a1c9561950f85b40bf0c2e6a92b36959af82`, source and exact replacement span in `workload/workload.json`. Implementation returns a bounded replacement, review examines that candidate and independent host checks, and the parent model makes a final adoption decision. Three actual generations per episode, no scored retries or answer repair. Host planning and task creation precede the implementation call; there is no model planning call.

Every actor receives a fresh prepared Agents-Core package, unchanged worker_context, and supplies its own complete result envelope. A parent-side MCP bridge validates it against the retained original contract before any payload extraction or downstream use. JSON grammar constrains format and identity, not code or approve/reject answers. All roles have the same 1024-token cap. Invalid, incomplete or incorrect results remain failures. Review and adoption still execute with failure evidence when an implementation cannot be accepted, so failure is not hidden by early stopping. Invalid envelopes are never passed onward as valid child outputs.

Static AST admission allows only the fixed parse-input block; no arbitrary model Python is executable. A fresh isolated subprocess runs 46 frozen behavior cases against the materialized module. The baseline must fail exactly four surrogate cases, passing 42 others. No repair snippet is supplied. The original repository source stays frozen; each actual generated code edit, unified diff and behavior report is saved separately for review. Successful adoption requires valid completed envelopes, a changed candidate passing all 46 cases, reviewer approval and parent approval, bound to current runtime source/profile and child acceptances.

## Factorial comparison

Accord uses parsed JSON `Contract` declarations and `Runtime`. Python uses native `JobSpec` and the existing independently implemented `PythonSDK`. Both share the same transport, cache adapter, prompts, validators, ledger mechanism, scope and budget policy. Each root allocates one credit to each of two children and retains one credit for its own model decision; all three credits are spent on real generations. Backend and sharing labels never appear in model prompts.

Eight episodes, in fixed reverse-block order: Accord/shared, Python/shared, Accord/disabled, Python/disabled, Python/disabled, Accord/disabled, Python/shared, Accord/shared. Two repetitions per cell; one distinct task. This balances early/late positions but not every predecessor. Unique prepared-contract IDs/revisions necessarily differ across episodes. Record exact prompts and full outputs. Different generated candidates can change subsequent prompts and token counts; do not attribute every latency change solely to cache reuse.

## Cache boundary

Pinned local Qwen3-8B Q4_K_M GGUF and llama.cpp b11429, one dedicated server process and slot, context 16384, batch/ubatch 512, Metal, flash attention, f16 KV, greedy sampling, seed 42. `--cache-ram 0 --no-cache-idle-slots --no-context-shift`; no snapshot directory or disk KV operations. The existing slot retains KV in engine memory. Complete rendered text is sent on every request. This experiment does not claim token-ID transport savings.

The exact common token prefix is computed from full engine-rendered/tokenized prompts across all packages, then verified against every actual prompt. A process-local opaque handle binds namespace, profile/source identity and prefix. Trusted host identities receive explicit revocable grants only after task scope checks. The adapter serializes complete requests and rejects incompatible input before dispatch. It is a local host policy, not authenticated multitenancy. A new episode's first useful implementation call is cold and populates the slot. Subsequent review and parent calls reuse it only in shared cells. Disabled cells always set cache_prompt=false. No separate prime, persistence, restoration, response memoization, parallel sibling execution or cross-process KV transfer is claimed.

Observed `timings.cache_n` and `prompt_n` must sum to full input tokens; cold requests must reuse zero; no truncation. Record actual reuse including any larger shared prefix beyond the registered minimum. A requested hit is not an observed hit. Engine loss may yield a legitimate cold miss, never a fabricated saving. Stale/unauthorized inputs fail rather than using fallback.

## Measurements and integrity

Record streamed client TTFT/total, engine prefill/decode times, evaluated/reused/generated tokens, request bytes, cache admission/lock times, task budgets, exact raw output and MCP validation, materialization and quality. Whole episode wall time includes validation-bridge waiting; also report active time excluding that human/tool scheduling wait, and summed model time. Package preparation occurs before the run and is recorded separately (the first pilot package's preparation time was not captured). Model startup and two synthetic answer-42 calibration calls are separate. No outcome-directed prompt/schema/checker changes after scored inference begins. Freeze runner/cache/checker/protocol hashes in evidence.

Local inference incurs zero provider API charges. Hardware, energy and the controlling Codex session are not priced by this experiment. Evaluated-token savings are computational work counters, not a currency estimate. Resident KV capacity is not reduced by skipping prefill. Small sample size and greedy numerical differences preclude a significance or general capability claim.

The parent independently reruns offline cache controls and workload checks and reviews lifecycle controls. Every requested model completion remains in the evidence, including errors. A transport crash halts the pilot for diagnosis; no silent replacement with a successful run. No snapshot API is allowed by the transport route allowlist.
