# Stronger-model bounded repair pilot

This experiment compares Accord IR with the existing native `PythonSDK` lifecycle on the exact repository repair used by the [resident shared-KV pilot](../shared_kv/README.md). It adds up to two corrections after the initial proposal and an automatic, real Agents-Core MCP preparation/validation client. Both frontends share the same inference and KV service. The complete [protocol](protocol.md) was fixed before model inference; the [evaluation report](../../../docs/accord-repair-loop-evaluation.md) records observed results and limitations.

The unchanged workload repairs raw-surrogate error normalization in `Contract.parse`, preserves the byte-size boundary and other behavior, and admits changes only to a bounded source span. A solution needs all 46 behavior checks plus independent review and parent adoption. Generated candidates are retained in result artifacts; the frozen baseline module is not updated by this experiment.

## Run and verify

Python 3.11 or newer, standard library only. The live runner additionally requires the installed local Agents-Core daemon and its configured Codex credentials, plus an exclusively owned pinned llama.cpp server. Credentials stay in memory and are never copied into results. Only preparation and validation MCP tools are available to this client.

From the repository worktree root:

```sh
python3 -B -m unittest experiments.accord.repair_loop.test_mcp_client experiments.accord.repair_loop.test_run experiments.accord.repair_loop.test_replay -v
python3 -B -m experiments.accord.repair_loop.analyze experiments/accord/repair_loop/results/qwen38-27b-m4max-20261014
python3 -B -m experiments.accord.repair_loop.replay experiments/accord/repair_loop/results/qwen38-27b-m4max-20261014
```

The analyzer verifies recorded source fingerprints, package/result identity, unchanged raw output, observed input/cache counters, retry order, request budgets, adoption outcomes and timing summaries. It reruns admitted candidate behavior tests. Stored MCP decisions are trusted captured receipts; offline replay is not a fresh MCP validation or a new model sample. The analyzer expects a complete primary pilot with inference artifacts; infrastructure-only preflight records have a separate diagnostic.

To repeat the primary experiment, start the already installed model with the exact profile in `protocol.md` and use a new output directory:

```sh
python3 -B -u -m experiments.accord.repair_loop.run --output /path/to/new-run
```

Prepare fresh task identities for a new experiment by changing the run prefix in `prepare`; do not reuse recorded packages as new delegations. The runner obtains a fresh original package for each stage and correction, sends its `worker_context` unchanged, validates raw returned JSON through MCP and applies independent semantic checks. Model workers have no tools. The host owns rendering, validation, candidate materialization and execution of the existing constrained checker.

`full_elapsed_ms` includes the whole per-episode workflow and its final artifact write. Setup and batch totals are retained separately. There are no manual MCP polling pauses. A five-request total budget is transferred as implementation=3, review=1 and parent=1; unused requests remain unspent. A new inference attempt always has a new attempt identity. Guard failures consume a runtime credit but do not falsely count as a model transport invocation.

## Files

- `run.py`: matched state machine, real model transport, prepared contracts, timing and artifact capture.
- `mcp_client.py`: bounded loopback HTTP MCP transport, private in-memory credentials and original-contract retention.
- `analyze.py`: offline consistency and behavior replay for complete inference captures. Infrastructure failures remain in the runner summaries but cannot pass this full-capture analyzer.
- `replay.py`: post hoc cross-frontend replay of identical captured answers. Verifies source hashes first, distinguishes partial snapshots and missing/unreplayable captures, and creates no new model samples or MCP validations.
- `test_run.py`: both runtimes with simulated model/MCP transport; explicitly simulated success reports and real failure checks.
- `test_mcp_client.py`: transport, credential suppression, raw output and original-contract tests.
- `test_replay.py`: changed-source, empty-snapshot and missing-capture negative controls.
- `results/`: complete primary evidence and the preserved initial context-budget preparation failure, which made zero model calls.
- `verification/`: independent worker contracts/results/validations, environment and setup diagnostics.

The shared Python host still implements the loop and delegation. The IR has not gained an interpreter operation for repair, reviewer control flow or KV handles. Observed equal outcomes therefore do not establish a need for a separate language; they test whether its existing contract representation has an immediate measured advantage in this workload.
