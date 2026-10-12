# Autodev runner

The runner of the self-development schedule (issue #338, epic #336). The design is
[autodev-plan.md](autodev-plan.md): the gate in section 9.2, the runner in 10.1, the commands
in 10.2 and the configuration in 10.3. This page describes what the code in `src/schedules/`
does today.

## Current limits

- **Dry-run only.** A dry-run checks the runner for drift, reads GitHub and prints the gate
  decision with the cleanup actions the gate wants. It executes none of them and starts no model.
- **Every real mode refuses**: `run`, `resume`, `enable --mode run` and `run-now --mode run`
  print a JSON error naming the missing parts (#342 broker, #347 run sequence, #350 automatic
  mode) and exit with code 1.
- **The selection stage does not run yet.** A `select` decision ends the dry-run; the selection
  session comes with #347.
- **macOS only.** The scheduler is a LaunchAgent, and supervision uses POSIX process groups.

## Commands

The installation starts the runner by absolute path as `<python> -P -m src.schedules …`, with
`PYTHONPATH` naming the installation. `-P` keeps the working directory off `sys.path`. Every
command prints one JSON object. The exit code is 0 on success, 1 when a command refused or a run
failed, and 2 for wrong arguments.

| Command | What it does |
|---|---|
| `tick` | The scheduler's cheap check. It starts a supervised run when a slot or a continuation is due and nothing blocks it. |
| `run autodev [--mode M] [--pr N] [--run-id ID]` | One run. `tick` and `run-now` start it under supervision. |
| `autodev install [--python PATH]` / `uninstall` | Installs or removes the scheduler LaunchAgent. Both are idempotent. |
| `autodev enable --mode dry-run` / `disable` | Turns the schedule on or off in `autodev.json`. |
| `autodev status` | The configuration, the scheduler job, the run lease, the next slot, the tick state and the five latest runs. |
| `autodev run-now [--mode dry-run]` | One supervised run now. |
| `autodev ready` | A stub until #350: it lists the missing parts and exits with code 1. |

## Tick

The LaunchAgent starts `tick` every 300 seconds. A run starts when the schedule is enabled and
one of these is due:

- a slot of `times` (local time), no older than `catch_up_minutes`;
- a continuation's `next_attempt` from the tick state, inside `allowed_hours`. A range may cross
  midnight, such as `22:00-02:00`.

A due run still waits when the run lease is held, a daemon update or maintenance is in progress
(`transaction.json` or `maintenance.json` in the state directory), `pytest` or reindexing is
running, or, with `require_ac_power`, the Mac is on battery. A due slot that waits is retried by
every tick until the catch-up window passes. A slot older than the window is recorded as
`missed`, and slots from before `enable` never count.

Slots are wall-clock times in the local time zone (a `zoneinfo` zone), compared as instants. On
a daylight-saving fall-back night a repeated slot runs once, at its first occurrence; a slot that
spring-forward skips runs at the instant it maps to, an hour later on the clock. A mark in the
tick state that lies after the current time (`last_slot`, `enabled_at`, `last_continuation`) can
only come from a clock that was ahead. The tick replaces it with the current time minus the
catch-up window and records the old values as `clock_reset`.

## Gate

`src/schedules/gate.py` is a pure function over a GitHub snapshot and the run records. It returns
`select`, `resume` or `wait` with a reason, plus a list of cleanup actions it does not execute.

1. A self-development PR has the `autodev` label and a branch `claude/autodev-<issue>-…` of
   this repository. When one is merged or closed, the gate removes its issue's
   `autodev:in-progress` label without a comment, also when the merge closed the issue (the
   snapshot reads such an issue by its number). After a close without merge, the issue is taken
   again only with `autodev:next`.
2. A claim without any PR waits while it is younger than 6 hours. An older claim is removed with
   a comment that starts with `<!-- autodev -->`.
3. An open self-development PR resumes when its trusted state says the review is unfinished and
   one of these holds: `next_attempt` has come, a trusted bot posted a new finding, the PR
   conflicts with its base, or its head has no review. It waits when the review is finished or
   `max_bot_rounds` is reached.
4. Any open self-development PR otherwise means `wait`: the WIP limit is 1.
5. Otherwise the gate returns `select`.

A state comment starts with `<!-- autodev -->`, then `<!-- autodev:state {JSON} -->`. The JSON
holds `head`, `next_attempt`, `rounds`, `review_status` and `findings`. The comment counts only
when its ID appears in a run record's `comments`; a copy is ignored, however well formed. Bot
findings count only from CodeRabbit and Copilot, matched by login and numeric ID.

## Files

All files live in the service state directory `<state_dir>` from `src.daemon.state.state_dir`:
`AGENTS_SERVICE_DIR` when set, otherwise `~/Library/Application Support/Agents-Core/<id>`.

| Path | Content |
|---|---|
| `schedules/autodev.json` | The configuration. Defaults are design 10.3; a missing key takes its default. Validated on every load and save, written atomically. |
| `schedules/tick.json` | The tick state: `enabled_at`, `last_slot`, `last_continuation`, `next_attempt`, `missed`, `clock_reset` and the last decision. |
| `schedules/run.lease` | The run lease. A run holds it from start to finish; the daemon's auto-update, `python -m src.daemon update` and `recover` hold it for their whole transaction, and the manual commands refuse with `an autodev run or a daemon update holds the run lease` while a run holds it. While a run holds it, the auto-update defers with `service is busy: an autodev run holds its run lease`; while the update holds it, a run ends as `failed: infra` (`lease`). A run that takes the lease checks `maintenance.json` and `transaction.json` again and ends as `failed: infra` (`update`) when one exists. A brief probe of the lease never fails a holder: taking it retries for one second. |
| `schedules/runs/<run-id>/record.json` | The run record: mode, trigger, status, gate decision, `pinned`, the GitHub paths read, and `comments` (the IDs of comments the runner wrote). Token-like strings are masked. |
| `schedules/runs/<run-id>/stdout.log`, `stderr.log` | The supervised run's output. |
| `schedules/tools.json` | The absolute paths of `claude`, `codex`, `gh` and `git` resolved at `install`. |
| `schedules/scheduler.log` | The scheduler's standard error. |
| `~/Library/LaunchAgents/local.agents-core.<id>.scheduler.plist` | The scheduler job, with an explicit `PATH`, `PYTHONPATH` and `AGENTS_SERVICE_DIR`. The daemon's own agents are never touched. |

## Supervision and failures

A run goes into a new process group under `caffeinate -i -w <pid>`, with a random per-run marker
`AGENTS_SUPERVISED_RUN` in its environment. A dry-run may take 30 minutes. However the run ends,
by a normal exit, a timeout, or SIGTERM, SIGHUP or SIGINT to the supervisor, the supervisor sweeps
it: SIGTERM to the group and to every process of the run, then, after a grace period, SIGKILL. A
process of the run descends from it by parent PID, is in its group, carries the marker (read with
`ps -E`, which also finds a double-forked `setsid` descendant), or was seen in the run by a
periodic scan and still has the same start time. The run's root stays unreaped until the sweep
ends, so its PID and group cannot be reused, and only PIDs from a fresh scan are signalled. macOS
hides the environment of its platform binaries (`/bin`, `/usr/bin` and the rest of the system), so
such a process that leaves the group and loses its parent before a scan sees it is not found:
`/usr/bin/ssh` with `ControlPersist`, for one, detaches its master at once, which then lives until
its persist time ends; when #347 adds git over ssh, its run environment should turn
`ControlMaster` off. When `ps` fails or times out (5 seconds), the scan is blind and the sweep
reaches only the run's group; the record counts such scans as `failed_process_scans`. Processes
swept after a normal exit are recorded as `swept`; a stop signal that arrives while the run starts
ends the run as soon as it has a PID. A stopped supervisor records `failed: infra` (`stopped`) and
exits with 128 plus the signal number.

Infrastructure failures end with the status `failed: infra` and a reason prefix: `cli` (a
command or the keychain failed, or the run exited without a result), `network`, `lease`,
`update`, `runner drift`, `timeout` and `stopped`. They set no label, and the next slot retries.

The drift check compares the runner's imported modules and `agents/`, `rules/`, `skills/` and
`implants/`, including untracked files there, with `origin/main` of the installation. This part
does not fetch.

## GitHub access

The gate reads GitHub through `src/schedules/github.py`. The client sends only `GET` requests,
only to paths under `/repos/<repository>/`. It takes the fine-grained token from the keychain
item named by `github.token` (default `agents-autodev-github`, account `WonderMr`), never the `gh`
token. The token is never printed, logged or recorded. The client follows no redirect: a 3xx
answer is an error, and the `Authorization` header is one that urllib keeps off any redirect.
