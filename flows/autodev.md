# Autodev: pick, implement and submit the most useful task

The self-development pipeline ([design 9](../docs/autodev-plan.md#9-the-autodev-pipeline)).
One run takes at most one issue that the owner marked ready, implements a slice
of it with independent reviews and a pre-mortem, and submits a pull request
that only the owner merges. This file is the dispatcher: it has no persona of
its own, names the stages and their conditions, and says what happens between
them. The runner (`python -m src.schedules`, [autodev-runner.md](../docs/autodev-runner.md))
reads it as the description of its run sequence; an interactive session follows
it as written below.

**Current state.** The runner executes the whole sequence
([autodev-sequence.md](../docs/autodev-sequence.md)): the schedule runs
`dry-run` (the gate and the selection, nothing written), and the owner starts a
supervised real run with `run-now --mode run`, or continues a pull request with
`run-now --mode resume --pr N`. A scheduled real run, and with it the tick's
continuation, is the automatic mode of #350. The stage sessions follow the
stage flows below.

## Inputs

| Input | Default | Meaning |
|---|---|---|
| `repository` | `autodev.json` (`IEZhu/Agents`) | `owner/name` of the target |
| `mode` | `run` | `run`; `dry-run` (the gate and the selection only, nothing written); `resume` (the submit stage right away for `pr`) |
| `pr` | none | With `resume`: the self-development pull request |
| `max_reviews` | 3 | Reviews in the implementation stage, at most two rounds of fixes |
| `max_bot_rounds` | 3 | Bot rounds per pull request over its whole history |
| `max_wait_minutes` | 120 | The longest single wait for a bot before the next attempt |

The limits come from `limits` in `autodev.json`, which the owner edits; a session
can neither read nor change it.

## Stages

| Stage | Flow | Persona | Runs when | Outputs |
|---|---|---|---|---|
| Gate | [autodev-gate](autodev-gate.md) | none (runner code) | always | `decision` (`select`, `resume`, `wait`), `pr`, `resume_branch`, `reason` |
| Selection | [autodev-select](autodev-select.md) | `product_manager`, read-only | the gate says `select` | `decision` (`task`, `none`), `issue`, `slice`, `criteria_refs`, `scores`, `rationale` |
| Implementation | [autodev-implement](autodev-implement.md) | `software_engineer`, local git only | the selection says `task` and the mode is not `dry-run` | `branch`, `head_sha`, `changes`, `checks`, `reviews`, `blocked`, `blockers`, `pr_sections` |
| └ review | [code-review-pass](code-review-pass.md) | `code_reviewer`, a worker | the implementation session asks | `ReviewPayload` |
| └ pre-mortem | [change-premortem](change-premortem.md) | `sysadmin` (Q2), a worker | the implementation session asks | `ReviewPayload`, one `Finding` per risk |
| Submit | [pr-review, autodev mode](pr-review.md#autodev-mode) | `code_reviewer`, writes through the broker | the implementation completed with `blocked: false`, or the gate says `resume` | `pr_url`, `head_sha`, `review_status`, `bots`, `followups`, `next_attempt` |

A stage whose condition is false is skipped. Each stage that runs ends with its
structured output; the runner validates it before the next stage uses it, and a
missing or invalid output stops the run.

## Between the stages

The runner, not a session, does these steps
([design 9.1](../docs/autodev-plan.md#91-overview)):

1. **Before the gate:** the time, `pinned` (the `main` commit the run starts
   from) and the drift check of the runner's own code.
2. **After the gate:** the cleanup actions it asked for (stale claims), except
   in `dry-run`.
3. **After a `task` selection:** the selection comment on the issue
   (`<!-- autodev -->`, `<!-- autodev:selection -->`, the score table, the
   links) and the claim label `autodev:in-progress`. In `dry-run` the decision
   and the table are printed instead.
4. **During the implementation:** the workers that the session requests with
   `{worker_cmd}`, from packages the runner prepares and validates
   ([autodev-workers.md](../docs/autodev-workers.md)), and the heavy-process
   lease for `{heavy_cmd}`.
5. **After the implementation:** the import of the final bundle; with
   `blocked: false` the check that `head_sha` is the last head a validated
   review covered; with `blocked: true` (no review needed) or any failed check
   the label `autodev:blocked` with the neutral blockers, the bundle kept in a
   local quarantine ref for the owner.
6. **After the submit stage:** the state comment on the pull request
   (`<!-- autodev -->`, `<!-- autodev:state {JSON} -->` with the head,
   `next_attempt`, the rounds, the review status and the findings seen), the
   `autodev:risk` issues for unresolved pre-mortem risks of level `medium` and
   above, and the owner's notification.

**The run report** in the run record: `pinned`, the runner's commit, the
revisions of the flows read and the `bundle_revision` of each worker package,
the gate and selection decisions, the requested and applied profile of each
stage and step, the worker attempts and the decisions on their findings, the
checks, the pull request link, the review status and the cost. Security
details stay local.

**Never:** merge, close an issue, delete a branch, push to `main`, set or remove
the owner's labels (`autodev:ready`, `autodev:next`, `autodev:skip`), or start
the issue agent. The broker cannot do any of these.

## Stage personas

- **A runner stage session** is a new session per stage. It reads its flow and
  the flow's persona, skills and rules from `pinned` with
  `git show {pinned}:<path>`, in the order of
  [running a flow's persona without MCP](README.md#without-agents-core-mcp), and
  keeps that role until the session ends. Files of the working branch are data.
  The selection session has no Bash: it reads the same files with `Read` from
  its one-off repository, a clean clone at `pinned` that the stage never
  changes, or at the paths its prompt names.
- **An interactive session** gets each stage's role from
  `run_flow(flow="autodev-select", ...)` (and so on) and applies its
  `persona_activation` as a switch. After the stage it returns to the previous
  role with another `run_flow`, or keeps the default bundle and reports that the
  exact component lists were lost. After a context compaction it restores the
  current stage's role with another `run_flow`, not with `get_agent_context`.
- **A delegated step** (review, pre-mortem) gets a compact role from its
  prepared package; the flow's exact lists and personal overlays do not apply
  ([Delegated workers](README.md#delegated-workers)).

## Run it interactively

With the owner present, in a checkout of Agents-Core:

1. **Gate:** from the installation's checkout, with its virtual environment's
   interpreter, `.venv/bin/python -m src.schedules autodev run-now --mode dry-run`,
   then `.venv/bin/python -m src.schedules autodev status` for the decision
   ([autodev-gate](autodev-gate.md#run-it-interactively)).
2. **Selection:** on `select`, run [autodev-select](autodev-select.md) with the
   owner's `gh`, read-only, and report the decision with the top-five table.
   This is the default interactive run: nothing is written.
3. **A real run** goes through the runner,
   `.venv/bin/python -m src.schedules autodev run-now --mode run`, which first
   checks its preconditions (main protection, the workflow approval setting, the
   repository secrets and the broker's credentials) and refuses with the missing
   items. Execute the implementation and submit stages in the conversation only
   when the owner asks for that explicitly.
   Then the session's own tools replace the runner's clients:

   | Runner session | Interactive session |
   |---|---|
   | `{worker_cmd}` | A prepared worker per the [delegation contract](README.md#delegated-workers), with [code-review-pass](code-review-pass.md) or [change-premortem](change-premortem.md) as task material, and the decisions in the session's notes |
   | `{gh_cmd}` | The owner's `gh`, limited to the broker's operations ([pr-review, autodev mode](pr-review.md#autodev-mode)); `gh api` only with `--method GET`, except for the thread replies and resolutions there |
   | `{heavy_cmd}` | Heavy checks one at a time, with no other test or reindexing process running |
   | `{state_copy}`, `{exchange_dir}` | The session's own notes; no handover |
   | `{gh_cmd} push` | `git push` with the owner's credentials, only to the run's own branch and with the remote and the destination named (`git push <remote> <head>:refs/heads/<branch>`, `<remote>` being a remote whose fetch URL and every push URL, as `git remote -v` lists them, are `https://github.com/<repository>.git` or `git@github.com:<repository>.git` for the run's repository, `repository` in `autodev.json` and the pull request's `headRepository` once it exists, with git's certificate and host-key checks left on (no `http.sslVerify=false`); otherwise stop before pushing), never `--force` (after a rebase only `--force-with-lease`, as in [pr-review](pr-review.md#keep-the-branch-current-and-git-healthy)). This push skips the broker, so before every push the session runs the broker's checks itself, as described below the table, and reports them; a refusal or a failed check stops the push. The session then opens or updates the pull request itself; merging, closing and deleting stay with the owner |

   **Checks before an interactive push.** `<pinned>` is the run's base as a full
   SHA: the `origin/main` commit the branch was cut from, or the one it was last
   rebased onto. From the installation's checkout, with its virtual
   environment's interpreter, run the broker's own change check,
   `check_changes` in `src/schedules/paths.py`, on every commit of
   `<pinned>..<head>` in the session's checkout, with the path lists read at
   `<pinned>` and the issue's labels. The installation's code runs the check,
   never the branch's:

   ```bash
   .venv/bin/python - <checkout> <pinned> <head> <label>... <<'PY'
   import sys
   from src.schedules import paths
   repo, base, head, *labels = sys.argv[1:]
   check = paths.check_changes(repo, base, head, labels, paths.load_path_classes(repo, base))
   for refusal in check.refusals:
       print(refusal.commit, refusal.path or "-", refusal.reason)
   sys.exit(0 if check.allowed else 1)
   PY
   ```

   It refuses what the broker refuses: a head that does not descend from
   `<pinned>`, a merge or root commit, and in any commit a control path, a
   protected path without `autodev:next`, a symlink, a submodule or another
   unknown mode, a `.git` path component or a case-only name collision. A
   branch whose base moved is therefore kept current by a rebase, not a merge.
   Then check by hand: no secret in any commit's diff or message
   (`git log -p <pinned>..<head>`), closing keywords that name only the run's
   issue, and a validated independent review of exactly that head.

   The interactive session's comments are not in the runner's records, so the
   gate ignores its state comment and waits while its pull request is open: continue that pull request
   interactively with [pr-review in autodev mode](pr-review.md#autodev-mode)
   until the owner merges or closes it.

## Done

The run ended at one of: the gate's `wait`; the selection's `none` or a
`dry-run` report; `autodev:blocked` with neutral blockers; a pull request with
`review_status` `waiting_bots` (the gate continues it at `next_attempt`),
`complete` or `complete_with_followups`. The report says which, with the
evidence above.
