# Playbook for agent sessions on this repository

Practices that held up in the 2026-09 sessions (the component ablation sweep and the
review rounds that followed). [AGENTS.md](../AGENTS.md) is the contributor entry
point; [CLAUDE.md](../CLAUDE.md) covers the routing protocol and the code layout.
This page covers how to work. For documentation maintenance, execute
[documentation-refresh.md](../flows/documentation-refresh.md). Related pages: [cloud-runs.md](cloud-runs.md)
for evals in cloud sessions, [`evals/ablation/README.md`](../evals/ablation/README.md)
for the ablation runbook, [`evals/telemetry/README.md`](../evals/telemetry/README.md)
for Langfuse analysis.

## Ground rules

- **Commit identity.** Check `git config user.email` in each checkout before the first
  commit; a repository-local setting overrides the global one. When amending, use
  `git commit --amend --reset-author` so author and committer both change.
- **Branches.** Use the requested base, or the current `HEAD` for a documentation
  refresh. When branching from a remote ref, use `--no-track`
  (`git worktree add --no-track -b codex/task-name <path> origin/main`), so a GUI client never
  pushes a feature branch to the upstream it was cut from. The repository deletes a
  PR's branch when it is merged.
- **Parallel work.** Give each independent change its own worktree, for example
  under `.worktrees/` or `.claude/worktrees/`. Check existing worktrees first and
  remove only your completed worktree once its changes are preserved and it is clean.
- **One heavy process at a time.** The development laptop has rebooted under parallel
  pytest runs and embedding-model loads. Run the full suite once, alone:
  `LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/ -q`.
  Run targeted test files while iterating; [tests/README.md](../tests/README.md)
  covers setup, slow tests and worktree isolation.
- **Keep eval outputs out of the repository and out of temporary directories** that
  get cleaned: `~/evals-runs/<name>/` has been the convention.
- **Secrets.** `.env` holds the Langfuse keys; the OpenRouter key for hosted A/B evals
  lives in a user-level env file, not in the repository. Never print a secret; a
  token that was pasted into a chat or printed in a log is rotated.
- **Prompt freshness.** Protocol 2 builds each issued bundle from current source
  content; an explicit refresh can update the active persona without restarting.
  A local `keep` retains the existing bundle. The running router and retrieval
  indexes are separate state: after changing agent membership, routing metadata
  or indexed skill/implant content or metadata, reload the serving process so
  those changes are indexed. Use the [daemon procedure](shared-mcp-daemon.md) for HTTP and reconnect
  the server for stdio. See [routing](routing_flow.md) for refresh semantics.

## PR review loop

Every PR goes through GitHub Copilot review until it has no concrete findings left;
CodeRabbit comments too, and its findings get the same treatment.

1. Request a review:
   `gh api -X POST repos/OWNER/REPO/pulls/N/requested_reviewers -f 'reviewers[]=copilot-pull-request-reviewer[bot]'`
2. Wait for it: `scripts/dev/wait_copilot.sh N [N ...]`. It waits until the PR head equals
   the pushed branch tip and a Copilot review exists on that commit.
3. Read everything: `python scripts/dev/pr_threads.py N` lists unresolved threads and the
   review bodies on the current head. Copilot also reports findings only in the review
   body, under "Previously missed", so read the body, not just the threads.
4. For each finding, verify it against the code before acting. Fix what is real;
   decline with a reason what is not. In 2026-09 Copilot was sometimes wrong: one
   suggested fix would have let an unpinned agent map pass the manifest check silently.
   Before tightening a validation rule, check it against existing data so it does not
   reject records that were fine.
5. Reply in the thread with the fix commit or the reason, then resolve it:
   `python scripts/dev/pr_threads.py N --resolve-mine` resolves threads whose last
   comment is yours.
6. Push, re-request the review, and repeat.

**When to stop.** Copilot tends to find one new edge case per round, and after several
rounds its overview may speak of "N unresolved issues" without naming any. Stop when a
round names no finding and no thread is open; ask it in a PR comment to name the file
and line if anything remains. Code that handles saved state (resume, rebuild, partial
runs) drew 10 to 16 rounds; writing out the state table and testing it up front is
cheaper.

**Merged PRs.** Unresolved threads on merged or closed PRs are still answered:
`python scripts/dev/pr_threads.py --closed` lists them.

## Evals

- Run A/B evals against hosted models (OpenRouter), not local ones: local models on the
  laptop are too slow, and hosted models are not deterministic even at temperature 0,
  so compare across samples. `evals/LOCAL_MODELS.md` documents `prompt_ab`.
- Pairwise judges prefer the second answer (64% of decisive verdicts in 2026-09): judge
  every pair in both orders and count a win only when both orders agree.
- Screen, then confirm. A single component losing on 2 cases is usually noise (the null
  model predicted about 9 such losers out of 129); re-test flagged components on 6 fresh
  cases before acting. Of 14 flagged components, 4 were confirmed.
- Judges may use web search for facts that decide a verdict, because their knowledge
  cutoff can make a correct recent fact look wrong.
- Long fan-out evals run in cloud sessions: [cloud-runs.md](cloud-runs.md).

## Telemetry

When configured and enabled, Langfuse records instrumented routing, loading,
retrieval and memory operations. `evals/telemetry/` exports and summarises those
traces; see its README for the procedure and for what the telemetry can and cannot tell.
