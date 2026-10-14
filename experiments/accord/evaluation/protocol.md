# Evaluation protocol (declared before paired runs)

Date: 2026-10-14. Source revision: `3d95c42b79693d6a73d017d602f1b4262f3a300e`.

Task: improve `tests/README.md` with a directly runnable focused command for compact subagent preparation/result validation and explain the structural-only boundary. The existing guide lists the two files in a table but offers no dedicated runnable section. Preserve all existing guide text. The agent-authored insertion is recorded in `fixtures/subagent-testing.md`; it is replayed identically, not freshly generated for each arm.

Three arms prevent conflating representation with runtime engineering:

1. `accord_json`: parse the saved contract and execute the existing Accord interpreter.
2. `accord_python`: construct the equivalent Contract directly in Python and use the same interpreter. This isolates the representation choice.
3. `python_sdk`: independent ordinary Python lifecycle code, configured by JobSpec, sharing only hash/storage primitives, domain checks and the Git adapter. It must implement the same tested single-task guards; child delegation is outside this comparison.

Hold candidate bytes, source snapshots, predicates/revisions, request budget, publication authority, target preconditions and fault placement equal. Run existing subagent tests once against the source revision and bind their recorded result to the input digests; do not count this as one model/test run per arm. Measure artifacts and state transitions, not prose quality or unsupported claims about human productivity.

Required scenarios: success; invalid/out-of-scope candidate; wrong command; missing, forged and wrong-attempt receipts; source change including ABA; resample after acceptance; validator/profile change; revocation/cancellation before admission; request budget race; reissued authorization with a changed target version; destination CAS conflict; lost response after Git commit; process death before send and after sink commit; recovery racing a live sender; cancellation after admission; completed-attempt replay; opaque destination uncertainty. Each arm starts from fresh state. Assert exact result bytes, no writes on denied admission, one publication after successful recovery, stable operation identity, spent authority, conserved credits and equivalent normalized outcomes. Report actual rows and any failure, not only aggregate success.

The revised Git adapter publishes an outcome commit to one dedicated, exclusively owned append-only local ref using a single `git update-ref` CAS. Each commit contains the document tree and operation metadata together. Denial commits preserve document bytes but advance the outcome-chain version. Reconciliation searches this same history. External ref rewind/deletion violates the adapter contract; filesystem corruption and power-loss recovery are not claimed. The working-tree file is materialized separately, after the parent checks the accepted diff and verifies the current preimage; this step is not claimed to be an atomic concurrent-filesystem CAS. No remote publication occurs.

Decision rule: any safety failure rejects that implementation until repaired. If plain Python attains equal tested safety and similar task orchestration, retain Accord as an experimental IR/library; this task does not justify a standalone language. Advancing to a compiler would require at least two independent backends consuming the same unmodified contracts and multiple distinct workflows with measured authoring/debugging benefits. This run supplies neither a human productivity study nor a model-quality/cost benchmark. Count shared adapters, validators, harness, runtime, task Python and JSON separately; do not present duplicate runtime implementation as task glue saved by a language.

## Review-driven additions

The first run covered 21 scenarios (63 arm runs). Independent review exposed a partial multi-ref commit failure and two command-validation holes. The adapter now uses one outcome ref; the command predicate requires an exact standalone shell-fence command. Three additional scenarios cover commented/echoed commands and a lost acknowledgement after the ref CAS. These were added after the initial run, not presented as preregistered cases. A separate diagnostic probe records that contradictory prose preserving required phrases passes the bounded predicates in every arm; this is a task-quality failure and is reported separately from state-machine conformance.

A subsequent deterministic adapter regression covers a duplicate sender winning between deduplication lookup and CAS. Lookup and CAS now share one captured immutable tip. This is an adapter test, not a twenty-fifth paired runtime scenario.
