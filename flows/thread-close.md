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

The persona contributes fact-checking discipline. Its own investigation phases and
"Verdict"/credibility output format do not apply: this flow's steps and its report
in step 8 do.

## How to invoke

```text
Run flows/thread-close.md.
Run flows/thread-close.md in apply mode.
```

Through Agents-Core MCP, call `run_flow(flow="thread-close", request="mode: apply")`
and apply its `persona_activation` first. Run the flow in the session being
closed: it needs that session's tools and context. Another session can close a
thread only from the transcript and the live state, and its report must say so.

## Inputs and authority

- **Mode.**
  - `draft` (default) reads, checks and reports. Its only write is the usual
    per-turn log the client makes anyway.
  - `apply` also writes memory items at level V2 or above, curates the closing
    history entry, and does the cleanup that step 7 verifies as safe.
- **Thread.**
  - **Claude Code.** Read the session's transcript with the inventory helper
    [`scripts/dev/thread_inventory.py`](../scripts/dev/thread_inventory.py) from
    the Agents-Core checkout. Run it as
    `python "$AGENTS_CORE_ROOT/scripts/dev/thread_inventory.py" --session <id>`.
    - The id is the directory name above the session's scratchpad.
    - `--project-dir <dir> --latest` lists other recently active sessions;
      confirm the right one before using it.
    - The helper also reads the transcripts of the session's subagents and
      workflow agents and marks their items with `by`.
  - **Finding the helper from another repository.** Derive `AGENTS_CORE_ROOT` as
    the [pr-review helpers](pr-review.md#optional-github-helpers-from-the-agents-core-installation)
    describe:
    - when `flow.source` is `builtin`, the parent of `flow.source_path` is the
      checkout's `flows/`;
    - for a personal copy, use the `builtin:thread-close` entry of `list_flows()`.

    Do not look for `scripts/dev/` in the target repository.
  - **Other clients.** They keep their own logs; Codex, for example, keeps
    rollouts under `~/.codex/sessions`. The helper does not read those. Read them
    directly when available, otherwise reconstruct the thread from the
    conversation and the live state. State in the report which source you used.
- **Scope.** Every repository and system the thread touched; the user can narrow it.
- **Authority.** Invoking the flow allows reading everything the thread touched
  and, in `apply` mode, the writes named above. Everything else needs the user's
  explicit yes in this session:
  - commits and pushes;
  - creating or closing issues and pull requests;
  - stopping background work in `draft` mode;
  - deleting anything that step 7 does not verify as safe;
  - writing to external systems (trackers, chat, schedulers);
  - memory below V2.

  Never merge, force-push or rewrite history as part of closing.

## Verification levels

| Level | Meaning | Typical evidence |
|---|---|---|
| V0 Claimed | Only the model's words | "tests pass" with no command or output found |
| V1 Read | A source was opened and supports it | file content, documentation, an API response |
| V2 Executed | A command, test or call produced the result | the command and its output, with time and commit |
| V3 Independently checked | A separate reviewer checked the current state and found no contradiction: fresh-context agent, review bot or human | review or verifier output tied to the current head or state |
| V4 Accepted | The claim is at V2 or above, and the owner merged or applied it and it held afterwards | merge commit plus the V2 evidence, a running service, the owner's use |
| ✗ Refuted | Counter-evidence shows it wrong, during the thread or in this audit | the counter-evidence |
| ⚠ Stale | Was supported, but the subject changed afterwards | a test run before the last commit, a review of an older head |
| ? Contested | Evidence conflicts, or a check could not be completed | both sources, or what blocked the check |

Rules:

- **Evidence comes from the close, not from narration.**
  - Assign each level from evidence seen during the close, or from evidence found
    in the transcript with its time.
  - Narration alone never raises a level: "tests pass" in the thread is V0 until
    the command and its output are found, or the tests are re-run.
  - Reports that subagents and reviewers gave during the thread are narration
    too, until their output is found. They reach V3 only for the head or state
    they actually reviewed.
- **One-line justification.** Name the command, file, pull request state or
  review behind every level, with its time or commit.
- **Doubt lowers the level; only counter-evidence refutes.** When unsure between
  two levels, take the lower. An item becomes ✗ only through evidence against it.
  A check that could not confirm an item leaves its level unchanged, or makes it `?`.
- **Same scale as the skills.**
  - V1 and V2 match HIGH in `skill-confidence-markers` ("checked this turn") and
    `VERIFIED` in `skill-fact-verification`: a primary source opened this turn.
  - V3 adds independence; V4 adds the owner's acceptance.
- **Preferences and lessons.**
  - A preference the user stated has the user's own words as primary evidence:
    record it as the user's statement, with the prompt's time. A V2 memory may
    hold it.
  - A lesson may go to memory only when the error it comes from is evidenced at
    V2 or above: a command, its output or a review shows the error happened.
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
   - commits that exist on no remote: `git log --branches --not --remotes --oneline`.
     This, not the upstream, decides whether work is pushed. Confirm a branch
     with `git branch -r --contains <branch>`: an empty result means its head is
     on no remote branch;
   - each branch's upstream, for information only:
     `git for-each-ref --format='%(refname:short) %(upstream:short) %(upstream:track)' refs/heads`.
     A missing upstream says nothing about pushing (`git push origin <branch>`
     without `-u`, or a `--no-track` branch), and an empty track column does not
     mean "in sync";
   - the commits the thread made.

## 2. Inventory the artifacts

Build one table with columns artifact, kind, location, made by (main session,
subagent or user), and what the thread claimed about it. Cover:

- **Code and documents.**
  - Commits, branches and stashes.
  - Worktrees and uncommitted or unpushed changes.
  - Pull requests, with state, head and reviews; issues, with state. Use the
    helper's `github_refs`.
- **Configuration outside git.**
  - Schedulers and cloud routines; settings and services.
  - Personal libraries, such as Agents-Core `flows/.user`; environment.
- **Knowledge.**
  - Decisions with their reasons, and options rejected and why.
  - Preferences the user stated.
  - Lessons from mistakes; open questions.
- **Memory** written during the thread: files and what changed.
- **External systems** the thread wrote to: trackers, chat, published pages,
  remote triggers. Use `external_writes`. Also inspect `unclassified_tools`:
  these are MCP calls whose names did not say whether they write.
- **Scheduled work**: wake-ups and cron jobs, from `scheduled`.
- **Session-local items**, which vanish with the session:
  - scratchpad and temporary files (`scratchpad_paths`);
  - background tasks, monitors and agents (`background_tasks`). One without an
    `ended` record may still be running.
- **Shell commands the user typed** (`user_commands`), which can change state as
  well.
- **Promises**: things the thread said it would do or check later, such as
  "after the pause I will request a review once".

The helper sees files changed by edit tools. Files changed by shell commands are
covered by the git state only inside repositories. Outside them, check the paths
named in the commands.

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
   list and the sources, but not the conversation. Ask it to check each claim
   against the sources and to return confirmed, refuted with counter-evidence, or
   unconfirmed with what blocked the check.
   - Refuted claims become ✗.
   - Unconfirmed claims keep their level, or become `?`.
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
- **Background and scheduled work.** List what still runs and who consumes its
  output. In `apply` mode, stop tasks, monitors, wake-ups and cron jobs whose
  purpose has ended; in `draft` mode, propose it.
- **Promises and open loops.** Give each an owner and a trigger: an issue or
  tracker item (on a yes), a follow-up command, or an explicit "dropped".
- **No new stores.** Use the existing ones.

## 6. Persist durable knowledge (`apply`)

- **Memory.** Only durable items at V2 or above, as defined above: preferences,
  lessons, and project facts the repository does not already record.
  - Update an existing memory instead of adding a duplicate, and give each item
    its date and level.
  - Items below V2 go to a "to verify" list, each with the check that would
    verify it.
  - If the client has no durable memory, say so.
- **Issues, tracker items and documentation.** Propose follow-up issues and
  tracker items, and create them only on a yes. Propose repository knowledge as a
  documentation change through the repository's pull request flow.
- **Secrets and personal data.** Scan everything you persist or report, and
  replace secrets with references. Keep medical, financial and other personal
  details out of cloud logs and shared stores unless the user has allowed them.
  The report is logged in both modes, so this applies to the report too.

## 7. Clean up (`apply`)

Remove a worktree only when all of these hold:

- `git status --short --ignored` lists nothing except regenerable caches
  (`__pycache__`, `.pytest_cache`, `.mypy_cache`, `.ruff_cache`). `git worktree
  remove` deletes ignored files such as `.env`, data and logs.
- Its branch head equals the head of a merged pull request, or the default
  branch contains it.
- It is not the current working directory, the MCP workspace root or the
  Agents-Core installation.
- No other session, open task or monitor uses it.

Delete a local branch only under the same branch condition. When any condition is
uncertain, list the item instead of removing it.

## 8. Report and close the turn

Compose the report in the user's language:

1. **Summary** in three to five lines: what was achieved, and how well it is
   supported overall.
2. **Artifacts:** a table of artifact, location, made by, level and evidence.
3. **What this thread got wrong.**
4. **Unsaved work and decisions needed**, each with a proposed action.
5. **Open loops**, each with owner and trigger.
6. **Persisted** (`apply`) or **would persist** (`draft`): memory changes and
   cleanup.
7. **Provenance**, labelled as such: models, responses, tokens (main session and
   subagents), session, time span, transcript source.
8. **How to resume:** session id or link, branch, next command.

Then make this turn's single `log_interaction` call, as the client protocol
requires for every answer. Do not make a second call:

- `agent_name` and `persona`: the active persona;
- `persona_action`: `switch` when the flow's `persona_activation` returned
  `SUCCESS` in this turn, `keep` when it returned `NO_CHANGE` or `ERROR`;
- `query`: the user's request, verbatim;
- `response_content`: the report exactly as it will be delivered.

In `apply` mode, also curate the call:

- `intent`: `Close thread: <title>`;
- `action`: `Flow: thread-close (apply)`;
- `outcome`: the summary and the count of artifacts per level;
- `files`: the artifact paths;
- `tags`: `#thread-close` and `#session-<id>`.

`history.md` is append-only. Before closing a thread again, look for its earlier
close with `read_history`, which returns recent or similar entries without a
session filter. Accept an entry only when its tags contain both `#thread-close`
and this thread's `#session-<id>`, and name that entry's time in `outcome`. If
none matches, say that no earlier close was found. Without Agents-Core MCP there
is no history entry; say so in the report.

**Hand-off keys.** List the session id, pull request and issue numbers, and
branch names in the report, so that daily summaries such as a personal day-close
flow can match this work instead of recording it twice.

## Done when

- Every artifact has a level with evidence, or is marked unverified.
- The audit ran with an independent verifier, or the report says why not.
- No unsaved work is left without a decision.
- Background work is stopped or its continuation is justified.
- In `apply` mode, every memory change is V2 or above and the closing log entry
  is curated.
- The report is delivered.

The flow does not prove a thread right. It records how well each result is
supported at the moment of closing, and a later change can make that stale.
