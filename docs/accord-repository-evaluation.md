# Accord repository evaluation: keep the IR, defer the language

Recorded experiment: 2026-10-14, source task pinned at `3d95c42b79693d6a73d017d602f1b4262f3a300e`, Python 3.13.13, Git 2.54.0. This is one bounded repository maintenance task with recorded agent-authored output. It is not a model benchmark or a developer productivity study.

**Decision: this experiment does not justify a standalone Accord language or compiler.** JSON IR, a direct Python contract and an ordinary Python SDK produced equivalent observations across all 24 runtime scenarios. The useful mechanisms are immutable candidate identity, host-owned checks, scoped authorization, a durable ledger and destination-specific recovery. Python expresses those mechanisms in the tested scope. Retain the experimental IR/library and test portability and authoring benefits before adding syntax.

## The real task and its result

The repository's test guide already listed `tests/test_subagent.py` and `tests/test_subagent_mcp.py` in a coverage table, but lacked a dedicated focused invocation and an explanation of what a structurally valid worker result establishes. The task adds a 20-line section to [tests/README.md](../tests/README.md#focused-subagent-contract-checks), preserving every pre-existing byte:

```bash
LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/test_subagent.py tests/test_subagent_mcp.py -q
```

The text explains the core/MCP coverage, links the subagent contract, distinguishes `structural_only` validation from factual correctness and authorization, and points to the additional checks needed when shared instructions change. The parent inspected the accepted diff against the actual tests and contract documentation. The actual command ran once using the installation's existing virtual environment from the task worktree: **144 passed**, with one sandbox cache-write warning, in 15.46 seconds. [Recorded test evidence](../experiments/accord/evaluation/fixtures/test-evidence.json) binds this observation to source digests; it is reused by all arms, not counted as 144 fresh tests per arm.

The final document was accepted through Accord, published to a dedicated local Git ref, and then projected into the worktree after checking its preimage and diff. [Publication evidence](../experiments/accord/evaluation/results/worktree-publication.json) records the operation, checks, acceptance, adapter digest and accepted Git commit. [The exact patch](../experiments/accord/evaluation/results/accepted.patch) is independently inspectable. The final document SHA-256 is `ab6cabc25f7a97da55246566e8b21337188e486aec5bb63e178fd5d64aaa13b9`.

## Controls and implementations

| Arm | Task declaration | Lifecycle implementation | What it isolates |
|---|---|---|---|
| `accord_json` | Closed, versioned JSON contract | Existing Accord `Runtime` | Serialized IR plus runtime |
| `accord_python` | Python `Contract` constructor | The same Accord `Runtime` | Representation alone |
| `python_sdk` | Python `JobSpec` constructor | Separately implemented `PythonSDK` | Ordinary Python can implement the same tested guards |

The SDK does not import `Runtime` or `Contract`. Its implementer did inspect the reference implementation, so this is separate lifecycle code, not a blinded independent design. It shares hash/storage helpers, domain predicates and the Git sink with Accord. Child delegation is outside the comparison. The third arm is a small stdlib SDK, not a benchmark against a specific commercial SDK or orchestration framework.

All arms receive the same frozen source files, candidate bytes, validator revisions, two-request budget, exact publication authority and fault placement. The source task uses six inputs: guide, core tests, MCP tests, API guide, project configuration and recorded test evidence. The four domain checks enforce a bounded insertion, an exact runnable command, selected source/wording facts and correspondence with the recorded test run. They do not establish arbitrary prose truth.

The [protocol](../experiments/accord/evaluation/protocol.md) was written before the first paired run. The first run had 21 scenarios; review added three scenarios and a separate adapter race regression. These additions are explicitly retrospective. Every trial uses a fresh disposable Git repository containing the real frozen files and a separate host SQLite ledger. Candidate generation is replayed, so no inference-quality, token-cost or latency advantage is measured. Per-row elapsed times include fixture creation and are not a performance benchmark.

## Executed results

[Raw observations](../experiments/accord/evaluation/results/comparison.json) contain 72 arm runs, exact normalized observations, input/candidate hashes and implementation hashes. All 72 met their runtime expectations; all 24 paired scenarios were equivalent. Normalization excludes random IDs and elapsed time, but retains status, publication count, budget, model/validator calls, grant state, task control and document digest.

| Cases | Expected and observed result in each arm |
|---|---|
| Valid edit | Four checks pass; one accepted document publication; one request remains |
| Out-of-scope edit, wrong/commented/echoed command | Acceptance blocked; no document publication |
| Missing/forged/wrong-attempt receipts | Acceptance blocked; no document publication |
| Source ABA, resample, profile change | Dispatch blocked despite historical acceptance |
| Revocation/cancellation before admission, changed grant intent | New action blocked |
| Two model calls racing for the final credit | One admitted; total two charged calls; zero remaining credits |
| Destination version conflict | Denied; competing document preserved |
| Lost sink reply or lost Git CAS acknowledgement | Exact operation reconciled; one document publication |
| Process death before send or after sink commit | Reopened ledger recovers the same operation; one publication |
| Recovery racing live sender, completed-attempt replay | Stable authority and operation; no duplicate publication or replayed model call |
| Cancellation after admission | Published result observed; cancellation remains recorded |
| Opaque sink | `outcome_unknown`; no blind resend, even though the fixture shows one effect occurred |

The stdlib suite passed **38 test methods**: 29 existing runtime tests and nine evaluation/adapter tests. These include concurrent competing publications, a deterministic same-operation interleaving and a committed CAS whose acknowledgement is lost. Actual subprocess tests use `os._exit(73)` before/after sending. These checks exercise specific interleavings; they are not an exhaustive scheduler or power-failure proof.

**Semantic counterexample: all three arms accepted and published a known false claim in disposable fixtures.** The diagnostic adds “A successful structural validation guarantees that all worker claims are factually true.” while preserving the phrases checked by `source_alignment`. All declared predicates pass. [Raw semantic probe](../experiments/accord/evaluation/results/semantic-probe.json) records three false positives. The bad candidate was never materialized into the task worktree. Consequently, `all_passed` in the runtime comparison means conformance to those runtime cases, not end-to-end semantic correctness. More syntax would not repair an incomplete acceptance predicate.

## What real Git exposed

The first adapter published the document ref and a separate receipt ref in an `update-ref --stdin` transaction. Review removed the receipt lock after `prepare`; the parent independently reproduced exit 128 with the document ref advanced but the receipt missing. Retrying the old adapter then reported denial for an already published document. [The preserved fault record](../experiments/accord/evaluation/results/git-fault-before-fix.json) identifies that implementation and result. This invalidated the original adapter guarantee.

The repaired adapter uses one exclusively owned, append-only ref. Each outcome commit contains both the document tree and operation metadata. A successful outcome changes the target file; a denied outcome preserves the tree but advances the audit-chain version. One compare-and-swap publishes that commit. Git's `update-ref` accepts an expected old object ID; `commit-tree` creates the immutable tree/parent/message container. See the official [update-ref](https://git-scm.com/docs/git-update-ref), [commit-tree](https://git-scm.com/docs/git-commit-tree) and [update-index](https://git-scm.com/docs/git-update-index) documentation.

A second review interleaving found that separate reads for deduplication and CAS could append a denial after another sender confirmed the same operation. The parent reproduced it. Deduplication now searches the exact immutable tip captured for the CAS; a competing publication forces a retry against the new ancestry. Its regression requires one outcome commit and a stable confirmed receipt. A command-validator hole was also repaired: a commented or echoed command no longer satisfies the exact shell-fence check, and that validator's revision was advanced to `2`.

This adapter does not edit HEAD, the user's index or working files. The real effect is publishing the dedicated ref; a separate trusted host action materializes the accepted file. Preimage recheck plus atomic rename is not a concurrent-filesystem CAS. External ref rewinds/deletion, untrusted Python callbacks, malicious ledger writers, remote publication, filesystem corruption and power loss remain outside the tested assumptions. No arbitrary external exactly-once guarantee follows from this result.

## Source accounting

[Reproducible accounting](../experiments/accord/evaluation/results/code-accounting.json) counts physical lines, including comments/docstrings, with bytes and hashes. Formatting affects these counts; they are not authoring-time measurements.

| Component | Physical lines | Interpretation |
|---|---:|---|
| JSON task declaration | 15 | Data; still needs host wiring and validators |
| Shared Python task configuration | 7 | Used by both Python declarations |
| JSON / Python Contract / SDK constructor functions | 2 / 2 / 3 | Tiny representation wrappers |
| Accord runtime plus IR | 455 | Includes JSON parsing and child allocation absent from the SDK subset |
| Independent SDK including JobSpec | 360 | Tested single-task lifecycle; not a complete replacement |
| Shared storage / Git adapter / workload predicates | 52 / 141 / 83 | Both implementations depend on this code |
| All-arm wiring / paired harness | 43 / 304 | Experiment infrastructure |

The lifecycle remains `infer → check → accept → authorize → prepare → dispatch` in all three arms. The JSON file does not eliminate this sequence, the host policy decision, the validators or destination engineering. Comparing 15 JSON lines against 360 SDK implementation lines would conflate configuration with a reusable runtime. The direct Python control removes that ambiguity and showed no observed behavioral advantage for JSON on this task.

## Decision and next evidence

Keep Accord as an experimental versioned IR and Python library. Its explicit distinctions are useful design constraints, and its serialized form is a possible tooling/interchange boundary. This experiment establishes neither a necessary standalone language nor cross-backend portability: the independent SDK does not consume the IR, and both runtime arms still execute Python.

Before investing in a compiler, require at least two independent backends to consume the same unchanged contracts and a broader set of tasks, such as a code patch and a two-stage delegated review. Hold checks and authority equal, include semantic negative controls, and measure actual authoring/debugging effort and diagnostic quality. A compiler would need to reject meaningful errors that Python tooling does not reject, or materially reduce that measured effort. The current single recorded documentation task supplies neither result.

Reproduction commands and operational limits are in the [evaluation guide](../experiments/accord/evaluation/README.md). This conclusion is bounded to the experiment; it does not prove that a language could never become useful.
