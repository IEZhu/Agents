# Shared MCP daemon operations

A macOS LaunchAgent serves local MCP clients at `http://127.0.0.1:8765/mcp`.
One Python process holds `intfloat/multilingual-e5-large`, the router, and shared
indexes. Desktop Chat connects through `bridge/stdio.mjs` (Node 22+, no npm
dependencies). MCP SDK 1.28.1 is pinned in `pyproject.toml`, `requirements.txt`,
and `uv.lock`. The port above is the default; `install --port` can change it.
Installation requires an existing e5-large snapshot under `FASTEMBED_CACHE_DIR`
(default `~/.cache/fastembed`); `install` does not download the model.

The daemon supports persona protocol 2 only. Transport migration does not
rewrite client instructions. Follow [the routing protocol](routing_flow.md) when
keeping, switching, restoring, or refreshing a role. HTTP never samples the final
answer and does not expose `clear_session_cache`; administrative cache clearing
uses the controller command below.

## Installation and client migration

Run commands from the installation root with its Python interpreter. The first
migration requires stopping this installation's stdio processes before replacing
code or connecting the daemon to shared memory. Capture a baseline and back up
client configurations first. Keep parent applications running and retain source
history.

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
to select another port.

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
Module-invoked `python -m src.server` processes are counted across the host because
their command lines do not identify an installation. Check the reported PIDs
when multiple installations are present.

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
| `desktop` | `~/Library/Application Support/Claude/claude_desktop_config.json` | `AGENTS_CLAUDE_DESKTOP_CONFIG` selects an exact configuration file |
| `claude-project` | `<workspace>/.mcp.json` | No environment override; requires `--workspace` |

Export variables in the shell that runs migration or audit. The controller cannot
infer another process's environment from the daemon. A nonempty
`CLAUDE_CONFIG_DIR` also changes the location of the user JSON: explicitly setting
it to `~/.claude` selects `~/.claude/.claude.json`, while leaving it unset selects
`~/.claude.json`. `CODEX_HOME` is the Codex state directory, not the configuration
file itself. Use an existing directory and absolute paths for alternate profiles.

`AGENTS_CURSOR_MCP_CONFIG` and `AGENTS_CLAUDE_DESKTOP_CONFIG` are Agents-Core
controller settings. They tell migration and audit which existing client file to
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

Private state lives at
`~/Library/Application Support/Agents-Core/<installation-hash>`. Directory
permissions are 0700; token, configuration, and backup permissions are 0600.
Place `--state /absolute/private/dir` before the subcommand to use isolated state.
The LaunchAgent is named `local.agents-core.<installation-hash>` and uses
ProcessType Interactive. Service logs are limited to six 10 MiB files; debug
logs are limited to seven days and 100 MiB. Debug writes skip symlinked path
components and create mode-0600 JSON files exclusively. Debug pruning leaves
symlinked directories and JSON entries untouched.

Token rotation includes custom client paths recorded by successful migrations,
using the same private backup journals as standard paths. Merely auditing a file
does not make it a managed rotation target. Rotation also updates private bridge
configurations; it does not depend on the profile environment remaining active.

`status` reports ready, starting, draining, or failed state when the service
responds; otherwise it reports `not_installed`, `starting`, or `stopped` from local
configuration and launchd. A live response includes PID, boot ID, request
counts (`inflight` for work, `streams` for open client notification streams),
`idle_seconds` since the last request finished, and running jobs. `/health` requires a bearer token. Readiness means the
model and indexes have warmed successfully. Admission is bounded at 32 work requests,
plus up to 32 open notification streams counted separately, with eight I/O workers and
one inference worker. Capacity exhaustion returns
busy. Cancelling an HTTP waiter retains the quota for its running job and does
not replay a mutation.

### Drain and connected clients

`stop`, `restart`, `uninstall`, `restore-clients`, `update`, `recover`, and
`token rotate` first drain the service: new requests get 503, and the command
waits up to 60 seconds for `inflight` (work), `io_pending` (queued I/O jobs) and
`streams` (open notification streams) all to reach zero; if any stays above zero,
including a stream that fails to close, the drain times out after 60 seconds.
Connected clients do not need to be closed. Each one holds a long-lived GET notification stream; those
are counted as `streams`, not as work, and drain ends them cleanly
([#76](https://github.com/IEZhu/Agents/issues/76)). The transport is stateless,
so a client's next request reaches the restarted process without a new session.
A request sent during the stop window fails and can be retried.

## Flow editor

```bash
.venv/bin/python -m src.daemon flows-ui          # opens the browser
.venv/bin/python -m src.daemon flows-ui --no-open
```

The daemon serves a local editor for [personal and repository
flows](../flows/README.md#personal-and-repository-flows) at `/ui`. It lists
built-in, personal and repository flows (pick a registered workspace for the
last), edits and creates them, shows saved versions, compares a local copy with
its built-in flow and reports conflicts with edits made from chat.

Access is separate from MCP. The command obtains a one-use code (valid two
minutes) with the service token and opens `/ui#code=...`; the page exchanges it
for an HttpOnly, SameSite=Strict cookie limited to `/ui` (30 minutes idle, eight
hours maximum). The browser never receives the bearer token, and the cookie
cannot call `/mcp` or administration. Requests must use the loopback Host;
changes also need a same-origin `Origin` and the `X-Agents-UI` header. The page
loads no external assets and runs under a nonce-based Content Security Policy.
Sessions live in daemon memory, so a restart requires running the command again.

## Memory and errors

HTTP never selects a project from cwd, environment variables, or client roots.
`X-Agents-Workspace` carries a UUID from the private registry. Errors
`workspace_required` and `workspace_invalid` mean memory is unavailable: routing
can continue, and logging must not be retried in a loop. `run_flow` also requires
this header and never uses `repo_path` as a replacement for workspace identity.
`list_flows`, `get_flow`, `save_flow` and `delete_flow` for built-in and personal
(`user:`) flows work without it; `repo:` flows need it. See the
[flow guide](../flows/README.md) for loading workflows into the caller's repository.

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
lock. A remaining stdio reader blocks the update. Reindexing runs in a separate
process only while the daemon is stopped. Git and reindex subprocesses retain
leases until they exit.

After the file transaction, writer leases are released and the same LaunchAgent
receives a single-use probation admission. The update succeeds only after
readiness. A failed warmup restores code and indexes and checks readiness of the
restored runtime. The controller journal remains until readiness completes; use
`recover` after interruption. Do not delete journals manually.

Dependency changes require a separate environment installation during maintenance.
Git credentials and SSH must work with the LaunchAgent's PATH and environment.

### Automatic updates (opt-in)

```bash
.venv/bin/python -m src.daemon auto-update enable
.venv/bin/python -m src.daemon auto-update enable --interval 900 --idle-seconds 120
.venv/bin/python -m src.daemon auto-update status
.venv/bin/python -m src.daemon auto-update disable
```

`enable` installs a second LaunchAgent, `local.agents-core.<installation-hash>.updater`,
that runs `auto-update run` every `--interval` seconds (default 900, minimum
60). A run fetches `AGENTS_AUTO_UPDATE_REMOTE` / `AGENTS_AUTO_UPDATE_BRANCH`
(defaults `origin` / `main`) and stops there when the
installation is up to date. It skips a target without touching the service when
the checked-out branch is different, tracked files have local changes, the
target is not a fast-forward, or dependency manifests changed; each skip is
logged once per target. Otherwise it waits until the service is ready, has no
work in flight, and has been idle for `--idle-seconds` (default 120), and then
runs the same transaction as `update`. It also waits while any stdio server
holds the installation, since `update` would stop the service only to find it
busy. A stopped service is left stopped. An unfinished transaction blocks
further runs until `recover`.

Downtime is the stop, the reindex, and the warmup. The reindex re-embeds only
when skills or implants changed, and only one model is loaded at a time. The
last outcome is in `auto-update.json` and the history in `auto-update.log`, both
in the private state directory. `uninstall` also removes the updater.

`migrate` reports its private backup directory. To roll back client migration:

```bash
.venv/bin/python -m src.daemon restore-clients /absolute/private/backup
.venv/bin/python -m src.daemon uninstall
```

Both commands drain the service first (see [Drain and connected
clients](#drain-and-connected-clients)). Disable Agents-Core in every client
before running them: afterwards the clients' configuration no longer points at a
running service.
Restoration checks the maintenance barriers and holds the controller lock across
daemon shutdown and configuration writes. It also checks that configurations
have not been edited since migration. Restore multiple migration backups in
reverse order. Uninstall retains backups, the registry, and history.

## Validation

`tests/conftest.py` redirects vector stores, router state, and updater state to a
temporary directory, seeded with available skills/implants indexes. When
`EMBEDDING_MODEL` is not already set, it reads the model from the checkout's `.env`.
Without either setting, the engine defaults to
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`; this differs from
the daemon's fixed e5-large model. Missing or incompatible indexes may require
model loading and rebuilding inside the temporary directory.

Run the suite from a checkout with this conftest and its development dependencies:

```bash
scripts/run_tests.sh
.venv/bin/python -m pytest -m slow tests/test_routing.py
node --test bridge/test.mjs
.venv/bin/python scripts/daemon_smoke.py
.venv/bin/python scripts/daemon_smoke.py --soak
```

Fast transport, controller, and configuration tests do not load the model. Smoke
tests require the e5-large model at `~/.cache/fastembed`, use port `18765`, one
temporary daemon, and isolated projects. They can still rebuild derived indexes
in the installation checkout, so run them in a dedicated checkout rather than a
live installation. They cover 20 clients, memory, digest and identity checks,
absence of sampling, and graceful shutdown. The soak test adds 1,000 connections, 20
workspaces, and a five-minute idle CPU measurement. Verify Dock launch,
sleep/wake, and reconnection separately in each GUI client; CLI and HTTP tests
do not cover those application workflows.
