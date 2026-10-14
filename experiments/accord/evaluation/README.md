# Repository task comparison

This experiment adds a focused subagent-testing section to the repository's test guide and compares JSON Accord, direct Python Accord and an ordinary Python SDK. Read the [result and language decision](../../../docs/accord-repository-evaluation.md) and the [declared protocol](protocol.md) before interpreting the measurements.

## Reproduce

Use Python 3.11+ and Git from this repository's root; no third-party package, paid model or network is required for the comparison. The local object database must contain source commit `3d95c42b79693d6a73d017d602f1b4262f3a300e`. This is a frozen task: current source edits do not silently change its input snapshot.

```sh
python3 -m unittest discover -s experiments/accord/tests -v
python3 -m experiments.accord.evaluation.run --output /tmp/accord-comparison
python3 -m experiments.accord.evaluation.semantic_probe
python3 -m experiments.accord.evaluation.metrics
```

`run` writes normalized observations and the accepted patch to the chosen directory, exits nonzero on a failed runtime expectation or unequal observations, and removes its disposable repositories/ledgers. The saved patch has zero context lines; applying it to the original document requires `git apply --unidiff-zero`. Those fixtures are ordinary test repositories, not managed worktrees containing user work. `semantic_probe` and `metrics` refresh their named files under `evaluation/results/`; run them in a writable task worktree. The semantic probe deliberately reports the known predicate blind spot, rather than asserting that its bad candidate should be accepted in production.

The recorded focused repository pytest run is shared input evidence, not re-executed by each trial. To refresh that evidence for another source revision, explicitly update `workload.BASE`, execute the documented tests against that revision, and regenerate the evidence/input digests. The experiment does not manufacture fresh test evidence from a previous result.

## Layout

- `clients.py`, `contract.json`: equivalent declarations and three-arm wiring.
- `python_sdk.py`: separately implemented single-task lifecycle; no Runtime/Contract dependency.
- `workload.py`, `fixtures/`: real source snapshots, recorded candidate and bounded predicates.
- `git_sink.py`: outcome commits published through a single local-ref CAS.
- `run.py`: 24 scenarios across three arms, including process termination and races.
- `semantic_probe.py`: known false prose accepted by all four predicates in each arm.
- `metrics.py`: source counts and hashes, with shared code accounted separately.
- `materialize.py`: historical one-shot host action used to project the accepted task into this worktree.
- `results/`: raw observations, semantic failure, source accounting, historical fault and final publication record.

`materialize` requires a linked `codex/` worktree whose HEAD is exactly the frozen base, an unchanged target preimage (or the exact accepted bytes) and an unused dedicated publication ref. It fails before mutation if the ref already exists. It is not a normal rerun step after committing the experiment. The saved publication commit/ref are local evidence; the portable patch and accepted document are committed files. A clone need not contain the private publication ref.

## Scope

The Git sink owns an append-only `refs/accord-eval/<namespace>/target`. Deduplication searches the same immutable ancestry used for CAS. A denied outcome advances that chain without changing document content; publication counts include confirmed document outcomes only. Rewinding/deleting the ref breaks the adapter's recovery assumptions. Hooks and signing are disabled, and tree construction uses a temporary index; HEAD and the user's index are preserved.

Python callbacks, registered predicates, storage and host authority are trusted. The comparison covers one recorded candidate and the SDK's single-task subset. It does not benchmark model quality, billing, performance, developer productivity, malicious-code containment or arbitrary prose correctness. The independently implemented SDK shares lower-level infrastructure and was written with access to the reference code. No compiler is implemented.
