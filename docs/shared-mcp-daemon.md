# Shared MCP daemon operations

A macOS LaunchAgent serves local MCP clients at `http://127.0.0.1:8765/mcp`.
One Python process holds `intfloat/multilingual-e5-large`, the router, and shared
indexes. Desktop Chat connects through `bridge/stdio.mjs` (Node 22+, no npm
dependencies). MCP SDK 1.28.1 is pinned in `pyproject.toml`, `requirements.txt`,
and `uv.lock`. The port above is the default; `install --port` can change it.
Installation requires an existing e5-large snapshot under `FASTEMBED_CACHE_DIR`
(default `~/.cache/fastembed`); `install` does not download the model.
`scripts/init_repo.sh` caches it when you choose the Full model
(`intfloat/multilingual-e5-large`). It asks only while `.env` has no
`EMBEDDING_MODEL`, and `--yes` (used by `install.sh`) selects Full only with
32 GB of RAM or more. `install` reads `FASTEMBED_CACHE_DIR` from the shell
environment, not from `.env`, so export a custom cache directory before running
it; the service then always uses the path recorded at installation.

The daemon supports persona protocol 2 only. Transport migration does not
rewrite client instructions. Follow [the routing protocol](routing_flow.md) when
keeping, switching, restoring, or refreshing a role. HTTP never samples the final
answer and does not expose `clear_session_cache`; administrative cache clearing
uses the controller command below.

## Installation and client migration

Run commands from the installation root with its Python interpreter. For the
first migration:

1. While the current clients are still running, capture the process baseline,
   back up client configurations and run `audit`.
2. Stop this installation's stdio servers by disabling Agents-Core in open
   clients or ending those sessions; the client applications can stay open.
   `install` fails while any stdio server of this installation holds the
   installation lease.
3. Run `install`, `start` and `migrate`.
4. Reconnect MCP in open clients and repeat the baseline (see below).

Migration changes client configurations only. Keep each project's `history.md`:
the service reads and appends to the same file.

```bash
.venv/bin/python scripts/daemon_baseline.py --installation "$PWD" --output /tmp/agents-before.json
.venv/bin/python -m src.daemon audit
.venv/bin/python -m src.daemon install
.venv/bin/python -m src.daemon start
.venv/bin/python -m src.daemon migrate --clients codex,claude,cursor
.venv/bin/python -m src.daemon migrate --workspace /absolute/project --clients codex,claude,cursor
.venv/bin/python -m src.daemon migrate --clients desktop
.venv/bin/python -m src.daemon status
```

Installation records absolute Python, Node, and Git paths, PATH, and the local
model snapshot. It checks port availability before changing configurations and
does not terminate another process occupying the port. Use `install --port NUMBER`
to select another port. `install --python PATH` and `install --node PATH` override
the recorded interpreters (by default, the Python running `install` and the first
`node` on PATH). Without a recorded Node, Desktop and tracked-configuration
migrations fail with `An absolute Node executable is required`.

Register each project directory and worktree separately. Registering the same
realpath again returns its existing UUID. Global entries provide routing,
personas, and built-in and personal flows without a workspace; memory,
repository (`repo:`) flows and `run_flow` require project configuration. After
creating a clone or worktree, run `migrate --workspace /absolute/worktree` before
connecting. Do not copy an MCP configuration containing another project's UUID.

Codex reads project `.codex/config.toml` in a trusted project; Claude Code uses
local scope in its selected user configuration; Cursor uses project
`.cursor/mcp.json`. An existing Claude project `.mcp.json` is also updated.
Tracked Codex configurations use a
string `http_headers_helper`; tracked JSON configurations use the Node bridge
with a private configuration outside the repository. Secrets are neither printed
nor written to tracked configurations. Untracked files containing a token are
added to the local Git exclude file.

The Desktop bridge is named `Agents-Core-Desktop`. Desktop migration also adds
denials for its namespace in Claude Code while retaining other policies. Verify
the Code tab separately: importing Desktop servers can create a lightweight
bridge, while tools are provided through the native `Agents-Core` entry.

Reconnect MCP in open clients after writing configurations. An existing stdio
process does not automatically become an HTTP client. `audit` reports scopes,
names, transports, and header names without values. Inspect user, local, project,
and plugin scopes, then repeat the baseline: the acceptance criterion is one
process holding the model.
Module-invoked `python -m src.server` processes and every
`python -m src.daemon ... serve` process are counted across the host because
their command lines do not identify an installation. Check the reported PIDs
when multiple installations or a smoke-test daemon are present.

While the service is installed, setup (`scripts/init_repo.sh`, also run by
`install.sh`) keeps Cursor, Claude Code and Claude Desktop on the service:
instead of writing stdio entries, it applies the same user-scope migration with a
private backup, and it fails during maintenance or while another controller
command holds the lock. Do not rerun setup while the service runs (see
[Updates and recovery](#updates-and-recovery)); use `restore-clients` and
`uninstall` to return to stdio.

### Alternate client configurations

Migration and audit share the same client path resolver. For each user target, an
explicit `--client-config CLIENT=PATH` takes precedence over its environment
override, then its default path. With `--workspace`, Codex and Cursor use the
fixed project paths below, ignoring user configuration overrides; an explicit
`--client-config` can select a different file. Paths identify configuration files;
workspace identity is supplied separately with `--workspace`.

| Client target | Default file | Environment override |
| --- | --- | --- |
| `claude` | `~/.claude.json` | `CLAUDE_CONFIG_DIR` selects `<dir>/.claude.json` |
| `claude-deny-desktop` | `~/.claude/settings.json` | `CLAUDE_CONFIG_DIR` selects `<dir>/settings.json` |
| `codex` | `~/.codex/config.toml`, or `<workspace>/.codex/config.toml` with `--workspace` | `CODEX_HOME` selects the user configuration directory; project paths stay unchanged |
| `cursor` | `~/.cursor/mcp.json`, or `<workspace>/.cursor/mcp.json` with `--workspace` | `AGENTS_CURSOR_MCP_CONFIG` selects the user MCP file; project paths stay unchanged |
| `desktop` | macOS: `~/Library/Application Support/Claude/claude_desktop_config.json`; setup on Linux: `$XDG_CONFIG_HOME/Claude/claude_desktop_config.json` (default `~/.config`); setup on Windows: `%APPDATA%\Claude\claude_desktop_config.json` | `AGENTS_CLAUDE_DESKTOP_CONFIG` selects an exact configuration file |
| `claude-project` | `<workspace>/.mcp.json` | No environment override; requires `--workspace` |

Export variables in the shell that runs migration or audit. The controller does
not read them from `.env` and cannot infer another process's environment from the
daemon. Setup (`install.sh`, `scripts/init_repo.sh`/`.bat`) and
`scripts/install_instructions.py` likewise read the ones they use only from their
process environment (see [env.example](../env.example)). A nonempty
`CLAUDE_CONFIG_DIR` also changes the location of the user JSON: explicitly setting
it to `~/.claude` selects `~/.claude/.claude.json`, while leaving it unset selects
`~/.claude.json`. `CODEX_HOME` is the Codex state directory, not the configuration
file itself. Use an existing directory and absolute paths for alternate profiles.

`AGENTS_CURSOR_MCP_CONFIG` and `AGENTS_CLAUDE_DESKTOP_CONFIG` are Agents-Core
settings. They tell migration, audit and setup which existing client file to
manage; they do not reconfigure the client application's own path selection.
In particular, a Cursor `--user-data-dir` or UI profile does not establish the
location of its MCP file. Supply the file that the client actually reads.

Select a Claude profile for both user and project-local registrations:

```bash
CLAUDE_CONFIG_DIR="$HOME/.claude-work" .venv/bin/python -m src.daemon migrate --clients claude
CLAUDE_CONFIG_DIR="$HOME/.claude-work" .venv/bin/python -m src.daemon migrate --clients claude --workspace /absolute/project
```

Use explicit files for other profiles or a Codex named profile that contains its
own MCP entry:

```bash
.venv/bin/python -m src.daemon migrate --clients codex,cursor \
  --client-config codex=/absolute/codex-home/work.config.toml \
  --client-config cursor=/absolute/cursor-profile/mcp.json
.venv/bin/python -m src.daemon migrate --clients desktop \
  --client-config desktop=/absolute/desktop-profile/claude_desktop_config.json \
  --client-config claude-deny-desktop=/absolute/claude-profile/settings.json
```

`migrate` accepts one explicit file per client target and rejects repeated
overrides for the same target. Migrate another profile in a separate invocation.
Select each overridden target with `--clients`; Desktop migration also selects
`claude-deny-desktop`, and Claude workspace migration selects an existing project
`.mcp.json` as `claude-project`.
Existing unrelated fields and client enablement settings are preserved. When a
workspace is supplied, the selected MCP entry receives that workspace's UUID.
Claude's user JSON keeps separate local entries for each project. Other file
formats hold a single `Agents-Core` entry: do not reuse the same file for
different projects that need separate memory.

Audit accepts repeated files for the same client and an optional workspace:

```bash
.venv/bin/python -m src.daemon audit --workspace /absolute/project \
  --client-config claude=/absolute/claude-work/.claude.json \
  --client-config claude=/absolute/claude-personal/.claude.json \
  --client-config cursor=/absolute/cursor-profile/mcp.json
```

Audit checks standard and active configuration roots, project scopes discovered
in the selected Claude registries, Codex named `*.config.toml` files, and plugin
MCP manifests under the selected client roots. It also checks explicit files and
previously migrated paths retained in private `client-configs.json`. Migration
records targets so a later audit can inspect an inactive profile after its
environment override is no longer set. Paths migrated before this registry was
introduced need an explicit path if they are outside the standard or active
roots. Restoring client configuration retains this inventory so audit can report
restored standalone entries; later migrations of other profiles do not block the
restore. Audit reads these sources without modifying them and reports malformed
files without exposing credentials.

This is a bounded configuration inventory, not a search of the entire disk or
every running process. Supply arbitrary inactive paths explicitly at least once
to migrate them. Inline command-line MCP definitions, dynamic extension
registrations, and another process's configuration environment may introduce
additional servers outside this inventory. After reconnecting clients, repeat
the process baseline to verify that only one model process remains.

The client path contracts are documented in [Claude Code environment
variables](https://code.claude.com/docs/en/env-vars), [Codex configuration and
profiles](https://learn.chatgpt.com/docs/config-file/config-advanced),
[Cursor MCP configuration](https://cursor.com/docs/mcp#configuration-locations),
and the [Claude Desktop MCP setup guide](https://modelcontextprotocol.io/docs/develop/connect-local-servers).

## Service control

```bash
.venv/bin/python -m src.daemon status
.venv/bin/python -m src.daemon stop
.venv/bin/python -m src.daemon start
.venv/bin/python -m src.daemon restart
.venv/bin/python -m src.daemon workspace list
.venv/bin/python -m src.daemon workspace register /absolute/project
.venv/bin/python -m src.daemon clear-cache
.venv/bin/python -m src.daemon token rotate
```

After editing skills or implants in the installation, `restart` drains, stops
and starts the service; its warmup rebuilds changed skill and implant indexes
with the service's e5-large model before it reports ready. `clear-cache` clears only the
process-local enriched-prompt cache (the HTTP counterpart of
`clear_session_cache`), not the persistent routing cache in `router/` under the
private state directory.

Private state lives at
`~/Library/Application Support/Agents-Core/<installation-hash>`. Directory
permissions are 0700; token, configuration, and backup permissions are 0600.
Place `--state /absolute/private/dir` before the subcommand to use isolated state.
The LaunchAgent is named `local.agents-core.<state-directory-name>` (the
installation hash for the default directory), so give isolated state directories
distinct names. It uses ProcessType Interactive, RunAtLoad and KeepAlive: launchd
starts it at login and restarts it after an unsuccessful exit. `stop` and
`restore-clients` unload it for the current login session only, so it starts
again at the next login; `uninstall` removes its plist from `~/Library/LaunchAgents`.

The service writes its output to `service.log` in the private state directory
(LaunchAgent stdout and stderr are discarded), limited to six 10 MiB files. With
`AGENTS_DEBUG=1` in the installation `.env`, debug JSON files go to
`debug/<date>/` in the same directory, limited to seven days and 100 MiB. Debug
writes skip symlinked path components and create mode-0600 JSON files
exclusively. Debug pruning leaves symlinked directories and JSON entries untouched.

Token rotation includes custom client paths recorded by successful migrations,
using the same private backup journals as standard paths. Merely auditing a file
does not make it a managed rotation target. Rotation also updates private bridge
configurations; it does not depend on the profile environment remaining active.
Rotation does not reach clients that already loaded the previous token: HTTP
clients receive 401 `unauthorized`, and running bridge processes report
`Agents-Core unavailable or response uncertain`. Reconnect MCP in every client
after `token rotate`.

`status` reports ready, starting, draining, or failed state when the service
responds; otherwise it reports `not_installed`, `starting`, or `stopped` from local
configuration and launchd. A live response includes PID, boot ID, request
counts (`inflight` for work, `streams` for open client notification streams),
`idle_seconds` since the last request finished, and pending job counts
(`io_pending` for running or queued I/O jobs, `inference_pending` for model work).
When the service does not respond, `status` also reports `supervised` (launchd
has the job loaded), `maintenance` and `transaction`; a `transaction` that remains
while no controller command is running requires `recover`. `/health` requires a
bearer token and, besides the MCP SDK `version`, returns `agents_core_version`
(the Agents-Core version shown in the footer, see
[routing reference](routing_flow.md)). Readiness means the model and indexes have warmed successfully.
Admission is bounded at 32 work requests, plus up to 32 open notification streams
counted separately, with eight I/O workers and one inference worker. Capacity
exhaustion returns busy. Cancelling an HTTP waiter retains the quota for its
running job and does not replay a mutation.

### Drain and connected clients

`stop`, `restart`, `uninstall`, `restore-clients`, `update`, `recover`, and
`token rotate` first drain the service: new requests get 503, and the command
waits up to 60 seconds for `inflight` (work), `io_pending` (running or queued I/O
jobs) and `streams` (open notification streams) all to reach zero. If any stays
above zero, including a stream that fails to close, the drain times out: the
controller resumes the service without killing active work, and the command fails
with `Drain timed out; runtime resumed without killing active work` without
applying its change. Retry after the work finishes. Two commands differ:
`uninstall` has already disabled automatic updates, and `token rotate` rolls back
by draining again and restarting the service; if that second drain also times
out, run `recover`.

Commands that restart the service (`restart`, `update`, `recover`, or `stop`
followed by `start`) do not require closing connected clients. Each client holds
a long-lived GET notification stream; those are counted as `streams`, not as
work, and drain ends them cleanly
([#76](https://github.com/IEZhu/Agents/issues/76)). The transport is stateless,
so a client's next request reaches the restarted process without a new session.
A request sent during the stop window fails and can be retried. After
`token rotate`, however, clients must reconnect MCP (see [Service control](#service-control)).

## Flow editor

```bash
.venv/bin/python -m src.daemon flows-ui          # opens the browser
.venv/bin/python -m src.daemon flows-ui --no-open
.venv/bin/python -m src.daemon flows-ui --revoke  # end every browser session
.venv/bin/python -m src.daemon flows-ui --auto off  # require the one-use code and end every session (on restores the default)
```

The daemon serves a local settings page at `/ui` with four tabs.

- **Flows** edit [personal and repository
  flows](../flows/README.md#personal-and-repository-flows). A User/System switch
  shows one category at a time. *User* lists personal flows and, below them, the
  repository flows of every repository under `flows/.user/repos/`, grouped by
  the stored origin (else the last known path, else the key); no repository has to be
  selected, and an existing repository flow is opened, saved, deleted and its
  history shown by key. *System* lists the read-only built-in flows. Creating a
  repository flow, or "Edit copy for a repository", asks which registered
  workspace it belongs to. Saved versions, the comparison of a local copy with
  its built-in flow and conflicts with edits made from chat work as before.
- **Rules**, **Skills** and **Implants** list every `rules/rule-*.mdc`,
  `skills/*.mdc` and `implants/*.mdc` with its description, a read-only view of its
  body (skills also show the agents that declare them and their tier, implants the
  agents that prefer them) and a switch. The files are never edited, renamed or
  deleted.

The page fills the window: the header and the detail pane stay in place and only
the list on the left scrolls (in the narrow layout, the list above the detail
pane scrolls on its own, and the detail pane scrolls separately when its content is
taller). Every tab has a search box above its list (`/` focuses it, `Esc` clears
it). Matching is case-insensitive and every whitespace-separated term must
appear. Items whose name (ID, title, short name) matches are listed first, then
items that match only in their text (description and body; flow content), marked
"in text" (on Flows, a repository heading is repeated for its text-only matches). While a query is active the box shows "N of M" for the visible
category, the query is kept per tab, and the open item stays open when the query
hides it. The filter runs in the page: `GET /ui/api/flows?with_content=1` adds a
`content` field to every flow, including repository flows; without the parameter
the response is unchanged.

Markdown is shown rendered. A flow opens in a *Rendered* view with a
*Rendered / Source* switch; editing happens in *Source*, which is the textarea
that save, unsaved-change tracking and conflict detection keep using, so
*Rendered* always shows the current unsaved text. New drafts, "Edit copy", a
loaded history version and a save conflict open in *Source*. A rule, skill or
implant body opens rendered, with *Source* showing the raw text. A leading
frontmatter block is a collapsed "Metadata" section. When a document has
headings (levels 1-4), a table of contents appears on its left and an entry
scrolls the document to its heading. "Hide contents" hides it; while hidden, hovering the left edge
of the view shows it as an overlay, and the toolbar's "Contents" button
toggles it (the way on touch screens). The choice is remembered in the browser's
`localStorage` and shared by all documents; without a stored choice it starts hidden at
widths up to 760 px. The renderer is part of the page script (no library, no
external request). It creates DOM nodes with `createElement`, `createTextNode`
and `setAttribute` only, so HTML in a document stays visible text; links are
made only for `http:`/`https:` URLs and in-page `#anchors`, images show their alt
text, and heading ids carry an `md-` prefix. Syntax it does not handle degrades to
readable text.

The switches are one installation-wide state in `flows/.user/components.json`
(git-ignored, written atomically, read fresh on every bundle build; a missing or
damaged file means everything is on). A switched-off component is left out of
persona bundles: its block, `*_loaded` list, footer entry and therefore
`bundle_revision`. Switching off a rule, a core skill or a preferred implant is
never an error; with every rule off the `rules_block` is empty and the footer
shows `—`, as with `RULES_ENABLED=0`. The `load_implants` tool also leaves out
switched-off implants. The per-query path (`_load_and_enrich` and the enrichment, which only the
evaluation harnesses use) ignores the switches on purpose, so evaluation results do not depend on local
settings. A change applies to the next bundle the server builds (a new
conversation, a switch to another agent, a restore, or a refresh whose revision
changed); conversations that keep their bundle are unaffected.

Access is separate from MCP. Nothing has to be run: when the page gets `401`,
it asks `/ui/api/session` to sign it in without a code, and the daemon agrees
only if the other end of that loopback connection is a process of the OS user
running the daemon. The check reads the operating system's connection table
([`src/daemon/peer.py`](../src/daemon/peer.py)): `/proc/net/tcp` and `tcp6` on
Linux (they record each socket's owner), `GetExtendedTcpTable` on Windows (the
owning process's user SID, and that process must be older than the socket's
bind, so a reused PID does not count), and `lsof` elsewhere, including macOS
(without root it lists only this user's processes, so another account's
connection is never found). Every error is a refusal, and so is any request
carrying `Forwarded`, `X-Forwarded-For`, `X-Forwarded-Host` or `X-Real-IP`; the
daemon starts uvicorn with `proxy_headers=False`, so the checked address is
always the socket's own peer. The trust boundary is your OS account: a process
of yours that relays connections for others, such as Docker Desktop's
`host.docker.internal` forwarding, `ssh -R` or a tunnel to port 8765, also
passes. If that matters on your machine, `flows-ui --auto off` turns automatic
sign-in off (the marker `ui_auto_sign_in_off` in the state directory) and, like
`--revoke`, replaces the key, ending every session; `--auto on` turns it back on.
An automatic sign-in still being checked when either command runs is refused,
and its cookie is signed with the key read before the check, so no automatic
session outlives either command. A refused browser, for example one run by another
account, still signs in with the one-use code: the command obtains it
(valid two minutes) with the service token and opens `/ui#code=...`. Either way
the page receives an HttpOnly, SameSite=Strict cookie limited to `/ui`. The
browser never receives the bearer token, and the cookie cannot call `/mcp` or
administration. Requests must use the loopback Host; changes, sign-in included,
also need a same-origin `Origin` and the `X-Agents-UI` header, so a cross-site
page cannot sign itself in. The page loads no external assets and runs under a
nonce-based Content Security Policy.
The cookie is signed (HMAC-SHA256) with a random
key in the private state directory (`ui_session_key`, mode 600, never logged or
served), so no session table exists and sessions survive daemon restarts and
updates. A session lasts 30 days from the last visit: every editor API response
sets a fresh cookie, so a browser that opens the page at least monthly stays
signed in. `flows-ui --revoke` replaces the key, which ends every session at once
(the daemon rereads the key on each request, so it works while the daemon runs);
browsers of the daemon's user then sign in again by themselves unless
`--auto off` is set, while a copied cookie stops working. `token rotate` does not revoke editor sessions. The sign-in
page, which names the command, appears only when automatic sign-in is refused. The
header shows `Agents-Core <version>` once. The persona footer links the version to
the bare `http://127.0.0.1:<port>/ui` address, which never carries a code. A copied valid
cookie works until revoked, like any bearer cookie; it is HttpOnly, limited to
`/ui` and useful only on the loopback port.

## Memory and errors

HTTP never selects a project from cwd, environment variables, or client roots.
`X-Agents-Workspace` carries a UUID from the private registry. Errors
`workspace_required` and `workspace_invalid` mean memory is unavailable: routing
can continue, and logging must not be retried in a loop. `run_flow` also requires
this header and never uses `repo_path` as a replacement for workspace identity.
`list_flows`, `get_flow`, `save_flow` and `delete_flow` for built-in and personal
(`user:`) flows work without it; `repo:` flows need it. See the
[flow guide](../flows/README.md) for loading workflows into the caller's repository.

On both transports, memory tools also return
`workspace_invalid: memory path escapes workspace` when the project's
`history.md`, `history/`, `CLAUDE.md` or `data/memory` resolves outside the
project, for example a `CLAUDE.md` symlinked to a shared file. Registering the
workspace again does not help: replace such links with files inside the project.
Workflows do not check these paths.

After `describe_repo`, pass the original `workspace_id`, `repo_path`, and
`repo_hash` to `write_repo_summary` together with the summary. The header must
identify the original workspace; source files are hashed again under the project
lock. If the write is rejected, obtain a fresh description. After an ambiguous
network result, read the output before retrying.

Stable sidecar locks protect `history.md` and managed sections. The daemon's
derived router and history indexes live in private service state. On platforms
with process locking, stdio reuses persistent slots under `data/stdio` in the
installation, holding an exclusive slot lease for the process lifetime. Concurrent
stdio servers use separate slots, and history indexes within each slot are keyed
by workspace. A restart reuses an available slot and its compatible indexes.
Without process locking, stdio uses temporary state and file locks serialize
threads within that process only. The HistoryStore LRU holds
at most eight stores and retains active entries. Cache resets and rollback
retain source history.

## Updates and recovery

```bash
.venv/bin/python -m src.daemon update
.venv/bin/python -m src.daemon recover
```

Updates require a clean target branch, a fast-forward, and unchanged dependency
manifests. Clients may stay connected (see [Drain and connected
clients](#drain-and-connected-clients)). The controller enters
maintenance, waits up to 60 seconds for drain,
stops the service, and acquires the exclusive installation lease and updater
lock. A running stdio server of this installation blocks the update. Reindexing
runs in a separate process only while the daemon is stopped. Git and reindex
subprocesses retain leases until they exit. While the service is installed, this
installation's stdio servers do not self-update, and during maintenance or an unfinished
transaction they exit at startup with
`Shared service is in maintenance; use the controller to recover`.

After the file transaction, writer leases are released and the same LaunchAgent
receives a single-use probation admission. The update succeeds only after
readiness. A failed warmup restores code and indexes and checks readiness of the
restored runtime. The controller journal remains until readiness completes; use
`recover` after interruption. Do not delete journals manually.

A manual `update` always drains and restarts a running service, even when there
is nothing to apply. When it applies a commit, it also starts a stopped service
for probation and leaves it running; `auto-update` leaves a stopped service alone.

While the service runs, update the installation only with `update` or
`auto-update`: `install.sh`, `git pull` or `scripts/init_repo.sh` in the checkout
change code, dependencies or indexes without drain, maintenance, index backup,
probation or rollback. `update` refuses a target that changes dependency
manifests. For such a target, `stop` the service and close this installation's
stdio servers, fast-forward the checkout (`git pull --ff-only`), install the
dependencies as setup does (`.venv/bin/python -m pip install -r requirements.txt`),
then `start` the service; its warmup rebuilds changed skill and implant indexes.
Git credentials and SSH must work with the LaunchAgent's PATH and environment.

### Automatic updates (opt-in)

```bash
.venv/bin/python -m src.daemon auto-update enable
.venv/bin/python -m src.daemon auto-update enable --interval 900 --idle-seconds 120
.venv/bin/python -m src.daemon auto-update status
.venv/bin/python -m src.daemon auto-update disable
```

`enable` installs a second LaunchAgent, `local.agents-core.<state-directory-name>.updater`,
that runs `auto-update run` every `--interval` seconds (default 900, minimum
60). A run fetches `AGENTS_AUTO_UPDATE_REMOTE` / `AGENTS_AUTO_UPDATE_BRANCH`
(defaults `origin` / `main`) and stops there when the installation is up to date.
Unlike stdio servers, the controller reads these variables from its own
environment, not from `.env`: the updater LaunchAgent does not set them, so
scheduled runs normally use the defaults, and a manual `update` uses the values
exported in its shell. A run skips a target without touching the service when
the checked-out branch is different, tracked files have local changes, the
target is not a fast-forward, or dependency manifests changed; each skip is
logged once per target. Otherwise it waits until the service is ready, has no
work in flight, and has been idle for `--idle-seconds` (default 120), and then
runs the same transaction as `update`. It also waits while any stdio server of
this installation is running, since `update` would stop the service only to find
it busy. A stopped service is left stopped. An unfinished transaction blocks
further runs until `recover`.

Downtime is the stop, the reindex, and the warmup. The reindex re-embeds only
when skills or implants changed, and only one model is loaded at a time. The
last outcome is in `auto-update.json` and the history in `auto-update.log`, both
in the private state directory. `uninstall` also removes the updater.

### Client rollback and uninstall

`migrate` reports its private backup directory. To roll back client migration,
run `restore-clients` before `uninstall`; `uninstall` deletes the service
configuration that restoration needs:

```bash
.venv/bin/python -m src.daemon restore-clients /absolute/private/backup
.venv/bin/python -m src.daemon uninstall
```

Both commands drain the service first (see [Drain and connected
clients](#drain-and-connected-clients)) but do not restart it. Clients connected
to the service lose Agents-Core until you reconnect MCP, which after
`restore-clients` loads the restored stdio entries.
Restoration checks the maintenance barriers and holds the controller lock across
daemon shutdown and configuration writes. It also checks that configurations
have not been edited since migration. `token rotate` and setup runs on an
installed service also write backups under `backups/` in the private state
directory without printing their paths; directory names follow creation time,
and `migration.json` names the most recent. Restore backups newest first: a
migration backup that wrote the token into client files is refused with
`Client config changed after migration` until the backup of any later
`token rotate` is restored, which also returns the previous token. Uninstall
retains backups, the registry, and history.

## Validation

Test isolation and model selection are described in
[tests/README.md](../tests/README.md#isolation-and-resource-use). Unless
`EMBEDDING_MODEL` is set in the environment or the checkout's `.env`, tests use
the engine default `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`,
not the daemon's fixed e5-large model.

Run the suite from a checkout with `tests/conftest.py` and its development dependencies:

```bash
LANGFUSE_TRACING_ENABLED=false scripts/run_tests.sh
LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest -m slow tests/test_routing.py
node --test bridge/test.mjs
.venv/bin/python scripts/daemon_smoke.py
.venv/bin/python scripts/daemon_smoke.py --soak
```

Run the slow tests, the smoke test and the soak test one at a time, never
alongside another embedding process. See [tests/README.md](../tests/README.md)
for environment setup.

Fast transport, controller, and configuration tests do not load the model. Smoke
tests require the e5-large model at `~/.cache/fastembed`, use port `18765`, one
temporary daemon, and isolated projects. They can still rebuild derived indexes
in the installation checkout, so run them in a dedicated checkout rather than a
live installation. They cover 20 clients, memory, digest and identity checks,
absence of sampling, and graceful shutdown. The soak test adds 1,000 connections, 20
workspaces, and a five-minute idle CPU measurement. Verify Dock launch,
sleep/wake, and reconnection separately in each GUI client; CLI and HTTP tests
do not cover those application workflows.
