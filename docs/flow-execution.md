# Execute flows without repeated model work

Apply this protocol to implementation, PR review, release validation and thread
closing. It changes how work is scheduled and evidence is carried, not the
flow's permissions, required checks or completion criteria.

## Carry one working record

Use the task's existing state/evidence area. Record the current phase, repository,
head and target SHA, acceptance criteria, commands and outcomes, evidence paths,
reviewed ranges, open findings, and each CI run ID and attempt. Recover from this
record after a context reset; inspect the original evidence behind its claims.
Do not reconstruct completed phases by dumping the entire conversation again.

Reuse a completed check only when its source, relevant inputs, dependencies,
environment and platform still match. A changed file, fixture, configuration,
dependency, branch base or execution environment invalidates affected evidence.
Run the affected checks and all checks the repository requires for the new head.
A failed, interrupted, skipped or unavailable check stays visible. Reuse never
substitutes an old-head run for a mandatory current-head CI or review gate.

Read mutable remote state again at decision boundaries: before requesting a new
review, merging, dispatching a release or reporting completion. Confirm the head,
attempt, required checks and review coverage there. A cached summary alone does
not approve a merge, prove publication or verify installed bytes.

## Batch reads and keep raw evidence out of the conversation

Use the [executable metadata guard and code-mode adapter](mcp-tool-safety.md)
before returning large tool metadata. Retain the original, decode its shape,
allowlist fields, redact selected sensitive values, and check the complete
serialized envelope before emission. Load the trusted adapter before the first
large call; native history reads need its closed item projection because output
options do not remove tool arguments or previews.

- Batch independent reads and checks in one orchestration call. Keep edits,
  dependent checks, approvals and adaptive follow-ups sequential.
- Save raw API pages, native output, receipts and full logs in the existing
  evidence area. Return a compact outcome with identity, changed state, counts,
  failures and evidence paths. Open relevant original ranges when diagnosing a
  finding; do not truncate away failures or treat a summary as proof.
- Read source and unchanged instructions once per relevant revision. Do not
  print a prepared worker package, full transcript, all completed agent results
  or the entire tool catalog merely to retain it. Forward each prepared
  `worker_context` unchanged and retain its original contract.
- Group related fixes and checks into one review round. Keep required fresh,
  independent review; avoid parallel reviewers repeating the same role and
  source range unless a distinct risk warrants them. Validate every returned
  result and assess its evidence as the delegation contract requires.

## Select an indexed project explicitly

Start code exploration with `tokensave_context` and check freshness with
`tokensave_status({})` for the default served project. In Codex code mode:

```javascript
await tools.mcp__tokensave__tokensave_status({});
```

Omit `graph_root` for that default project. Use `graph_root` only when deliberately
querying an explicitly different, already initialized project supported by the
server; use `graph_branch` only for one of that project's tracked branches. A
target repository or worktree path is not automatically a valid selector. Do not
initialize indexing, add a tracked branch or run a potentially initializing CLI
status command to make a lookup succeed without the owner's request.

Inspect the selected root, branch, fallback warnings and freshness before using
the result. When the current branch is untracked or the index is stale, verify
relevant output against the current file or a pinned Git object. Follow the
repository's CodeGraph-first rule for architecture when its index exists.

For UI work, follow [fresh accessibility observations](mcp-tool-safety.md#derive-ui-indices-from-the-latest-observation).
An accessibility observation and the action derived from it are dependent steps;
do not hide a refresh inside an otherwise independent read batch.

## Let a monitor wait

Use a host completion event, a native worker wait or one scripted monitor for
each external run. Prefer the longest supported bounded wait within the host's
limits, and back off when state is unchanged. Keep user updates at the cadence
the host requires. Do not alternate short model sleeps, clock reads, process
polls and full remote status fetches for the same unchanged run. When a monitor
already owns a run, consume its result instead of starting another observer or
re-querying the run alongside it. A timeout remains pending, never successful or
evidence that a bot is unavailable.

For GitHub Actions, an optional bounded observer is
[`watch_github_run.py`](../scripts/dev/watch_github_run.py). Run it from the target
repository using the installed helper's absolute path (derive the installation
as [pr-review](../flows/pr-review.md#optional-github-helpers-from-the-agents-core-installation)
describes). Select the repository, full source SHA, run and attempt from checked
primary state; select the expected job count from that workflow when known:

```bash
python "$AGENTS_CORE_ROOT/scripts/dev/watch_github_run.py" \
  --repo OWNER/REPO --run-id RUN_ID --sha FULL_SHA --attempt 1 \
  --expected-jobs EXPECTED_COUNT --output "$EVIDENCE_DIR/run-RUN_ID-attempt-1"
```

The helper performs GETs through `gh`, waits internally for at most 55 seconds,
reads every page of that attempt's jobs, and rechecks the run after reading them.
It prints one JSON result, retaining raw responses in the chosen directory.
Exit code 0 means the pinned run and all observed jobs succeeded; code 3 means
still pending; code 1 means attention or unverified. Skipped/neutral/cancelled jobs,
missing or inconsistent pages, changed SHA/attempt and API errors never clear
its success gate. Optional skipped jobs require the repository's explicit
assessment, rather than being silently counted as success. A host with shorter
limits must pass a smaller `--seconds` and leave margin for process startup.

This observation is not a complete release gate. Read all required review
bodies, resolve findings, verify actual packaged launch/artifact evidence and
installation where the task requires them. Apply the repository's protections
and authorization. A successful aggregate CI status cannot stand in for those
checks. Bot quota/rate-limit evidence remains unavailability, not approval.

For bot reviews, use the native integration or an existing bounded review
watcher and inspect substantive exact-head completion evidence. Do not keep
polling a paused bot before its recorded next-attempt time. Existing findings
still need decisions and any required independent review still needs completion.

## Measure without promising a cheaper replay

When investigating model cost, report ordinary request counts separately from
compaction and worker requests; split uncached input, cached input and output.
Deduplicate by response ID and do not sum cumulative snapshots. State missing
worker accounting and classification limitations. Cached context is not free
and the token total is not a billing receipt.

Compare the same scripted waiting or output workload before and after the
change. Report fewer model-visible returns or output bytes as those measured
quantities, not as demonstrated end-to-end token, cost or quality improvement.
Retain identical failure cases and source/attempt checks in the comparison.

