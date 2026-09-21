# Shared MCP daemon operations

A macOS LaunchAgent serves local MCP clients at `http://127.0.0.1:8765/mcp`.
One Python process holds `intfloat/multilingual-e5-large`, the router, and shared
indexes. Desktop Chat connects through `bridge/stdio.mjs` (Node 22+, no npm
dependencies). MCP SDK 1.28.1 is pinned in `pyproject.toml`, `requirements.txt`,
and `uv.lock`.

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
realpath again returns its existing UUID. Global entries provide routing and
personas without a workspace; memory requires project configuration. After
creating a clone or worktree, run `migrate --workspace /absolute/worktree` before
connecting. Do not copy an MCP configuration containing another project's UUID.

Codex reads project `.codex/config.toml` in a trusted project; Claude Code uses
local scope in `~/.claude.json`; Cursor uses project `.cursor/mcp.json`. An existing
Claude project `.mcp.json` is also updated. Tracked Codex configurations use a
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

`status` reports ready, starting, draining, or failed state, PID, boot ID, request
counts, and running jobs. `/health` requires a bearer token. Readiness means the
model and indexes have warmed successfully. Admission is bounded at 32 requests,
with eight I/O workers and one inference worker. Capacity exhaustion returns
busy. Cancelling an HTTP waiter retains the quota for its running job and does
not replay a mutation.

## Memory and errors

HTTP never selects a project from cwd, environment variables, or client roots.
`X-Agents-Workspace` carries a UUID from the private registry. Errors
`workspace_required` and `workspace_invalid` mean memory is unavailable: routing
can continue, and logging must not be retried in a loop.

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
manifests. The controller enters maintenance, waits up to 60 seconds for drain,
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
Automatic background updates are disabled for a shared installation. Git
credentials and SSH must work with the LaunchAgent's PATH and environment.

`migrate` reports its private backup directory. To roll back client migration:

```bash
.venv/bin/python -m src.daemon restore-clients /absolute/private/backup
.venv/bin/python -m src.daemon uninstall
```

Restoration checks the maintenance barriers and holds the controller lock across
daemon shutdown and configuration writes. It also checks that configurations
have not been edited since migration. Restore multiple migration backups in
reverse order. Uninstall retains backups, the registry, and history.

## Validation

```bash
scripts/run_tests.sh
.venv/bin/python -m pytest -m slow tests/test_routing.py
node --test bridge/test.mjs
.venv/bin/python scripts/daemon_smoke.py
.venv/bin/python scripts/daemon_smoke.py --soak
```

Fast transport, controller, and configuration tests do not load the model. Smoke
tests use the locally cached e5-large model, one temporary daemon, and isolated
projects. They cover 20 clients, memory, digest and identity checks, absence of
sampling, and graceful shutdown. The soak test adds 1,000 connections, 20
workspaces, and a five-minute idle CPU measurement. Verify Dock launch,
sleep/wake, and reconnection separately in each GUI client; CLI and HTTP tests
do not cover those application workflows.
