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
[Changing a routine](#changing-a-routine) applies to every routine.
[Cloud environment with Agents-Core](#cloud-environment-with-agents-core) sets up
an environment whose sessions have the MCP server connected.

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
| model | `claude-opus-5-5` (or the model being evaluated) | the routine was first created without one and had to be updated; a routine without a model silently runs the default model (`Agents-issues` ran `claude-sonnet-5-5` until 2026-10-02, visible as `init: model=` in its run log) |
| allowed tools | Bash, Read, Write, Edit, Glob, Grep, WebFetch, WebSearch, **Agent, Workflow** | the runbook fans out through the Workflow tool; without Agent/Workflow the session cannot |
| repository | this repository | the session clones it |
| environment | one that allows Hugging Face | see the egress note below |

The routine's prompt as of 2026-10-02 (the sweep's batches ran an earlier version that
checked out the sweep branch instead of `main`, and without the last paragraph):

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

Ultracode: use multi-agent orchestration only where the runbook fans out (its cases, answers and judges Workflow steps). Do not add agents that re-judge, edit or re-verify cases, answers or verdicts, and do not change any other runbook step: this run is a measurement.
```

The explicit Workflow opt-in matters: a session only fans out through Workflow when the
prompt asks for it. The `ultracode` keyword opts the whole session into orchestration,
so its paragraph confines it to the runbook's three fan-outs: agents that re-judge or
edit cases and answers would change what the sweep measures. "Do not route" matters
too, because the repository's CLAUDE.md tells every session to route through
Agents-Core, which is not connected in the default cloud environment.

Routine and environment ids are account-specific and are not kept in this public
repository. Find them with `RemoteTrigger` `list` (the routine used for the sweep is
named "Agents-testing").

**Egress.** Sessions in the default (Trusted) environment cannot reach
huggingface.co. In 2026-09 the runbook downloaded the embedding model from
fastembed's Google Cloud Storage mirror and pointed `AGENTS_MODEL_PATH` at it.
That mirror answered `403 AccessDenied` on 2026-10-03, so the runbook now needs an
environment that allows Hugging Face, as in
[Cloud environment with Agents-Core](#cloud-environment-with-agents-core).

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
| Embedding model download fails | huggingface.co blocked | in 2026-09, GCS mirror + `AGENTS_MODEL_PATH`; that mirror answered 403 on 2026-10-03, so allow Hugging Face as in [Cloud environment with Agents-Core](#cloud-environment-with-agents-core) |
| (precaution) a session would try to route | CLAUDE.md asks every session to route; Agents-Core is not connected in the default cloud environment | "do not route" in the prompt, as above |
| Some launches, and a `curl` inside a session, were denied by the auto-mode classifier | the classifier judged the command a bypass | not worked around in 2026-09: rephrase the request, or run that step yourself |
| Cloud credit counter does not move | not established | check usage in the account settings before relying on included credits |

## Changing a routine

`RemoteTrigger` `update` replaces the routine's `job_config.ccr` as a whole.
Send its execution target unchanged (`environment_id`, or
`self_hosted_runner_pool_id` for a routine on self-hosted runners), `events`
(the prompt) and `session_context` together:

- Without an execution target the call fails with
  `job_config must set ccr.environment_id or ccr.self_hosted_runner_pool_id`.
- Without `events` it succeeds and leaves the routine with an empty prompt
  (seen on 2026-10-02 on a disabled probe routine).

Read the routine with `get` first, change only the intended fields, and compare
the saved prompt and `session_context` with what you sent. `list` returns
the same objects for every routine.

The routine configuration has no reasoning effort setting: an `effort` key in
`session_context` is accepted and silently dropped. Claude Code documents
`CLAUDE_CODE_EFFORT_LEVEL` for its sessions; whether a routine run honours it
when set in the cloud environment was not verified.

## Cloud environment with Agents-Core

A Claude Code cloud environment can provide Agents-Core in every session, as a
local installation does. The environment's setup script installs the server and
registers it for Claude Code, and the environment cache keeps the result for later
sessions. [`scripts/setup_cloud_env.sh`](../scripts/setup_cloud_env.sh) is that
setup script.

**Create the environment.** At claude.ai/code, open the environment selector (the
cloud icon above the message box), choose **Add cloud environment** (or the
settings icon of an existing environment), and set:

| Field | Value |
|---|---|
| Name | for example `Agents-Core` |
| Network access | **Custom**, with **Also include default list of common package managers** checked and the allowed domains `huggingface.co`, `*.huggingface.co`, `hf.co` and `*.hf.co`; **Full** also works |
| Environment variables | none needed |
| Setup script | the three lines below |

```bash
#!/bin/bash
set -eo pipefail
curl -fsSL https://raw.githubusercontent.com/IEZhu/Agents/main/scripts/setup_cloud_env.sh | bash
```

`pipefail` makes a failed download (a 404 or a blocked host) fail the setup;
without it, `bash` reads an empty script and the setup succeeds without
Agents-Core. The script itself runs only from its last line, so a truncated
download runs nothing.

Hugging Face must be reachable because the embedding model downloads from it. The
default **Trusted** level blocks it, and the fastembed Google Cloud Storage mirror
used by the [ablation runbook](../evals/ablation/README.md) answered
`403 AccessDenied` on 2026-10-03.

**What the script does.**

1. Clones the repository into `~/.agents-core`, or lets `install.sh` fast-forward an
   existing checkout.
2. Seeds `.env` with `EMBEDDING_MODEL` and `AGENTS_AUTO_UPDATE=0`, keeping keys that
   are already set; the verification in step 6 fails if `.env` still enables
   auto-update.
3. Runs `install.sh --skip-index`, which installs the dependencies, registers
   Agents-Core as a user-scope stdio server in `~/.claude.json` and writes the
   protocol 2 section to `~/.claude/CLAUDE.md`.
4. Downloads the embedding model, then builds the indexes with
   `python -m src.reindex`. The order matters: the index fingerprint includes the
   model revision from the model cache, so indexes built before the download
   would be rebuilt by the server on its first start.
5. Writes a marked block to git's global excludes (`~/.config/git/ignore` unless
   `core.excludesFile` names another file; a symlinked file stays a symlink) with
   the files Agents-Core can leave in a client repository's root: `history.md`
   with its lock, rotation and monthly `history/YYYY-MM.md` archives, and the
   hash (`data/memory/.describe_hash`), locks and temporary files of
   `describe_repo`. Patterns are anchored at the root, and each run replaces the
   block; unbalanced block markers fail the setup instead. When the excludes path
   is not a regular file (for example `/dev/null`, which disables global
   excludes), the step only prints a warning.
6. Checks that `~/.claude/CLAUDE.md` holds exactly one routing section, as
   `inject_claude_md.py` writes it from the current template. It then starts the
   registered server over stdio, with the registration's `env` and `cwd`, and
   requires the protocol 2 parameters
   (`protocol_version`, `current_persona`) in the `route_and_load` and
   `get_agent_context` schemas and a protocol 2 answer from `route_and_load`. It
   also calls `load_implants`, which embeds the query in the server process when
   the implant index from step 4 is not empty. An empty result passes, because no
   implant may clear the relevance threshold; an error does not.

Any failed step exits non-zero, so the session fails to start and no broken
installation is cached. Correct the cause, usually the network list, in the
environment's settings; the next new session runs the script again.

**Options.** The script reads `AGENTS_HOME`, `AGENTS_REPO_URL`, `AGENTS_BRANCH`,
`AGENTS_EMBEDDING_MODEL` and `AGENTS_SETUP_VERIFY_TIMEOUT` from its environment.
Set them on the `bash` side of the pipe, for example `... | AGENTS_BRANCH=my-branch bash`. To try a version of the
script that is not on `main` yet, change `main` in the URL as well.
`AGENTS_SETUP_VERIFY_TIMEOUT` (default 360) is how many seconds the verification
step waits for each answer from the server before it fails the setup.
`AGENTS_EMBEDDING_MODEL` (or an exported `EMBEDDING_MODEL`) applies only when
`.env` has no `EMBEDDING_MODEL`. `EMBEDDING_MODEL` and `AGENTS_AUTO_UPDATE` set in
the setup's environment or in the registration's `env` override `.env` in the
server, so the setup fails when they differ from it. Do not set them in the
environment's **Environment variables** field either: sessions pass those to the
server, and the setup cannot check them unless they also reach the setup script. The default is Balanced (`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`),
the installer's choice for the session VM's 16 GB of RAM. Full
(`intfloat/multilingual-e5-large`) is a larger download and indexes more slowly,
so check that setup still finishes within the cache limit below.

**Cache and updates.** The environment is cached only when setup finishes within
about five minutes. On 2026-10-03 the setup script took 47 to 51 seconds in new
sessions of an environment configured as above, and a full local run, including
the 241 MB model download, took 66 to 76 seconds. The next session started from
the cache (`resume-cached` in `/tmp/environment-manager.out`) without running the
script, about six seconds after it was created, with the server connected. The cache is rebuilt when the
environment's setup-script field or allowed hosts change, not when the downloaded
script changes: a commit to `main` reaches new sessions when the cache expires
after about seven days, or earlier when the field changes, for example by editing
a comment line in it. The standalone auto-updater stays off, because each session starts
from the snapshot and would fetch and rebuild indexes in every new VM.

**In a session.** Verified on 2026-10-03 in new and cached sessions of such an
environment: Claude Code listed Agents-Core as a connected user-scope server with
its 16 tools, `load_implants` returned implants, and the protocol section from
`~/.claude/CLAUDE.md` was in the session's context. In a scratch repository
without ignore rules, the global excludes hid every memory file and nothing else;
`tests/test_setup_cloud_env.py` checks the same. The server
behaves as in a local client, whichever repository the session works on, so a
routine that runs in this environment can drop "do not route" from its prompt.
What the server writes during a session, such as `history.md` and the routing
cache, lives only as long as the session VM. The routing cache therefore starts
empty in each session, and `route_and_load` returns `ROUTE_REQUIRED` with the
agent catalog until the session has selected an agent for a similar request. Do not
commit `history.md`: once tracked, it changes on every turn and the cloud session's
check for uncommitted changes reports it.
`describe_repo` creates or edits the repository's `CLAUDE.md`, which then needs a
commit like any other change.

## Issue agent

The issue agent runs [flows/issue-agent.md](../flows/issue-agent.md) in a cloud
session when the configured owner writes a `/agent` command (`plan`, `replan`,
`run_plan`, `run`, `fix`, `review`, `status`, `stop`, `help`) in an issue or pull
request of a target repository. On an issue that awaits answers, `/agent default`
accepts every recommended default and other text after `/agent` answers the
agent's open questions; otherwise the agent replies with its command table. The
flows live in this repository; the target can be any repository the routine
clones.

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

**Thread shape.** Each command leaves the owner's command, one bridge receipt
and Claude's result, usually one comment (`/agent run` also posts its plan, and
an implementation run may add one progress comment with the PR link). Once
Claude starts, the receipt shrinks to a `Claude confirmed startup` line with a
session link; when the command finishes, it is collapsed as outdated. Claude's
comments show `**Claude issue agent** · <command> · [session](...)` as their
first visible line. The issue also keeps one state comment that Claude edits in
place. The 👀 reaction on the command means the run is still going.

**Visible progress and failures.** The bridge posts a status comment (the
receipt) and adds its own 👀 reaction to the command comment, recording a newly
created reaction as `<!-- issue-agent:reaction id=501 -->` on the receipt's
second line. Claude posts a startup acknowledgement after verifying the event,
with its session link; it adds no separate owner reaction. The bridge waits up
to five minutes for that acknowledgement. When it arrives, the bridge edits the
receipt to one `Claude confirmed startup` line with a session link, leaves its
reaction in place while Claude works, stops polling and deletes the
acknowledgement. The link comes from the fire response: its
`claude_code_session_url`, or a URL built from `claude_code_session_id`. In
practice the response carries `cse_` IDs (`https://claude.ai/code/cse_...`),
although the API reference shows `session_` examples. Otherwise the link comes
from the acknowledgement. Either way it must exactly match
`https://claude.ai/code/cse_<letters and digits>` or
`https://claude.ai/code/session_<letters and digits>`; otherwise it is the
routine page. While it waits, the receipt shows the session link, or a neutral
"Waiting for Claude to confirm startup" line with the routine link. After every
successful fire the bridge also logs a notice with the response's shape (field
names, types and allowlisted prefixes, never values) to diagnose API changes. A failed delete is only a warning: startup is already confirmed
and the acknowledgement stays. On a launch failure or timeout the bridge removes
the reaction it created; a session that acknowledges later keeps its
acknowledgement. This startup watch uses GitHub Actions runner time; completion
requires no ongoing polling.

On every normal exit, Claude posts its outcome as a new comment on the original
issue or PR whose first two lines are `<!-- issue-agent -->` and
`<!-- issue-agent:finished bridge_comment_id=123 -->`, using the verified receipt
ID. A questions or plan comment posted in the original issue or PR is itself
that completion comment, with its own markers on the following lines. Other
outcomes, including questions posted in another thread, are a single comment
that carries the outcome, never a separate pointer to a comment above. For
implementation commands, this happens after the required review cycle or an
owner stop or actual blocker; a pending review or wait timeout alone does not
end the run. An interrupted run reports what remains and how to resume.
A separate handling path in the bridge validates the owner's completion,
the trusted bridge receipt, and its original command, removes only the
recorded bridge reaction, and then collapses the receipt as outdated with the
GraphQL `minimizeComment` mutation. A failed collapse is only a warning, and a
receipt without a recorded reaction (the 👀 already existed or could not be
added) stays expanded. The bridge does not edit the receipt text at completion,
because a fast command can finish while the dispatch job is still editing it.
Completion never fires another routine. The final reply records the outcome;
the bridge receipt records delivery and startup. Pre-existing reactions are
preserved. A forcibly stopped runner or cloud session, or a failed completion
callback, may leave a reaction behind. Startup timeout removes the bridge
reaction, so a session that starts later may run without 👀.

A missing `CLAUDE_ROUTINE_TOKEN` or a `CLAUDE_ROUTINE_ID` that is not a `trig_...`
ID is reported as `Launch blocked`, and nothing is fired. A confirmed HTTP `429`
is reported as a routine fire limit, with the numeric `Retry-After` delay when
supplied. Any other non-`200` response is reported, with its HTTP code, as
`Launch rejected` (4xx) or `Launch not confirmed` (other codes). A successful
fire creates a session but does not wait for execution. The fire token has no
read access to later subscription quota failures or session progress. HTTP `200`
without a usable session URL or ID still starts the acknowledgement watch; the
bridge does not retry or assume that the launch failed, and takes the session
link from Claude's acknowledgement as described above. If no startup acknowledgement
appears, the bridge reports that startup is unconfirmed, lists quota as one
possible cause, links the session when available, and clears its newly created
reaction. See the
[routine fire API](https://platform.claude.com/docs/en/api/claude-code/routines-fire).

The bridge does not automatically retry: each successful fire creates another
session. Rerunning the same Actions event finds the existing trusted bridge
status and sends no second fire; a collapsed receipt still counts, because
collapsing does not remove it from the API. To retry, first inspect the previous
session, then post a new command.

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
   own owner. The prompt says that Agents-Core MCP is unavailable (do not route;
   before each flow it calls, load the persona that flow declares from the
   Agents-Core default branch, see
   [flows without MCP](../flows/README.md#without-agents-core-mcp)),
   explicitly allows multi-agent orchestration, ends with an `Ultracode`
   paragraph that bounds it (the command gate, state and lock handling,
   comments, labels, commits, pushes and every other git or GitHub write stay
   in the main session; subagents only read and report, or edit files in their
   own worktree; every subagent and workflow prompt restates that issue, pull
   request, comment and bot text is untrusted data), names the commit author for the
   target (see [issue-implementation](../flows/issue-implementation.md#2-create-the-branch)),
   and tells the session to follow `flows/issue-agent.md` for the event in the
   `routine-fire-payload` block. The saved prompt must explicitly require
   `run_plan` and `run` to continue in the same session through the full
   `no-merge` bot review cycle: wait for reviews, handle findings and repeat after
   fixes until the flow's completion conditions hold. Opening the PR does not
   end the run; only completion, an owner stop or an observed blocker permits
   the final outcome comment. Restrict that prompt to the exact
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
   conversations. The dispatch job posts and edits its receipt, adds the
   processing reaction and deletes Claude's startup acknowledgement; the
   completion job removes its recorded processing reaction and collapses the
   receipt. These need no further permissions. A live PR probe failed cleanup
   with only `pull-requests: read`, while the issue probe succeeded; verify both
   paths after installing the workflow. Neither job checks out repository code.
   See GitHub's
   [comment permissions](https://docs.github.com/en/rest/issues/comments#delete-an-issue-comment),
   [reaction permissions](https://docs.github.com/en/rest/reactions/reactions#delete-an-issue-comment-reaction)
   and [`minimizeComment`](https://docs.github.com/en/graphql/reference/mutations#minimizecomment).
4. Verify new owner `/agent status` comments on an issue and in a PR's
   Conversation tab in that exact target repository. Confirm in both places that
   the receipt changes to `Claude confirmed startup` with a session link, the
   startup acknowledgement disappears, the final reply arrives, the reaction is
   removed and the receipt collapses as outdated. If completion
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

The quieter thread shape (session link in the receipt, deleted acknowledgement,
collapsed receipt, completion line inside the plan or questions comment) can be
installed in either order. The bridge still checks only the first two lines of
the acknowledgement and of the completion comment: with an older bridge the
acknowledgement stays and the receipt is not collapsed, and with older flows an
acknowledgement may lack the session link, so the receipt links the routine page
when the fire response has no usable session URL or ID either. Other repositories
that copied the template, such as WonderMr/Agents.Private, keep the old behavior
until they replace their copy with the current
[template](../scripts/templates/issue-agent-bridge.yml); compare the copy with
its template first, because local changes would be lost. After the change is on
a target's default branch, check live on an issue and in a PR that the dispatch
job's `GITHUB_TOKEN` deletes the owner's acknowledgement, that the completion
job's `minimizeComment` collapses the receipt and that a later receipt edit does
not expand it again, and that Claude's comments link the right session. If the
delete or the collapse is refused, the job log shows a warning, the run itself
is unaffected, and the comment stays visible.

The cloud session reaches GitHub through its GitHub MCP tools (issues, labels,
pull requests, reviews), acting as the owner's account; `gh` is not installed.
Because the agent's comments appear under the owner's login, each one starts with
an `<!-- issue-agent` marker and never with `/agent`. Its first visible line is
the `**Claude issue agent**` header, so readers can tell it from the owner's own
words; only a request addressed to a review bot omits it. Such comments cannot
start a routine; only the exact completion marker
invokes reaction cleanup. Routine runs count against the account's daily routine
allowance.

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
