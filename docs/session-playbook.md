# Playbook for agent sessions on this repository

Practices that held up in the 2026-09 sessions (the component ablation sweep and the
review rounds that followed). [AGENTS.md](../AGENTS.md) is the contributor entry
point; [CLAUDE.md](../CLAUDE.md) covers the routing protocol and the code layout.
This page covers how to work. For documentation maintenance, execute
[documentation-refresh.md](../flows/documentation-refresh.md). Related pages: [cloud-runs.md](cloud-runs.md)
for evals and the issue agent in cloud sessions, [`evals/ablation/README.md`](../evals/ablation/README.md)
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
  pytest runs and embedding-model loads. Run the regular suite, which excludes tests
  marked `slow`, once and alone:
  `LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/ -q`; add `-m ''`
  to include slow tests.
  Run targeted test files while iterating; [tests/README.md](../tests/README.md)
  covers setup, slow tests and worktree isolation.
- **Keep eval outputs out of the main line and out of temporary directories** that
  get cleaned: `~/evals-runs/<name>/` has been the convention for local runs. The
  [A/B eval flow](../flows/ab-eval.md) keeps its runs on an `eval/<name>` branch that
  is never merged, because its cloud routine reads them from there; only the results
  document reaches the main line, with the change it measured.
- **Secrets.** `.env` holds the Langfuse keys; the OpenRouter key for hosted A/B evals
  lives in a user-level env file, not in the repository. Never print a secret; a
  token that was pasted into a chat or printed in a log is rotated.
- **Prompt freshness.** Protocol 2 builds each issued bundle from current source
  content; an explicit refresh can update the active persona without restarting.
  A local `keep` retains the existing bundle. The running router and retrieval
  indexes are separate state: after changing agent membership, routing metadata
  or indexed skill/implant content or metadata, reload the serving process so
  those changes are indexed. For HTTP, run `.venv/bin/python -m src.daemon restart`
  ([service control](shared-mcp-daemon.md#service-control)); for stdio, reconnect
  the server. See [routing](routing_flow.md) for refresh semantics.

## PR review loop

Execute [flows/pr-review.md](../flows/pr-review.md) for the review and merge
process. It is the shared source for concise English descriptions, checking their
alignment after every commit, reviewing full discussions, and replying in English
without long dashes or judgments about findings.

Review the diff yourself before the bots, and every fix before pushing it.
CodeRabbit and Copilot start the first review automatically in the expected GitHub
setup. Confirm that they started. Handle each round's findings together and push
the fixes once. CodeRabbit normally reviews later pushes; re-request Copilot
explicitly after each push of fixes while it is available, at the Lite review
effort: a review at Balanced (GitHub's default since 2026-09-28) stops further
requests until the owner switches it. From the third round, fix in the PR only
blocker, major, security, data-corrupting and production-code findings and
regressions of the PR's own fixes, and move the rest to the PR's Follow-ups
([converging](../flows/pr-review.md#keep-the-cycle-converging)).
When a bot reports a quota limit, rate limit or error,
follow the flow's [quota rules](../flows/pr-review.md#quota-rate-limits-and-errors):
record the evidence and the next attempt time, pause that bot instead of
re-requesting it on every push, and continue with the available bots. Use the
flow's current-head review, validation, and merge conditions before finishing;
commits that no bot reviewed need an independent review before a merge.

**Merged PRs.** Unresolved threads on merged or closed PRs are still answered:
`python scripts/dev/pr_threads.py --closed` lists them.

## Evals

- Routing, retrieval and tier regressions use the deterministic harness:
  `./scripts/eval.sh run` scores `evals/datasets/routing.jsonl`. To compare with the
  committed `evals/reports/baseline.md`, run `save`, then `diff <report>`; `baseline`
  rewrites that file. The harness needs the `evals` extra, fetches query texts from
  Hugging Face unless `evals/datasets/_unlabeled.jsonl` exists locally, and loads the
  embedding model, so run it alone. `./scripts/eval.sh help` lists the other
  commands. `bench` (MCP vs vanilla) and the `scripts/bench_*.sh` wrappers read API
  keys, endpoints and the judge from `.env`.
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
