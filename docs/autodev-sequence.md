# Autodev run sequence

What one autodev run does from the lease to the notification (issue #347, epic #336). The design
is [autodev-plan.md](autodev-plan.md), sections 5.3, 5.7, 5.8, 9 and 10, as amended by #347; the
dispatcher flow is [flows/autodev.md](../flows/autodev.md). The pieces it joins have their own
references: the [runner](autodev-runner.md), the [broker](autodev-broker.md), the
[sandbox](autodev-sandbox.md), [Codex](autodev-codex.md), the [workers](autodev-workers.md) and the
[guards](autodev-guards.md).

| Code | What |
|---|---|
| `src/schedules/sequence.py` | The run: the steps, one stage session, the checks between stages, the records, the end |
| `src/schedules/candidates.py` | The select stage's candidates, read with GET requests only |
| `src/schedules/stages.py` | The stage schemas, the output check, the prompts, the inputs files, the stream parser |
| `src/schedules/preconditions.py` | What a real run needs before it starts |
| `src/schedules/run_state.py` | `state.json` and its read-only copy |
| `src/schedules/report.py` | The run report and the owner's notification |
| `src/schedules/integrity.py` | The post-session check of a one-off repository's `.git` |
| `src/flow_execution.py` | The effort values each client takes |
| `tests/test_schedules_sequence.py` | Whole runs with fakes ([tests/README.md](../tests/README.md)) |

## Modes

| Mode | Started by | What it does |
|---|---|---|
| `dry-run` | the schedule, or `autodev run-now` | The gate and, on `select`, the select stage; prints the decision and the top-five table. No broker exists, so nothing is written to GitHub and no `broker.jsonl` appears. The preconditions are not checked. |
| `run` | `autodev run-now --mode run` | The supervised real run: preconditions, gate, select, implement, submit. |
| `resume` | `autodev run-now --mode resume --pr N` | The submit stage again for an open self-development PR, when the gate agrees. A `run` whose gate decides `resume` takes the same path. |

A scheduled real run is the automatic mode of #350: `enable --mode run` refuses, `ready` names it
missing, and a tick refuses a `run` mode it finds in `autodev.json`. The sequence still writes the
tick's `next_attempt`, so #350 needs no change there; until then the notification of a
`waiting_bots` run names the command that continues it.

## Order

1. **The lease and the markers** (`runner.run_autodev`): the run lease, the daemon's update
   markers.
2. **The drift check** (`runner.default_drift`): `main` is fetched into the trusted repository,
   its quarantine older than 30 days is pruned, and the installation is compared with `pinned`.
3. **The preconditions** (real modes only, `preconditions.check`): the broker's credentials,
   main's protection rules, workflows that cannot approve pull requests, and no repository secret
   `CLAUDE_ROUTINE_TOKEN`. Anything missing ends the run `refused` with the list of items, before
   the gate, any label or any session.
4. **The gate** on a read-only snapshot (`gate.gate`). `wait` ends the run; in a real run the
   gate's cleanup actions run first through the broker.
5. **The candidates** (`candidates.fetch_candidates`): every open issue labelled `autodev:ready`
   or `autodev:next`, with the fields of the select flow's inputs table. No candidate ends the run
   with the decision `none` and no session.
6. **The select stage.** A dry-run ends here. A real run with a `task` decision binds its broker
   to the issue (`Broker.bind_issue`), posts the selection comment with the score table and sets
   `autodev:in-progress` (`Broker.select`), and records the comment's ID.
7. **The implement stage** on a one-off repository at `pinned`, with the worker service and the
   broker on the runner socket.
8. **After the implement stage** (below): submit, or `blocked`.
9. **The submit stage** on a one-off repository that holds the run's branch, with the same worker
   service importing from the submit stage's exchange directory.
10. **After the submit stage** (below).
11. **The end** (below).

A resume replaces steps 5 to 8: the PR's issue and branch come from the gate's snapshot, the
issue's labels from GitHub, and the broker is bound with `bind_issue` and `bind_resume`. The
trusted repository's branch head must be the PR's head; otherwise the run ends `blocked` with
"the pull request's head is not the head this runner pushed" before any session (Q-resume-head).
The base is `pinned` when the head descends from it, else the merge base of the two (Q-resume-push):
the broker's change check, the outgoing scan and the bundle imports work against that base, the
path lists are still read at `pinned`, and there is no force push. A fresh worker service starts
with its push gate closed, so the first push needs a new review in this run. The submit inputs
add the PR, its rounds, the findings already seen and the head of the trusted state comment.

## A stage session

`Sequence.stage` builds and runs one session in this order:

1. The client (`stages.stage_profile`): a stage configured for Codex runs as Codex only for
   `select` and only while the stored availability says so; `implement` and `submit` fall back to
   Claude with `codex_stage_commit_unsupported` (a `workspace-write` Codex session cannot write
   `.git`, Q-codex-commit), and an unavailable Codex with its reason (Q-codex-fallback). The
   record keeps `client_requested`, `client_applied` and `fallback_reason`.
2. The effort and model checks (`flow_execution.check_effort`, `check_model`) against the
   capability table read from the installed CLIs; a value the client does not take refuses the
   run with `flow_invalid` and the allowed values.
3. The layout (`sandbox.session_layout`): `<sessions>/<run-id>/<kind>/` with `repo`, `exchange`,
   `gh` and `settings.json`, and one state copy per run, `<sessions>/<run-id>/state/`, shared by
   the stages. The one-off repository (`create_session_repository`, with the branch for submit)
   and the environment at `pinned` (`TrustedRepository.environment`).
4. The inputs file `<state copy>/inputs-<kind>.json` (read-only; the select stage's bodies and
   comments in `issues/<n>/`) and `state.json` with its read-only copy.
5. For a session with Bash, the runner socket with the registry of the run (the heavy-process
   lease, the broker's `gh.*` and the worker service's `worker.*` types), the stage's writable
   paths as `session_writable` and the session's root PID as `session_root`; the record's
   `endpoint` is written before the session starts.
6. `sandbox.build` with the stage's output schema as `--json-schema`, `probes.guard` (a probe
   session for a stage with Bash, the static checks for `select`), `probes.record_report`. A
   failed report refuses the run before the session.
7. A `.git` snapshot of the one-off repository (`integrity.git_snapshot`), the prompt
   (`stages.render_prompt`), and the session through `probes.start_session`, its standard output
   into `<run_dir>/stages/<kind>/stream.jsonl`. After the stage's timeout the runner ends the
   session's process group, and it ends whatever the session left in its group after a normal
   exit too.
8. `probes.stage_tripwire` on the stream (a decoy named or a marker: the schedule turns off, the
   run is `refused`); the `.git` check (a change: the schedule turns off, the run is `refused`,
   nothing more is imported, Q-git-integrity); the timeout (`failed: infra`); then the output:
   `parse_stage_stream` and `stages.validate_output` with the run's repository, issue, candidates
   and branch. An invalid or missing output ends the stage `invalid` and the run `blocked`.

A Codex select stage builds `codex_sandbox.CodexPaths` with the state copy as its read-only root
and the stage schema in it (`stages.write_schema`, `--output-schema`), decides with
`codex_probes.decide` (recorded as the step `stage-select`), starts through `codex_probes.start`,
reads the `-o` file and checks it the same way; a decision that falls back runs the Claude session
instead, and an abort refuses the run.

Timeouts (Q-timeouts): select 30 minutes, implement `stages.implement.budget.timeout_minutes`
(180 by default), submit 90; the supervisor's limit is their sum plus 30 minutes:
60 for a dry-run, 330 for a run, 120 for a resume.

## After the implement stage

`Sequence.after_implement` decides between the submit stage and `blocked`:

- the final handover bundle is imported (a refused bundle is a blocker);
- the change check runs on the imported head against `pinned` with the issue's labels
  (`paths.check_changes`, the lists at `pinned`);
- a valid, unblocked output must name the imported head, which must be the last head a validated
  review covered (`broker.validated_review_head`), and the pre-mortem must have been accepted
  (`premortem.json`);
- no security-marked pre-mortem risk may be open (`workers.open_security_risks`, design 5.8);
- a blocked output brings its own blockers.

Any blocker ends the run `blocked`. A control-path change (a refusal with `control_path`) is
exported first: `<run_dir>/export/<run-id>.patch` (`git format-patch`) and a verified
`<run-id>.bundle` (`repos.TrustedRepository.export_commits`), named in the record's `exports`, the
report and the notification. The imported branch moves to
`refs/autodev/quarantine/<run-id>/final`, the issue gets the neutral blocker list with
`autodev:blocked` (`Broker.block`), and the claim `autodev:in-progress` is removed
(`Broker.unclaim`, Q-claim) so the gate does not wait six hours for it. The blockers the runner
writes itself are fixed phrases: "the final head is not the head the session handed over", "no
validated review covers the final head", "the pre-mortem did not run on this change", "an open
pre-mortem risk needs the owner", "the change needs a control path; a local patch for the owner is
in the run records", "the change touches protected paths without autodev:next" and "the
implement stage ended without a valid result".

## After the submit stage

`Sequence.after_submit`, when the session opened or resumed a PR:

- the state comment (`Broker.state`): `head`, `next_attempt` (only with `waiting_bots`, at most 24
  hours ahead), `rounds`, `review_status` and the finding IDs; a new comment for a new PR, an edit
  of the comment an earlier run record names for a resumed one; its ID goes into the record's
  `comments` as `{"id", "kind": "state", "number": <pr>}`;
- risk issues for `workers.risk_candidates` (known risks of level `medium` and above without a
  security signal) through `Broker.create_risk_issues`, at most three per run;
- with `waiting_bots`, `next_attempt` in `schedules/tick.json` as `{"at", "pr"}`.

A submit stage that ends `blocked` (or without a valid output) blocks the issue with its reason;
the claim stays while a PR exists. A pull request is refused by the broker while a security risk
is open (`Broker(pr_allowed=...)`).

## Records

The run record (`schedules/runs/<run-id>/record.json`) gains, besides the fields the
[runner](autodev-runner.md#files) describes: `pinned`, `runner_commit`, `gate`, `candidates`
(count, numbers, how many were dropped over the cap), `selection` (the validated select output
with `top_five` and the Markdown `table`), `issue`, `branch`, `pr`, `pr_url`, `review_status`,
`next_attempt`, `head`, `comments`, `endpoint`, `probes` (one report per guarded session),
`stages` (per kind: `status`, `client`, `requested_client`, `fallback_reason`, `execution` with
`requested` and `applied`, `probe_digest`, `stream`, `output_sha256`, `exit`, `timed_out`,
`cost_usd`, `integrity`, `problems`), `capabilities`, `workers` and `clients` (from the worker
service), `checks` (the implement output's), `implement` (the imported head and the change
check), `blockers`, `exports`, `quarantined`, `risk_issues`, `report` and `notification`. Every
write goes through `records.update_record` and is masked with the run's known secrets (the decoy
markers, the broker's token and deploy key, the OAuth token, a Codex API key).

`<run_dir>/state.json` (`run_state.RunState`) holds the stage, the stages done, the issue, the
branch, the PR, `base`, `head`, the slice, `criteria_refs`, the flow revisions at `pinned` and the
limits; its read-only copy in the state copy is what a session re-reads after a context loss,
next to the worker service's `worker-log.json`.

The report (`report.build_report`) holds `pinned`, the runner's commit, the flow revisions, the
`bundle_revision` of each worker package, the gate and selection decisions, each stage's and each
step's `requested` and `applied` execution, the checks, the PR link, the review status, the risk
issues, the exports, the blockers, the cost the CLI reported and the security counts only.

## Statuses

| Status | When |
|---|---|
| `completed` | The gate waited, no candidate, the selection said `none`, a dry-run's selection, or a PR with `complete`, `complete_with_followups` or `waiting_bots` |
| `blocked` | A blocker after the implement stage, a blocked submit stage, an invalid stage output, a resume whose head is foreign |
| `refused` | A missing precondition, a failed probe, the decoy tripwire, a changed `.git`, a sandbox or login refusal, an effort the client does not take |
| `failed: infra` | The network, a CLI, the lease, drift, a timeout, or a defect in the sequence itself |

The owner is notified of every real run and of a dry-run that did not complete (Q-notify-dry): a
title and a message of fixed phrases, counts, the PR link and the export paths, passed to
`osascript` as arguments of a constant script, never inside it.

## Before the first real run

These are the owner's steps; no test can make them:

1. The broker's credentials (Q15) and the token's read permissions for the preconditions
   (recalled, not verified: `Administration: read` for `actions/permissions/workflow` and
   `Secrets: read` for `actions/secrets`); without them the run refuses with `cannot verify ...
   (HTTP 403)`.
2. The live sandbox probes of every stage kind with the stage argv, now with `--json-schema`:
   `python -m src.schedules probe --kind select|implement|submit --confirm-live-probe`.
3. A dry-run that reaches the select session: `autodev run-now`; compare the printed top five
   with the owner's own ranking (three dry-runs that broadly match, per the select flow).
4. Optionally `codex-probe --kind stage --confirm-live-probe` (on this Mac Codex stays
   unavailable, so a Codex select stage falls back to Claude).
5. The first supervised real run: `autodev run-now --mode run`, watched. It checks what the tests
   only fake: that a stage session's result event carries `structured_output` with
   `--json-schema` and `stream-json` (and the `model` and `total_cost_usd` fields the report
   reads), that the thin clients start inside the sandbox and reach the socket, that the sandbox
   keeps the one-off repository's `.git` unchanged, and that the notification appears.
