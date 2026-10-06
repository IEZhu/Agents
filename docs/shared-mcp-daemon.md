# Shared MCP daemon operations

A macOS LaunchAgent serves local MCP clients at `http://127.0.0.1:8765/mcp`.
One Python process holds the embedding model (`microsoft/harrier-oss-v1-270m` by
default), the router, and shared
indexes. Desktop Chat connects through `bridge/stdio.mjs` (Node 22+, no npm
dependencies). MCP SDK 1.28.1 is pinned in `pyproject.toml`, `requirements.txt`,
and `uv.lock`. The port above is the default; `install --port` can change it.
Installation requires the default model's plain-file copy under
`FASTEMBED_CACHE_DIR` (default `~/.cache/fastembed`, then
`local/microsoft--harrier-oss-v1-270m/<pinned revision>/` with its `.complete`
marker); `install` does not download the model. `scripts/init_repo.sh` downloads
it, since setup installs this one model on every machine. `install --model
intfloat/multilingual-e5-large` pins a cached e5-large snapshot instead. See
[Embedding model](#embedding-model) for the switch on update. `install` reads `FASTEMBED_CACHE_DIR` from the shell
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

### Sync at installation

`install` reports `user_sync`: `set up`, `pending` (set up but not started) or
`off`, with how to finish or set up [user library sync](#user-library-sync): the
[Sync page](#sync-page) of `flows-ui` once the service runs, or
`python -m src.user_sync setup` in a terminal. When setup asks whether to set up
sync and the service is installed, yes opens that page only when the service
answers `/health` as ready and its page has the Sync page; otherwise setup starts
the terminal wizard, without a connection error. The wizard, and setup from the
`AGENTS_USER_SYNC_*` variables ([installers](user-sync.md#installers)), keep
sync's settings in the service's state directory (`<directory>/user-sync`, the
directory `install --state` chose), where its loop finds them.

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
with the service's pinned model before it reports ready. `clear-cache` clears only the
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
(`io_pending` for running or queued I/O jobs, user sync's engine calls included,
`inference_pending` for model work), and the `user_sync` summary
(see [User library sync](#user-library-sync)).
When the service does not respond, `status` also reports `supervised` (launchd
has the job loaded), `maintenance` and `transaction`, and reads the `user_sync`
summary with the sync engine; a `transaction` that remains while no controller
command is running requires `recover`. `/health` requires a
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
jobs, including user sync's last cycle, at most 10 seconds after the last
request) and `streams` (open notification streams) all to reach zero. If any stays
above zero, including a stream that fails to close, the drain times out: the
controller resumes the service without killing active work, and the command fails
with `Drain timed out; runtime resumed without killing active work` without
applying its change. Retry after the work finishes. Two commands differ:
`uninstall` has already disabled automatic updates, and `token rotate` rolls back
by draining again and restarting the service; if that second drain also times
out, run `recover`. `log_interaction` answers before its history and Langfuse
writes, so the drain does not count those queued writes; the stopping service
flushes them for up to 10 seconds before it exits.

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

The daemon serves a local settings page at `/ui` with five tabs.

- **Flows** edit [personal and repository
  flows](../flows/README.md#personal-and-repository-flows). A User/System switch
  shows one category at a time. *User* lists personal flows and, below them, the
  repository flows of every repository under `flows/.user/repos/`, grouped by
  the stored origin (else the last known path, else the key); no repository has to be
  selected, and an existing repository flow is opened, saved, deleted and its
  history shown by key. *System* lists the read-only built-in flows. Creating a
  repository flow, or "Edit copy for a repository", asks which registered
  workspace it belongs to. Personal and repository flows keep saved versions,
  compare a local copy with its built-in flow ("Show built-in") and detect
  conflicts with edits made from chat.
  The **Persona** panel of an open flow (built-in ones included) chooses the
  agent and, per kind, either the agent's default or an exact list of skills,
  implants and rules; see [choosing a flow's
  agent](../flows/README.md#choose-a-flows-agent-and-components). The choice is
  personal, stored in `flows/.user/personas/`, and never changes the flow's text;
  "Reset to flow default" restores the flow's frontmatter. Components switched
  off on the other tabs are marked "(off)" and still load when a flow names them.
- **Agents** lists the same agents as the Persona panel: every
  `agents/<name>/system_prompt.mdc` whose `identity.name` matches its directory,
  by display name with the role below. An open agent shows its ID, role, tone,
  trigger command, aliases when it has them, routing keywords, core, preferred and
  capable skills, preferred implants and a read-only view of its prompt body. The
  tab is read-only: agents have no switch, and their files are never changed.
- **Rules**, **Skills** and **Implants** list every `rules/rule-*.mdc`,
  `skills/*.mdc` and `implants/*.mdc` with its description, a read-only view of its
  body (skills also show the agents that declare them and their tier, implants the
  agents that prefer them) and a switch. The files are never edited, renamed or
  deleted.

The page fills the window: the header and the detail pane stay in place and only
the list on the left scrolls (in the narrow layout, the list above the detail
pane scrolls on its own, and the detail pane scrolls separately when its content is
taller). The header holds the version, the [sync status](#sync-page), the
controls of the open item, the tabs and, on Flows, "New flow". A flow's controls are *Rendered / Source*, "Contents",
the history, "Show built-in" (on a local copy of a built-in flow), "Delete" and
"Save" (a built-in flow shows its "Edit copy" buttons instead of the history,
"Delete" and "Save"); a rule, skill or implant has *Rendered / Source*, "Contents" and its
switch, and an agent the same without a switch. With nothing open the header
shows none. The controls end at the divider before the tabs and form the tab of
the open item's pane: the two share one tint and join like a folder and its tab,
and the list and the item are rounded panels. The page measures where the
controls fit: when the tab would reach past the pane's rounded corner, the controls
move left, right after the version, and keep the tint without the join; when the
first header row cannot hold them, they take a row of their own, right-aligned. The
item's title stays above the document. In the narrow layout, where the list sits
between them, the tab and the pane keep the tint but are not joined.
Every tab has a search box above its list (`/` focuses it, `Esc` clears
it). Matching is case-insensitive and every whitespace-separated term must
appear. Items whose name (ID, title, short name; an agent's ID and display name)
matches are listed first, then items that match only in their text (description
and body; flow content; an agent's role, routing keywords and prompt body), marked
"in text" (on Flows, a group heading, such as Personal or a repository, is repeated for its text-only matches). While a query is active the box shows "N of M" for the visible
category, the query is kept per tab, and the open item stays open when the query
hides it. The filter runs in the page: `GET /ui/api/flows?with_content=1` adds a
`content` field to every flow, including repository flows, and
`GET /ui/api/agents?with_content=1` adds the fields the Agents tab shows and
searches; without the parameter either response is unchanged (for agents, the
`id`, `display_name` and `role` that the Persona panel uses).

Markdown is shown rendered. A flow opens in a *Rendered* view with a
*Rendered / Source* switch; editing happens in *Source*, which is the textarea
that save, unsaved-change tracking and conflict detection keep using, so
*Rendered* always shows the current unsaved text. New drafts, "Edit copy", a
loaded history version and a save conflict open in *Source*. A rule, skill,
implant or agent body opens rendered, with *Source* showing its raw text; these
bodies arrive without their frontmatter (the Agents tab lists an agent's fields).
A flow's leading frontmatter block is a collapsed "Metadata" section. When a document has
headings (levels 1-4), a table of contents appears on its left and an entry
scrolls the document to its heading. "Hide contents" hides it at once, even with the pointer
still over it; while hidden, hovering the left edge of the view shows it as an overlay, and the
header's "Contents" button
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

### Sync page

A chip under the version says how [user library sync](user-sync.md) stands, in
words: `Synced 2m ago`, `3 pending`, `Syncing…`, `Offline, retry 14:05`, `Paused`,
`Needs attention` (a setup that has not started included), `2 conflicts` or
`Sync off`. It sits under the version, in the height the version's line had, so it
takes no width from the open item's controls and the header keeps its one row at
1250 px. A click opens the Sync page in place of the list and the item; a tab, or
the chip again, returns to what was open, unsaved text included. The page reads the status
when it loads and every 30 seconds while the browser tab is visible; a status that
cannot be read shows as "Sync status unavailable", not as the last one that could.
These polls take the status the daemon keeps while it is younger than a scan
interval, counted from its read or from the last scan that confirmed it. A scan
confirms it only when nothing changed in the library or in sync's own files (the
settings, the state and this machine's key pair) since that status was read with
the library in sync, so what the command line changes (`configure`, `run`,
`github regenerate-key`, `disconnect`) shows within a scan. A status read that
started before a newer one was kept, or before an operation that changed what it
says ended, is not kept. Opening the Sync page and the page's own actions read the
status afresh, and no poll replaces such a read while it runs. Polls do not count
as activity, so an open page never holds back an automatic update; a GitHub
sign-in's polls do while the sign-in waits for its code, because an update would
end it.

While sync is off, the page is a wizard:

1. **Connect.** GitHub sign-in shows the code on the page with a link to GitHub's
   device page; the daemon asks GitHub for the code and polls it, so neither the
   device code nor the token reaches the browser. Another host takes an SSH URL.
2. **Repository** (GitHub): a library found on the account, a new private
   repository (default `agents-library`) or another private one by `OWNER/NAME`.
3. **Identity**: the commit name and email (required) and the machine label. Setup
   follows: another host's key fingerprints to confirm, this machine's public key
   to add as a deploy key (on GitHub it is added, and a missing one can be added
   again), and Check access.
4. **What syncs**: every scope group on by default; including one again first lists
   the files it would upload. "Ask before uploading" is set here too.
5. **Preview**: uploads, downloads, deletions on this machine, conflicts, files the
   scanner held back and repository groups with their origins. Where privacy cannot
   be checked, the owner confirms it. Start sync sends the preview's hash, so a plan
   that changed meanwhile is shown again instead of started.

Once sync has started, the page has these sections:

- **Status**: state, last success and attempt, repository (`owner/name` on GitHub,
  never with credentials), branch, pending changes and next fetch; Sync now,
  Pause or Resume (which answer at once, also while another request of the page
  runs), and the fetch interval. A confirmation the engine waits for (a rewritten
  remote, a mass deletion) shows its preview with "Confirm and sync".
- **Machines**: this machine's label and the deploy keys Agents-Core added to the
  repository on GitHub (titled `Agents-Core <label>`), with Remove for the other
  machines. Deploy keys that sync did not add are listed apart, under "Other deploy
  keys", and never removed from here. This machine's key is marked in either list:
  found by its public key, or by the deploy key recorded as this machine's when sync
  added or found it (`deploy_key_id` in `user-sync.json`).
- **Conflicts**: Open (a flow opens in the editor with the kept version beside the
  current text in the split pane; another file shows both versions here), Keep
  current, Use mine (a normal save of the kept version) and Dismiss.
- **What syncs**: the groups with their switches, repositories uploaded for the
  first time with a one-click Exclude, groups waiting for approval with Approve
  (after their upload list), and "ask before uploading".
- **Access**: the GitHub account (Reconnect; Forget account, with the page where
  the authorization is revoked) or manual SSH, this machine's public key with Copy,
  Check access and Regenerate key. For a repository on GitHub, a new key needs the
  account signed in: the new key becomes a deploy key first and the old one is
  removed after it; for another host, you add the new public key yourself.
- **Identity**: commit name and email, machine label.
- **Activity**: the last 20 cycles with their time, result, the flows sent and
  received and the machines they came from; changed non-Markdown files (scripts)
  are marked separately.
- **Disconnect**: the library's files and `.git` stay; this machine's key is deleted
  here and, while the GitHub account is signed in, its deploy key (found by the key,
  or by the recorded `deploy_key_id`) is removed from the repository (otherwise the
  page says to remove it in the repository's settings).

A flow open in the editor that a cycle updates shows "Updated from laptop at 14:02"
with Reload. Unsaved text stays: saving it meets `flow_conflict`, and the message
names the sync while the split pane shows the synced text. A flow that a cycle
deleted says so, and its text stays in the editor to copy. A setup that stopped at
the host's fingerprints, and then the page was reloaded, shows the fingerprints
again. On GitHub, where nothing waits for a confirmation, such a setup (one that
stopped before GitHub's host keys were stored) offers Set up again, which stores
them and adds this machine's deploy key, as the first setup does. The page chooses
between the wizard and its steps by whether sync is set up and started, which every
answer reads afresh, never by a status that may be a scan old.

While a request of the page runs, its other controls wait: buttons ignore clicks,
text fields are read-only and switches and choices go back, so nothing typed
meanwhile is lost or sent half-changed. Pause and Resume never wait.

The page calls only the daemon, under `/ui/api/sync`, with the editor's
protections: loopback Host, the session cookie, `X-Agents-UI` and a same-origin
`Origin`. A GET here needs the header too, and an `Origin` it carries must be the
page's own, because some reads reach the network. Answers carry
`Cache-Control: no-store` and never the GitHub token, this machine's private key or
a sign-in's device code.

| Route | Body or query | Runs |
|---|---|---|
| `GET /ui/api/sync` | optional `fresh=1` | the engine's status with `loop`, `set_up`, `started`, `conflict_list` (at most 50; `conflict_total` counts them all), `repository`, `github` and `host_key_unconfirmed`, which are read with every request; the status itself, without `fresh=1`, is the one the daemon keeps (read, or confirmed by a scan, within a scan interval: see above), and with `fresh=1` a read of its own that starts with the request |
| `POST /ui/api/sync/github/device`, then `GET …/github/device?attempt=` | — | at once; a poll sooner than GitHub's interval is answered without asking GitHub, polls of one sign-in run one at a time, and a sign-in that GitHub grants after Forget account keeps nothing |
| `GET …/github/libraries`, `POST …/github/create` (`name`), `…/github/forget` | — | at once (GitHub's API only) |
| `POST /ui/api/sync/setup` | `github` (`owner/name`) or `remote`; `name`, `email`; optional `label`, `trust_host_key`, `ask_new_repositories`; or `again` (with an optional `trust_host_key`): setup with the remote and identity sync keeps, followed on a repository of the signed-in GitHub account by this machine's deploy key | queued |
| `POST …/check`, `GET …/preview`, `POST …/start` (`confirm`, optional `confirm_private`), `POST …/run` (optional `confirm`), `POST …/key/regenerate`, `POST …/github/add-key`, `POST …/disconnect` | | queued |
| `PUT /ui/api/sync/settings` | `fetch_minutes`, `ask_new_repositories`, `name`, `email`, `label`, `paused` | at once |
| `GET …/conflict?id=`, `POST …/conflicts/resolve` (`id`, `action`: `keep`, `mine` or `dismiss`) | | at once |
| `GET`, `PUT …/scopes` | `exclude`, `include`, `approve`, `approve_files`, `confirm` | at once; including or approving answers `confirmation_needed` with `hash` and `upload` until the same request carries that hash |
| `GET …/machines`, `POST …/machines/remove` (`id`) | | queued, because they read this machine's key, which a new key may be replacing (GitHub's API only; no status is read after them); `managed` marks the keys sync added and `this` this machine's key; this machine's key (`this_machine`) and keys sync did not add (`not_a_machine`) are refused |

Queued operations run in the sync task, one at a time with its cycles (see below).
These requests do not count in `inflight`: like `/admin/user-sync/*` they follow the
sync task's drain. Bodies are JSON objects of at most 64 KiB (413 otherwise). Answers:

- 400 with `status`, `reason` (`invalid_request`, or `identity` for a name, email
  or label that sync or git would refuse, checked before anything is stored) and
  `message` for a body or parameter that does not fit the route;
- 404 for an unknown route, and for a sign-in poll whose attempt is unknown or over
  (`reason: no_sign_in`);
- 409 with `status`, `reason` and `message` when the engine or GitHub refuses, among
  them a sign-in poll after its code expired (`reason: expired_token`, which ends
  the attempt) and one that Forget account cancelled (`reason: cancelled`);
- 503 with `error: draining` and a `message` while the service drains, and with
  `error: unavailable` when the sync task is not running, for a queued operation
  that would never start;
- 500 without a traceback for an unexpected failure.

## User library sync

When [user library sync](user-sync.md) is set up and started, the daemon runs its
cycles (`src/daemon/sync_loop.py`). Its task starts with the service and does
nothing by itself until the service is ready, so it never competes with the model
warmup. Triggers:

- **A change in this process.** A save in the flow editor (flows, personas,
  component switches) or through an MCP tool (`save_flow`, `delete_flow`,
  `set_flow_persona`) schedules a cycle 10 seconds later. Later changes do not
  move it; a change during a cycle schedules one more.
- **A scan every 30 seconds** finds writes by stdio servers and manual edits. It
  compares the names, sizes and modification times below the library (`.git`
  aside) with those recorded when the library was last seen in sync; only when
  they differ does it ask the engine's status, which reads every file and applies
  the scope rules. A change that needs sending starts a cycle at once.
- **A fetch every `fetch_minutes`** (the setting in `user-sync.json`, 1 to 60,
  default 5), and one right after the service becomes ready. After a network
  failure the next cycle waits for the engine's `retry_at` (1, 2, 5, 10, then
  every 30 minutes); "Sync now" does not.
- **Sync now:** `user-sync run`, `POST /admin/user-sync/run` or the [Sync page](#sync-page).

Nothing runs by itself while sync is off, waiting for access (set up but not
started) or paused, or while `maintenance.json` or `transaction.json` shows an
update transaction; a held cycle runs when the transaction ends, and until then
the summary says `held: "update"` (a file that stays behind after an interrupted
update needs `recover`). Cycles, `run`, `setup`, `check`, `preview`, `start` and
`disconnect`, and the Sync page's operations that run git or reach the remote (a new
key included), run one at a time, so the service's own operations do not collide.
`pause` and `resume` answer at once, also while one of them runs, and so do the
page's settings, scopes and conflicts: the loop
follows the settings it read last, so a cycle or job that read them before a
pause never turns sync back on. Engine calls run in their own threads and count
in `io_pending`: automatic updates and a drain wait for them.

When the service starts, and at `install`, it removes this installation's
scheduled sync run (`python -m src.user_sync schedule`), which would only compete
with the daemon's loop for the sync lock; a removal that fails is logged in
`service.log`. `uninstall` says how to schedule runs again.

On drain (`stop`, `restart`, `update`, a logout), once the requests admitted
before the drain have ended, the task commits and pushes what is left in one last
cycle: changes heard or found that no cycle sent yet. It has 10 seconds, and
`io_pending` holds the drain until then. A cycle still running after that is
abandoned: it stops counting, the task ends, and the stopping process ends it.
The engine's sync lock and its stale-lock cleanup make that harmless; the next
start syncs what was left. This last cycle also runs during an update
transaction, which stops the service next. After `/admin/resume` the loop
continues, and every later drain gets its own last cycle, also one that starts
while the previous drain's last cycle still runs.

Use the commands below while the daemon runs. The engine's own command line
(`python -m src.user_sync`), which also lists and resolves conflicts and changes
the scope, keeps working next to it: the engine's lock lets one runner sync at a
time, and the other reports `lock_held`. The scan picks up the files it changes.

```bash
.venv/bin/python -m src.daemon user-sync status
.venv/bin/python -m src.daemon user-sync setup --remote git@github.com:me/agents-library.git \
    --name "My Name" --email me@example.com [--label laptop] [--ask-new-repositories]
.venv/bin/python -m src.daemon user-sync check
.venv/bin/python -m src.daemon user-sync preview
.venv/bin/python -m src.daemon user-sync start --confirm <hash from preview>
.venv/bin/python -m src.daemon user-sync run [--confirm <hash>]   # Sync now
.venv/bin/python -m src.daemon user-sync pause | resume | disconnect
```

Each command calls the service and prints its JSON answer with `"via": "service"`.
When nothing listens on the service's port, or the service is not installed, it
runs the same engine operation in the command's own process (`"via": "direct"`)
on the service's library (`AGENTS_USER_FLOWS_DIR` from the environment or `.env`)
and its state directory, holding the installation's session lease like the
engine's command line: while an update is installed it answers `busy` and runs
nothing. A service that answers slowly (`timeout`), resets the connection
(`service_unreachable`) or answers with something other than its JSON
(`service_error`) is not bypassed, so an operation it may still finish never runs
twice. The exit code is 1 when the answer is an error or the state needs
attention, `status` included; `busy`, like `lock_held`, is not an error. `setup`
also takes `--branch`, `--trust-host-key SHA256:…` and `--confirm-private`, as in
[the engine's command line](user-sync.md#command-line).

The service's endpoints need the bearer token like every `/admin` path:

| Endpoint | Body (JSON) | Answer |
|---|---|---|
| `GET /admin/user-sync/status` | — | the engine's status and `loop`: `state`, `active`, `syncing`, `held`, `next_fetch_seconds`, `last_cycle` |
| `POST /admin/user-sync/run` | optional `confirm` | one cycle that ignores the retry delay |
| `POST /admin/user-sync/setup` | `remote`, `name`, `email`; optional `label`, `branch`, `ask_new_repositories`, `trust_host_key`, `confirm_private` | the engine's setup result, with the public key to add as a deploy key |
| `POST /admin/user-sync/check`, `preview` | — | the engine's results |
| `POST /admin/user-sync/start` | `confirm`: the preview's hash | the first upload or join |
| `POST /admin/user-sync/pause`, `resume`, `disconnect` | — | the engine's results |

An engine refusal answers 409 with `status`, `reason` and `message` (for example
`lock_held` from `check` while another runner holds the library); a body that is
not a JSON object, or a client that leaves while sending it, 400, a body larger
than 64 KiB 413, a wrong method 405, an unexpected failure 500 without a
traceback, and a draining service 503 for everything except `status`, `pause`
and `resume`. `/health` and the controller's `status` include `user_sync`:
`state` (`syncing` while a cycle runs), `reason`, `last_success`, `conflicts`,
`pending` and `held`, never the key, the remote, the identity or a path. When the
service does not answer, `status` reads that summary with the engine.

## Memory and errors

HTTP never selects a project from cwd, environment variables, or client roots.
`X-Agents-Workspace` carries a UUID from the private registry. Errors
`workspace_required` and `workspace_invalid` mean memory is unavailable: routing
can continue, and logging must not be retried in a loop. `run_flow` also requires
this header and never uses `repo_path` as a replacement for workspace identity.
`list_flows`, `get_flow`, `save_flow`, `delete_flow` and `set_flow_persona` for
built-in and personal (`user:`) flows work without it; `repo:` flows need it. See the
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
clients](#drain-and-connected-clients)).

The update is built while the service keeps serving. Holding the updater lock,
the controller checks out the target in a worktree under `data/.prepared/<sha>`.
It copies the live skill and implant indexes there and runs the reindex in a
separate process with the service's interpreter and model. The reindex re-embeds
only an index whose sources changed. While it re-embeds, a second model process
runs next to the service. A target that moved or changes dependency manifests is
refused before anything is built. The reindex gets the installation's `.env`
under the controller's own settings, as the service does. The build records
the settings that shape the embeddings (`EMBEDDING_*`, `AGENTS_MODEL_*` and
`FASTEMBED_*`). If they change while it builds or at any point before
activation, which checks them once more after the stop, the build is not
activated and is made again, so the restarted service does not re-embed its
indexes during warmup. Every build passes
activation's checks before the service stops, so a build that activation would
refuse fails here. A failed build leaves the service and the live tree untouched,
and the next run retries it.

A build can take minutes: scheduled runs build at the updater LaunchAgent's
`Background` priority (see [automatic updates](#automatic-updates-opt-in)). The
controller does not hold the control lock while it builds, so `stop`, `restart`
and `auto-update disable` keep working, and `AGENTS_AUTO_UPDATE_REINDEX_TIMEOUT`
(default 3600 s) only stops a hung build. Afterwards the controller takes the
control lock again. If the service configuration changed meanwhile, it defers,
and the next run builds for the new configuration. A scheduled run then checks
again that the service is ready and idle and that no stdio server holds the
installation. If that changed, it defers and keeps the build, and the next
scheduled run uses that build without rebuilding; a manual `update` always
builds anew.

Only then does the controller enter maintenance, wait up to 60 seconds for drain,
stop the service, and acquire the exclusive installation lease and the updater
lock. A running stdio server of this installation blocks the update. Activation
backs up the live indexes, fast-forwards the checkout to the built commit and
moves the built indexes into `data/`. These are file operations only, so the
service is down for its stop, the activation and its warmup. Before the build
moved ahead of the stop, the reindex ran while the service was down: on
2026-10-04 an update that added one skill kept it down for six minutes, and
Claude Code, which stops reconnecting after about 17 seconds, had to be
reconnected by hand. A failed fast-forward or move restores the previous code
and indexes. If the tree changed after the build was checked, activation discards
the build and the service restarts on the previous code. A rollback or `recover`
also discards the build, so a partly moved build is never activated later. Git
and reindex subprocesses retain leases until they exit. While the service is installed, this installation's stdio servers do not
self-update, and during maintenance or an unfinished transaction they exit at
startup with `Shared service is in maintenance; use the controller to recover`.

After the file transaction, writer leases are released and the same LaunchAgent
receives a single-use probation admission. The update succeeds only after
readiness. A failed warmup restores code and indexes and checks readiness of the
restored runtime. The controller journal remains until readiness completes; use
`recover` after interruption. Do not delete journals manually.

A manual `update` with nothing to apply and no model switch pending leaves the
service running. When it applies a commit or switches the embedding model, it
drains and restarts a running service, and it also starts a stopped service for
probation and leaves it running; `auto-update` leaves a stopped service alone.

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
(defaults `origin` / `main`) and stops there when the installation is up to date
and no [model switch](#embedding-model) is pending.
Unlike stdio servers, the controller reads these variables,
`AGENTS_AUTO_UPDATE_TIMEOUT` and `AGENTS_AUTO_UPDATE_REINDEX_TIMEOUT` from its own
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

Downtime is the stop, the activation and the warmup; the build runs before the
stop. The updater LaunchAgent runs as a `Background` process with low-priority
I/O, and its reindex inherits that throttling. A skills index that rebuilds in
about 30 seconds at normal priority took about six minutes in a scheduled run,
which is why the build must not run while the service is down. A model switch
that rebuilt both indexes there came close to the former 600 s ceiling, so
`AGENTS_AUTO_UPDATE_REINDEX_TIMEOUT` now defaults to 3600 s: a build that hits
the ceiling fails and is retried on every run, so a short ceiling on a slower
machine means an update that never lands. The last outcome
is in `auto-update.json` and the history in `auto-update.log`, both in the private
state directory. `uninstall` also removes the updater.

### Embedding model

`service.json` pins the model by `model`, `model_path` and `model_artifact`,
and records `model_generation`. A service installed before the current
generation ([src/model_migration.py](../src/model_migration.py)) moves to
`microsoft/harrier-oss-v1-270m` once, from e5-large or any other model, in an
`update` transaction:

1. While the service still serves, the controller downloads the pinned plain-file
   copy (about 1.1 GB). A failed download changes nothing; the next `update` or
   scheduled run retries it.
2. After the drain, stop and file update, it saves `service.json` and the skill and
   implant indexes in `rollback/` under the private state directory, writes the new
   model into `service.json` and rebuilds the indexes with it, holding the
   installation leases (`AGENTS_AUTO_UPDATE_REINDEX_TIMEOUT`, default 3600 s; in
   the [2026-10-03 measurements](embedding-models-eval-results.md#results) a
   harrier index build peaked at 2.8 GB). Unlike a file update, this rebuild still
   runs while the service is down. A file update that failed to build or to
   activate (`PREPARE_…`, `ACTIVATE_…` or `INVALID_…`) leaves the switch pending
   for the next update.
3. Probation starts the service on the new model. When it fails, the transaction
   restores the previous `service.json`, indexes and code and starts the service
   again; `recover` does the same after an interrupted switch.

Histories re-embed and the router cache resets on first use. With automatic
updates enabled, the first scheduled run after the update that brought this code
switches the model even without a new commit, once the service is idle. Without
them, run `update` once more after the update that brought this code: the
running controller is still the previous version during that update. A model
chosen after the switch stays. To choose one, run `uninstall`, then
`install --model ...` (the model must already be cached) and `start`. `install`
writes a new token, so rerun `migrate` for every client and workspace, and run
`auto-update enable` again if you used it.

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
the engine default `microsoft/harrier-oss-v1-270m` (about 1.1 GB on first use),
which is also the service's default.

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
tests require the default model's plain-file copy under `FASTEMBED_CACHE_DIR`
(default `~/.cache/fastembed`), use port `18765`, one
temporary daemon, and isolated projects. They can still rebuild derived indexes
in the installation checkout, so run them in a dedicated checkout rather than a
live installation. They cover 20 clients, memory, digest and identity checks,
absence of sampling, and graceful shutdown. The soak test adds 1,000 connections, 20
workspaces, and a five-minute idle CPU measurement. Verify Dock launch,
sleep/wake, and reconnection separately in each GUI client; CLI and HTTP tests
do not cover those application workflows.
