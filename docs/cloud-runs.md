# Running evals in Claude Code cloud sessions

Long fan-out evals, such as a batch of the component ablation sweep (dozens of agents
writing cases, answering and judging), are run in Claude Code cloud sessions rather
than on a laptop. The sections up to [Traps met in 2026-09](#traps-met-in-2026-09)
record the procedure that worked for the 2026-09 sweep and the traps met on the way.
The eval itself is described in
[`evals/ablation/README.md`](../evals/ablation/README.md), which is also the runbook the
cloud session follows.

The [Issue agent](#issue-agent) section is the maintained setup and operations
reference for the cloud issue agent that runs
[flows/issue-agent.md](../flows/issue-agent.md).

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
session when the configured owner writes a `/agent` command (`plan`, `replan`,
`run_plan`, `run`, `fix`, `review`, `status`, `stop`, `help`) in an issue or pull
request of a target repository. The flows live in this repository; the target
can be any repository the routine clones.

Only comments whose GitHub author login matches `AGENT_OWNER` and whose raw
Actions event author type is `User` can trigger it. Set `AGENT_OWNER` to the exact
login of the one allowed user; a missing or empty setting disables dispatch.
Display names, mentions, and collaborator or member status grant no command access. Bot reviews
and other people's comments are evidence within the owner's task; they do not
start or resume sessions.

`/agent run_plan` and `/agent run` include the complete bot review cycle after
implementation. The same cloud session opens the PR, waits for available bots,
handles findings, pushes fixes and obtains fresh reviews of the resulting head
under [pr-review](../flows/pr-review.md) in `no-merge` mode. It keeps its issue
lock during review and records the current head, bot results and pending work in
the trusted state summary. PR creation is an intermediate milestone. The owner
uses `/agent review` to recover after an actual interruption or request later
review work; it is not a required follow-up to a successful implementation run.

Post commands as new ordinary comments in the issue or the PR's Conversation
tab, starting with `/agent` as the first characters. The slash prefix avoids
mentioning an unrelated GitHub account. Editing an existing comment or replying
in an inline code review thread does not trigger the bridge.

**Why a bridge.** Routine GitHub triggers cover pull request and release events.
A trigger for `issue_comment` was accepted by the API in 2026-09 but never fired,
so a small GitHub Actions workflow forwards verified owner command comments to the
routine's API trigger. It runs no model and sends only a pointer (repository,
number, command comment id, and bridge status comment id); the agent reads and
verifies the referenced comments itself.

**Visible progress and failures.** The bridge posts a status comment and adds
its own 👀 reaction to the command comment, recording a newly created reaction
as `<!-- issue-agent:reaction id=501 -->` on the receipt's second line. Claude
posts a startup acknowledgement after verifying the event; it adds no separate
owner reaction. The bridge waits up to five minutes for that acknowledgement.
When it arrives, the bridge stops polling and leaves its reaction in place while
Claude works; on a launch failure or timeout it removes the reaction it created.
This startup watch uses GitHub Actions runner time; completion requires no ongoing
polling.

On every normal exit, Claude posts a new comment on the original issue or PR
whose first two lines are `<!-- issue-agent -->` and
`<!-- issue-agent:finished bridge_comment_id=123 -->`, using the verified receipt
ID. For implementation commands, this happens after the required review cycle
or an owner stop or actual blocker; a pending review or wait timeout alone does
not end the run. An interrupted run reports what remains and how to resume.
A separate handling path in the bridge validates the owner's completion,
the trusted bridge receipt, and its original command, then removes only the
recorded bridge reaction. Completion never fires another routine. The final
reply records the outcome; the bridge receipt records delivery and startup.
Pre-existing reactions are preserved. A forcibly stopped runner or cloud session,
or a failed completion callback, may leave a reaction behind. Startup timeout
removes the bridge reaction, so a session that starts later may run without 👀.

A missing `CLAUDE_ROUTINE_TOKEN` or a `CLAUDE_ROUTINE_ID` that is not a `trig_...`
ID is reported as `Launch blocked`, and nothing is fired. A confirmed HTTP `429`
is reported as a routine fire limit, with the numeric `Retry-After` delay when
supplied. Any other non-`200` response is reported, with its HTTP code, as
`Launch rejected` (4xx) or `Launch not confirmed` (other codes). A successful
fire creates a session but does not wait for execution. The fire token has no
read access to later subscription quota failures or session progress. HTTP `200`
without a usable session ID still starts the acknowledgement watch; the bridge
does not retry or assume that the launch failed. A known session URL may also
appear in Claude's acknowledgement. If no startup acknowledgement appears, the
bridge reports that startup is unconfirmed, lists quota as one possible cause,
links the session when available, and clears its newly created reaction. See the
[routine fire API](https://platform.claude.com/docs/en/api/claude-code/routines-fire).

The bridge does not automatically retry: each successful fire creates another
session. Rerunning the same Actions event finds the existing trusted bridge
status and sends no second fire. To retry, first inspect the previous session,
then post a new command.

**One-time setup per target repository:**

Each target needs its own active bridge on its default branch, its own
repository settings, and the Claude GitHub App ([GitHub access](#one-time-setup));
without the App, the session cannot push its `claude/` branches to the target.
A workflow under `scripts/templates/` does not run, and installing the bridge in
WonderMr/Agents.Private does not enable commands in IEZhu/Agents.
Agents-Core installs the bridge at
[.github/workflows/issue-agent-bridge.yml](../.github/workflows/issue-agent-bridge.yml);
its tests exercise both that installed workflow and the reusable template.

1. Create a dedicated routine for the target (for example `Agents-issues` for
   IEZhu/Agents or `Private-issues` for WonderMr/Agents.Private). Select the
   target and this repository as sources; when the target is IEZhu/Agents,
   select it once. Set model `claude-opus-5-5`, allowed tools
   Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch, Agent, Workflow, and
   only the connectors it needs. Look up the owner's GitHub account and verify
   its login and numeric user ID before pinning `AGENT_OWNER` and
   `AGENT_OWNER_ID` in the routine prompt. For the GitHub.com `WonderMr` account,
   the verified numeric ID is `5370211`; other installations must verify their
   own owner. The prompt says that Agents-Core MCP is unavailable (do not route),
   explicitly allows multi-agent orchestration, names the commit author for the
   target (see [issue-implementation](../flows/issue-implementation.md#2-create-the-branch)),
   and tells the session to follow `flows/issue-agent.md` for the event in the
   `routine-fire-payload` block. The saved prompt must explicitly require
   `run_plan` and `run` to continue in the same session through the full
   `no-merge` bot review cycle: wait for reviews, handle findings and repeat after
   fixes until the flow's completion conditions hold. Opening the PR does not
   end the run; only completion, an owner stop or an observed blocker permits
   the final outcome and completion receipt. Restrict that prompt to the exact
   target repository, written as `GITHUB_REPOSITORY` reports it (the bridge
   sends that value as `repo`, and a renamed or transferred repository's old
   name does not match); a routine restricted to WonderMr/Agents.Private must
   not process IEZhu/Agents commands.
2. In the routine's web page, add an **API** trigger and generate its token.
3. In the target repository, add the secret `CLAUDE_ROUTINE_TOKEN` and the
   variables `CLAUDE_ROUTINE_ID` (the routine's `trig_...` ID) and `AGENT_OWNER`,
   then copy [scripts/templates/issue-agent-bridge.yml](../scripts/templates/issue-agent-bridge.yml)
   to `.github/workflows/issue-agent-bridge.yml`. Give both jobs `issues: write`
   for issue comments and reactions, plus `pull-requests: write` for PR
   conversations. The dispatch job posts status comments and processing reactions;
   the completion job removes its recorded processing reaction. A live PR probe
   failed cleanup with only `pull-requests: read`, while the issue probe succeeded;
   verify both paths after installing the workflow. Neither job checks out
   repository code. See GitHub's
   [comment permissions](https://docs.github.com/en/rest/issues/comments#create-an-issue-comment)
   and [reaction permissions](https://docs.github.com/en/rest/reactions/reactions#delete-an-issue-comment-reaction).
4. Verify new owner `/agent status` comments on an issue and in a PR's
   Conversation tab in that exact target repository. Confirm the startup reply,
   final reply, and automatic reaction cleanup in both places. If completion
   fails, its log reports the failed GitHub operation and HTTP status without
   response bodies, headers or credentials. Other exceptions remain sanitized.
   Comments created before installation are not replayed; post a new command
   after setup is complete. A successful probe in another repository does not
   verify this installation.

When updating an existing installation, first pin the verified owner identity
in the routine prompt. Keep its login aligned with the repository's `AGENT_OWNER`
and name the numeric ID `AGENT_OWNER_ID`; this is a cloud verification setting,
not another Actions variable. Apply the same requirement to complete review in
existing routine prompts. Update any saved command examples or prefix checks
to `/agent` as well. Then publish these flows before updating the copied bridge
on the target's default branch. For Agents.Private, merge the public Agents flow
change before its private bridge change, so new sessions know how to acknowledge
startup and signal completion. Older comment callers without `bridge_comment_id` remain supported,
but have no bridge startup acknowledgement or reaction cleanup callback. Review
event payloads are rejected.

The cloud session reaches GitHub through its GitHub MCP tools (issues, labels,
pull requests, reviews), acting as the owner's account; `gh` is not installed.
Because the agent's comments appear under the owner's login, each one starts with
an `<!-- issue-agent` marker and never with `/agent`. Such comments cannot start a
routine; only the exact completion marker invokes reaction cleanup. Routine runs
count against the account's daily routine allowance.

**Connector identity checks.** Cloud GitHub tools can omit an author's `type`.
The cloud flow requires the referenced comment's exact pinned owner login and
numeric ID for commands, state, plans, and questions. If `type` is present, it
must be `User`; if omitted, the pinned identity still permits verification.
A missing author ID, a display name, repository role, or `get_me` alone cannot
establish the comment's author. Issue and marker checks still apply.

On GitHub.com, bridge receipts must identify `github-actions[bot]` with numeric
ID `41898282`; a supplied `type` must be `Bot`. Only that verified identity permits
an omitted `type`. Another GitHub host requires its own verified, explicitly
configured bridge bot identity. These connector rules do not relax the raw
Actions event checks, which still require `User` and the configured owner login.
