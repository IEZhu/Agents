# Running evals in Claude Code cloud sessions

Long fan-out evals, such as a batch of the component ablation sweep (dozens of agents
writing cases, answering and judging), are run in Claude Code cloud sessions rather
than on a laptop. This page is the procedure that worked for the 2026-09 sweep and the
traps met on the way. The eval itself is described in
[`evals/ablation/README.md`](../evals/ablation/README.md), which is also the runbook the
cloud session follows.

## The short version

1. The repository has the Claude GitHub App installed, so a cloud session can push.
2. A **routine** (a Claude Code remote trigger) holds the prompt, the model and the
   allowed tools. Each run is one batch.
3. Fire it with a short text naming the batch, wait for the results branch, then
   aggregate and archive locally.

## Do not use `Agent(isolation: "remote")` for this

In the 2026-09 attempt, agents launched with `isolation: "remote"` ran in local
worktrees instead of the cloud: the output's `cwd` was a local path, and no cloud
usage was recorded. Check the `cwd` of the first result before trusting any "remote"
run. Routines are the path that does run in the cloud.

## One-time setup

**GitHub access.** Install the Claude GitHub App on the repository ("Only select
repositories" is enough). Without it the session clones the repository but its
push fails with 403.

**The routine.** Create it once, from a session with the `RemoteTrigger` tool or at
claude.ai/code. Set these explicitly, because the defaults did not work:

| Setting | Value | Why |
|---|---|---|
| model | `claude-opus-5-5` (or the model being evaluated) | the routine was first created without one and had to be updated |
| allowed tools | Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch, **Agent, Workflow** | the runbook fans out through the Workflow tool; without Agent/Workflow the session cannot |
| repository | this repository | the session clones it |
| environment | the default cloud environment | see the egress note below |

The routine's prompt as of 2026-09-26 (the sweep's batches ran an earlier version that
checked out the sweep branch instead of `main`):

```text
Run one batch of the component ablation sweep in this repository.

First: git fetch origin main && git checkout main && git merge --ff-only origin/main
Then follow evals/ablation/README.md exactly. I explicitly want the Workflow tool
used for its three fan-out steps (cases, answers, judges); multi-agent
orchestration is intended here. Agents-Core MCP is not available in this session:
do not route, just follow the runbook.

Which components: the text attached to this run says either "batch N"
(N = 1..13, run name batch-NN) or "ids: <component ids>" with a run name.
If no text is attached, run the smoke set with run name smoke:
ids: rule-anti-sycophancy skill-analysis-critical implant-chain-of-verification

Push the results to claude/ablation-<run name> as the README says and end with
the README's final report.
```

The explicit Workflow opt-in matters: a session only fans out through Workflow when the
prompt asks for it. "Do not route" matters too, because the repository's CLAUDE.md tells
every session to route through Agents-Core, which is not connected in the cloud.

Routine and environment ids are account-specific and are not kept in this public
repository. Find them with `RemoteTrigger` `list` (the routine used for the sweep is
named "Agents-testing").

**Egress.** Cloud sessions cannot reach huggingface.co. The runbook downloads the
embedding model from fastembed's Google Cloud Storage mirror and points
`AGENTS_MODEL_PATH` at it (step 1 of the runbook). Any new eval that builds prompts
needs the same.

## Running a batch

Fire the routine with the run request as its text:

| Request text | Runs |
|---|---|
| `batch 3` | batch 3 of `components.json`, run name `batch-03`, 2 cases per component |
| `ids: skill-a implant-b name: retest-x cases: 6` | the listed components, run name `retest-x`, 6 cases each |
| (empty) | the smoke set, run name `smoke` |

With the `RemoteTrigger` tool this is `action: "run"` with the routine id and the body
`{"text": "batch 3"}`. Several batches can run at once; each pushes its own branch.

Watch progress with `RemoteTrigger` `list_runs` and `get_run_log`, or wait for the
results branch:

```bash
scripts/dev/watch_new_branch.sh claude/ablation-
```

In 2026-09 a run took 19 to 38 minutes from start to its last event, both for a batch
of 10 components with 2 cases each and for a re-test of about 5 components with 6 cases.

## After a run

1. Fetch the branch and read its `RESULTS.md` and `build_errors.json`; the runbook's
   scripts exit 1 on any gap, and the session's final report must say so.
2. Combine runs locally: `python evals/ablation/aggregate.py RUN_DIR [RUN_DIR ...]`.
3. Archive: copy the run directory, unchanged, into an `archive/*` branch next to the
   earlier runs, note the commit it was built from (its `build_meta.json`), then delete
   the run branch. The 2026-09 runs are on `archive/ablation-runs-2026-09`.

## Traps met in 2026-09

| Symptom | Cause | Fix |
|---|---|---|
| Results of "remote" agents show a local `cwd`; no cloud usage | `isolation: "remote"` ran locally | use a routine |
| Session clones but cannot push (403) | GitHub App not installed on the repository | install it for this repository |
| Session cannot fan out | routine created without a model and without Agent/Workflow in allowed tools | set model and allowed tools explicitly |
| Embedding model download fails | huggingface.co blocked | GCS mirror + `AGENTS_MODEL_PATH` (runbook step 1) |
| (precaution) a session would try to route | CLAUDE.md asks every session to route; Agents-Core is not connected in the cloud | "do not route" in the prompt, as above |
| Some launches, and a `curl` inside a session, were denied by the auto-mode classifier | the classifier judged the command a bypass | not worked around in 2026-09: rephrase the request, or run that step yourself |
| Cloud credit counter does not move | not established | check usage in the account settings before relying on included credits |

## Issue agent

The issue agent runs [flows/issue-agent.md](../flows/issue-agent.md) in a cloud
session when the owner writes a `/agent` command (`plan`, `replan`, `run_plan`,
`run`, `fix`, `review`, `status`, `stop`, `help`) in an issue or pull request of a
target repository. The flows live in this repository; the target can be any
repository the routine clones.

Post commands as new ordinary comments in the issue or the PR's Conversation
tab, starting with `/agent` as the first characters. The slash prefix avoids
mentioning an unrelated GitHub account. Editing an existing comment or replying
in an inline code review thread does not trigger the bridge.

**Why a bridge.** Routine GitHub triggers cover pull request and release events.
A trigger for `issue_comment` was accepted by the API in 2026-09 but never fired,
so a small GitHub Actions workflow forwards comments and review-bot reviews to the
routine's API trigger. It runs no model and sends only a pointer (repository,
number, comment or review id, and bridge status comment id); the agent reads and
verifies the referenced comments itself.

**Visible progress and failures.** The bridge posts a status comment and adds
its own 👀 reaction to the command comment (or to the status comment for a review
event). Once Claude verifies the event, it adds a separate reaction under the
owner's account and posts a startup acknowledgement. The bridge waits up to five
minutes for that acknowledgement, updates its status, and removes its own
reaction. It stops waiting as soon as the acknowledgement arrives. This watch
uses GitHub Actions runner time. Claude removes only its own newly created
reaction on normal exits; pre-existing reactions are preserved. A forcibly stopped
runner or cloud session may leave its reaction behind.

A confirmed HTTP `429` is reported as a routine fire limit, with the numeric
`Retry-After` delay when supplied. A successful fire creates a session but does
not wait for execution. The fire token has no read access to later subscription
quota failures or session progress. If no startup acknowledgement appears, the
bridge reports that startup is unconfirmed, lists quota as one possible cause,
and links the session when available. See the
[routine fire API](https://platform.claude.com/docs/en/api/claude-code/routines-fire).

The bridge does not automatically retry: each successful fire creates another
session. Rerunning the same Actions event finds the existing trusted bridge
status and sends no second fire. To retry, first inspect the previous session,
then post a new command.

**One-time setup per target repository:**

1. Create a routine (for example `Private-issues`) with both repositories as
   sources (the target and this one), model `claude-opus-5-5`, allowed tools
   Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch, Agent, Workflow, and
   only the connectors it needs. Its prompt names the owner login, says that
   Agents-Core MCP is unavailable (do not route), explicitly allows multi-agent
   orchestration, and tells the session to follow `flows/issue-agent.md` for the
   event in the `routine-fire-payload` block.
2. In the routine's web page, add an **API** trigger and generate its token.
3. In the target repository, add the secret `CLAUDE_ROUTINE_TOKEN` and the
   variables `CLAUDE_ROUTINE_ID` and `AGENT_OWNER`, then copy
   [scripts/templates/issue-agent-bridge.yml](../scripts/templates/issue-agent-bridge.yml)
   to `.github/workflows/issue-agent-bridge.yml`. The job needs `issues: write`
   for status comments and reactions; it does not check out repository code.
   See [GitHub's reaction permissions](https://docs.github.com/en/rest/reactions/reactions#create-reaction-for-an-issue-comment).

When updating an existing installation, publish these flows first, then update
the copied bridge on the target's default branch. For Agents.Private, merge the
public Agents flow change before its private bridge change, so new sessions know
how to acknowledge startup. Update any command examples or prefix checks in the
routine's saved prompt to `/agent` as well. Older callers without
`bridge_comment_id` remain supported, but have no bridge startup acknowledgement.

The cloud session reaches GitHub through its GitHub MCP tools (issues, labels,
pull requests, reviews), acting as the owner's account; `gh` is not installed.
Because the agent's comments appear under the owner's login, each one starts with
`<!-- issue-agent -->` and never with `/agent`; the bridge and the flow both ignore
such comments. Routine runs count against the account's daily routine allowance.
