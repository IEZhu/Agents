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

- **No run opens the service yet.** The run sequence (#347) creates one `workers.WorkerService`
  per run for the implementation stage, registers it with `workers.register(registry, service)`
  on the runner socket next to the broker, passes the session's state copy as `Binding.log_copy`,
  the issue's acceptance criteria as `Binding.criteria` and
  `importer=workers.Handover(trusted, exchange, branch)` (the stage's exchange directory and the
  run's branch `claude/autodev-<issue>-<slug>`, which the session then also pushes), and calls
  `service.close()` when the stage ends. It also passes the decoys and the run's known secrets
  (broker token, deploy key) to the `worker_launch.Launcher`. A second service of the same run
  starts with the push gate closed: it removes `validated-review.json` and `premortem.json` and
  sets `worker-decisions.json` aside, since it did not write them.
- **#347 relies on the push gate.** `broker.validated_review_head` opens only for a clean review
  record or one whose remaining findings are declined `medium` ones (see [the gate](#the-push-gate));
  #347 still requires an accepted pre-mortem before the PR and keeps open security risks local.
- **On this Mac the reviewer is Claude.** `codex_probes.decide` falls back with
  `nested_sandbox_unavailable` (#344), so a `code-review-pass` profile with `client: codex` runs
  the Claude worker and the record says so.
- **Not observed live.** No test starts a real `claude -p` or `codex exec`. How Claude Code 2.1.296
  and codex-cli 0.162.1 treat the schema comes from their help, documentation and sources (the
  research of 2026-10-14), not from a model turn. The owner's live step is the worker check:

  ```bash
  python -m src.schedules worker-probe --confirm-live-probe
  ```

  It builds one Claude worker exactly as a review does (`worker_launch.Launcher`,
  `sandbox.build_worker`, the `code-review-pass` profile's Claude model and effort) on a synthetic
  tree with a trivial schema (`{"verdict": "ok"}`) and asks for one `Read` of a file outside the
  tree. It passes (exit code 0, `status: completed`) only when the result carries
  `structured_output`, that `Read` was denied, no other tool call was denied, the file's content
  never reached the output and the tripwire found nothing; the run record keeps the outcome under
  `worker_check`. It is a model turn, so it refuses without `--confirm-live-probe`.

## The session's commands

The thin client `{worker_cmd}` takes one JSON object, like `gh` ([runner socket](autodev-runner.md#thin-clients)).
Every type is session-bound.

| Command | What it does |
|---|---|
| `worker review '{"head": "<40-hex>"}'` | Starts the next review of `head` and prints its handle (`review-1`, `review-2`, ...) at once |
| `worker review '{"head": "<40-hex>", "verification": true}'` | The verification review after the pre-mortem (`verify-1`) |
| `worker premortem '{"head": "<40-hex>"}'` | The pre-mortem of `head` (`premortem-1`) |
| `worker wait '{"handle": "review-1"}'` | Waits up to 540 seconds (`"seconds"` may shorten it) and prints the state; call it again while it says `running` |
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
any do). The worker's evidence, checks,
limitations and question never reach the session; `input_required` says only that the worker
asked.

## The review policy

| Request | Range | Effort | Considers | Counted |
|---|---|---|---|---|
| review 1 | the run's base (`pinned`) to the head | the delegate's effort | everything | yes |
| review 2 to `max_reviews - 1` | the last reviewed head to the head (the same range again when the head did not move), plus `decisions.json` | `effort_followup` | everything | yes |
| review `max_reviews` (when it is not the first) | as review 2 | `effort_followup` | `critical`, `high`, security and regressions caused by fixes | yes |
| pre-mortem | the run's base to the head, plus the review log and the decisions | the `change-premortem` profile | the three to five likeliest failure causes | no |
| verification | the last full review's head to the head | `effort_followup` | `critical`, `high` and security | no |

No review starts beyond `max_reviews` (default 3). The pre-mortem needs an accepted review whose
head is the requested head and that opens [the push gate](#the-push-gate) (clean, or its
remaining findings declined `medium` ones); the verification review needs an accepted pre-mortem
and new commits since the last full review. For the restricted reviews the runner also drops
findings outside their scope before the session sees them: anything below `high` that carries no
security keyword (`broker.SECURITY_KEYWORDS`) and, in the last review, no regression. A restricted
review (the last one or the verification) that still reports a finding ends the stage blocked
(plan 9.4): its record says `blocked: true`, the push gate stays closed and the service refuses
every further worker.

**Attempts.** An attempt is accepted only when the output passes `validate_result` against the
retained contract, its status is `completed`, an evidence item names the reviewed range
`<base>..<head>` in its reference or description, and no evidence item names a 40-hex commit
other than the run's base, the range's ends and the commits since the run's base. The tripwire
scans every attempt first, a failed one included: standard output (the first 4 MiB), the raw
result and the permission denials. Otherwise:

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
  criteria as the runner read them), `decisions.json` and `reviews.json`.

A gitlink anywhere in the tree or the range, a symbolic link whose target may leave the tree
(`repos._link_escapes`), an unusual mode, and a path that is absolute or holds `..`, `.git` or a
control character refuse the materials, and the review ends `blocked`. Symbolic links are never
created: `links.json` lists the inward ones as text. Each file is created new (`O_EXCL`,
`O_NOFOLLOW`) in a directory the module made; every git output is bounded (8 MiB of diff, 64 MiB
of changed files, 256 MiB of tree, 1,000 commits).

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
start. `sandbox.check_worker_spec` compares argv, settings on disk, environment, temporary root and
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
goes to `validate_result` as text, which also refuses duplicate keys. When `decide` falls back,
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
| `workers/<handle>/attempt-<n>.json` | Every attempt: the package revision and bundle ID, the retained contract, the client (`client_requested`, `client_applied`, `fallback_reason`), model and effort, the CLI's exit and subtype, the launch description (flags without paths or the schema, variable names, digest), the raw structured output with its SHA-256 and form (`object` for Claude, `text` for Codex), the validation response, the range, base and head, the verdict and its reason | The owner, #347 |
| `workers.json` | The service's state: each task's range, status, reason, attempts and accepted attempt | The owner, #347 |
| `record.json` | `workers`: a summary of each finished task; `clients`: each Codex decision | The runner, the status command |
| `validated-review.json` | After an accepted review or verification: `{"validated": true, "status": "completed", "head", "base", "range", "handle", "kind", "number", "restricted", "clean", "blocked", "findings": [{"key", "level", "security"}], ...}`, `findings` being those of level `medium` or higher | `broker.review_gate` and `broker.validated_review_head` (the push check, the pre-mortem's precondition) |
| `premortem.json` | After an accepted pre-mortem: `{"validated": true, "findings": [{"id", "level", "title", "body", "security"}], ...}`, `id` the finding key, `security` from `broker.SECURITY_KEYWORDS` | `broker.read_premortem` (risk issues) |
| `worker-decisions.json` | The latest decision per key with at most five earlier ones (`MAX_HISTORY`), location and explanation cut to the session's limits; at most 1,000 decisions per run | The push gate; #347 (the PR sections) |

Records are masked with the decoy markers and every known secret of the run (the Codex API key
included); an output that tripped the tripwire keeps only its SHA-256. The raw output's SHA-256
is computed before masking; a record whose output masking changed says `masked: true`.

**Decisions.** A finding's key is the first 16 hex digits of the SHA-256 of
`[location, explanation]` as canonical JSON. `worker decide` takes `fixed` or `declined` for a
review or verification finding and `fixed`, `mitigated` or `known_risk` for a pre-mortem finding,
applied to the latest accepted result that holds the key. After every decision and every
finished worker the runner writes `worker-log.json` (mode 0400) into the session's state copy, a
directory the session can read but not write: every task's parsed state with its findings, and
the decisions, for recovery after a compaction.

## The push gate

`broker.review_gate` reads `validated-review.json` and `worker-decisions.json`; the broker's push
check and the pre-mortem's precondition take its head (`validated_review_head`). It opens only
when the record says `blocked: false` and either it is `clean` with no finding of level `medium` or
higher, or every such finding is `medium`, has `security: false` and was `declined` with a reason
by a decision that names this review's handle (the latest such decision on its key, current or
in its history). A `critical`, `high` or security finding keeps the gate closed even when
declined: it stays open until a later review accepts the reason by not reporting it again. A
`fixed` decision does not open it either, since the fix is a new head that needs its own review.
A missing or malformed field closes the gate, and so does a record of an earlier service.
