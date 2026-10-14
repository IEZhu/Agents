# Resident shared KV for delegated agents

This isolated experiment gives Accord `Runtime` and ordinary `PythonSDK` the same process-local prefix-cache adapter. The actual KV tensors remain resident in a dedicated llama.cpp slot. Python passes opaque authorization handles; it neither serializes tensors nor memoizes answers.

`cache.py` implements immutable profile/prefix binding, namespace isolation, explicit grants/revocation, bounded handle eviction, fork rejection, full-request serialization and observed engine-counter validation. One instance must exclusively own slot 0. The host authenticates actors, checks source/task freshness and renders/tokenizes complete prompts. Those trusted inputs are not credentials supplied by a model. The prototype does not provide parallel KV forks, multi-tenant isolation or cross-model portability.

`run.py` executes a real three-call workflow: implementation child, review child, parent adoption. It crosses Accord/Python with shared/disabled KV in eight ordered episodes. Each has fresh Agents-Core packages, a conserved three-request budget, the same generated-output schema, and the same independent code checks. See [the frozen protocol](protocol.md) and [the evaluation](../../../docs/accord-shared-kv-evaluation.md).

The workload freezes a real defect in `experiments/accord/ir.py` at commit `7a22a1c`: UTF-8 encoding can raise outside `Contract.parse`'s error handler. Only one narrow source span may change. The static materializer rejects arbitrary Python and executes admitted candidates in a separate constrained subprocess against 46 checks. The original source stays frozen to preserve prior experiment reproducibility. Generated candidate edits and their diffs are evidence, not an automatic production patch.

## Offline verification

From the repository root:

```sh
python3 -B -m unittest experiments.accord.shared_kv.test_cache experiments.accord.shared_kv.test_workflow -v
python3 -B -m experiments.accord.shared_kv.analyze experiments/accord/shared_kv/results/qwen3-8b-m4max-20261014
```

The analyzer checks frozen source hashes, exact inputs and raw responses, original contract identities, retained MCP validation receipts, observed cache counters and budgets. It reruns each materialized candidate's behavior tests. Offline replay verifies evidence consistency; it does not constitute a new model run or a fresh MCP structural validation.

## Repeating live inference

Start the pinned server with the options in the report, using a dedicated process and no snapshot path. Prepare a new package for every role/execution with `get_subagent_context`, preserving the same task semantics and unique attempt identity. The committed packages are evidence of the recorded executions and must not be reused as new delegations.

```sh
python3 -B -m experiments.accord.shared_kv.run --packages /path/to/fresh-packages.json --output /path/to/new-run
```

Package input has `packages` mapping `kv-e01-implement`, `kv-e01-review`, `kv-e01-adopt` through episode 08 to complete prepared packages, and `preparation_ms` mapping package IDs to measured preparation time. The orchestrating parent must retain each original contract outside the model exchange.

After each generated response, the runner writes `pending-validation.json` and pauses for at most 600 seconds. The parent calls `validate_subagent_result` with its original contract and the unchanged result, then atomically writes `validations/<call_id>.json` containing `call_id`, `raw_sha256`, `contract_revision`, the actual `validation` object and `mcp_validation_ms`. The runner checks the result binding and accepts only `valid: true`, then independently checks code/behavior. A local JSON file is a trusted orchestration channel, not a cryptographically authenticated MCP receipt. Do not fabricate a successful gate.

MCP scheduling waits, actual model time and active host work are reported separately. Transport failures retain partial evidence and stop the pilot. There is no scored retry loop. All HTTP routes are allowlisted; save/restore APIs are absent. Whole text is still sent for each completion even when KV is reused.

Two post-pilot protocol-correction calls are retained under `verification/`. They returned actual validator diagnostics under fresh attempt IDs. The final response was structurally valid but partial; neither call changes primary outcomes. `protocol_correction.py --consistent-status` additionally constrains the relation between status and remaining work, not the model decision.
