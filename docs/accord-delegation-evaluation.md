# Accord: delegated code change and IR interchange

Recorded 2026-10-14, frozen input revision `8ab433012fc16e68f95ca538dfbb4bf4d082a3d7`.

The IR has a demonstrated, bounded use as an inspectable exchange format: two lifecycle implementations consume the same contract bytes, and a separate process inspects them without importing the task program. The equally protected Python SDK produces the same runtime outcomes and can export the identical contract. This supports a shared data format and Python authoring API; it does not justify separate language syntax or a compiler.

## Real task and delegation

The repository's `bounded_object` rejected nested invalid JSON values at `$`, hiding their location. The task changes `src/engine/subagent_validation.py` to report paths such as `$.evidence[0].score`, adds `tests/test_subagent_paths.py`, and updates `docs/subagent-contract.md`. Existing accepted values, error codes and structural limits must remain intact.

Two actual implementation worker attempts used separate prepared Agents-Core contracts and structural result validation. The first candidate passed its 45 focused cases and the parent's original 19 behavior checks. Independent review nevertheless found error-code precedence regressions: an invalid Unicode string or oversized integer could mask a later array-limit error, and UTF-8 validation could mask the canonical character limit. The second attempt fixed the order and expanded the suite to 56 cases. The original artifact and exchange remain in [fixtures](../experiments/accord/delegation/fixtures/); this was a review-driven correction, not a blinded first-attempt success.

Parent verification ran the corrected module against 144 existing subagent/MCP cases plus the 56 new cases: **200 passed**, including after integration into this worktree. The independent behavior probe now has 22 checks: base 14/22, first candidate 19/22, corrected candidate 22/22. A syntactically valid mutation weakening object/array limits from 100 to 1000 passes the child's bundle-shape check but fails the parent's behavior check in every arm. These checks demonstrate selected predicates, not arbitrary code correctness.

## Four-arm control

| Arm | Declaration | Lifecycle |
|---|---|---|
| `accord_json` | JSON contract set | Accord `Contract` and `Runtime` |
| `accord_python` | Native Python dataclasses | Same Accord Runtime |
| `sdk_python` | Native Python `JobSpec` | Independently implemented PythonSDK |
| `sdk_json` | Same JSON bytes as `accord_json` | Independent decoder and PythonSDK |

The SDK was extended with child credit transfer, scope narrowing and ancestor cancellation before comparison; 15 regression tests cover its delegation behavior. It imports neither `Contract` nor `Runtime`, but shares storage and hashing primitives. Both implementations were accessible to the implementer, so this is implementation separation, not a blinded clean-room study.

All arms share frozen source snapshots, candidate bytes, validators, a three-file Git sink and explicit parent adoption code. A root receives three credits, transfers one to a non-publishing child, then spends one on adoption. Success leaves one root credit and zero child credits. Native workers performed the actual AI work outside the prototype; replay callbacks are not provider calls, token charges or a measurement of the worker's internal activity.

The experiment runs eleven scenarios across all four arms, plus success under a second configuration: **48/48 checks passed; all 12 paired groups have equal observations**. The scenarios cover success, child input/target/output expansion, excess allocation, ancestor cancellation, weakened code bounds, parent reuse of child receipts, changed accepted output, stale source and repeated publication. Every rejection produces zero publications; success publishes the exact three files once. The second configuration changes root budget from 3 to 5, destination and validator name/revision without modifying backend code. Its validator predicate remains the same, so this is configuration interchange rather than a new semantic workload.

[Raw comparison](../experiments/accord/delegation/results/comparison.json) records input/candidate/implementation hashes, credits, callbacks, acceptance state, paths and publication digests. Behavior subprocess results are cached by exact source and schema bytes across arms; the four recorded source executions cover base, initial candidate, final candidate and the mutant. Subprocess execution is not a security sandbox. [Protocol and amendments](../experiments/accord/delegation/protocol.md) distinguish the original plan from review-driven test additions.

## What belongs to the IR

The data consumer rejects unknown fields/versions, duplicate keys, Boolean budgets, unavailable validators and child scope expansion before execution. Formatting preserves normalized identity; changing budget changes it. A standalone inspector succeeds for both configurations without importing `Runtime` or `declarations.py`. Native Python `JobSpec` exports to the same normalized representation and produces the same inspection result.

This is practical interoperability inside a controlled local trust domain. It required a generic decoder/exporter/inspector, SDK delegation support and common publication/adoption adapters. It does not establish cross-language portability, authenticated cross-host receipts, reduced authoring effort or superiority over other schema formats. The benefit comes from the shared contract, and is available to a Python-first SDK.

IR 0.1 still has no edge meaning “adopt this child's accepted artifact.” `adopt_checked_child` explicitly checks parent identity, current child acceptance/attempt, source/profile freshness and output digest. The trusted caller serializes this handoff; it is not a general concurrent or atomic adoption primitive. The parent then performs its own checks. A declarative graph and transactional handoff would need separate semantics and tests before extending the IR.

The initial regression is also an attribution control: structural exchange validation accepted its report; tests and review found the behavioral problem. An IR cannot make an incomplete validator complete. Keep the original failed artifact to avoid turning a corrected result into an overstated safety claim.

## Actual repository effect and verification

After validation, the accepted bundle was published to a dedicated local Git outcome ref and repeated dispatch returned the same operation without a second publication. The sink left HEAD, index and checkout untouched. The parent checked all three worktree preimages, then copied exactly the accepted file contents into the worktree. That integration was sequential, not an atomic multi-file filesystem transaction. [Publication evidence](../experiments/accord/delegation/results/local-publication.json) records the commit, ref, operation and file hashes. No remote publication or live daemon change occurred.

The complete experimental unittest suite passes **60 methods**: 29 runtime, 9 earlier repository evaluation, 15 SDK delegation and 7 new interchange/handoff/sink tests. The focused production suite passes **200 cases**. The broader repository suite was not run. Earlier three-arm reports are historical snapshots at their recorded hashes; the SDK and shared sink have since changed.

Run from a worktree with the repository test dependencies installed in the selected Python environment:

```sh
python -m experiments.accord.delegation.run --output /tmp/accord-delegation-results
python -m unittest discover -s experiments/accord/tests -q
LANGFUSE_TRACING_ENABLED=false python -m pytest tests/test_subagent.py tests/test_subagent_mcp.py tests/test_subagent_paths.py -q
python -m experiments.accord.delegation.sdk_ir experiments/accord/delegation/contracts-1.json
```

The replay command creates disposable ordinary Git repositories, not managed worktrees; it does not modify the checkout or publish remotely. The live worker exchanges are preserved evidence, not automatically relaunched by replay.

The [token transport probe](accord-prompt-artifacts.md) investigates an optional backend representation separately. The next discriminating study would measure repeated authoring/debugging across distinct tasks, or execute exact prepared chat inputs on an inference engine with cold/warm prefix caches. Neither result is implied by the current observations.
