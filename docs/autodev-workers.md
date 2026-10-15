# Autodev workers

How a self-development stage session gets a delegated review or pre-mortem: the runner prepares an
Agents-Core package, assembles the materials from git objects, starts the worker CLI, validates
the result against the contract it kept, and records everything (issue #345, epic #336). The
design is [autodev-plan.md](autodev-plan.md), sections 5.6, 7.6, 9.4, Q2 and Q10, as amended by
#345. The worker sandbox reuses [autodev-sandbox.md](autodev-sandbox.md) (#343) and
[autodev-codex.md](autodev-codex.md) (#344); the records feed the [broker](autodev-broker.md) (#342).

| Code | What |
|---|---|
| `src/schedules/workers.py` | The `worker.*` socket service: planning, the review policy, attempts, validation, records, decisions |
| `src/schedules/worker_launch.py` | One attempt: the Claude worker, or Codex through `codex_probes.decide` and `start` with the Claude fallback; the Codex schema |
| `src/schedules/materials.py` | The tree at `pinned` and the materials, from git objects of the trusted repository |
| `src/schedules/sandbox.py` (`WorkerPaths`, `build_worker`, `check_worker_spec`) | The Claude worker's argv, settings and environment |
| `tests/test_schedules_workers.py`, `tests/fixtures/fake_worker.py` | The tests; worker modes of `fake_claude.py` and `fake_codex.py` |

## Current limits

- **One service per run (#347).** The run sequence ([autodev-sequence.md](autodev-sequence.md))
  creates one `workers.WorkerService` when the implement stage starts and keeps it until the
  submit stage ends, because a second service of the same run starts with the push gate closed:
  it removes `validated-review.json`, `findings-ledger.json` and `premortem.json` and sets
  `worker-decisions.json` aside, since it did not write them. The binding gets the run's shared
  state copy as `Binding.log_copy` (so `worker-log.json` stays visible to the submit session)
  and the issue body and the owner's comments the selection named as `Binding.criteria`; the
  importer is `workers.Handover(trusted, exchange, branch)` with the implement stage's exchange
  directory, and `WorkerService.rebind_importer` points it at the submit stage's before that
  stage starts. `max_verifications` is `1 + max_bot_rounds`. The service is registered on the
  runner socket next to the broker and the heavy-process lease, the `worker_launch.Launcher`
  gets the decoys and the run's known secrets (the OAuth token of the keychain login mode, the
  broker's token and deploy key), and `service.close()` runs when the run ends. A resume run
  opens a fresh service on the resumed branch, so its first push needs a new review.
- **The Codex key reaches the broker.** The broker is built before the service, and the service
  gets `on_known=broker.add_known`: a worker's Codex decision, which reads the API key only when
  the attempt starts, adds it to the broker's outgoing-text scan and its log masking at once.
- **The run record is written through `records.update_record`.** The worker thread adds its
  `workers` entry and a Codex decision its `clients` entry while the run sequence writes the same
  record; the run sequence merges every field it adds with `update_record` too, so nothing is
  dropped.
- **The stage prompt is rendered by `stages.render_prompt`.** It fills `{branch}` (the run's
  branch, the one `Handover` imports), `{exchange_dir}` (the stage's exchange directory),
  `{worker_cmd}` and the others of plan 10.4, and asks for a Bash timeout of 600000 ms for every
  `worker wait`, which blocks up to 540 seconds.
- **The run sequence relies on the push gate and adds its own checks.** After the implement
  stage it requires the output's head to be the imported head and `validated_review_head`, an
  accepted pre-mortem (`premortem.json`) and no open security risk (`open_security_risks`: only
  `fixed` followed by an accepted verification closes a security-marked risk, never the session's
  own `mitigated` or `known_risk`); after the submit stage it opens risk issues for
  `risk_candidates` through the broker.
- **On this Mac the reviewer is Claude.** `codex_probes.decide` falls back with
  `nested_sandbox_unavailable` (#344), so a `code-review-pass` profile with `client: codex` runs
  the Claude worker and the record says so.
- **Not observed live.** No test starts a real `claude -p` or `codex exec`. How Claude Code 2.1.296
  and codex-cli 0.162.1 treat the schema comes from their help, documentation and sources (the
  research of 2026-10-14), not from a model turn. The owner's live step is the worker check:

  ```bash
  python -m src.schedules worker-probe --confirm-live-probe
  ```

  It builds one Claude worker as a review does (`worker_launch.Launcher`, `sandbox.build_worker`,
  the `code-review-pass` profile's Claude model and effort) in a review's layout: a synthetic tree
  and materials under `<run_dir>/workers/` (`tree-worker-probe`, `worker-probe/materials`),
  inside the state directory that the worker's `denyRead` covers and its `allowRead` re-opens for
  them. It asks for three `Read` calls: a file in the tree and one in the materials, each holding a
  random line, and a sibling file outside both, and for `{"verdict": "ok", "tree", "materials"}`
  with the two lines. It passes (exit code 0, `status: completed`) only when the structured output
  carries both random lines (both Reads inside succeeded), the outside `Read` was denied, no other
  tool call was denied, the outside file's content never reached the output and the tripwire
  found nothing; the run record keeps the outcome under `worker_check` (with `read_tree` and
  `read_materials`), and the tree, the materials and the outside file are removed. It is a model
  turn, so it refuses without `--confirm-live-probe`.

## The session's commands

The thin client `{worker_cmd}` takes one JSON object, like `gh` ([runner socket](autodev-runner.md#thin-clients)).
Every type is session-bound.

| Command | What it does |
|---|---|
| `worker review '{"head": "<40-hex>"}'` | Starts the next review of `head` and prints its handle (`review-1`, `review-2`, ...) at once |
| `worker review '{"head": "<40-hex>", "verification": true}'` | The verification review after the pre-mortem (`verify-1`) |
| `worker premortem '{"head": "<40-hex>"}'` | The pre-mortem of `head` (`premortem-1`) |
| `worker wait '{"handle": "review-1"}'` | Waits up to 540 seconds (`"seconds"` may shorten it; `0` only polls) and prints the state; call it again while it says `running` |
| `worker decide '{"key": "<16-hex>", "decision": "fixed"}'` | Records a decision on a finding; `declined`, `mitigated` and `known_risk` need a `"reason"` |

The session names no range and no ref: only a head. Before every `review` and `premortem` the
runner imports the session's handed-over bundle (plan 9.4 item 5): the session writes
`git bundle create <exchange>/handover.bundle claude/autodev-<issue>-<slug> ^<base>`, and
`workers.Handover` runs `repos.TrustedRepository.import_bundle` for the run's branch, checked
against the run's base, under the service lock. A refused bundle refuses the request with its
reason, and with a bundle the head must be the bundle's head. Without the file nothing is
imported. Either way the head must then be the 40-hex tip of an imported branch
`claude/autodev-<issue>-<slug>` that descends from the run's base. The runner computes the range
itself, and every git command puts its revisions after `--end-of-options`. A second worker is
refused while one runs; after a `blocked` or `failed` worker, or a restricted review that ended
the stage blocked, every request is refused.

A finished state holds only parsed `ReviewPayload` fields, each cut to a limit: the summary (2000
characters) and per finding its key, severity, location (300), explanation (1500),
recommendation (800) and the decision taken so far, at most 50 findings. It also says how many
findings a restricted review left out (`dropped_out_of_scope`), how many are `medium` or higher,
and for a restricted review how many count as blocking (`stage_blocked: true` and a reason when
any do). A review or verification also lists `open_findings`: the key, severity, reviewing handle
and decision of every finding of any review that keeps the push gate closed now. The worker's
evidence, checks,
limitations and question never reach the session; `input_required` says only that the worker
asked.

## The review policy

| Request | Range | Effort | Considers | Counted |
|---|---|---|---|---|
| review 1 | the run's base (`pinned`) to the head | the delegate's effort | everything | yes |
| review 2 to `max_reviews - 1` | the last reviewed head to the head (the same range again when the head did not move), plus `decisions.json` and `open-findings.json` | `effort_followup` | everything, and every open earlier finding again | yes |
| review `max_reviews` (when it is not the first) | as review 2 | `effort_followup` | `critical`, `high`, security, regressions caused by fixes and the open earlier findings | yes |
| pre-mortem | the run's base to the head, plus the review log and the decisions | the `change-premortem` profile | the three to five likeliest failure causes | no |
| verification | the last full review's head to the head, plus `decisions.json` and `open-findings.json` | `effort_followup` | `critical`, `high`, security and the open earlier findings | no |

No review starts beyond `max_reviews` (default 3). The pre-mortem needs an accepted review whose
head is the requested head while [the push gate](#the-push-gate) is open (no open blocker in the
findings ledger); the verification review needs an accepted pre-mortem and new commits since the
last full review. `open-findings.json` lists each ledger entry that keeps the gate closed, with
its severity, location, explanation, recommendation, the review that reported it last and its
decision state (`null` for undecided, `fixed`, or `declined` with the reason) and, for a `fixed`
entry a review of the same head left open, a `note` saying so; the worker is told
to report again, with location and explanation unchanged, every one still present or whose
decline reason does not hold. For the restricted reviews the runner also drops findings outside
their scope before the session sees them: anything below `high` that carries no security keyword
(the wide `broker.SECURITY_KEYWORDS`, location included, so the scope errs towards keeping a
finding) and, in the last review, no regression. A restricted review (the last one or the
verification) that still reports a finding in its scope ends the stage blocked (plan 9.4): its
record says `blocked: true`, the push gate stays closed and the service refuses every further
worker. An open earlier finding that it reports again outside its scope does not block the stage
but stays open in the ledger.

**Attempts.** An attempt is accepted only when the output passes `validate_result` against the
retained contract, its status is `completed`, an evidence item names the reviewed range
`<base>..<head>` in its reference or description, and no evidence item names a 40-hex commit
other than the run's base, the range's ends and the commits since the run's base. The tripwire
scans every attempt first, a failed one included: standard output (the first 4 MiB), the raw
result and the permission denials. A Claude worker killed at its timeout or by a stop prints no
result object (`--output-format json` writes it, with the denials, only at the end), so a decoy
it read during that attempt is not seen; the attempt counts as a CLI failure. Otherwise:

| Outcome | Next |
|---|---|
| A decoy named in a denied tool call, or a decoy marker or another known secret in the output, whatever the CLI's outcome | `blocked`; a decoy also turns the schedule off (`decoys.trip`) |
| `partial` or `failed`, evidence that does not name the reviewed range or names another commit, `valid: false`, a CLI that ended without a result (exit code, subtype, timeout, missing or oversized output) | A new attempt with a new `attempt_id` and a new package; at most three attempts in all, then `blocked` |
| `input_required` | `blocked` |
| No CLI, no login, a launch that fails its checks, a tree with project configuration, a package that cannot be prepared or whose `worker_context` length differs from `context_chars`, a Codex decision that aborted the step | `failed` (infrastructure); no worker starts |

The plan gives `valid: false` a new attempt without a limit; here it shares the cap of two new
attempts, so a worker that keeps breaking the contract cannot loop.

## Packages

Each attempt calls `src.engine.subagent.prepare_subagent` with `result_type="review"`, the agent
`code_reviewer` for reviews and `delegates.change-premortem.role` (default `sysadmin`, Q2) for the
pre-mortem, `task_id` `autodev-<run-id>-<handle>`, the attempt number as `attempt_id` and a
budget of 32,000 characters. `AGENTS_USER_FLOWS_DIR` points to an empty directory during the call
(under a process-wide lock), so no personal component toggle changes the worker. The task text
names the range and the head; the acceptance criteria ask for findings with locations, failure
scenarios and remedies and for the range as evidence; the restricted reviews and the
verification say what they consider. The service keeps the contract, checks that
`len(worker_context) == context_chars`, and passes `worker_context` to the CLI's standard input
byte for byte.

## Materials

`src/schedules/materials.py` writes two runner-owned directories under
`<run_dir>/workers/`, from git objects of the trusted repository only:

- `tree-<pinned>`: every regular file of the run's base, the worker's working directory, written
  once per run, except project configuration a CLI could load from its working directory
  (`.claude/` and `.codex/` at any depth and `.mcp.json`, compared case-insensitively; the
  export lists them as `left_out`). The Claude launcher also refuses a tree that holds project
  settings (`probes.repository_problems`, as for a session).
- `<handle>/materials`: `README.md`, `range.json` (with `pinned`), `commits.txt` (the messages of
  the reviewed range), `diff.patch` (`git diff --no-ext-diff --no-textconv` of the reviewed range),
  `head/<path>` (every file that differs between `pinned` and the head, as it is at the head, so
  a follow-up or verification review also sees the files earlier rounds changed), `deleted.txt`
  and `links.json` (since `pinned`), and where they apply `criteria.md` (the issue's acceptance
  criteria as the runner read them), `decisions.json`, `open-findings.json` (a later review and
  the verification) and `reviews.json` (the pre-mortem).

A gitlink anywhere in the tree or the range, a symbolic link whose target may leave the tree
(`repos._link_escapes`), an unusual mode, and a path that is absolute or holds `..`, `.git` or a
control character refuse the materials, and the review ends `blocked`. Symbolic links are never
created: `links.json` lists the inward ones as text. Each file is created new (`O_EXCL`,
`O_NOFOLLOW`) in a directory the module made; every git output is bounded (8 MiB of diff, 64 MiB
of changed files, 256 MiB of tree, 1,000 commits). The changed-file and tree byte limits count
each written path, even when several paths share one blob; a batch that would exceed the
remaining tree budget is refused before any of its files are written.

## The Claude worker

```text
claude -p --restricted --safe-mode --tools Read,Grep,Glob --strict-mcp-config --permission-mode dontAsk
  --permission-prompts none --disallowedTools mcp__* --no-session-persistence --settings <file>
  --add-dir <materials> --output-format json --json-schema <schema> [--model M] [--effort E]
```

The working directory is the tree at `pinned`; `--add-dir` names only the materials. The flags
beyond the plan's line (`--permission-mode dontAsk`, `--disallowedTools mcp__*`, `--settings`)
are #343's, for the same reasons: a pinned permission mode, no claude.ai connector tools, and the
session's settings. The settings (`sandbox.worker_settings`) keep
`blockReadsOutsideWorkingDirectories`, have no allow rules, deny `Edit` on the tree and the
materials, and carry the session's closed sandbox block with nothing writable but the worker's own
temporary root (`sandbox.make_temp_root`, removed after the attempt); `check_worker_settings`
refuses anything else. The environment is the session allowlist (`sandbox.environment`) with the
persistent runner-owned `CLAUDE_CONFIG_DIR`, whose login `sandbox.require_login` checks before every
start. That is the stage session's directory, a deliberate amendment of the issue's "its own
`CLAUDE_CONFIG_DIR`": a separate directory has no subscription login (plan 5.6, Login), so a
worker runs with the one login while the session waits in `worker wait`, and both CLI processes
use the directory's files for that time. `sandbox.check_worker_spec` compares argv, settings on disk, environment, temporary root and
the executable's resolved path and SHA-256 with what the launcher built.

`--json-schema` takes the package's `result_schema` unchanged, as inline compact JSON: Claude
Code validates the structured output with Ajv against every keyword the `subagent/1` schema uses
and re-prompts on a mismatch (research). Only exit code 0 with a `result` of `subtype: success`,
no `is_error` and an object `structured_output` yields an output; `error_max_structured_output_retries`
and everything else is a failed attempt. Standard output goes to a private file that is removed
once read (at most 4 MiB; a larger output fails the attempt, and its first 4 MiB are still
scanned). A denied tool call that names a decoy is caught from `permission_denials`. Output files
are opened without following a link and without blocking (`O_NONBLOCK`), and only a regular file
with one link is read, so a FIFO cannot stall the runner and a hard link to another file is not
read.

## The Codex worker

A `code-review-pass` profile with `client: codex` calls `codex_probes.decide` before every
attempt, with a fresh temporary root, `CODEX_HOME` and `HOME` (`codex_sandbox.prepare_run_directories`),
a new empty output directory, so the `-o` file cannot exist before the start, and copies of the
tree and the materials with the derived schema file in a second fresh temporary root
(`worker_launch.codex_inputs`): `codex_sandbox.CodexPaths` refuses a read root inside the state
directory, where the service writes them. Both roots are removed after the attempt.
`codex_probes.record_decision` adds the decision to the run record's `clients`. A decision that
aborts the step (`Decision.abort`, a deny probe got through) fails the task: neither Codex nor
the Claude worker runs, and the decision's known secrets (the API key) still reach the service's
masking.
When Codex is applied, `codex_probes.start` starts it (the only way) and the output is the raw text
of the `-o` file, read without following links (at most 512 KiB) and only after exit code 0; it
goes to `validate_result` as text, which also refuses duplicate keys. Standard output is capped
at 4 MiB as for Claude: overflow fails the attempt even with a valid `-o` result, and the bounded
prefix is still scanned for the tripwire. When `decide` falls back,
the Claude worker runs with the profile's `claude` model and effort (default `opus`, `high`; a
follow-up uses `effort_followup` when Claude knows it), and the attempt record says
`client_requested: codex`, `client_applied: claude` and `fallback_reason`.

**The schema.** codex-cli 0.162.1 sends `--output-schema` verbatim with `strict: true`, and
OpenAI's strict subset does not take `minLength` or `maxLength` (research). `worker_launch.codex_schema`
drops `minLength`, `maxLength` and `title` in schema positions (a property named `title` stays) and
writes `const` as a one-value `enum`. `worker_launch.relaxation_problems` checks mechanically that
the derived schema is the original with only those changes, so it accepts every output the
original accepts; every other difference (a new keyword, a changed value, a dropped `required` or
property) refuses the launch. `validate_result` on the raw output stays the only strict check.

## Records

| File (under `<state>/schedules/runs/<run-id>/`) | Written | Read by |
|---|---|---|
| `workers/<handle>/attempt-<n>.json` | Every attempt: the package revision and bundle ID, the retained contract, the client (`client_requested`, `client_applied`, `fallback_reason`), model and effort, the CLI's exit and subtype, the launch description (flags without paths or the schema, variable names, digest), the raw structured output with its SHA-256 and form (`object` for Claude, `text` for Codex), the validation response, the range, base and head, the verdict and its reason | The owner, the run report |
| `workers.json` | The service's state: each task's range, status, reason, attempts and accepted attempt | The owner, the run report |
| `record.json` | `workers`: a summary of each finished task; `clients`: each Codex decision; both through `records.update_record` | The runner, the status command |
| `findings-ledger.json` | After an accepted review or verification, before its record: `{"head", "handle", "findings": [{"key", "level", "security", "handle", "kind", "status", "resolved_by", "reported_by", "location", "explanation", "recommendation"}], "reviews": [{"handle", "head", "given", "repeated", "resolved", "added", "unmoved"}]}`, every `medium`-or-higher finding of every accepted review | `broker.review_gate` |
| `validated-review.json` | After an accepted review or verification: `{"validated": true, "status": "completed", "head", "base", "range", "handle", "kind", "number", "restricted", "clean", "blocked", "findings": [{"key", "level", "security"}], ...}`, `findings` being this review's of level `medium` or higher, `security` from `workers.gate_security_signal`; `clean` speaks of this review alone. A task that ends `blocked` or `failed` replaces it with `{"validated": false, "status": "blocked" \| "failed", "blocked": true, "handle", ...}` | `broker.review_gate` and `broker.validated_review_head` (the push check, the pre-mortem's precondition) |
| `premortem.json` | After an accepted pre-mortem: `{"validated": true, "findings": [{"id", "level", "title", "body", "security"}], ...}`, `id` the finding key, `security` from `broker.SECURITY_KEYWORDS` | `broker.read_premortem` (risk issues); the run sequence (an accepted pre-mortem before the submit stage) |
| `worker-decisions.json` | The latest decision per key with at most five earlier ones (`MAX_HISTORY`), location and explanation cut to the session's limits; at most 1,000 decisions per run | The push gate; `open_security_risks` and `risk_candidates` (the run sequence); the run report's decision counts |

Records are masked with the decoy markers and every known secret of the run (the Codex API key
included); an output that tripped the tripwire keeps only its SHA-256. The raw output's SHA-256
is computed before masking; a record whose output masking changed says `masked: true`.

**Decisions.** A finding's key is the first 16 hex digits of the SHA-256 of
`[location, explanation]` as canonical JSON. `worker decide` takes `fixed` or `declined` for a
review or verification finding and `fixed`, `mitigated` or `known_risk` for a pre-mortem finding,
applied to the latest accepted result that holds the key (for a ledger entry, the review that
reported it last, also when that was a restricted review outside its scope). After every
decision and every finished worker the runner writes `worker-log.json` (mode 0400) into the
session's state copy, a directory the session can read but not write: every task's parsed state
with its findings, the decisions and the open ledger entries, for recovery after a compaction.

## The push gate

The runner, not the session or a worker, carries the open findings: the **findings ledger**
(`findings-ledger.json`) holds every finding of level `medium` or higher of every accepted review
and verification, under the handle of the review that reported it last. An accepted review
changes it so:

| The review ... | The entry |
|---|---|
| reports it again (in its scope, or outside it when the entry was in its `open-findings.json`) | stays or becomes `open` under the new handle, at the higher of both levels; a decision must name the new review |
| was given it `declined` and does not report it again | `resolved` |
| was given it `fixed`, does not report it again and reviewed another head than the review that reported it | `resolved` |
| was given it `fixed` but reviewed the head that reported it (no commit since) | unchanged and `open`, with `unmoved` naming the review: the same diff again cannot show a fix |
| was given it undecided, or was not given it, and does not report it again | unchanged: an undecided finding stays open whatever later reviews say |
| reports a new `medium`-or-higher finding in its scope | a new `open` entry |

`broker.review_gate` reads `validated-review.json`, the ledger and `worker-decisions.json`; the
broker's push check and the pre-mortem's precondition take its head (`validated_review_head`).
It opens only when the record says `blocked: false` (and `clean: true` when it lists no finding),
the ledger covers the record's head and handle and holds every finding the record lists, and
every `open` entry is `medium`, has `security: false` and was `declined` with a reason by a
decision that names the entry's handle (the latest such decision on its key, current or in its
history). Such a declined `medium` has left the gate; a later review that reports it again
re-opens it. A `critical`, `high` or security entry keeps the gate closed even when declined, until
a later review that was given the decline does not report it again. A `fixed` decision does not
open it either: the fix is resolved by the next review of a later head that was given the claim. A
missing or malformed field closes the gate, and so does a record of an earlier service. A worker
that ends `blocked` or `failed` ends the stage blocked: its marker replaces the record, and the
gate stays closed also over an earlier clean review. A finding key a result lists twice counts once,
at its higher severity.

The security signal of a review finding is `workers.gate_security_signal`: a fixed keyword rule
(`GATE_SECURITY`) over its explanation and recommendation, not its location, without the bare
`auth*`, `token*`, `leak*`, `sandbox*` and `permission*` of `broker.SECURITY_KEYWORDS`, so an
author line, a tokenizer, a memory leak or a finding in `sandbox.py` can be declined, while
credentials, secrets, injection, privilege escalation, exfiltration, sandbox escapes and the like
cannot. The pre-mortem's risks keep the wide rule, which keeps a risk local (design 5.8).
