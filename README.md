# 🤖 Agents Framework

**Universal MCP Server for AI Agent Roles, Skills & Cognitive Implants**

A local MCP server that loads specialized agent personas, domain skills, shared rules, and cognitive reasoning implants. Clients can connect through standalone stdio processes or a shared macOS HTTP service. Protocol 2 keeps a fitting persona in the conversation and routes only when selection is needed.

- [Documentation map](docs/README.md): current guides, reference material, and evaluation reports.
- [AI contributor instructions](AGENTS.md) and [Claude instructions](CLAUDE.md).
- [Model workflows](flows/README.md): reusable Markdown instructions, including
  the [documentation refresh process](flows/documentation-refresh.md).

---

## 🚀 Quick Start

### One-command install (macOS and Linux)

Requires `git`, `curl`, and Python 3.11 or newer as `python3`, with `pip3` on
`PATH` (see [After Cloning](#after-cloning)).

```bash
curl -fsSL https://raw.githubusercontent.com/IEZhu/Agents/main/install.sh | bash
```

The script clones the repository to `~/.agents-core` (or updates an existing
checkout), then runs `scripts/init_repo.sh --yes` after a single confirmation.
`--yes` applies the defaults:

- an `EMBEDDING_MODEL` already set in `.env` is kept, except that an `.env`
  from before the current model generation moves to the default model once
  ([model switch on update](#model-switch-on-update)); otherwise setup writes the
  one default model (see [Environment Variables](#environment-variables));
- an existing `.venv` is reused and its dependencies are refreshed; it is
  recreated only when its Python version is unknown or older than 3.11;
- the Claude instruction and routing-reminder prompts are accepted. Client
  registration and Codex instructions never ask (see [After Cloning](#after-cloning));
- sync between machines is not asked about: the summary says how to turn it on,
  and the `AGENTS_USER_SYNC_*` variables set it up without questions
  ([Sync between machines](#sync-between-machines)).

Without a terminal the confirmation is skipped. To inspect the script first,
download it, read it, then run `bash install.sh`. Pass `init_repo.sh` flags with
`... | bash -s -- --skip-index`; `--skip-mcp` leaves client configurations and
instructions unchanged.

| Variable | Default | Purpose |
|---|---|---|
| `AGENTS_HOME` | `~/.agents-core` | Install directory |
| `AGENTS_REPO_URL` | `https://github.com/IEZhu/Agents.git` | Repository for a fresh clone |
| `AGENTS_BRANCH` | `main` | Branch to install; the standalone auto-updater acts only on `AGENTS_AUTO_UPDATE_BRANCH` (default `main`) |
| `AGENTS_ASSUME_YES` | unset | `1`, `true` or `yes` skips the confirmation, as does `... \| bash -s -- --yes` |
| `AGENTS_USER_SYNC_REPO`, `AGENTS_USER_SYNC_REMOTE` | unset | Set up [sync between machines](#sync-between-machines) without questions, with the variables listed there |

The installer never uses `sudo`. It refuses a non-empty `AGENTS_HOME` that is not
an Agents-Core checkout.

Rerunning it updates an existing checkout: it checks out `AGENTS_BRANCH`,
fast-forwards it from the checkout's `origin`, then reruns setup, which refreshes
dependencies in `.venv` and the indexes in place. First close the client sessions
that run this installation's stdio server, as for any manual Git operation or
rebuild. If this checkout serves the shared macOS service, update it with
`.venv/bin/python -m src.daemon update` instead (see [Updates](#updates)).
An update is refused when the checkout has uncommitted changes to tracked files
(commit or stash them first). Untracked files do not block an update and are left
in place; `git pull --ff-only` aborts without touching them if an incoming file
would overwrite one.

For step-by-step prompts, or on Windows, use [After Cloning](#after-cloning).

### Claude Code cloud sessions

To have Agents-Core in [Claude Code cloud sessions](https://code.claude.com/docs/en/claude-code-on-the-web),
create a cloud environment whose setup script runs
[`scripts/setup_cloud_env.sh`](scripts/setup_cloud_env.sh) and whose network
allows Hugging Face. The [cloud environment guide](docs/cloud-runs.md#cloud-environment-with-agents-core)
lists the settings.

### After Cloning

Use Python 3.11 or newer, as required by [pyproject.toml](pyproject.toml).
On macOS and Linux, setup uses the `python3` on `PATH`, which needs the `venv`
module, and requires `pip3` on `PATH`. It does not look for versioned names such
as `python3.12`, and the macOS Command Line Tools `python3` is too old. On
Windows, `init_repo.bat` tries `py -3`, `python` and `python3`.
An existing `.venv` must also use a supported version. If it uses an older Python,
choose to recreate it when the installer asks; setup stops if you decline.
On NixOS, enable `programs.nix-ld` first: setup adds its library directory to
`LD_LIBRARY_PATH` for the indexing run and in each MCP registration's `env`.
Without it, setup only warns, and NumPy can fail to load `libstdc++`. Commands you
run yourself (such as `src.reindex` or tests) and client entries you write by hand
need `LD_LIBRARY_PATH=/run/current-system/sw/share/nix-ld/lib` too; for a manual
Codex entry, set it in `[mcp_servers."Agents-Core".env]`.

```bash
git clone <repository-url>
cd Agents

# Run initialization script
./scripts/init_repo.sh
```

On Windows, run `scripts\init_repo.bat`. It accepts `--skip-env`, `--skip-index`
and `--skip-mcp`, and `--yes` (or `AGENTS_ASSUME_YES=1`), which there only stops the
sync question: the other prompts still ask, and take their defaults when stdin is
not a console.

The interactive script:

- creates `.env` from `env.example` (or adds keys missing from an existing one),
  creates `.venv/` and installs dependencies, writes the default embedding model
  to `.env` when none is set and downloads it, and builds the skill and implant
  indexes. When `.venv/` exists and you
  keep it (the default answer), dependencies are not reinstalled; answer `y` to
  recreate it, pass `--yes`, or run `.venv/bin/pip install -r requirements.txt`;
- registers Agents-Core without asking as a standalone stdio server in each
  detected client's user-level configuration (Claude Code `~/.claude.json`, Cursor
  `~/.cursor/mcp.json`, Claude Desktop `claude_desktop_config.json`, Google Antigravity
  `~/.gemini/config/mcp_config.json`), after copying each file to `<file>.backup.<epoch>`;
- asks before installing protocol 2 instructions in the global Claude
  configuration. When Codex is detected, it installs the same protocol in Codex's
  global instructions without asking; this does not connect Codex to MCP (see
  [Codex instruction installation](#codex-instruction-installation));
- targets another profile when `CLAUDE_CONFIG_DIR` (Claude Code),
  `AGENTS_CURSOR_MCP_CONFIG`, `AGENTS_CLAUDE_DESKTOP_CONFIG` or
  `AGENTS_ANTIGRAVITY_MCP_CONFIG` is exported in the shell that runs setup. Setup
  does not read them from `.env`, and a set variable also counts as detecting
  that client (see [alternate client configurations](docs/shared-mcp-daemon.md#alternate-client-configurations));
- on macOS, once the shared service is installed, keeps these clients on the
  service instead of writing standalone stdio entries. Do not rerun setup while
  the service runs (see [service updates](docs/shared-mcp-daemon.md#updates-and-recovery));
- asks once, at the end, whether to set up [sync between machines](#sync-between-machines),
  unless it is set up already; with `--yes` or without a terminal it does not ask,
  and the summary says how to turn it on.

`--skip-mcp` skips all client changes. Use `./scripts/init_repo.sh --help` for the
available options (`--yes` accepts all defaults). For the shared macOS service and
client connections, continue with [MCP client configuration](#-mcp-client-configuration).

For an existing installation, refresh global Codex and Claude instructions with
`python3 scripts/install_instructions.py`. This standalone command needs Python
3.11 or newer and does not rerun setup; see the [instruction update options](#codex-instruction-installation).

### Manual Setup

```bash
# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate   # Windows: .venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp env.example .env
# Edit .env for any optional integrations. To use a model other than the
# default, set EMBEDDING_MODEL together with EMBEDDING_MODEL_GENERATION=2

# Download the model and build the indexes
python -m src.reindex
```

---

## ⚙️ Configuration

### Environment Variables

The core router needs no external API key. Configure `.env` using
[env.example](env.example). Setup creates `.env` from it, so a new `.env` starts
with these values:

```env
# Optional: set both keys to enable Langfuse tracing. Keep comments on their own
# line: older python-dotenv reads "KEY=  # text" as the value "# text".
LANGFUSE_PUBLIC_KEY=
LANGFUSE_SECRET_KEY=
LANGFUSE_HOST=https://cloud.langfuse.com
ANTHROPIC_API_KEY=sk-ant-...    # Optional: for document OCR
AGENTS_DEBUG=0                  # Set to 1 for per-call JSON debug logs
```

Both keys set enable tracing, so leave them empty to run without Langfuse. The
placeholders `pk-lf-...` and `sk-lf-...`, which an `.env` copied from an older
`env.example` may still hold, do not count as keys. The optional
[document OCR server](src/mcp_servers/document_ocr/README.md) needs a real
`ANTHROPIC_API_KEY`.

Embeddings run locally through FastEmbed (ONNX Runtime); the initial model download
requires network access. The default model is `microsoft/harrier-oss-v1-270m`
(MIT, multilingual, about 1.1 GB to download and 0.85 GB loaded): on the project's
retrieval sets it ranks implants and skills better than the earlier defaults
([results](docs/embedding-models-eval-results.md)). It loads from a plain-file
copy of the onnx-community export, pinned in
[embedding_prompts.py](src/engine/embedding_prompts.py), which also loads offline
once downloaded. Setup no longer asks: harrier-270m is the one model for every
machine, replacing the earlier Full, Balanced and Light choices. Setup writes it
to `EMBEDDING_MODEL` in `.env`, together with `EMBEDDING_MODEL_GENERATION`. When
`EMBEDDING_MODEL` is unset, for example after `--skip-index`, standalone stdio
uses it as well. Any FastEmbed model stays selectable,
`intfloat/multilingual-e5-large` included: edit `EMBEDDING_MODEL` and, with
server sessions stopped, run
`.venv/bin/python -m src.reindex`. An `.env` without
`EMBEDDING_MODEL_GENERATION=2`, such as one copied by hand from `env.example`,
needs that line too; otherwise the next start switches it to the default
([model switch](#model-switch-on-update)). The [shared macOS service](#shared-macos-service)
pins its model in its own configuration.

#### Model switch on update

Earlier setups pinned the model chosen then (Full e5-large, Balanced multilingual
MiniLM or Light English MiniLM). The first idle start after an update to this
version moves an `.env` from any of these, or any other model, to harrier-270m
once: it rewrites `EMBEDDING_MODEL`, keeps the old value in a comment, and
records `EMBEDDING_MODEL_GENERATION=2`
([model_migration.py](src/model_migration.py)). The server restarts itself
into the new model, downloads it in the background and re-embeds the skill,
implant and history indexes; the router cache resets. Until the indexes are ready,
tools answer `warming_up`. Rerunning `init_repo.sh` or `init_repo.bat` applies the
same switch and downloads the model during setup. A model set after the switch,
for example e5-large again, stays: the generation marker is already current. An
`EMBEDDING_MODEL` exported in the server's environment overrides `.env` as before.
The shared service switches in its update transaction instead
([daemon guide](docs/shared-mcp-daemon.md#embedding-model)).

| Setting | Default | Purpose |
|---|---|---|
| `EMBEDDING_MODEL` | `microsoft/harrier-oss-v1-270m` when unset | Standalone embedding model |
| `EMBEDDING_MODEL_GENERATION` | Unset counts as `1`; setup and the model switch write `2` | Model generation the `.env` reached; an older one switches to the default model once ([model switch](#model-switch-on-update)) |
| `EMBEDDING_PROMPTS` | `on` | Model-specific query and passage prompts ([embedding_prompts.py](src/engine/embedding_prompts.py)); `off` embeds text as given |
| `EMBEDDING_BATCH_SIZE` | `4` | Documents per embedding batch (1–256) when history, skill or implant indexes are built. Memory grows with batch size, by up to about 68 MB per 512-token document with `intfloat/multilingual-e5-large` (a full harrier-270m index build peaked at about 2.8 GB with the default in the [recorded run](docs/embedding-models-eval-results.md)); larger values may be faster on machines with spare memory. Inputs are also capped at 2048 tokens |
| `FASTEMBED_CACHE_DIR` | `~/.cache/fastembed` | Persistent model cache; the shared service's `install` reads it only from the shell ([daemon guide](docs/shared-mcp-daemon.md)) |
| `AGENTS_CLIENT_REPO_ROOT` | Unset: a tool call's `workspace` inside the client's MCP roots, else the nearest `.git` or `CLAUDE.md` at or above `CLAUDE_PROJECT_DIR` or the working directory ([rules](#-repository-memory)) | Explicit stdio memory and workflow target. Refused with `workspace_unsafe` when it is a system, program or home directory. Set it in a per-project MCP entry's `env`, never in the installation `.env`, a shared registration or the Desktop entry |
| `RULES_ENABLED` | `1` | Include [shared rules](rules/README.md) in loaded context |
| `INTENT_CLASSIFIER_ENABLED` | `0` | Enable the optional intent-based enrichment classifier |
| `AGENTS_USER_FLOWS_DIR` | `flows/.user` in the installation | Location (absolute path) of [personal and repository flows](flows/README.md#personal-and-repository-flows), their persona choices, and the installation's rule, skill and implant switches (`components.json`) |
| `AGENTS_GITHUB_HOST` | `github.com` | GitHub host that [library sync](docs/user-sync.md#github) signs in to and calls; set it for GitHub Enterprise Server |
| `AGENTS_GITHUB_CLIENT_ID` | The Agents-Core OAuth App, once registered | Client ID of an OAuth App with Device Flow enabled for [library sync](docs/user-sync.md#github) sign-in; required until the Agents-Core app is registered, and for forks |
| `WARMUP_WAIT_SECONDS` | `20` | Seconds (1–600) a retrieval tool waits for background startup (stores, embedding model, rules) before it answers `warming_up`; the MCP handshake never waits ([details](docs/routing_flow.md#startup-and-readiness)) |

Routing thresholds and enrichment settings are defined in
[src/engine/config.py](src/engine/config.py). Its environment overrides, such as
`ROUTER_SIMILARITY_THRESHOLD`, `SKILLS_RELEVANCE_THRESHOLD`,
`IMPLANTS_RELEVANCE_THRESHOLD`, the
[`IMPLANT_*` settings](implants/README.md#1-agent-preferences-and-semantic-retrieval) and the
[intent classifier](docs/intent-classifier.md) thresholds in `env.example`, are
tuning and evaluation settings; normally keep their defaults. HTTP memory uses a
registered workspace header instead of `AGENTS_CLIENT_REPO_ROOT`.

### Sync between machines

Personal (`user:`) and repository (`repo:`) flows, persona choices and the
component switches live in the personal library (`flows/.user`, or
`AGENTS_USER_FLOWS_DIR`). Sync keeps that library the same on your machines
through one private git repository per user, on macOS, Windows and Linux
([reference](docs/user-sync.md)). It is off until you set it up.

By default it syncs everything with a portable identity: personal flows, persona
choices, the switches, the flows' history, and the flows of every repository that
has an `origin` remote, work repositories included. Repository folders without an
`origin` and machine-local files stay on the machine. The wizard and the Sync page
show a preview and wait for your confirmation before the first upload or a join;
`python -m src.user_sync scope --exclude repos/<key>` (or `--exclude-file PATH`)
later keeps a group or a file out on every machine without deleting it anywhere.

To set it up:

- **During setup.** `init_repo.sh` and `init_repo.bat` ask once, at the end:
  "Set up sync between your machines now? [y/N]". On macOS, when the shared
  service answers and serves the Sync page, yes opens its settings page there;
  otherwise it starts the terminal wizard. When the wizard has started sync on a
  machine without the service, setup also asks "Also sync every 5 minutes in the
  background? [Y/n]". Setup does not ask under `--yes` (which `install.sh` always
  passes), without a terminal, while git 2.32 or newer or `ssh-keygen` is missing,
  or when the library holds a `.git` that sync did not create, which it leaves
  alone. Updates never ask: `install.sh` runs setup with `--yes`, and the
  service's `update` and `auto-update` do not run it. Sync that is set up is only
  reported, and the summary says whether background sync is on.
- **Later**, from the installation root: `.venv/bin/python -m src.user_sync setup`
  (the terminal wizard), or the Sync page of `.venv/bin/python -m src.daemon flows-ui`
  with the shared service.
- **Without questions**, for example on a new machine: run
  `.venv/bin/python -m src.user_sync setup --from-env` with these variables in its
  environment. Setup reads them only there, never from `.env`. The installers honour
  them too, but the separate command keeps the token to that one command.

| Variable | Purpose |
|---|---|
| `AGENTS_USER_SYNC_REPO` | `OWNER/NAME` of an existing private GitHub repository, empty or holding the library |
| `AGENTS_USER_SYNC_REMOTE` | Instead of the repository: the SSH URL of a private repository on another host |
| `AGENTS_USER_SYNC_NAME`, `AGENTS_USER_SYNC_EMAIL` | Required: the commit identity; sync never reads your git configuration |
| `AGENTS_USER_SYNC_LABEL` | This machine's label (default: the platform and a random suffix) |
| `AGENTS_GITHUB_TOKEN` | Optional, for a GitHub repository: a token with the `repo` scope, which setup checks with GitHub, keeps in the OS secret store and uses to add this machine's deploy key |

From the installation root, with the token typed without echo and set for this
one command only:

```bash
read -rs t
AGENTS_GITHUB_TOKEN="$t" AGENTS_USER_SYNC_REPO=me/agents-library AGENTS_USER_SYNC_NAME="My Name" \
    AGENTS_USER_SYNC_EMAIL=me@example.com .venv/bin/python -m src.user_sync setup --from-env
unset t
```

In PowerShell on Windows, the token lives in the session's environment until you
remove it:

```powershell
$secure = Read-Host -AsSecureString "GitHub token"
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure)
$env:AGENTS_GITHUB_TOKEN = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
[Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
$env:AGENTS_USER_SYNC_REPO = "me/agents-library"
$env:AGENTS_USER_SYNC_NAME = "My Name"
$env:AGENTS_USER_SYNC_EMAIL = "me@example.com"
.venv\Scripts\python.exe -m src.user_sync setup --from-env
Remove-Item Env:AGENTS_GITHUB_TOKEN
```

The command takes the token out of its own environment before anything else, so
none of its child processes (git, ssh, ssh-keygen) inherits it. When you give the
token to the installers instead, `install.sh` passes it to `init_repo.sh` only,
never to git, and `init_repo.sh` passes it to the sync step only. `init_repo.bat`
clears it, because cmd cannot keep a variable from its children: on Windows, use
the separate command. Never keep the token in `.env`.

Without a token or an earlier `github login`, setup prints this machine's public
key to add to the repository as a deploy key with write access; run it again
afterwards, and the installer's summary says so. Setup then checks access and
privacy, prints the preview and starts sync without waiting, except that joining
a library with conflicts stops at "confirmation needed" with the commands to
review the preview and to start with its hash. Where privacy cannot be checked,
another host's key needs confirming, or the repository belongs to another owner,
it says to run it again with `--confirm-private`, `--trust-host-key` or
`--confirm-owner OWNER`. On a machine without the shared service
it then turns background sync on (`schedule enable`, every 5 minutes). A second
run with another repository replaces a setup that the variables made and that
never started, also one that stopped at a mistyped host; GitHub accepts a key on
one repository only, so setup first removes this machine's deploy key from the
old repository, or, without the API, gives this machine a new key and names the
old repository to clean up. Once sync has started, the variables change nothing.
A token GitHub refuses, or one that cannot see the repository, is reported with
the variable's name and the `repo` scope it needs. The commands setup prints say
where to run them: `cd <installation> && …` on macOS and Linux, `in <installation>,
run …` on Windows, where cmd and PowerShell chain commands differently.

GitHub sign-in in the wizard and on the Sync page is the OAuth device flow of the
Agents-Core OAuth App, so `gh` is not needed. The app's client ID ships once the
app is registered; until then, set `AGENTS_GITHUB_CLIENT_ID` in `.env` to an OAuth
App of your own with Device Flow enabled (see [Environment Variables](#environment-variables)),
use `AGENTS_GITHUB_TOKEN`, or give an SSH URL and add the deploy key by hand.

Changes reach your other machines when each machine syncs. With the shared
service, it syncs every few minutes and after each save; `install` says whether
sync is set up. Without it, the scheduled run (`.venv/bin/python -m src.user_sync
schedule enable`) syncs every 5 minutes; without that, a machine sends and
receives changes only in the cycle that follows each save made through its stdio
servers. The [command reference](docs/user-sync.md#command-line) covers status,
conflicts, pausing and the scope.

### Updates

The shared daemon has its own update controller. Run
`.venv/bin/python -m src.daemon update` for a manual update; automatic daemon
updates are opt-in through `.venv/bin/python -m src.daemon auto-update enable`.
See [service updates and recovery](docs/shared-mcp-daemon.md#updates-and-recovery).
While the service runs, do not update its checkout with `install.sh`, `git pull` or
`scripts/init_repo.sh`: they bypass drain, maintenance, index backup, probation
and rollback. The daemon disables the standalone updater described below.

#### Standalone stdio auto-update

The server can keep itself current. Updates are **two-phase** — prepared in the
background, activated on the next idle start — so the live install is never mutated
mid-session:

1. **Prepare** (background): a daemon thread (non-blocking, so it never delays
   serving) fetches the target branch and, if the install is fast-forwardable,
   builds the new version's vector stores in an isolated git worktree under
   `data/.prepared/<sha>`, then writes a marker. The live tree and stores are
   untouched.
2. **Activate** (next idle start): if a valid prepared update exists, the server
   fast-forwards the live tree (local, no network) and atomically moves the
   pre-built stores into `data/` — the expensive embedding already happened in
   phase 1. Activation runs only at a start with no other server session of this
   installation, including the shared daemon; otherwise the update stays pending.
   A start that overlaps activation waits and then loads the new code. Git and
   reindex children keep the installation leased until they and their
   descendants exit, so this wait can exceed the command timeout. Background
   preparation uses a separate lock and does not delay startup.

It is **safe by default**:

- acts **only** when the checked-out branch is `AGENTS_AUTO_UPDATE_BRANCH` (default
  `main`) — a **no-op on feature branches**, so local development is never touched;
- only when the working tree is clean, and only **fast-forward** (never merge, rebase,
  or switch branches);
- a failed staged build discards the worktree and leaves the install as-is; crash
  recovery also uses the store's torn-pair detection and content-hash re-embed;
- staging and store paths must not contain symlinks; store moves are covered by
  the recovery journal below, so an interrupted move blocks startup instead of
  serving mixed indexes;
- offline/fetch/build failures leave the current version available. A failed
  activation merge (including a timeout after HEAD moved) restores the old tree.
  If rollback or re-exec fails, or an unexpected activation error leaves the tree's
  state unknown, startup stops instead of serving mixed versions;
- dependencies are **not** auto-installed. When `requirements.txt` or
  `pyproject.toml` changed, the updater only logs a warning. The staged updater
  warns while preparing, before the live checkout has the new manifests, and
  builds with the current environment, in which the activated code then also
  runs. For such an update, stop the server sessions and update manually:
  `git pull --ff-only`, `.venv/bin/pip install -r requirements.txt`,
  `.venv/bin/python -m src.model_migration .env` (applies a pending
  [model switch](#model-switch-on-update) before the rebuild), then
  `.venv/bin/python -m src.reindex`. If it has already activated, stop the
  sessions and install the dependencies before restarting. Only with
  `AGENTS_AUTO_UPDATE_STAGING=0` is the new `requirements.txt` already in place
  when the warning appears.

Lifetime locks are `flock` on Linux and macOS and `LockFileEx` on Windows. On a
file system without byte-range locks (some Windows network shares) the server
runs with automatic updates disabled. When upgrading from a version that does
not hold these locks (on Windows, any version before #195), restart all existing
server sessions once. Manual Git operations and rebuilds must also be done with
those sessions stopped.

Windows differs in two places. A Windows lock belongs to the process that took
it, so a git or reindex child cannot keep the installation leased after the
server exits: the children run in a job object that ends them with the server,
and the next prepare starts over. Windows also has no `exec`: after activation,
the server runs the updated code as a child process with its own standard
handles, releases its leases to it, and exits with the child's exit code, so the
client keeps talking to the process it started.

Before changing the live tree, the updater flushes a recovery journal to
`data/.update_in_progress.json`. It removes this guard only after successful
activation or rollback. An interrupted mutation or failed rollback keeps the
journal (and available staging artifacts); subsequent starts stop before importing
the application, even with auto-update disabled. To recover, stop the sessions,
inspect the recorded `old_sha` / `target_sha` and Git state, restore a complete
chosen revision, and successfully run `python -m src.reindex` there. Remove the
journal only after verifying the restored checkout and rebuilt stores, then
restart the server. An unreadable journal also requires this explicit recovery.

```env
AGENTS_AUTO_UPDATE=1                     # 0 to disable
AGENTS_AUTO_UPDATE_REMOTE=origin
AGENTS_AUTO_UPDATE_BRANCH=main           # only updates when this branch is checked out
AGENTS_AUTO_UPDATE_TIMEOUT=30            # seconds per git op
AGENTS_AUTO_UPDATE_INTERVAL=900          # throttle network checks (0 = every start)
AGENTS_AUTO_UPDATE_REINDEX_TIMEOUT=3600  # seconds per index build (staged or in-place); stops only a hung build
AGENTS_AUTO_UPDATE_STAGING=1             # 0 = synchronous in-place update at an idle start
# AGENTS_AUTO_UPDATE_STAGING_DIR=/path   # staging parent (default data/.prepared; same filesystem as data/)
```

With `AGENTS_AUTO_UPDATE_STAGING=0` the updater falls back to the legacy in-place
path at an idle start under the same exclusive installation lock: fast-forward
the live tree and rebuild the stores right there, rolling back to the previous
commit if the rebuild fails. This mode can delay startup for fetch and reindex;
it no longer mutates files in a background thread while sessions are serving.

With serving processes stopped, run a manual rebuild using `python -m src.reindex`.

---

## 🎯 How It Works

The server exposes MCP tools that any compatible client can call:

| Tool | Purpose |
|------|---------|
| `route_and_load(query, chat_history?, protocol_version=2, current_persona?)` | Semantic routing when selection is requested; returns a loaded role or candidates for the client to choose |
| `get_agent_context(agent_name, query, reasoning?, chat_history?, protocol_version=2, current_persona?, force_reload=False)` | Direct agent loading when the target is already known; the active agent returns `NO_CHANGE` unless `force_reload=True` restores lost instructions |
| `refresh_persona_context(query, current_persona, chat_history?)` | Refresh skills/implants for the active role without reselecting it |
| `load_implants(query\|task_type)` | Load cognitive reasoning strategies by semantic query or preset bundle |
| `list_agents()` | Enumerate all available agents with metadata |
| `list_flows(scope="all")` | Discover built-in, personal (`user:`) and repository (`repo:`) Markdown workflows, with IDs and content revisions |
| `run_flow(flow, request="", repo_path=None, current_persona?)` | Load a workflow for the caller's repository; returns `needs_execution` for the current model to carry out using its tools, plus `persona_activation` when the flow names a persona |
| `get_flow` / `save_flow` / `delete_flow` | Manage personal and repository flows from chat, with history and conflict detection; stored in the git-ignored `flows/.user` ([details](flows/README.md#personal-and-repository-flows)) |
| `set_flow_persona(flow, agent?, skills?, implants?, rules?, reset=False)` | Choose the agent and exact skills, implants and rules a flow runs with, without copying its text; stored in `flows/.user/personas/`. Without `agent` the flow runs without a persona; `reset=true` restores the flow's frontmatter ([details](flows/README.md#choose-a-flows-agent-and-components)) |
| `log_interaction(agent_name, query, response_content, persona?, persona_action?, intent?, action?, outcome?, files?, tags?, request_id?, reasoning?)` | End-of-turn attribution log ([details](#-repository-memory)); `persona` is the last `SUCCESS`/`NO_CHANGE` descriptor object copied verbatim with all 7 keys (`agent`, `activation_id`, `bundle_revision`, `scope`, `skills_loaded`, `implants_loaded`, `rules_loaded`), `persona_action` is `keep`, `switch`, `refresh` or `restore` and only goes with `persona`; `files`/`tags` are JSON arrays. Malformed attribution is written as `unverified` |
| `clear_session_cache()` | Stdio only: administrative reset of the shared enrichment cache; not required for persona changes. For HTTP, use `.venv/bin/python -m src.daemon clear-cache` |
| `describe_repo(repo_path=None, force_refresh=False)` | Repository summary bootstrap; returns `needs_summary` when sampling is unavailable (always over HTTP) or fails ([details](#-repository-memory)) |
| `write_repo_summary(summary, repo_hash, repo_path=None, workspace_id=None)` | Persists the summary after `needs_summary`; pass its `repo_hash`, `repo_path` and `workspace_id` back unchanged |
| `read_history(limit?, since?, query?, machine?, offset?, entry_id?)` | Recent entries or lazy semantic recall over the action log, with long texts as previews that keep the result under Claude Code's 50,000-character limit; `offset` pages on, `entry_id` returns one entry whole; with [user library sync](docs/user-sync.md#repository-history), also the entries the user's machines shared, filtered by `machine` (a label, or `local`) |

The server also registers MCP prompts (slash commands): `ask` routes like
`route_and_load`; one prompt per agent `routing.trigger_command` and per
`routing.aliases` entry loads that agent directly; `describe_repo` (optional
`force=true`) drives the repository memory bootstrap. Agent and `ask` prompts
accept `current_persona` as descriptor JSON. See the [protocol API](docs/routing_flow.md#api)
for parameters and responses.

### Workflows in the caller's repository

Ask the model to use Agents-Core to run `documentation-refresh` or `pr-review`
in the current repository. It calls `list_flows()` to discover flows and
`run_flow(flow="documentation-refresh")` to get the instructions. For review,
pass the PR/MR URL and constraints in `request`, such as `no-merge`.

The flow files stay in the MCP installation. Inspection, edits, tests and PR/MR
actions target the caller's repository. HTTP requires a registered workspace in
`X-Agents-Workspace`, even when `repo_path` is supplied. Stdio uses the project
resolved as described in [Repository Memory](#-repository-memory), and a workflow
fails instead of falling back to the installation. An optional `repo_path` must
stay within that workspace.

The tool reads and returns instructions; the current model executes them with
its existing permissions and tools. `needs_execution` does not mean the work is
complete. See the [workflow guide](flows/README.md#through-agents-core-mcp) for
the response contract, target resolution, and authoring rules.

Most built-in flows name a persona in their frontmatter: `pr-review` runs as
`code_reviewer`, `documentation-refresh` as `tech_writer`. For such a flow,
`run_flow` also returns `persona_activation`, a protocol 2 response: pass
`current_persona` to `run_flow` and apply the activation as a switch before
executing the flow. To choose another agent or exact skills, implants and rules
for a flow, use `set_flow_persona` or the flow editor's **Persona** panel
([details](flows/README.md#choose-a-flows-agent-and-components)).

Besides the built-in flows, users keep personal (`user:`) and per-repository
(`repo:`) flows in the installation's git-ignored `flows/.user`. Ask the model to
save, change, restore or delete one; it uses `get_flow`, `save_flow` and
`delete_flow`. A bare flow name resolves `repo:`, then `user:`, then the built-in.
With the shared daemon, the version link in the persona footer or
`.venv/bin/python -m src.daemon flows-ui` opens a local
[settings page](docs/shared-mcp-daemon.md#flow-editor); a browser of the OS user
running the daemon signs in by itself. It opens on an overview of the
installation: usage statistics, the AI apps that use it, a live diagram of a
request's path, and the model, directories and their sizes. The same page edits
flows, shows every agent with its metadata and prompt (read-only), and lists rules,
skills and implants and switches each one on or off for the installation.

### Persona continuity

Before each request, the model silently checks whether its active role fits the
task. If it does, it keeps that role without routing, candidate selection or
enrichment calls. A needed specialization change triggers routing; a known
requested role loads directly. Lost instructions restore the known role; a
justified refresh rebuilds its context without reselecting it.

The server returns separate persona, rules, skills and implants blocks, an
activation descriptor and an exact footer (the four labelled component lists, then
a plain `Agents-Core <version>` segment; under the daemon the version links to the
web UI). Every successful switch, restore or
refresh replaces all four blocks, including changed or removed rules, while
preserving higher-priority instructions, conversation facts, goals, permissions
and tool results. This is logical replacement: MCP cannot physically delete old
messages. No history or cache clearing is required. The server never samples an
answer.

Protocol 2 is the only protocol since 2026-09-29 (installer default since
2026-09-26). Clients pass `protocol_version=2` (also the default); any other value
returns `ERROR` without loading a persona. [Client decisions](docs/routing_flow.md#client-decisions)
records the telemetry and evaluation evidence for the switch and links the
remaining validation gaps for each tested client/model.

The checked-in `CLAUDE.md` uses the same template; the global installer does not
change this tracked file. After editing the template, refresh this checkout's
managed section:

```bash
.venv/bin/python scripts/_helpers/inject_claude_md.py CLAUDE.md scripts/templates/routing-protocol-core.md
```

On Windows, use `.venv\Scripts\python.exe` for the same command.
The helper replaces only the text between the markers and preserves repository
notes outside them. When the section changes, it leaves an untracked
`CLAUDE.md.backup.<timestamp>` in the checkout; do not commit it. Backup retention
and routing-reminder migration are described in
[installation, migration and rollback](docs/routing_flow.md#installation-migration-and-rollback).
Review your own project instructions and memory for conflicting unconditional
`route_and_load` requirements; these are not automatically rewritten.

Client instructions still written for protocol 1 no longer match this server:
their calls receive protocol 2 bundles they do not describe. Rerun the installer or `scripts/install_instructions.py` to replace them.
A client whose server predates protocol 2 reports the mismatch once and answers
without an activated persona until the server is updated.

See [routing, compatibility and migration](docs/routing_flow.md) and the
[client protocol](scripts/templates/routing-protocol-core.md) for the complete
contract. Behavioral support must be established for each client/model pair;
passing server tests alone does not establish it.

---

## 🏗️ Architecture

```
Agents/
├── .github/workflows/    # Windows installer CI and the issue-agent bridge
├── agents/               # Agent personas, discovered from system_prompt.mdc
│   ├── software_engineer/
│   │   └── system_prompt.mdc
│   ├── common/           # agent-schema.json (frontmatter contract)
│   └── schemas/          # Output schemas named in agent prompts
├── skills/               # Reusable knowledge chunks (RAG)
│   └── skill-*.mdc
├── implants/             # Cognitive reasoning strategies (RAG)
│   └── implant-*.mdc
├── rules/                # Shared directives (rule-*.mdc; see rules/README.md)
├── src/
│   ├── server.py         # FastMCP tools, prompts, and standalone entrypoint
│   ├── startup.py        # Installation leases and isolated stdio indexes
│   ├── self_update.py    # Standalone staged updates
│   ├── reindex.py        # Skill and implant index rebuild
│   ├── model_migration.py # One-time switch to the default embedding model
│   ├── version.py        # Agents-Core version shown in the persona footer
│   ├── flows.py          # Built-in flow catalog and run_flow bundles
│   ├── user_flows.py     # Personal and repository flows (flows/.user)
│   ├── flow_persona.py   # A flow's agent and exact components
│   ├── component_toggles.py # Installation-wide on/off switches for rules, skills and implants
│   ├── component_catalog.py # Read-only agent and component listing for the web UI
│   ├── client_paths.py   # Client configuration paths shared by installers and migration
│   ├── file_lock.py      # Stable sidecar file locks
│   ├── daemon/           # HTTP app, service control, workspaces, client migration, web UI and its sign-in
│   ├── mcp_servers/      # Optional document OCR server and the MCP server template
│   ├── engine/
│   │   ├── router.py     # Semantic routing (cache-first)
│   │   ├── persona.py    # Protocol 2 activation, restore, and refresh
│   │   ├── persona_bundle.py # Fresh instruction blocks and bundle revision
│   │   ├── readiness.py  # Background retrieval startup (warming_up)
│   │   ├── skills.py     # Skill retrieval (vector search)
│   │   ├── implants.py   # Implant retrieval (vector search)
│   │   ├── rules.py      # Shared rule loading
│   │   ├── config.py     # Centralized configuration
│   │   ├── embedder.py   # FastEmbed wrapper (ONNX Runtime)
│   │   ├── embedding_prompts.py # Model prompts and extra model registrations
│   │   ├── vector_store.py # NumPy-based vector store
│   │   ├── enrichment.py # Tier-based context enrichment
│   │   ├── intent.py     # Optional task classification
│   │   ├── fingerprint.py # Index compatibility fingerprint
│   │   ├── context.py    # Context retrieval (history formatting)
│   │   └── language.py   # Language detection
│   ├── memory/           # Repository summary, history, managed sections
│   ├── schemas/          # protocol.py (request, response, persona), tool_args.py (MCP argument types)
│   └── utils/
│       ├── prompt_loader.py
│       ├── debug_logger.py     # Optional JSON debug logging
│       └── langfuse_compat.py  # Optional Langfuse layer
├── bridge/               # Node stdio bridge to the shared HTTP service
├── scripts/              # Setup, validation, test, and maintenance commands
├── scripts/templates/    # Versioned client instruction templates and the issue-agent bridge workflow
├── tests/                # Deterministic and opt-in integration tests
├── evals/                # Routing, enrichment, and client dialogue evaluations
├── docs/                 # Guides and reference documents
├── flows/                # Markdown workflows served to caller repositories through MCP
├── data/                 # Installation indexes and leased stdio state (ignored)
├── install.sh            # One-command installer (macOS and Linux)
├── env.example           # Template for .env
├── pyproject.toml        # Python project metadata
└── requirements.txt
```

### Key Components

| Component | Description |
|-----------|-------------|
| **Agents** | Specialized personas with unique system prompts |
| **Skills** | Domain-specific knowledge chunks (retrieved via RAG) |
| **Implants** | Cognitive patterns & reasoning strategies |
| **Rules** | [Shared directives](rules/README.md) included in every loaded bundle when enabled, unless a flow persona names an exact set |
| **Router** | Semantic matching + caching for fast agent selection |
| **Persona bundle** | Versioned role instructions, rules, skills, and implants retained by the client |
| **Flows** | [Markdown workflows](flows/README.md) served to the caller's repository through `run_flow`, optionally with their own persona |
| **Memory** | Per-project summary and action history |

---

## 🔌 MCP Client Configuration

### Codex instruction installation

During the client setup stage, both installers automatically update Codex's global
instructions when `CODEX_HOME` is non-empty, the default `~/.codex` directory exists, or
the `codex` command is available. `CODEX_HOME` selects the target directory when
non-empty; otherwise the target is `~/.codex`. There is no additional Codex prompt.
`--skip-mcp` skips this instruction update along with client configuration.

The installer updates a non-empty `AGENTS.override.md` when present; otherwise it
updates `AGENTS.md`. This matches Codex's global instruction precedence. Personal
instructions outside the Agents-Core markers are preserved. Project instructions
can override global guidance, so review any old project-level routing rules too.
Start a fresh Codex session after updating instructions. See the
[official Codex instruction guide](https://learn.chatgpt.com/docs/agent-configuration/agents-md).

To refresh global instructions for detected Codex and Claude clients from the
current checkout without rerunning setup:

```bash
python3 scripts/install_instructions.py
```

On Windows, use `py -3 scripts\install_instructions.py`. The command needs Python
3.11 or newer and only uses its standard library; no virtual environment is
required. Add `--clients codex` to update only Codex. The command uses the installer's
managed-section replacement and backup retention. It migrates an existing exact
generated Claude routing reminder and its entry in `memory/MEMORY.md` of the Claude
configuration directory (`$CLAUDE_CONFIG_DIR` when set, otherwise `~/.claude`),
but does not create an absent reminder. Like the installers, it reads
`CLAUDE_CONFIG_DIR` and `CODEX_HOME` from its environment.

This command updates global instructions, that reminder and its memory-index
entry, and their managed backups. It does not install dependencies, change `.env`
or vector indexes, register MCP connections, or change
service configuration. The shared daemon migration below configures Codex's MCP
connection on macOS; without the service, add the [standalone Codex entry](#codex-configtoml).
Refreshing instructions alone does not require restarting the daemon.

### Shared macOS service

On macOS, one shared daemon can serve Codex, Claude Code, Cursor and Claude Desktop.
Before `install`, capture a baseline and stop this installation's stdio servers,
either by disabling Agents-Core in open clients or by ending those sessions;
`install` fails while any of them holds the installation lease. See
[installation and client migration](docs/shared-mcp-daemon.md#installation-and-client-migration).
The service pins the plain-file copy of `microsoft/harrier-oss-v1-270m`, which
`install` does not download: run setup with the default model, or set
`EMBEDDING_MODEL` to it in `.env` and run `.venv/bin/python -m src.reindex` with
stdio sessions stopped. `install --model intfloat/multilingual-e5-large` pins a
cached e5-large snapshot instead. Export a custom `FASTEMBED_CACHE_DIR` in the shell before `install`,
which does not read it from `.env`. Desktop and tracked project configurations use
the Node 22+ bridge. See the [prerequisites](docs/shared-mcp-daemon.md).
Run controller commands from the installation checkout (`~/.agents-core`, or
`AGENTS_HOME`, for the one-command installer).

```bash
.venv/bin/python -m src.daemon install
.venv/bin/python -m src.daemon start
.venv/bin/python -m src.daemon migrate --clients codex,claude,cursor
.venv/bin/python -m src.daemon migrate --workspace /absolute/path/to/project
.venv/bin/python -m src.daemon migrate --clients desktop
```

Global registrations provide routing; project memory requires a registered
workspace. Register each clone or worktree separately with `migrate --workspace`.
Migration manages private bearer headers and backups. Reconnect MCP in open
clients after migration. See [service operations](docs/shared-mcp-daemon.md) for
scope audits, updates, token rotation and rollback.

On Windows the same commands, run with `.venv\Scripts\python.exe`, install the
service as a hidden Task Scheduler task of the current user, with its private
state in `%USERPROFILE%\.agents-core`. Stdio servers on Windows take no
installation lease, so `install` cannot see them: end them yourself first.
`update` and automatic updates are not available there yet (#195). See
[Windows](docs/shared-mcp-daemon.md#windows-task-scheduler).

Setup, migration and audit read `CLAUDE_CONFIG_DIR`, `CODEX_HOME`,
`AGENTS_CURSOR_MCP_CONFIG`, `AGENTS_CLAUDE_DESKTOP_CONFIG` and
`AGENTS_ANTIGRAVITY_MCP_CONFIG` only from the environment of the command;
`--client-config CLIENT=/absolute/file` selects an explicit file (see
[alternate client configurations](docs/shared-mcp-daemon.md#alternate-client-configurations)).
For example:

```bash
CLAUDE_CONFIG_DIR="$HOME/.claude-work" .venv/bin/python -m src.daemon migrate --clients claude
.venv/bin/python -m src.daemon migrate --clients cursor --client-config cursor=/absolute/profile/mcp.json
.venv/bin/python -m src.daemon audit --workspace /absolute/path/to/project
```

The configurations below are for explicit standalone stdio use (including platforms
without the macOS service). To return from the shared service to stdio, run
`restore-clients` and then `uninstall` (see
[rollback](docs/shared-mcp-daemon.md#updates-and-recovery)); setup writes
standalone stdio registrations again only after `uninstall`. Replace both absolute
installation paths below (the one-command installer uses `~/.agents-core`; expand
`~`). Set `AGENTS_CLIENT_REPO_ROOT` to the client project when its launch
directory is not reliable.

### Claude Code (`.mcp.json` in project root)

```json
{
  "mcpServers": {
    "Agents-Core": {
      "command": "/absolute/path/to/Agents/.venv/bin/python",
      "args": ["/absolute/path/to/Agents/src/server.py"],
      "env": {
        "AGENTS_CLIENT_REPO_ROOT": "/absolute/path/to/project"
      }
    }
  }
}
```

Over stdio the server answers the MCP handshake without waiting for its stores,
the embedding model or Langfuse; they load in the background, and a retrieval
tool called before that finishes waits up to `WARMUP_WAIT_SECONDS` (default 20) and
then returns `warming_up`, so the call can be retried ([details](docs/routing_flow.md#startup-and-readiness)).
Claude Code gives a server 30 seconds to connect by default; raise it with the
`MCP_TIMEOUT` environment variable (milliseconds) if a slow machine still times out.
Do not register Agents-Core in both the Claude Desktop config and the Claude Code
registry: the desktop app runs its own server and injects it into the Code
sessions it launches, while each Code session also starts one from the Code
registry, so one session can run several server processes. `init_repo` warns
when it configures both. The injected server is shared by all Code sessions and
started outside any project, so its memory and flow tools need the `workspace`
argument ([rules](#-repository-memory)).

### Cursor (`.cursor/mcp.json` in the client project)

```json
{
  "mcpServers": {
    "Agents-Core": {
      "command": "/absolute/path/to/Agents/.venv/bin/python",
      "args": ["/absolute/path/to/Agents/src/server.py"],
      "env": {
        "AGENTS_CLIENT_REPO_ROOT": "/absolute/path/to/project"
      }
    }
  }
}
```

### Codex (`config.toml`)

Setup installs Codex instructions but does not register this server; without an
entry, Codex follows the instructions' MCP-unavailable fallback. Add to
`~/.codex/config.toml` (or `$CODEX_HOME/config.toml`):

```toml
[mcp_servers."Agents-Core"]
command = "/absolute/path/to/Agents/.venv/bin/python"
args = ["/absolute/path/to/Agents/src/server.py"]
```

Set `AGENTS_CLIENT_REPO_ROOT` in an `[mcp_servers."Agents-Core".env]` table only in
a trusted project's `.codex/config.toml`. In the user-level file it would direct
every session's memory and flows to that one project.

### Generic stdio

```bash
source .venv/bin/activate
python src/server.py
# Server communicates via stdin/stdout using MCP protocol
```

---

## 🧠 Creating New Agents

1. Create directory: `agents/<agent_name>/`
2. Create `system_prompt.mdc` with frontmatter:

```yaml
---
identity:
  name: "my_agent"
  display_name: "My Agent"
  role: "Expert in X"
  tone: "Professional, Clear"
routing:
  domain_keywords: ["keyword1", "keyword2"]
  trigger_command: "/my_command"
core_skills: []             # required lists of skill IDs, may be empty
preferred_skills: []
capable_skills: []
---
# My Agent System Prompt

## Identity
You are an expert in X...
```

`identity.name` must match the directory name, or the agent fails to load. Use
lowercase letters, digits and underscores (`^[a-z0-9][a-z0-9_]*$`): the activation
descriptor accepts no other agent name.
`trigger_command` and each optional `routing.aliases` entry become MCP slash
prompts. See [Skill tiers](#skill-tiers) and the
[field reference](agents/README.md#agent-source-and-metadata), including the
optional `preferred_implants`.

Validate the frontmatter with `.venv/bin/python scripts/validate_agents.py`. It
checks required fields and ID format, not whether the listed files exist; a
missing core skill or preferred implant file can make activation return `ERROR`.
The MCP server discovers the agent on its next startup; restart the shared daemon
through its controller when using that transport. The schema is
[agents/common/agent-schema.json](agents/common/agent-schema.json).

### Skill tiers

Each agent picks its skills explicitly in frontmatter, in three tiers:

```yaml
core_skills: [skill-content-structure, skill-dev-clean-code]
preferred_skills: [skill-dev-debugging, skill-dev-performance]
capable_skills: [skill-dev-testing, skill-git-conventions]
```

- `core_skills` are always loaded, unless switched off for the installation in the web UI.
- `preferred_skills` join the semantic pool with their distance multiplied by a boost factor (0.7), so they win close matches.
- `capable_skills` join the same pool at their base distance.

Skills outside the three lists are never loaded for that agent, except when a flow's persona lists exact skills ([details](flows/README.md#choose-a-flows-agent-and-components)). Guidance that applies to every agent belongs in [`rules/`](rules/README.md), not in a skill.

---

## 🧠 Repository Memory

The server stores repository summaries and action history separately for each
client project:

- **`describe_repo`** — generates a compressed, LLM-consumable repo overview via MCP sampling and writes it into the managed *Repository Memory* section of `CLAUDE.md`. Without sampling, or when the sampling call fails, it writes nothing and returns `needs_summary` (the prompt plus `repo_hash`, `repo_path` and `workspace_id`), and the caller persists its own summary with `write_repo_summary`. The shared HTTP daemon never samples, so a needed refresh over HTTP always returns `needs_summary`. Idempotent: re-runs are no-ops unless the repository fingerprint changes (top-level names, first- and second-level directory names, key manifest contents and the head of `README.md`), the managed section is missing, or `force_refresh=True`.
- **`log_interaction`** — end-of-turn logger. Appends `intent / action / outcome` entries (with optional files and tags) to `history.md` at the repo root; deduplicated by content hash; rotated to `history/YYYY-MM.md` when the file exceeds 512 KB. Also sends a Langfuse generation trace if keys are configured. A partial, malformed or mismatching `persona`, or `persona_action` without `persona`, never drops the turn: the entry is written with an `unverified`/`mismatch` persona line and the response carries `warnings`; an unavailable workspace is the only request-level rejection (sinks stay best-effort: a full queue or a sink failure can still lose a write; a failed history write is reported as `history_last_error`, see below). It returns at once with a local `timestamp` (`YYYY.MM.DD HH:MM:SS`), which the final answer shows as its first line when the footer lists the `answer-timestamp` rule; both writes happen in the background and are drained on shutdown.
- **`read_history`** — returns recent entries by recency/`since` filter, or runs a lazy semantic search backed by the same `NumpyVectorStore` used for routing. A listing stays under the 50,000 characters above which Claude Code saves a tool result to a file. `outcome` comes as a preview of up to 600 characters, `intent` and `action` of up to 300, `files` and `tags` keep up to 20 items, and a shortened entry gives each cut field's full length in `truncated` (characters, or items for a list). When that is still too much, the previews get shorter, and then the last entries are left out, counted in `omitted`; `offset` skips that many entries, and the `instruction` gives the `offset` and `limit` that read them; entries logged between two calls shift those pages. Semantic results carry `intent`, `action` and `outcome` like recent ones, without `files` and `metadata`. `read_history(entry_id=...)` returns one entry whole, also one that rotation moved to `history/YYYY-MM.md`. With [user library sync](docs/user-sync.md#repository-history) set up, a repository's top-level checkout (with an `origin`) reads its `history.md` and `history/*.md` merged with the entries the user's machines shared for that repository, including this machine's other checkouts: one per entry hash, the checkout's own copy first, ordered by time and read newest month first. Each entry carries `machine` (null for the checkout's own entries, else the label of the machine that wrote it); `machine=<label>` keeps that machine's entries and `machine=local` this checkout's own. `history.md` itself stays the checkout's journal. A machine shares a repository's entries only after its owner reviewed them with `python -m src.user_sync history export`: approval covers the repository's later entries on that machine and exactly the past entries listed, so another checkout that joins later waits for its own review.

Over stdio, the project is `AGENTS_CLIENT_REPO_ROOT` when set; otherwise the
nearest `.git` or `CLAUDE.md` at or above the start directory. The start directory
is `CLAUDE_PROJECT_DIR`, which Claude Code exports to the servers it starts, or else
the launch directory. A `CLAUDE_PROJECT_DIR` without a marker is used as named. A
launch directory without a marker is refused with `workspace_required`: it says
nothing about the project. A root (including the override) that is a filesystem
root, the home directory, or a system or program directory is refused with
`workspace_unsafe`: on Windows the Windows directory, `%ProgramFiles%`,
`%ProgramFiles(x86)%`, `%ProgramData%` and `C:\Users` itself; on POSIX `/usr`,
`/var`, `/home`, `/Users` and similar, plus `/etc`, `/bin`, `/usr/bin` and the other
system subtrees (`/usr/local`, `/private/tmp` and macOS temporary directories stay
allowed). If the launch directory no longer exists, memory falls back to the
installation and workflows return an error.

One stdio process can also serve several sessions. The Claude desktop app starts the
servers in `claude_desktop_config.json` once, in `C:\Windows\System32` without
`CLAUDE_PROJECT_DIR`, and its Code-tab sessions reach that process when Agents-Core is
registered there too. Its `roots/list` answer lists the folders of every open session,
so neither the process nor its roots tell which project a call belongs to. The memory
and flow tools therefore take `workspace`, the absolute path of the caller's working
directory, which the routing instructions tell the model to pass. When the client
declares the MCP roots capability, `workspace` is checked on every call: it must lie
inside one of the client's roots, resolves like `CLAUDE_PROJECT_DIR` (a git worktree
to its main checkout, as Claude Code does for a worktree session) and is refused when
unsafe; `source` is then `workspace`. It takes precedence over `CLAUDE_PROJECT_DIR`
and the launch directory, never over `AGENTS_CLIENT_REPO_ROOT`, and is never pinned
for the process. A client without roots cannot vouch for the path, so the process
root applies and a refusal names `AGENTS_CLIENT_REPO_ROOT`. Set
`AGENTS_CLIENT_REPO_ROOT` only on a per-project registration, never on a shared or
Desktop one.

`log_interaction` and `read_history` results carry `workspace` (`root`, `source`) and
`pid`, and, after a failed history write, `history_last_error`
(`code=history_unwritable`, `errno`, `path`, `at`), which the model should mention once.
Over stdio their workspace errors carry `pid` and `workspace_inputs` (`cwd`,
`claude_project_dir`, `agents_client_repo_root`, `client`, `roots`); when the client
declares roots and `workspace` was omitted, the refusal asks for it and
`log_interaction` tells the model to retry once with it.
Over HTTP, memory requires the registered `X-Agents-Workspace` header;
the daemon does not infer a project from its working directory. The user-scope
entries of Claude Code and Codex are a bridge per session that registers the
session's project and sends that header itself ([details](docs/shared-mcp-daemon.md#installation-and-client-migration)).
If a memory tool returns `workspace_required`, `workspace_unsafe` or
`workspace_invalid`, routing remains available.
For HTTP without that bridge, register the project with `migrate --workspace /absolute/project` (see
[service operations](docs/shared-mcp-daemon.md#installation-and-client-migration))
and reconnect MCP before retrying memory operations; for stdio, set
`AGENTS_CLIENT_REPO_ROOT` as described above. Do not retry logging in a loop.

Source history and `CLAUDE.md` stay in the project. The summary hash is stored in
the project's `data/memory/`. Derived router and history indexes live in private
daemon state or, for standalone processes, in leased `data/stdio/` slots of the
installation (a temporary per-process directory on Windows). These
indexes can be rebuilt without deleting the source history.

See [service memory behavior](docs/shared-mcp-daemon.md#memory-and-errors) for the
current transport contract and [the original memory design](docs/memory-subsystem-spec.md)
for its rationale.

`log_interaction` stores the supplied prompt and response by default; callers can
provide curated `intent`, `action`, and `outcome` fields. This checkout ignores
`history.md` and `history/`. In a client project, check the ignore rules for
`history.md`, `history/`, `data/memory/` and the hidden lock files
(`.history.md.lock`, `.CLAUDE.md.lock`, `.agents-description.lock`) before
committing its action log.

---

## 📊 Observability

Selected routing, loading, retrieval, and memory operations are instrumented with
Langfuse. `log_interaction` records the answer and declared persona attribution in
the background: its response reports both sinks as `queued` (Langfuse `skipped`
while retrieval warms up), a failed history write appears as `history_last_error`
on later results, and Langfuse failures go only to the server log. Set real Langfuse keys
in `.env` to enable it; leave them empty for local-only operation (see
[Environment Variables](#environment-variables)). Set
`LANGFUSE_TRACING_ENABLED=false` when running checks that should not send traces.

---

## 🛠️ Development

Read [AGENTS.md](AGENTS.md) for contributor instructions, [the documentation
refresh process](flows/documentation-refresh.md) for documentation work, and
[tests/README.md](tests/README.md) for the test matrix.

Keep reusable model task instructions in `flows/` and list them in the
[workflow catalog](flows/README.md), which explains how to run them locally or
through `run_flow`.

### Validation

Run these commands from the checkout root in the test environment described in
[tests/README.md](tests/README.md#environment) (`requirements.txt` plus
`-e '.[evals]'`). The environment that setup creates installs only
`requirements.txt` and cannot run the whole suite:

```bash
LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/ -q
.venv/bin/python scripts/validate_agents.py
```

The default pytest configuration excludes `slow` tests. Use
`LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/ -m '' -q` to
include them. Bridge changes also require `node --test bridge/test.mjs`.
The test configuration isolates derived indexes in temporary storage; tests that
load embeddings still need the selected model to be available.

### Debug Logging

Enable detailed per-call JSON logging when [running the server manually](#generic-stdio):

```bash
AGENTS_DEBUG=1 python src/server.py
```

Standalone debug logs are written under the client project's `logs/`; HTTP
debug logs are written under the daemon's private `debug/` directory. Files use
`{YYYY-MM-DD}/{HH-MM-SS.fff}_{uuid}_{tool}_{direction}.json`. Logging is disabled
unless `AGENTS_DEBUG=1` (or `true`). Standalone logs are never pruned and can
contain query text; `logs/` is ignored only in this checkout, so exclude it in the
client project or delete the files after diagnosis. The daemon keeps its debug
files for at most seven days and 100 MiB.

---

## 📝 License

MIT
