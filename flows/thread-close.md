---
persona:
  agent: investigative_analyst
  skills: [skill-content-structure, skill-fact-verification, skill-forensic-process, skill-temporal-validation, skill-confidence-markers, skill-git-conventions, skill-dense-summarization]
  implants: [implant-chain-of-verification, implant-uncertainty-quantification, implant-narrative-of-thought]
---
# Close a thread without losing its work or keeping its errors

This is an executable instruction for an AI at the end of a conversation (a
thread): before `/clear`, before switching to an unrelated task, or when the user
asks to close it. It inventories what the thread produced, checks each result
against the current state and gives it an evidence-based verification level. It
audits the thread for its own mistakes, secures unsaved work and records one
report.

The thread's own account is a lead, never proof. A long, expensive thread can
still be wrong. Its summary may predate later changes, and a compacted context
may have dropped what actually happened.

## How to invoke

```text
Run flows/thread-close.md.
Run flows/thread-close.md in apply mode.
```

Through Agents-Core MCP, call `run_flow(flow="thread-close", request="mode: apply")`
and apply its `persona_activation` first. Run the flow in the session being
closed: it needs that session's tools and context. Another session can close a
thread only from the transcript (Claude Code) and the live state, and its report
must say so.

## Inputs and authority

- **Mode.**
  - `draft` (default) reads and reports and writes nothing.
  - `apply` also writes the history entry and memory items at level V2 or above,
    and removes verified-safe leftovers (step 5).
- **Thread.**
  - In Claude Code, read the session's transcript with
    `scripts/dev/thread_inventory.py --session <id>` from the Agents-Core
    installation. The id is the directory name above the session's scratchpad.
    `--project-dir <dir> --latest` lists other recently active sessions: confirm
    the right one before using it.
  - Other clients have no transcript to read. Reconstruct the thread from the
    conversation and the live state, and state in the report that no transcript
    was read.
- **Scope.** Every repository and system the thread touched; the user can narrow it.
- **Authority.** Invoking the flow allows reading everything the thread touched
  and, in `apply` mode, the writes named above. Everything else needs the user's
  explicit yes in this session:
  - commits, pushes and merges;
  - creating or closing issues and pull requests;
  - deleting anything that step 5 does not verify as safe;
  - writing to external systems (trackers, chat, schedulers);
  - memory below V2.

  Never merge, force-push or rewrite history as part of closing.

## Verification levels

| Level | Meaning | Typical evidence |
|---|---|---|
| V0 Claimed | Only the model's words | "tests pass" with no command or output found |
| V1 Read | A source was opened and supports it | file content, documentation, an API response |
| V2 Executed | A command, test or call produced the result | the command and its output, with time and commit |
| V3 Independently checked | A separate reviewer found no contradiction: fresh-context agent, review bot or human | review or verifier output tied to the current state |
| V4 Accepted | Merged or applied by the owner and confirmed afterwards | merge commit, running service, the owner's use |
| ✗ Refuted | Shown wrong, during the thread or by this audit | the later evidence |
| ⚠ Stale | Was supported, but the subject changed afterwards | a test run before the last commit, a review of an older head |
| ? Contested | Evidence conflicts | both sources |

Rules:

- **Evidence comes from the close, not from narration.** Assign each level from
  evidence seen during the close, or found in the transcript with its time.
  Narration alone never raises a level: "tests pass" in the thread is V0 until
  the command and its output are found or the tests are re-run.
- **One-line justification.** Name the command, file, pull request state or
  review behind every level, with its time or commit.
- **Doubt lowers the level.** When unsure between two levels, take the lower. When
  sources conflict, mark the item `?`.
- **Same scale as the skills.** V1 and V2 correspond to HIGH in
  `skill-confidence-markers` ("checked this turn"). V3 needs independence, as
  `VERIFIED` does in `skill-fact-verification`.
- **Provenance is recorded, never scored.** Record the models, number of
  responses, token totals, session id, time span and client. They describe effort
  and capability, not correctness, and never set a level. At most they decide
  what to review first: output of a small model, or a long correction loop.

## 1. Establish the scope

1. Read the current time with `date -u`. Fix the mode and identify the thread.
2. In Claude Code, run the inventory helper. Keep its `notes`: other active
   sessions in the same project mean you must confirm which session is being
   closed.
3. Work out which repositories to check: the helper's `git_roots` plus any named
   in the conversation. For each one, collect:
   - `git status --short`, the current branch, `git stash list` and
     `git worktree list`;
   - local branches ahead of their upstream:
     `git for-each-ref --format='%(refname:short) %(upstream:track)' refs/heads`;
   - the commits the thread made.

## 2. Inventory the artifacts

Build one table with columns artifact, kind, location, and what the thread
claimed about it. Cover:

- **Code and documents.**
  - Commits, branches and stashes.
  - Worktrees and uncommitted or unpushed changes.
  - Pull requests, with state, head and reviews; issues, with state.
- **Configuration outside git.**
  - Schedulers and cloud routines; settings and services.
  - Personal libraries, such as Agents-Core `flows/.user`; environment.
- **Knowledge.**
  - Decisions with their reasons, and options rejected and why.
  - Preferences the user stated.
  - Lessons from mistakes; open questions.
- **Memory** written during the thread: files and what changed.
- **External systems** the thread wrote to: trackers, chat, published pages,
  remote triggers. Take them from the helper's `external_writes`.
- **Session-local items**, which vanish with the session: scratchpad and
  temporary files, background tasks, monitors, scheduled wake-ups and cron
  jobs. Take them from `scratchpad_paths` and `background_tasks`. A task with
  no `ended` record may still be running.
- **Promises**: things the thread said it would do or check later, such as
  "after the pause I will request a review once".

Files edited through shell commands do not appear in `files_written`; the git
state covers them.

## 3. Verify against the current state

Check each artifact's live state:

- `git`, `gh pr view`, `gh issue view`;
- the scheduler's own read call;
- whether the file exists and what it contains;
- a cheap re-run of a check where the thread claimed a result.

Assign the level with its evidence. Mark an item ⚠ when the evidence predates a
later change. Note changes the thread did not make, such as a pull request the
owner merged. In `draft` mode, run only reads and checks without side effects.

## 4. Audit the thread itself

1. **Claims.** List the load-bearing claims the thread made to the user: facts,
   numbers, statuses, causes and recommendations, mainly from its final answers.
2. **Corrections and contradictions.**
   - Read the helper's `corrections`: they are candidates, not conclusions.
   - Find decisions the thread reversed and statements contradicted by later turns.
   - The claims they invalidate become ✗, with the later evidence.
3. **Fresh-context verifier.** Give a subagent (Agent or Workflow tool) the claim
   list and the sources, but not the conversation. Ask it to refute each claim
   from the sources only, defaulting to refuted when it cannot confirm.
   - Refuted claims become ✗, or `?` when the evidence conflicts.
   - A claim reaches V3 only if the verifier actually checked its sources.
   - Without subagents, re-check the most important claims yourself and say that
     no independent verifier ran. Nothing then reaches V3 through this step.
4. **What the thread got wrong.** List each error, its effect, whether it was
   fixed, and the lesson.

## 5. Secure the work

- **Uncommitted or unpushed changes.** For each, propose a commit and push to the
  right branch under the repository's rules, or an explicit drop. Never decide
  silently; act only on a yes.
- **Scratchpad and temporary files.** Name what is still needed (drafts, plans,
  generated data) and propose a lasting place for it: repository documentation, an
  issue, memory or a published page. The rest is disposable.
- **Background work.** Stop tasks, monitors, wake-ups and cron jobs whose purpose
  has ended. List those that must keep running and who consumes their output.
- **Promises and open loops.** Give each an owner and a trigger: an issue or
  tracker item (on a yes), a follow-up command, or an explicit "dropped".
- **Cleanup (`apply`).**
  - Remove a worktree only when it is clean and its branch head equals the head
    of a merged pull request, or the default branch contains it.
  - Delete a local branch only under the same condition.
  - List everything else instead of removing it.
- **No new stores.** Use the existing ones.

## 6. Persist (`apply`)

- **History.** Make one `log_interaction` call:
  - `intent`: `Close thread: <title>`;
  - `action`: `Flow: thread-close (apply)`;
  - `outcome`: the report summary with each artifact's level;
  - `files`: the artifact paths;
  - `tags`: `#thread-close` and `#session-<id>`.

  `history.md` is append-only, so a re-run appends a new entry that names the
  entry it supersedes.
- **Memory.** Only durable items at V2 or above: preferences, lessons, and project
  facts the repository does not already record.
  - Update an existing memory instead of adding a duplicate, and give each item
    its date and level.
  - Items below V2 go to a "to verify" list, each with the check that would
    verify it.
  - If the client has no durable memory, say so.
- **Issues, tracker items and documentation.** Propose follow-up issues and
  tracker items, and create them only on a yes. Propose repository knowledge as a
  documentation change through the repository's pull request flow.
- **Secrets and personal data.** Scan everything you persist and replace secrets
  with references. Keep medical, financial and other personal details out of
  cloud logs and shared stores unless the user has allowed them.
- **Hand-off keys.** List the session id, pull request and issue numbers, and
  branch names, so that daily summaries such as a personal day-close flow can
  match this work instead of recording it twice.

## 7. Report

Report in the user's language:

1. **Summary** in three to five lines: what was achieved, and how well it is
   supported overall.
2. **Artifacts:** a table of artifact, location, level and evidence.
3. **What this thread got wrong.**
4. **Unsaved work and decisions needed**, each with a proposed action.
5. **Open loops**, each with owner and trigger.
6. **Persisted** (`apply`) or **would persist** (`draft`): the history entry,
   memory changes and cleanup.
7. **Provenance**, labelled as such: models, responses, tokens, session, time span.
8. **How to resume:** session id or link, branch, next command.

## Done when

- Every artifact has a level with evidence, or is marked unverified.
- The audit ran with an independent verifier, or the report says why not.
- No unsaved work is left without a decision.
- Background work is stopped or its continuation is justified.
- In `apply` mode, the history entry exists and every memory change is V2 or above.
- The report is delivered.

The flow does not prove a thread right. It records how well each result is
supported at the moment of closing, and a later change can make that stale.
