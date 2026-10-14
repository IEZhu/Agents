# Autodev runner

The runner of the self-development schedule (issues #338 and #340, epic #336). The design is
[autodev-plan.md](autodev-plan.md): the session rights and the runner socket in section 5.6, the
gate in 9.2, the runner in 10.1, the commands in 10.2 and the configuration in 10.3. This page describes what the code in `src/schedules/`
does today.

## Current limits

- **Real runs on demand, or on the schedule once `ready` passes.** `run-now --mode run` and
  `run-now --mode resume --pr N` run the whole sequence under supervision
  ([autodev-sequence.md](autodev-sequence.md)): the preconditions, the gate, the select,
  implement and submit stages, and the runner's steps between them. A scheduled real run is the
  [automatic mode](#automatic-mode) (#350): it starts only while `autodev ready` passes, your
  acceptance of the residual risks included. The code is in place; whether `ready` passes on
  this Mac, and the three automatic real runs of the phase 1b acceptance, are your live steps.
- **A dry-run runs the select stage.** It reads GitHub, asks the gate and, on `select`, runs the
  select session (static probe checks, no socket, no broker) and prints the decision with the
  top-five table. It executes no cleanup action and writes nothing to GitHub.
- **Not verified live yet.** The stage sessions, the clients inside the sandbox (that they start
  and cannot write the socket's directory), a stage session's `--json-schema` argv, the
  stream-json fields the report reads (`model` of the init event, `total_cost_usd` of the
  result) and the owner's notification run only with fakes in the tests; the owner's first
  supervised run checks them ([autodev-sequence.md](autodev-sequence.md#before-the-first-real-run)).
- **Leftover socket directories.** The runner removes a killed run's socket, clients and private
  directory as the record's `endpoint` names them; the run sequence records it before the first
  session with Bash starts. A supervisor that is itself killed, or a reboot, before it cleans up
  still leaves an `autodev-socket-*` directory in the runner's temporary directory.
- **macOS only.** The scheduler is a LaunchAgent, and supervision uses POSIX process groups.

## Commands

The installation starts the runner by absolute path as `<python> -P -m src.schedules …`, with
`PYTHONPATH` naming the installation. `-P` keeps the working directory off `sys.path`. Every
command prints one JSON object. The exit code is 0 on success, 1 when a command refused or a run
failed, and 2 for wrong arguments.

| Command | What it does |
|---|---|
| `tick` | The scheduler's cheap check. It starts a supervised run when a slot or a continuation is due and nothing blocks it. |
| `run autodev [--mode M] [--pr N] [--run-id ID]` | One run of the sequence ([autodev-sequence.md](autodev-sequence.md)) in mode `dry-run`, `run` or `resume`. `tick` and `run-now` start it under supervision. |
| `autodev install [--python PATH]` / `uninstall` | Installs or removes the scheduler LaunchAgent. Both are idempotent. |
| `autodev enable --mode dry-run\|run [--accept-shared-account]` / `disable` | Turns the schedule on or off in `autodev.json`. `--mode run` refuses without `--accept-shared-account` (`acceptance_required`, with the risks) and while a condition of `ready` other than the acceptance is missing (`not_ready`, with the list); then it records your acceptance and turns run mode on ([automatic mode](#automatic-mode)). The [settings page](#settings-page) has the same switch. |
| `autodev status` | The configuration, the scheduler job, the run lease, the next slot, the tick state, the five latest runs and `automatic` (`on`, the acceptance and whether it is current, and the `ready` command; `status` reads nothing from GitHub). |
| `autodev run-now [--mode dry-run\|run\|resume] [--pr N]` | One supervised run now; `resume` needs `--pr`. A real run checks its preconditions first and ends `refused` with the missing items. |
| `autodev ready` | Checks every condition of the [automatic mode](#automatic-mode) and names each missing one; exit code 0 only when all pass. It reads GitHub and the stored records; it never starts a probe. |
| `probe [--mode M] [--kind K] --confirm-live-probe` | The owner's live check of the session sandbox: one probe session of kind `implement`, `submit` or `select` with exactly a run's argv, settings and environment (a real model turn, so it refuses without the flag). See [autodev-sandbox.md](autodev-sandbox.md#command). |
| `codex-probe [--kind worker\|stage] --confirm-live-probe` | The owner's live check of Codex: the outer profile, the model-free probes, the nested-sandbox check and the decision between Codex and the Claude worker; a stage check also runs the probe plan through `codex exec` (a model turn) when those pass. See [autodev-codex.md](autodev-codex.md#command). |
| `worker-probe --confirm-live-probe` | The owner's live check of the Claude worker (#345): one worker turn built as a review's, in a review's layout under the run directory, with a `Read` in its tree and one in its materials that must succeed and one outside both that must be denied (a model turn, so it refuses without the flag). See [autodev-workers.md](autodev-workers.md#current-limits). |

## Settings page

The daemon's web UI has an Autodev page (#348): the **Autodev** chip under the version opens it in
the place of the list and the item, and `/ui#autodev` opens it directly. It is an early,
autodev-only slice of the Schedules tab of #211. The routes are in `src/daemon/autodev_ui.py`,
the page in `src/daemon/flows_ui.html`. It edits `autodev.json` only through
`src/schedules/config.py` and turns the schedule on and off only through the runner's `enable`
and `disable`, the functions behind the CLI commands above.

- **Schedule:** the slots (local `HH:MM`, defaults 02:17 and 13:47), the catch-up window, the
  allowed hours of continuations and the AC-power requirement (Q4).
- **Limits:** `max_reviews`, `max_bot_rounds` and `max_wait_minutes` (Q5, Q10).
- **Profiles:** for each stage (`select`, `implement`, `submit`) and delegated step
  (`code-review-pass`, `change-premortem`) the client and, per client, the model and the effort;
  the effort of the later reviews (`effort_followup`) as a setting of its own, where **Same as
  the first** is stored as `effort_followup: null` and the later reviews then run with the first
  review's effort (a pre-mortem runs once, so its `effort_followup` is neither shown nor checked);
  the pre-mortem role, chosen from the installation's agents (default `sysadmin`) (Q2, Q8, Q10,
  Q11). The efforts offered are the capability table's (`src/flow_execution.py`), which the page
  builds from `claude --version`, `claude --help` and `codex --version` (no model turn, a fresh
  empty `HOME`) and keeps for ten minutes. Saving a Claude fallback for a Codex profile is
  allowed: a Codex stage or step that cannot run uses it. A client object left empty falls back
  to its defaults after the save, as `load` merges the defaults in: the run reads a stage's Codex
  settings from it, so the page shows the values the run would use.
- **Codex as a stage client (Q14):** shown as unavailable, with the reason in words (every reason
  the probes store has a text), until the stored stage availability says available
  (`codex_probes.configured_stage_availability`, which the run sequence asks too: the codex
  version and the launch shape the configuration makes must match, and the check must be at most
  7 days old). `implement` and `submit` commit, which a Codex stage cannot, so Codex stays
  unavailable for them. A Codex delegated step is decided before every step
  ([autodev-codex.md](autodev-codex.md)).
- **Switch and status:** **Turn on** enables the chosen mode. `run` is offered only while
  `autodev ready` passes, your current acceptance included, so the first acceptance is the CLI's
  (`enable --mode run --accept-shared-account`); until then it is listed as **run (not ready)**
  and the status lists what is missing. Turning run mode on asks you to accept the residual risks
  of plan section 11, rows 1, 2 and 4 (a confirmation that lists them), and the daemon then runs
  `enable` as the CLI does, with `accept_shared_account: true`: it checks every condition afresh
  and records the acceptance (`source: page`). The status shows on or off and the mode, the next
  slot (`tick.next_slot`), whether the scheduler job is installed, the automatic mode (ready, or
  what is missing; the preconditions it reads from GitHub with the broker's token are kept for
  60 seconds) and the ten newest runs from the run records. Reading it starts no run and no probe.

Every save goes through `config.validate` (the same checks as `load`, `enable` and the runner)
and then the capability table (`check_effort` for every client's effort and the review's `effort_followup`,
`check_model`, whose warnings do not block) and the agent list, before `config.save` writes the
file atomically into the private `schedules/` directory. A refusal lists every problem and leaves
the file as it was. A configuration that changed since the page read it (the CLI, an editor) is
not overwritten (409 `config_changed`), and a file that does not validate is shown with its
problems and not written; fix it or remove it to get the defaults. The page's revision is the
SHA-256 of the configuration it read (the file merged over the defaults) without `enabled` and
`mode`: the switch, `autodev enable` and `disable` and the decoy tripwire change only those, so a
form read before them still saves, and a save never writes them back. Every writer of
`autodev.json` (the page's save, `enable`, `disable` and the tripwire) holds one lock,
`schedules/autodev.lock` (`config.writing`), from its load to its write, so a save takes `enabled`
and `mode` from the file as it is then: it cannot undo a tripwire's `enabled: false` or a
switch made meanwhile. The lock has no timeout: a writer stuck while holding it (a suspended
`autodev enable`, a hung disk) would make a run's tripwire wait for it; a writer that ends,
crashed or not, releases it. With unsaved edits, the switch keeps the form and its revision, so a
change made elsewhere before it still refuses the save. Keys the page does not edit, among them
`github`, `sandbox`, `codex.login` and the stage `budget`, keep their stored values; the first save
writes every other key too, with the current defaults, as `enable` does, so a later change of a
default does not apply to them. The page never shows the GitHub token, the deploy key, the logins
or executable paths, and run reasons are masked. Stage sessions reach neither the page (they have
no network, #343) nor the file (the state directory is closed to them). Like the flows editor,
the page trusts the OS account: any process of yours can sign in automatically and use these
routes, unless `flows-ui --auto off` is set
([shared-mcp-daemon.md](shared-mcp-daemon.md#flow-editor)), run mode included: such a process
can turn it on with the acceptance flag once the conditions hold, as it could run the CLI.

The API is `GET /ui/api/autodev`, `PUT /ui/api/autodev` with `{expected_revision, settings}`,
`POST /ui/api/autodev/enable` with `{mode, accept_shared_account}` (409 `acceptance_required` or
`not_ready` for `run`, with `missing`) and `POST /ui/api/autodev/disable`. Like the Sync and
History routes, every one needs the UI session and the page's `X-Agents-UI` header; a read may
carry no `Origin` but the page's own, and a change must carry the page's own
([shared-mcp-daemon.md](shared-mcp-daemon.md#flow-editor)).

## Automatic mode

Phase 1b of the plan (#350, [plan 5.7](autodev-plan.md#57-modes-probes-and-enabling-conditions)):
`run` on the schedule, under your account. The code is `src/schedules/automatic.py` and
`runner.readiness`. `autodev ready` checks, and names each missing item:

- **The real-run preconditions** of #347 (`preconditions.check`, read from GitHub with the
  broker's token): the deploy key, the pinned host keys and the token (Q15); the ruleset of
  `main` with the `deletion` and `non_fast_forward` rules and a `pull_request` rule with at least
  one required approving review; `can_approve_pull_request_reviews` false; and
  `CLAUDE_ROUTINE_TOKEN` no repository secret (it lives in the `issue-agent` environment). A read
  GitHub refuses is `cannot verify …`.
- **A passing, current record of every sandbox probe** (#343): for each mode (`dry-run`,
  `run-now`, `scheduled`, `resume`) and each stage kind a Claude session runs (`select`,
  `implement`, `submit`), the newest report in the run records: the one `probe --mode M --kind K
  --confirm-live-probe` records, or a run's guarded stage session under the mode it ran in. A
  report counts when its probe session ran or when it failed (a select session's static checks
  alone neither replace a probe nor hide a failure); it must have passed and be at most 7 days
  old (one from the future is stale). Twelve records, as
  [acceptance A7](autodev-acceptance.md#a7-probes-hold-in-every-mode-and-the-broker-refuses) runs
  them.
- **The Codex stage availability** (#344) of a stage that runs with Codex (only `select` can;
  `implement` and `submit` configured for Codex run with Claude and need their Claude probes,
  which the `probe` command now takes with the Claude settings a run would use):
  the stored check of `codex-probe --kind stage --confirm-live-probe`, current for the installed
  codex and the configured launch, as the run sequence asks for it. Such a `select` needs no
  Claude probe records (the `probe` command refuses a stage configured for Codex).
- **Your acceptance** of the residual risks of plan section 11, rows 1 (a sandbox bug or bypass),
  2 (an agent's action under your account counts as yours) and 4 (a broker bug, a leaked deploy
  key), in `schedules/acceptance.json`: the date, the rows, the version of the risk list (SHA-256
  of the rows' text, `automatic.RESIDUAL_RISKS`, which a test pins to the plan) and where it was
  given. It is current while the rows and the version are today's and no tripwire turned the
  schedule off at or after it. A runner update that changes the risk list, or a tripwire, asks
  for a new acceptance.

`ready` reads GitHub and the stored state only; it starts no probe session and no model turn.
`autodev enable --mode run --accept-shared-account` refuses while any condition other than the
acceptance is missing, then records the acceptance with the current date and turns run mode on;
without the flag it refuses and names it. Every such `enable` records a fresh acceptance.

A tick that finds `"mode": "run"` checks `ready` before it starts the run (a slot's `run`, or a
continuation's `resume`). While `ready` fails, nothing starts: the tick writes a run record with
`status: refused`, the reason `automatic mode not ready: …` and the full `missing` list, and the
notification `Autodev run refused` names the run; the slot counts as handled. A dry-run tick is
unchanged and checks nothing of this.

**Tripwires.** Any of these turns the schedule off (`enabled: false`, `decoys.trip`, recorded in
`schedules/tripwire.json` with `cause`): a decoy read or leaked, a changed `.git`, a stream too
long to scan (`cause: tripwire`), and since #350 a failed sandbox probe (`cause: probe`): a failed
report before a stage session in any mode, a failed `probe` command, and a Codex deny probe that
got through. The run or probe command that tripped it records `schedule_off` and sends a
notification titled `Autodev schedule off` whose message starts with a fixed phrase (`a tripwire
fired and turned the schedule off` or `a sandbox probe failed and turned the schedule off`) and
names the run, passed to `osascript` as arguments only (`report.notify`). A tripwire also makes
your acceptance stale, so run mode needs `enable --mode run --accept-shared-account` again, once
you have read the run's record and `tripwire.json`.

## Tick

The LaunchAgent starts `tick` every 300 seconds. A run starts when the schedule is enabled and
one of these is due:

- a slot of `times` (local time), no older than `catch_up_minutes`;
- a continuation's `next_attempt` from the tick state, inside `allowed_hours`. A range may cross
  midnight, such as `22:00-02:00`.

A due run still waits when the run lease is held, a daemon update or maintenance is in progress
(`transaction.json` or `maintenance.json` in the state directory), a session holds the
heavy-process lease, `pytest` or reindexing is running, or, with `require_ac_power`, the Mac is on
battery. A due slot that waits is retried by
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

## Runner socket

A sandboxed stage session reaches the runner only through the runner socket
(`src/schedules/socket_service.py`, design 5.6). The session may connect to it but write nothing
around it, and the runner never executes a command sent through it.

- **Placement.** The socket is `{run_dir}/control.sock` when that absolute path fits the
  `AF_UNIX` limit: 103 bytes on macOS, 107 on Linux. The run directory under
  `~/Library/Application Support/Agents-Core/<id>/` is longer, so in practice the socket goes into
  a private directory `autodev-socket-*` that `tempfile.mkdtemp` creates in the runner's own
  temporary directory. Either directory must belong to the user, must not be a symlink, and must
  not be writable by its group or others. It must also neither lie inside nor contain a path the
  session can write; the caller names the one-off repository, the exchange directory and the
  session's `TMPDIR`, so a session needs a `TMPDIR` other than the runner's. Inside and around are
  compared by resolved path and by file identity (device and inode of the directory and its
  ancestors), so a case-variant spelling on a case-insensitive volume or a
  `/System/Volumes/Data` alias counts as the same directory; a session path that does not exist
  yet counts by its nearest existing ancestor. A violation, or any existing file at the socket
  path, raises before the socket is bound, and an existing file is never removed.
  `Endpoint.to_record()` names the socket and the clients for the run record.
- **Lifetime.** `serve(run_dir, registry, session_writable=[…], session_root=…)` is a context
  manager. On exit, normal or by an exception, it stops accepting, closes open connections, which
  releases what they hold, and removes the socket, the clients and the private directory. In the
  main thread, SIGTERM and SIGHUP raise `RunStopped` (a `SystemExit` with 128 plus the signal
  number) inside it, so the supervisor's SIGTERM ends the run the same way; a signal that arrives
  during the cleanup waits for it. The previous signal handlers are restored on exit. A SIGKILL
  leaves the socket and the clients behind: once the run's process tree is gone, after a timeout
  or a stopped supervisor as after a normal exit, the runner passes the record's `endpoint` to
  `remove_leftovers`. It removes only the user's own socket at the socket path, a `clients`
  directory holding nothing but the clients, and a private directory named `autodev-socket-*`
  with mode 0700 that holds nothing else, and records what it removed or kept as
  `endpoint_cleanup`.
- **Protocol.** One JSON object per line. A request is `{"type": str, "args": object}` of at most
  64 KiB; the answer is `{"ok": true, "result": …}` or `{"ok": false, "error": str}`. A whole
  request must arrive within 10 seconds of its first byte, however slowly it trickles; a
  connection that holds nothing must also start it within 10 seconds, while one that holds a
  resource, such as the heavy-process lease, may wait idle. At most 16 connections are open at a
  time.
- **Registry.** Handlers register under fixed types `gh.<operation>`, `worker.<operation>` and
  `heavy.<operation>`, each with an argument specification (`Field`: `str`, `int` or `bool`). An
  unknown type, a malformed request, or an unknown, missing or mistyped argument is refused before
  a handler runs. There is no generic type: the registry refuses operations such as `exec` or
  `shell` and argument names such as `command`, `argv` or `shell`. A string argument must match
  a narrow pattern: by default letters, digits, `.`, `_` and `-`, starting with a letter or digit,
  without `..`, so no space, slash, shell syntax, `:`, `@`, `=` or `+` that git would read as a
  refspec, a reflog, an option value or a range. A field that declares free text, such as a
  comment body, takes any text the handler treats as data only, at most 32 KiB by default. No
  string may hold a control character other than newline and tab, or a lone surrogate. An
  integer lies between 1 and 1,000,000 unless its field sets other bounds. This part registers
  `heavy.acquire` and `heavy.release`; the broker registers its `gh.*` types
  ([autodev-broker.md](autodev-broker.md)) and the worker launcher its `worker.*` types
  ([autodev-workers.md](autodev-workers.md)).
- **Handlers.** Each type has a deadline (5 seconds unless it registers another, at most 600, so
  that `worker.wait` can wait 9 minutes within the 10 minutes a session's Bash call may take). A
  handler gets a `RequestContext` with that deadline, the peer's PID from the kernel and a
  `closing()` check; it bounds its own subprocess and network timeouts by the deadline and checks
  `closing()` right before any external write, aborting without the write once the server
  closes. Closing waits 5 seconds in all for the connection threads, not per connection; a
  handler still running afterwards cannot keep a resource on its connection any more
  (`Connection.take` and `Connection.keep` refuse once it is closed). A type registered
  `session_bound=True` answers only a process whose ancestors include the current stage session's
  root PID, which the runner names through `session_root`; the check reads the process table on
  every request and refuses when it cannot. The broker's and the worker launcher's types are session-bound, and the broker logs each remote write before and after it, so a write that a
  close interrupts stays visible.

### Thin clients

`serve` writes three executables, mode 0555, into `clients/` beside the socket: `gh`, `worker`
and `heavy`. Each is `src/schedules/client_stub.py` with an absolute `#!<python> -I` line and
the socket path embedded. The interpreter is the runner's base interpreter with symlinks resolved
(`os.path.realpath(sys._base_executable)`, or `sys.executable` without one), not the virtual
environment's link to it, and it runs isolated. The client uses only the standard library, so a
session needs read access to the clients directory and that interpreter only.
`Endpoint.commands()` gives `gh_cmd`, `worker_cmd` and `heavy_cmd`, the shell-quoted absolute
paths for the session prompt.

| Client | What it does | Exit code |
|---|---|---|
| `gh <operation> [JSON object]` | Sends `gh.<operation>` and prints the result as JSON | 0, or 1 when refused, with the reason on standard error |
| `worker <operation> [JSON object]` | The same for `worker.<operation>` | 0 or 1 |
| `heavy -- <command> [args]` | Takes the heavy-process lease, runs the command itself without a shell, with the inherited standard streams, keeps the connection open while it runs, then releases the lease. It passes SIGTERM, SIGHUP and SIGINT on to the command | The command's status, or 75 when the lease is refused, with the reason on standard error |

Every client exits with 2 for wrong arguments and 69 when the socket cannot be reached; `heavy`
exits with 127 or 126 when the command cannot be started. The run sequence serves one registry
for the implement and submit stages, with the heavy-process lease, the broker's `gh.*` types and
the worker launcher's `worker.*` types; the select stage has no Bash and gets no socket.

### Heavy-process lease

The lease (`src/schedules/heavy.py`) is the lock file `schedules/heavy.lease` beside the run
lease. The runner holds it for as long as the granting connection stays open, so a client that
dies releases it, and the socket's close releases it too, even while a grant is still checking.
`heavy.acquire` is refused while another holder keeps the lock, while a foreign heavy process
runs, one whose command line names `pytest`, `src.reindex` or `src/reindex`, and when `ps` fails,
so the process table cannot be read. The runner itself and the requesting client with its
ancestors do not count, because the client's command line, and its shell's, name the command it
is about to start. When the runner names the stage session's root PID, only the ancestors up to
that root are excluded: a `pytest` that started the session, or runs beside it, still counts. The
client's PID comes from the kernel (`LOCAL_PEERPID` on macOS, `SO_PEERCRED` on Linux). The
service only grants and releases the lease; it never starts a command. The tick waits with the
reason `the heavy-process lease is held` while the lease is held, and `autodev status` reports
`heavy_lease_held`. The tick's own probe for heavy processes fails open: when `ps` fails, it sees
none.

The lease is cooperative coordination between well-behaved sessions, not a guarantee. Nothing
stops a session from running `pytest` without `heavy --`, and a session that runs as the same
user can exhaust the user's process limit so that `ps` fails. Enforcing one heavy process against
a misbehaving session needs process and resource limits on the session's tree, which
`probes.start_session` does not set; it stays a known residual of the run sequence (#347), bounded
by the stage's timeout, after which the runner ends the session's process group.

## Files

All files live in the service state directory `<state_dir>` from `src.daemon.state.state_dir`:
`AGENTS_SERVICE_DIR` when set, otherwise `~/Library/Application Support/Agents-Core/<id>`.

| Path | Content |
|---|---|
| `schedules/autodev.json` | The configuration. Defaults are design 10.3; a missing key takes its default. Validated on every load and save, written atomically; the [settings page](#settings-page) edits it. |
| `schedules/tick.json` | The tick state: `enabled_at`, `last_slot`, `last_continuation`, `next_attempt`, `missed`, `clock_reset` and the last decision. |
| `schedules/run.lease` | The run lease. A run holds it from start to finish; the daemon's auto-update, `python -m src.daemon update` and `recover` hold it for their whole transaction, and the manual commands refuse with `an autodev run or a daemon update holds the run lease` while a run holds it. While a run holds it, the auto-update defers with `service is busy: an autodev run holds its run lease`; while the update holds it, a run ends as `failed: infra` (`lease`). A run that takes the lease checks `maintenance.json` and `transaction.json` again and ends as `failed: infra` (`update`) when one exists. A brief probe of the lease never fails a holder: taking it retries for one second. |
| `schedules/heavy.lease` | The heavy-process lease. The runner holds it while a session's `heavy` client runs a check, and the tick waits while it is held. |
| `schedules/runs/<run-id>/control.sock`, `schedules/runs/<run-id>/clients/` | The runner socket and its thin clients while a run serves them, when the socket path fits the `AF_UNIX` limit; otherwise both live in a private `autodev-socket-*` directory in the runner's temporary directory. Removed when the run ends, and by the runner after a killed run. |
| `schedules/runs/<run-id>/record.json` | The run record: mode, trigger, status, gate decision, `pinned`, the GitHub paths read, and `comments` (the IDs of comments the runner wrote, each `{"id", "kind", "number"}`); `issue`, `pr`, `branch`, `selection`, `stages` (each stage's status, execution and probe digest), `probes`, `workers`, `blockers`, `exports`, `risk_issues`, `report` and `notification` ([autodev-sequence.md](autodev-sequence.md#records)); `endpoint` (the socket and its clients, once a run opens them) and `endpoint_cleanup` (what the runner removed or kept after a killed run). Token-like strings and the run's known secrets are masked. |
| `schedules/runs/<run-id>/state.json` | The run state (`src.schedules.run_state`): the stage, the issue, the branch, the PR, `base`, `head` and the flow revisions; a read-only copy sits in the run's state copy for the sessions. |
| `schedules/runs/<run-id>/stages/<kind>/stream.jsonl`, `stderr.log` | A stage session's raw output (private; the run record keeps only verdicts). |
| `schedules/runs/<run-id>/broker.jsonl`, `workers/`, `workers.json`, `validated-review.json`, `premortem.json` | The broker's log ([autodev-broker.md](autodev-broker.md#broker-log)) and the worker service's records ([autodev-workers.md](autodev-workers.md#records)). |
| `schedules/runs/<run-id>/export/<run-id>.patch`, `.bundle` | A control-path change for the owner ([autodev-sequence.md](autodev-sequence.md#after-the-implement-stage)). |
| `schedules/runs/<run-id>/stdout.log`, `stderr.log` | The supervised run's output. |
| `<clone>/../sessions/<run-id>/` | The run's session directories: `<kind>/repo`, `exchange`, `gh`, `settings.json`, and the shared read-only `state/` (inputs files, `state.json`, `worker-log.json`); removed when the run ends. |
| `clone` (default `~/.local/share/agents-autodev/IEZhu-Agents`) | The trusted bare repository: `refs/remotes/origin/main` (`pinned`), imported `refs/heads/claude/autodev-*` branches, bundles refused after the fetch under `refs/autodev/quarantine/<run-id>/<n>`, and bundles refused before the fetch as read-only files `autodev-quarantine/<run-id>/<n>.bundle` inside it; `prune_quarantine()` drops both after 30 days. Outside the state directory. |
| `schedules/tools.json` | The absolute paths of `claude`, `codex`, `gh` and `git` resolved at `install`. |
| `schedules/claude-config/`, `schedules/decoys.json`, `schedules/tripwire.json` | The sessions' `CLAUDE_CONFIG_DIR`, the decoy registry and the last tripwire with its `cause` (`tripwire` or `probe`, #350) ([autodev-sandbox.md](autodev-sandbox.md#files)). |
| `schedules/acceptance.json` | Your acceptance of the residual risks for the [automatic mode](#automatic-mode): `accepted` (UTC), `rows`, `risk_list_sha256`, the risks' text and `source` (`cli` or `page`). |
| `schedules/codex-availability.json` | Whether Codex is available as a stage client. A Codex process's `HOME`, `CODEX_HOME` and `TMPDIR` are per run, inside the run's temporary root ([autodev-codex.md](autodev-codex.md#files)). |
| `schedules/scheduler.log` | The scheduler's standard error. |
| `~/Library/LaunchAgents/local.agents-core.<id>.scheduler.plist` | The scheduler job, with an explicit `PATH`, `PYTHONPATH` and `AGENTS_SERVICE_DIR`. The daemon's own agents are never touched. |

## Supervision and failures

A run goes into a new process group under `caffeinate -i -w <pid>`, with a random per-run marker
`AGENTS_SUPERVISED_RUN` in its environment. Its limit is the sum of its stages' timeouts plus
30 minutes (`sequence.run_timeout`): a dry-run 60 minutes (select 30), a real run 330 (select 30,
implement `stages.implement.budget.timeout_minutes`, 180 by default, submit 90) and a resume 120. However the run ends,
by a normal exit, a timeout, or SIGTERM, SIGHUP or SIGINT to the supervisor, the supervisor sweeps
it: SIGTERM to the group and to every process of the run, then, after a grace period, SIGKILL. A
process of the run descends from it by parent PID, is in its group, carries the marker (read with
`ps -E`, which also finds a double-forked `setsid` descendant), or was seen in the run by a
periodic scan and still has the same start time. The run's root stays unreaped until the sweep
ends, so its PID and group cannot be reused, and only PIDs from a fresh scan are signalled. macOS
hides the environment of its platform binaries (`/bin`, `/usr/bin` and the rest of the system), so
such a process that leaves the group and loses its parent before a scan sees it is not found:
`/usr/bin/ssh` with `ControlPersist`, for one, detaches its master at once, which then lives until
its persist time ends; the broker's push therefore runs ssh with `ControlMaster=no` and
`ControlPath=none` (`broker.ssh_command`). When `ps` fails or times out (5 seconds), the scan is blind and the sweep
reaches only the run's group; the record counts such scans as `failed_process_scans`. Processes
swept after a normal exit are recorded as `swept`; a stop signal that arrives while the run starts
ends the run as soon as it has a PID. A stopped supervisor records `failed: infra` (`stopped`) and
exits with 128 plus the signal number. The run itself handles the sweep's SIGTERM: it records,
notifies and cleans up within the grace period, then releases its claim as far as the grace
allows
([autodev-sequence.md](autodev-sequence.md#the-end)); a run that timed out or was stopped without
notifying is notified by the launching process (`runner.notify_ended`).

Infrastructure failures end with the status `failed: infra` and a reason prefix: `cli` (a
command or the keychain failed, or the run exited without a result), `network`, `lease`,
`update`, `runner drift`, `timeout` and `stopped`. Before a run claims an issue they set no
label, and the next slot retries; after the claim the issue gets `autodev:blocked` with a generic
phrase and loses the claim, unless the run has a pull request
([autodev-sequence.md](autodev-sequence.md#the-end)).
A run also ends `completed`, `blocked` (the issue got `autodev:blocked`, or the select stage gave
no valid result) or `refused` (a missing precondition, a failed probe, the decoy tripwire, a
changed `.git`): see [autodev-sequence.md](autodev-sequence.md#statuses).

The drift check fetches `main` into the trusted repository (below) and compares the runner's
imported modules and `agents/`, `rules/`, `skills/` and `implants/` of the installation, including
untracked files there, with that `pinned` commit, never with the installation's own `origin/main`
or with a branch a session works on. It compares content: `git ls-tree -r <pinned>` is read from
the trusted repository through `gitsafe`, and every working-tree file under those paths is hashed
as a git blob (raw bytes, a symbolic link as its target, the executable bit as the mode). The
installation's index flags (`--skip-worktree`, `--assume-unchanged`), configuration, fsmonitor and
ignore rules play no part, so dot-files count too; only `.DS_Store` is skipped. The installation
need not have `pinned`: a commit on main that touches none of these paths is no drift. A failed
fetch is `network`; an unusable trusted repository, or a file the check cannot read there, is
`cli: trusted repository: …`.

## Trusted repository, one-off repositories and bundles

`src/schedules/repos.py` (issue #339, design 5.4 and 9.4) keeps the runner's git away from
metadata a session can write. Every git command goes through `src/schedules/gitsafe.py`, which the
change check (`src/schedules/paths.py`) uses too: no system or global configuration, no hooks
(`core.hooksPath` at the null device), no fsmonitor, replace objects, grafts or commit-graph, and
no inherited `GIT_*` variables. The trusted repository adds `transfer.fsckObjects=true`, turns
automatic gc and maintenance off, and sets `core.bigFileThreshold=1m` and `pack.threads=1`, so
git streams large blobs instead of holding them whole and a bundle cannot make git's own memory
grow with their inflated size. A git timeout inside an import refuses the bundle.

- **Trusted repository.** A bare repository at `clone` in `autodev.json` (default
  `~/.local/share/agents-autodev/IEZhu-Agents`), created on first use with mode 0700 and owned by
  the runner's user. The configuration refuses a `clone` that is relative or lies under, or
  above, `~/Documents` or the installation. A repository with `info/grafts` or
  `objects/info/alternates` is refused. `main` is fetched from the explicit URL
  `https://github.com/<repository>.git` with the refspec
  `+refs/heads/main:refs/remotes/origin/main`; the repository has no remotes, and `pinned` is
  the SHA of `refs/remotes/origin/main`.
- **One-off repository.** Each session gets `git clone --no-local --no-hardlinks --template=
  --revision=refs/remotes/origin/main` of the trusted repository: its own objects, no
  alternates, no hook samples, detached at `pinned`, without an `origin` remote, with the local
  identity `Alexey Zhuchkov <alexey.zhuchkov@gmail.com>`. `clone --revision` needs git 2.49 or
  later. The submit stage's repository also holds the run's imported branch, fetched from the
  trusted repository by its path and checked out, without a remote or an upstream. The function
  returns only the path and the head; nothing in the runner runs git in a one-off repository
  afterwards (the post-session check reads its `.git` without git, `src.schedules.integrity`),
  so hooks or configuration a session plants there never run outside the sandbox.
- **Bundles.** The session hands its commits over with
  `git bundle create <exchange>/<name>.bundle claude/autodev-<issue>-<slug> ^<base>`, where
  `<base>` is the `pinned` its one-off repository was created at.
  `import_bundle(bundle, exchange, branch, run_id, base)` takes that base
  (`SessionRepository.pinned`), which must still be an ancestor of the current `pinned`, so a
  fetch of `main` while the session works does not refuse its bundle. The runner copies the file
  (a regular file with exactly one hard link, not a symbolic link or a directory, directly inside
  the exchange directory, at most 128 MiB), then requires that `git bundle list-heads` names
  exactly `refs/heads/<expected branch>` with a 40-hex head and that the bundle's prerequisites
  exist in the trusted repository. A bundle refused at this point is kept, read-only, as
  `autodev-quarantine/<run-id>/<n>.bundle` inside the trusted repository; a file the copy itself
  refuses (a link, not a regular file, too large) is not kept. Otherwise the runner fetches the
  objects with fsck into `refs/autodev/quarantine/<run-id>/<n>` and checks `base..head`: the head
  descends from `base`, there are between 1 and 200 commits (`MAX_IMPORT_COMMITS`, counted with
  `rev-list --count` before anything else), every commit has exactly one parent, and no commit
  adds or changes a gitlink or a symbolic link that is absolute or climbs out of the tree (a `..`
  after a named component counts as climbing out, since that component may be a link). One
  `diff-tree --stdin` walks all commits with bounded output; link targets are sized with one
  `cat-file --batch-check`, a target over 4096 bytes is refused unread, and the rest are read with
  one bounded `cat-file --batch`. The whole import has a deadline of 900 seconds. Only then is the
  head promoted to `refs/heads/<branch>`, fast-forward only: a bundle that would rewrite an
  already imported branch is refused. `ImportedBundle.commits` lists the commits this import
  added, oldest first. A refusal names its reason; objects already fetched stay under the
  quarantine ref, and nothing reaches `refs/heads/`. `prune_quarantine()` removes quarantine refs
  and kept files of runs older than 30 days (`QUARANTINE_RETENTION`, judged by the time in the
  runner's run ID) and then runs `git gc --prune=now`; the drift step of every run
  (`runner.default_drift`) calls it under the run lease, right after the fetch.
- **Push.** `push_arguments(url, sha, issue, slug)` builds the arguments the broker
  (`src.schedules.broker.push_command`) runs: `push --porcelain --no-follow-tags --no-signed <url> <sha>:refs/heads/claude/autodev-<issue>-<slug>`
  with a GitHub URL, never a remote name. Only the broker pushes.
- **Environment.** `TrustedRepository.environment(commit, runner_dir)` returns
  `<runner_dir>/venvs/<hash>`, where the hash is SHA-256 over `requirements.txt`,
  `pyproject.toml` and `uv.lock` read at `commit` with `git cat-file blob` (an absent file
  counts as absent). `commit` must be `pinned` or an ancestor of it: an imported session head is
  refused, so dependency files a session wrote are never installed before review. The runner
  directory lies outside the trusted repository, `~/Documents` and the installation; `venvs/`
  and `venvs/<hash>` must be directories of the runner's user, not symbolic links. The
  environment is built (`python -m venv`, then `pip install -r requirements.txt`, with only
  `PATH`, `HOME` and `TMPDIR` inherited and pip's configuration files off) only when it is
  missing or lacks its completion marker `.autodev-complete`. The build runs in a private
  sibling `venvs/.build-*`, whose scripts are pointed at the final path, is made read-only and is
  renamed into place, all under a lock per key (`venvs/<hash>.lock`): a second caller waits and
  gets the first one's environment. The run sequence calls it once per run, for `pinned`.

## GitHub access

The gate reads GitHub through `src/schedules/github.py`. The client sends only `GET` requests,
only to paths under `/repos/<repository>/`. It takes the fine-grained token from the keychain
item named by `github.token` (default `agents-autodev-github`, account `WonderMr`), never the `gh`
token. The token is never printed, logged or recorded. The client follows no redirect: a 3xx
answer is an error, and the `Authorization` header is one that urllib keeps off any redirect.
