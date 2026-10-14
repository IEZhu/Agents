# Autodev

Autodev is the installation's scheduled self-development (epic #336). A local Python runner
picks one issue that the owner marked ready. A Claude session implements a slice of it, with
independent reviews and a pre-mortem, and the runner submits a pull request that only the owner
merges. The runner, not a model, holds the gate, the GitHub credentials and every write to
GitHub. Each model session runs in a sandbox without network access or credentials.

This page is the owner's guide: what to set up, how to run autodev, what a run costs and where
its limits are. Details live in the references below; this page links to them instead of
repeating them. The phase 1a acceptance runs are in [autodev-acceptance.md](autodev-acceptance.md).

**State (phase 1a, #338 to #348).** The schedule runs dry-runs only. The owner starts real runs
by hand with `run-now` and watches them. Scheduled real runs are the automatic mode of #350.
None of the live checks on this page had run when it was written (2026-10-14).

**Public records.** The repository is public. When you note the results of live checks in the
acceptance table, the plan or the open items of these pages, write only pass or fail, run IDs
and neutral reasons. Keep a failed deny check, a decoy or tripwire finding, the contents of a
plant marker and the paths or commands that got through in the local run records or a private
note ([plan 5.8](autodev-plan.md#58-public-visibility)).

| Need | Reference |
|---|---|
| The design, its decisions and the checklist plan | [autodev-plan.md](autodev-plan.md) |
| Commands, files, the settings page, the tick and the gate | [autodev-runner.md](autodev-runner.md) |
| One run step by step: stages, checks, records, report, statuses | [autodev-sequence.md](autodev-sequence.md) |
| What autodev may do on GitHub | [autodev-broker.md](autodev-broker.md) |
| What a session can reach, the probes and the decoys | [autodev-sandbox.md](autodev-sandbox.md) |
| Codex and the fallback to Claude | [autodev-codex.md](autodev-codex.md) |
| The review and pre-mortem workers | [autodev-workers.md](autodev-workers.md) |
| Protected paths, control paths and the outgoing-text scan | [autodev-guards.md](autodev-guards.md) |
| What each stage session is asked to do | [flows/autodev.md](../flows/autodev.md) |

## Prerequisites

### This Mac

- **macOS only.** The scheduler is a LaunchAgent, supervision uses POSIX process groups, and the
  session sandbox's Unix-socket rule works only on macOS.
- **The installation.** Run every command on this page from the live checkout
  `~/Documents/Agents` with its virtual environment, as `.venv/bin/python -m src.schedules …`.
  The runner keeps its state in the service state directory `<state_dir>`: `AGENTS_SERVICE_DIR`
  when set, otherwise `~/Library/Application Support/Agents-Core/<id>`. `autodev status` prints
  the path of the configuration file (`config_file`), `<state_dir>/schedules/autodev.json`.
- **Command-line tools.** `claude`, `git` 2.49 or later (the one-off repositories use
  `clone --revision`), `gh` (the sandbox probes expect it on the session's `PATH`), and `codex`
  only if you want Codex. `autodev install` records their absolute paths in
  `<state_dir>/schedules/tools.json`.
- **For your own commands.** `jq`, which this page and the
  [acceptance runbook](autodev-acceptance.md) use to read the JSON that every command prints, and
  your own `gh` login (`gh auth login`): the host-key command below and the runbook's snapshots
  and issue lists read GitHub with it, using read-only requests only (REST `GET`, and the GraphQL
  queries behind `gh issue list` and `gh pr view`). The runner never uses it.
- **The trusted repository.** The runner keeps its own bare clone at `clone` in `autodev.json`
  (default `~/.local/share/agents-autodev/IEZhu-Agents`) and creates it on first use. It must lie
  outside `~/Documents` and the installation. The session directories go into `sessions/` beside
  it ([trusted repository](autodev-runner.md#trusted-repository-one-off-repositories-and-bundles)).

### GitHub

Phase 0 set these up on 2026-10-12 ([plan, phase 0](autodev-plan.md#phase-0-decisions-and-protection)).
A real run reads the first three again before it starts and ends `refused` with the missing items
(`src/schedules/preconditions.py`); a dry-run does not check them.

- `main` is protected: the rules `deletion` and `non_fast_forward`, and a `pull_request` rule
  with at least one required approving review.
- Workflows cannot approve pull requests (`can_approve_pull_request_reviews` is false).
- `CLAUDE_ROUTINE_TOKEN` is not a repository secret; it lives in the `issue-agent` environment.
- The labels of [plan 9.6](autodev-plan.md#96-labels) exist; the preconditions do not check
  them. You set `autodev:ready`, `autodev:next` and `autodev:skip`; the runner sets the others.

### Broker credentials (Q15)

The broker is the runner's only path for GitHub writes
([credentials and modes](autodev-broker.md#credentials-and-modes)). A real run without these
ends `refused` before the gate.

- **The deploy key** at `github.deploy_key` (default `~/.config/agents-autodev/deploy_key`), a
  regular file, registered on `IEZhu/Agents` with write access (done on 2026-10-12).
- **The pinned host keys** in `github_known_hosts` beside the key (default
  `~/.config/agents-autodev/github_known_hosts`), a regular file in `known_hosts` format. The push
  trusts only this file (`StrictHostKeyChecking=yes`). No autodev command creates it. One way is
  the source the user sync uses for github.com (`src/user_sync/keys.py`), the `ssh_keys` of
  GitHub's `meta` endpoint, read with a GET:

  ```bash
  gh api meta | jq -r '.ssh_keys[] | "github.com " + .' > ~/.config/agents-autodev/github_known_hosts
  ```

  Neither path may contain whitespace.
- **The fine-grained token** in the keychain item named by `github.token` (default
  `keychain:agents-autodev-github`, account `WonderMr`). It was created on 2026-10-12 for
  `IEZhu/Agents` only, with Pull requests and Issues write and Contents read, and a test showed
  that it cannot create branches or merge. The preconditions also read
  `actions/permissions/workflow` and `actions/secrets`. As recalled, not verified, these need
  `Administration: read` and `Secrets: read`; without them a real run refuses with
  `cannot verify … (HTTP 403)`. Never give the token rights to write contents or merge.

Even a dry-run needs the token: the gate and the candidate list read GitHub with it, using GET
requests only. The `gh` token is never used.

### The Claude login of the sessions

`sandbox.login` in `autodev.json` chooses how the sessions log in
([the session](autodev-sandbox.md#the-session)):

- `"config-dir"` (default): a separate `CLAUDE_CONFIG_DIR` that you log in once:

  ```bash
  CLAUDE_CONFIG_DIR="<state_dir>/schedules/claude-config" claude auth login
  ```

  A probe or a run without a login ends `refused` and names this command (`login_command`).
- `"keychain:<service>"`: an OAuth token from `claude setup-token`, stored in that keychain item.
  The runner passes it to the session as `CLAUDE_CODE_OAUTH_TOKEN` and masks it in records.

### Codex (optional)

By default `delegates.code-review-pass` asks for Codex (`gpt-6.1-sol`, effort `high`); every
stage and the pre-mortem ask for Claude.

- **What Codex needs.** The `codex` CLI and an API key of a separate OpenAI project with a
  spending limit, never your own account's key. Store it so that it never appears on a command
  line (`security` prompts for it):

  ```bash
  security add-generic-password -a autodev -s autodev-codex-api-key -U -w
  ```

  Then set `"codex": {"login": "keychain:autodev-codex-api-key"}` in `autodev.json` (see
  [Settings page](#settings-page)). Without it every Codex step falls back with `login_missing`
  ([login](autodev-codex.md#login-the-keychain-only)).
- **Why it falls back on this Mac.** The runner starts Codex only inside its own outer Seatbelt
  profile. On macOS 26.5.1 a nested Seatbelt sandbox cannot apply, so Codex's own sandbox would
  fail. Every Codex review therefore runs the Claude worker, and the record says
  `client_applied: claude` with `fallback_reason: nested_sandbox_unavailable`
  ([current limits](autodev-codex.md#current-limits)).
- **Codex as a stage client.** Only `select` can run as Codex, and only after
  `codex-probe --kind stage` stored Codex as available. `implement` and `submit` always run as
  Claude (`codex_stage_commit_unsupported`), because a Codex session cannot commit.

## Install

```bash
cd ~/Documents/Agents
.venv/bin/python -m src.schedules autodev install
.venv/bin/python -m src.schedules autodev status
```

`install` writes and loads the LaunchAgent `local.agents-core.<id>.scheduler`, which starts
`tick` every 300 seconds, and records the tool paths. `--python PATH` names another interpreter;
the default is the one that runs the command. A tick starts nothing until the schedule is
enabled. `autodev uninstall` removes the job; both commands are idempotent. Every command prints
one JSON object and exits with 0 on success, 1 when it refused or a run failed, and 2 for wrong
arguments ([commands](autodev-runner.md#commands)).

## Modes

| Mode | Started by | What it does | Writes to GitHub | Time limit |
|---|---|---|---|---|
| `dry-run` | `autodev run-now` (the default mode), or the schedule | The gate and, on `select`, the select stage; prints the decision and the top-five table | Nothing: a dry-run builds no broker | 60 minutes |
| `run` | `autodev run-now --mode run` | The preconditions, the gate, then select, implement and submit | Through the broker: the selection comment and the claim, the push, the pull request, comments, labels, risk issues | 330 minutes (implement 180 by default) |
| `resume` | `autodev run-now --mode resume --pr N` | The submit stage again for an open self-development pull request, when the gate agrees | Through the broker | 120 minutes |

`run-now` blocks until the run ends, then prints the run record: `run_id`, `status`, `reason`,
`gate`, `selection` and the rest ([records](autodev-sequence.md#records)). You get a macOS
notification for every real run and for a dry-run that did not complete. A run ends
`completed`, `blocked`, `refused` or `failed: infra` ([statuses](autodev-sequence.md#statuses)).

**The schedule** runs a dry-run at each slot (02:17 and 13:47 by default) once you turn it on
with `autodev enable --mode dry-run` or the settings page; `autodev disable` turns it off. The
tick waits while a run or a daemon update holds the run lease, while a heavy process runs, or on
battery power ([tick](autodev-runner.md#tick)).

**Why `run` mode is refused on the schedule.** A scheduled real run is the automatic mode of
#350. It needs the runner's own checks of [plan 5.7](autodev-plan.md#57-modes-probes-and-enabling-conditions)
and your recorded acceptance of the residual risk of the shared account
(`--accept-shared-account`), and #350 adds both. Until then `autodev enable --mode run` refuses
with `mode_unavailable`, `autodev ready` lists `#350 automatic mode` and exits with 1, and a tick
that finds `"mode": "run"` in `autodev.json` refuses too. `run-now --mode run` stays available
because you watch it (Q13).

## Settings page

Open the daemon's web UI with `.venv/bin/python -m src.daemon flows-ui`, then the **Autodev**
chip under the version, or go to `/ui#autodev`
([flow editor](shared-mcp-daemon.md#flow-editor)). The page edits the slots, the catch-up
window, the allowed hours of continuations and the AC-power rule; the limits `max_reviews`,
`max_bot_rounds` and `max_wait_minutes`; the client, model and effort of every stage and
delegated step, with the follow-up reviews' `effort_followup`; and the pre-mortem role. Its
switch turns the schedule on in `dry-run`, and its status lists the newest runs. Every save is
validated as the runner validates the file. The reference is
[autodev-runner.md, Settings page](autodev-runner.md#settings-page).

The page does not edit `github`, `sandbox`, `codex.login` or a stage's `budget`. Change them in
`<state_dir>/schedules/autodev.json`; every load validates the file. This snippet changes a key
under the same lock the page and `autodev enable` take:

```bash
cd ~/Documents/Agents
.venv/bin/python - <<'EOF'
from src.daemon.state import state_dir
from src.schedules import config
from src.schedules.cli import INSTALLATION

directory = state_dir(INSTALLATION)
with config.writing(directory):
    settings = config.load(directory)
    settings["codex"]["login"] = "keychain:autodev-codex-api-key"  # the change
    config.save(settings, directory)
EOF
```

## Cost

What a run spends, as model turns:

- **A dry-run:** one select session (no probe session, since `select` has no Bash).
- **A real run:** the select session; a probe session before each session with Bash; the
  implement session with its worker turns (at most `max_reviews` reviews, default 3, and the
  pre-mortem); the submit session; and verification reviews in either stage, at most
  `1 + max_bot_rounds` in a run. The defaults are Opus with effort `high` for select, submit and
  the pre-mortem, and `xhigh` for implement. Reviews ask for Codex; on this Mac they run the
  Claude fallback, Opus with `high` for the first review and `medium` for later ones (Q10, Q11).
- **A resume:** a probe session and the submit session, plus a new review before any push.
- **The live checks:** every `probe` and `worker-probe` is a model turn; `codex-probe --kind stage`
  is one only when its model-free checks pass.
- **Codex,** when it runs, bills the separate OpenAI project, whose budget bounds it.

What bounds it: the gate decides in code before any session, one run at a time, the review and
bot-round limits, waits for bots outside sessions, and the stage timeouts (select 30 minutes,
implement 180, submit 90). The runner sets no spending cap of its own.

Where it shows: each stage session's cost as the CLI reported it, in the run record under
`stages.<kind>.cost_usd`, and their sum as `report.cost_usd_total`. Worker turns and probe
sessions are not counted there. The runner reads the cost from the `total_cost_usd` field of the
session's stream-json result; that field name is recalled, not verified, and the first real run
checks it ([before the first real run](autodev-sequence.md#before-the-first-real-run)). There are
no measured numbers yet: the plan sets target values after the first ten runs
([plan 12](autodev-plan.md#12-metrics)).

## Limitations and residual risks

- **Not observed live.** The stage sessions, the thin clients inside the sandbox,
  `--json-schema` with stream-json, the cost fields and the notification have run only with fakes
  ([runner limits](autodev-runner.md#current-limits),
  [sequence](autodev-sequence.md#before-the-first-real-run)). The sandbox and Codex have lists of
  behaviours that are documented or inferred but not observed on this machine
  ([sandbox](autodev-sandbox.md#open-items-for-the-live-check),
  [Codex](autodev-codex.md#open-items-for-the-live-check)), and the workers' schema handling
  comes from the CLIs' help and sources ([workers](autodev-workers.md#current-limits)).
- **The sandbox is the main wall, and it is not ours.** A bug or an unknown bypass in the Claude
  Code sandbox could expose the owner's secrets; decoys detect a hole but do not prevent one
  ([plan 11](autodev-plan.md#11-design-pre-mortem), rows 1 and 16;
  [decoys](autodev-sandbox.md#decoys)). Whether the keychain stays closed through `securityd` is
  an open item.
- **One account for the owner and autodev** (Q6 = no). Labels and comments that any of your
  agents make under your account count as yours (plan 11, row 2). The last line of defence
  against a harmful but plausible change is your review before you merge (row 18).
- **Codex.** A model command can reach the run's `CODEX_API_KEY`; the separate project's budget
  bounds the damage ([residual risks](autodev-codex.md#residual-risks)). Running Codex with its
  own sandbox off inside the outer profile is an
  [open decision](autodev-codex.md#open-decision-for-the-owner).
- **The heavy-process lease is cooperative.** A session can still start `pytest` without it; the
  stage timeout bounds it ([heavy-process lease](autodev-runner.md#heavy-process-lease)).
- **No force push.** A resume pushes against the merge base when `main` moved; a conflict ends
  the run blocked for you
  ([decision](autodev-broker.md#decision-pushing-a-resume-run-after-main-moved-347)).
- **Leftovers.** A supervisor that is killed itself, or a reboot, can leave an
  `autodev-socket-*` directory in the runner's temporary directory.
- **Control paths are closed to autodev.** No label opens `.github/**`, `src/schedules/**`,
  `src/engine/**` and the rest of the runner's import closure, `agents/`, `rules/`, `skills/`,
  `implants/` or `flows/autodev*` (`.github/autodev-control-paths`); a task that needs them ends
  blocked with a local patch for you ([guards](autodev-guards.md)).

## Your steps, in order

1. **Install** the scheduler and check `autodev status` ([Install](#install)).
2. **Credentials:** the deploy key, the pinned host keys, and the token's two extra read
   permissions ([broker credentials](#broker-credentials-q15)).
3. **The Claude login** of the sessions ([login](#the-claude-login-of-the-sessions)).
4. **Optional Codex:** the API key in the keychain and `codex.login` ([Codex](#codex-optional)).
5. **Settings:** check the profiles, limits and slots on the [settings page](#settings-page).
6. **Live checks.** `probe` and `worker-probe` are model turns; `codex-probe` runs model-free
   checks, and for `--kind stage` a model turn only when they pass:

   ```bash
   .venv/bin/python -m src.schedules probe --kind select --confirm-live-probe
   .venv/bin/python -m src.schedules probe --kind implement --confirm-live-probe
   .venv/bin/python -m src.schedules probe --kind submit --confirm-live-probe
   .venv/bin/python -m src.schedules worker-probe --confirm-live-probe
   .venv/bin/python -m src.schedules codex-probe --kind worker --confirm-live-probe  # optional
   .venv/bin/python -m src.schedules codex-probe --kind stage --confirm-live-probe   # optional
   ```

   Each prints a run record; `status: completed` means it passed (for `codex-probe`, that Codex
   is available, which this Mac is not expected to give). Note what you observe in the open
   items of [autodev-sandbox.md](autodev-sandbox.md#open-items-for-the-live-check) and
   [autodev-codex.md](autodev-codex.md#open-items-for-the-live-check), as pass or fail only
   (*Public records* above).
7. **Label candidates:** give several open issues `autodev:ready`
   ([plan 9.6](autodev-plan.md#96-labels)). Without an open `autodev:ready` or `autodev:next`
   issue, a dry-run ends with `selection.decision: none` and no table.
8. **Three dry-runs** with `.venv/bin/python -m src.schedules autodev run-now`; compare each top
   five with your own ([acceptance A1 and A2](autodev-acceptance.md#a1-a-dry-run-prints-the-decision-and-writes-nothing)).
9. **Turn the schedule on in dry-run mode** only if they broadly match:
   `.venv/bin/python -m src.schedules autodev enable --mode dry-run`.
10. **The first supervised real run** on a small `autodev:ready` issue, watched, after the probe
    loop of acceptance A7 and, for Codex, step 4:
    `.venv/bin/python -m src.schedules autodev run-now --mode run`
    ([acceptance A3](autodev-acceptance.md#a3-the-first-real-run-ends-with-a-pull-request-or-blocked)).
11. **Continue a pull request** that waits for bots: the notification names
    `run-now --mode resume --pr N`.
12. **The rest of the acceptance runs** in [autodev-acceptance.md](autodev-acceptance.md), in the
    order it gives.
13. **Day to day:** label issues `autodev:ready` (during calibration) or `autodev:next`, skip
    some with `autodev:skip`, handle `autodev:blocked` issues, and review and merge the pull
    requests yourself ([plan 4](autodev-plan.md#4-human-and-autonomy)). Every run's record is
    `<state_dir>/schedules/runs/<run-id>/record.json`; the other files are listed in
    [autodev-runner.md, Files](autodev-runner.md#files).
