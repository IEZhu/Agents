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

- an `EMBEDDING_MODEL` already set in `.env` is kept; otherwise the model is
  chosen from installed RAM (see [Environment Variables](#environment-variables));
- an existing `.venv` is reused and its dependencies are refreshed; it is
  recreated only when its Python version is unknown or older than 3.11;
- the Claude instruction and routing-reminder prompts are accepted. Client
  registration and Codex instructions never ask (see [After Cloning](#after-cloning)).

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
and `--skip-mcp`, but not `--yes`.

The interactive script:

- creates `.env` from `env.example` (or adds keys missing from an existing one),
  creates `.venv/` and installs dependencies, selects and downloads an embedding
  model, and builds the skill and implant indexes. When `.venv/` exists and you
  keep it (the default answer), dependencies are not reinstalled; answer `y` to
  recreate it, pass `--yes`, or run `.venv/bin/pip install -r requirements.txt`;
- registers Agents-Core without asking as a standalone stdio server in each
  detected client's user-level configuration (Claude Code `~/.claude.json`, Cursor
  `~/.cursor/mcp.json`, Claude Desktop `claude_desktop_config.json`), after
  copying each file to `<file>.backup.<epoch>`;
- asks before installing protocol 2 instructions in the global Claude
  configuration. When Codex is detected, it installs the same protocol in Codex's
  global instructions without asking; this does not connect Codex to MCP (see
  [Codex instruction installation](#codex-instruction-installation));
- targets another profile when `CLAUDE_CONFIG_DIR` (Claude Code),
  `AGENTS_CURSOR_MCP_CONFIG` or `AGENTS_CLAUDE_DESKTOP_CONFIG` is exported in the
  shell that runs setup. Setup does not read them from `.env`, and a set variable
  also counts as detecting that client (see [alternate client configurations](docs/shared-mcp-daemon.md#alternate-client-configurations));
- on macOS, once the shared service is installed, keeps these clients on the
  service instead of writing standalone stdio entries. Do not rerun setup while
  the service runs (see [service updates](docs/shared-mcp-daemon.md#updates-and-recovery)).

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
# Edit .env for the model and any optional integrations

# Download the selected model and build the indexes
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
requires network access. Setup writes the selected model to `EMBEDDING_MODEL` in
`.env` and keeps an existing setting on later runs. Full is
`intfloat/multilingual-e5-large`, Balanced is
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, and Light is the
English-only `sentence-transformers/all-MiniLM-L6-v2`. `scripts/init_repo.sh`
offers a default based on installed RAM, which `--yes` applies: Full with 32 GB or
more, Balanced with 16 GB or more, otherwise Light (Balanced when RAM cannot be
detected); `init_repo.bat` defaults to Balanced. When `EMBEDDING_MODEL` is
unset, for example after `--skip-index` (which also skips the model choice),
standalone stdio uses Balanced. To change the model, edit `EMBEDDING_MODEL` and,
with server sessions stopped, run `.venv/bin/python -m src.reindex`. The
[shared macOS service](#shared-macos-service) always uses a cached Full snapshot
selected by its controller.

| Setting | Default | Purpose |
|---|---|---|
| `EMBEDDING_MODEL` | Balanced (multilingual MiniLM) when unset | Standalone embedding model |
| `FASTEMBED_CACHE_DIR` | `~/.cache/fastembed` | Persistent model cache; the shared service's `install` reads it only from the shell ([daemon guide](docs/shared-mcp-daemon.md)) |
| `AGENTS_CLIENT_REPO_ROOT` | Unset: inferred from `CLAUDE_PROJECT_DIR` or the working directory ([rules](#-repository-memory)) | Explicit stdio memory and workflow target, used as given. Set it in an MCP entry's `env`, never in the installation `.env`, which every stdio session loads |
| `RULES_ENABLED` | `1` | Include [shared rules](rules/README.md) in loaded context |
| `INTENT_CLASSIFIER_ENABLED` | `0` | Enable the optional intent-based enrichment classifier |
| `AGENTS_USER_FLOWS_DIR` | `flows/.user` in the installation | Location (absolute path) of [personal and repository flows](flows/README.md#personal-and-repository-flows) |

Routing thresholds and enrichment settings are defined in
[src/engine/config.py](src/engine/config.py). Its environment overrides, such as
`ROUTER_SIMILARITY_THRESHOLD`, `SKILLS_RELEVANCE_THRESHOLD`,
`IMPLANTS_RELEVANCE_THRESHOLD`, the
[`IMPLANT_*` settings](implants/README.md#1-agent-preferences-and-semantic-retrieval) and the
[intent classifier](docs/intent-classifier.md) thresholds in `env.example`, are
tuning and evaluation settings; normally keep their defaults. HTTP memory uses a
registered workspace header instead of `AGENTS_CLIENT_REPO_ROOT`.

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
  `git pull --ff-only`, `.venv/bin/pip install -r requirements.txt`, then
  `.venv/bin/python -m src.reindex`. If it has already activated, stop the
  sessions and install the dependencies before restarting. Only with
  `AGENTS_AUTO_UPDATE_STAGING=0` is the new `requirements.txt` already in place
  when the warning appears.

Lifetime locks require POSIX `flock` (Linux/macOS). On platforms without it the
server runs with automatic updates disabled. When upgrading from a version that
does not hold these locks, restart all existing server sessions once. Manual Git
operations and rebuilds must also be done with those sessions stopped.

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
AGENTS_AUTO_UPDATE_REINDEX_TIMEOUT=600   # seconds allowed for the staged index build
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
| `refresh_persona_context(query, current_persona)` | Refresh skills/implants for the active role without reselecting it |
| `load_implants(query\|task_type)` | Load cognitive reasoning strategies by semantic query or preset bundle |
| `list_agents()` | Enumerate all available agents with metadata |
| `list_flows(scope="all")` | Discover built-in, personal (`user:`) and repository (`repo:`) Markdown workflows, with IDs and content revisions |
| `run_flow(flow, request="", repo_path=None)` | Load a workflow for the caller's repository; returns `needs_execution` for the current model to carry out using its tools |
| `get_flow` / `save_flow` / `delete_flow` | Manage personal and repository flows from chat, with history and conflict detection; stored in the git-ignored `flows/.user` ([details](flows/README.md#personal-and-repository-flows)) |
| `log_interaction(agent_name, query, response_content, persona?, persona_action?, intent?, action?, outcome?, files?, tags?, request_id?, reasoning?)` | End-of-turn attribution log ([details](#-repository-memory)); `persona` is the active descriptor, `persona_action` is `keep`, `switch`, `refresh` or `restore` |
| `clear_session_cache()` | Stdio only: administrative reset of the shared enrichment cache; not required for persona changes. For HTTP, use `.venv/bin/python -m src.daemon clear-cache` |
| `describe_repo(repo_path=None, force_refresh=False)` | Repository summary bootstrap; returns `needs_summary` when sampling is unavailable (always over HTTP) or fails ([details](#-repository-memory)) |
| `write_repo_summary(summary, repo_hash, repo_path=None, workspace_id=None)` | Persists the summary after `needs_summary`; pass its `repo_hash`, `repo_path` and `workspace_id` back unchanged |
| `read_history(limit?, since?, query?)` | Recent entries or lazy semantic recall over the action log |

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

Besides the built-in flows, users keep personal (`user:`) and per-repository
(`repo:`) flows in the installation's git-ignored `flows/.user`. Ask the model to
save, change, restore or delete one; it uses `get_flow`, `save_flow` and
`delete_flow`. A bare flow name resolves `repo:`, then `user:`, then the built-in.
With the shared daemon, `.venv/bin/python -m src.daemon flows-ui` opens a local
[flow editor](docs/shared-mcp-daemon.md#flow-editor); the same page lists
rules, skills and implants and switches each one on or off for the installation.

### Persona continuity

Before each request, the model silently checks whether its active role fits the
task. If it does, it keeps that role without routing, candidate selection or
enrichment calls. A needed specialization change triggers routing; a known
requested role loads directly. Lost instructions restore the known role; a
justified refresh rebuilds its context without reselecting it.

The server returns separate persona, rules, skills and implants blocks, an
activation descriptor and an exact footer (the four labelled component lists, then
a muted `Agents-Core <version>` segment, with the web UI link under the daemon). Every successful switch, restore or
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
│   ├── flows.py          # Built-in flow catalog and run_flow bundles
│   ├── user_flows.py     # Personal and repository flows (flows/.user)
│   ├── client_paths.py   # Client configuration paths shared by installers and migration
│   ├── file_lock.py      # Stable sidecar file locks
│   ├── daemon/           # HTTP app, service control, workspaces, client migration, flow editor
│   ├── mcp_servers/      # Optional document OCR server and the MCP server template
│   ├── engine/
│   │   ├── router.py     # Semantic routing (cache-first)
│   │   ├── persona.py    # Protocol 2 activation, restore, and refresh
│   │   ├── persona_bundle.py # Fresh instruction blocks and bundle revision
│   │   ├── skills.py     # Skill retrieval (vector search)
│   │   ├── implants.py   # Implant retrieval (vector search)
│   │   ├── rules.py      # Shared rule loading
│   │   ├── config.py     # Centralized configuration
│   │   ├── embedder.py   # FastEmbed wrapper (ONNX Runtime)
│   │   ├── vector_store.py # NumPy-based vector store
│   │   ├── enrichment.py # Tier-based context enrichment
│   │   ├── intent.py     # Optional task classification
│   │   ├── fingerprint.py # Index compatibility fingerprint
│   │   ├── context.py    # Context retrieval (history formatting)
│   │   └── language.py   # Language detection
│   ├── memory/           # Repository summary, history, managed sections
│   ├── schemas/protocol.py # Request, response, and persona schemas
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
| **Rules** | [Shared directives](rules/README.md) included in every loaded bundle when enabled |
| **Router** | Semantic matching + caching for fast agent selection |
| **Persona bundle** | Versioned role instructions, rules, skills, and implants retained by the client |
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
The service uses a cached `intfloat/multilingual-e5-large` snapshot, which
`install` does not download: choose Full during setup, or set `EMBEDDING_MODEL`
to it in `.env` and run `.venv/bin/python -m src.reindex` with stdio sessions
stopped. Export a custom `FASTEMBED_CACHE_DIR` in the shell before `install`,
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

Setup, migration and audit read `CLAUDE_CONFIG_DIR`, `CODEX_HOME`,
`AGENTS_CURSOR_MCP_CONFIG` and `AGENTS_CLAUDE_DESKTOP_CONFIG` only from the
environment of the command; `--client-config CLIENT=/absolute/file` selects an
explicit file (see [alternate client configurations](docs/shared-mcp-daemon.md#alternate-client-configurations)).
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

`identity.name` must match the directory name, or the agent fails to load.
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

- `core_skills` are loaded unconditionally.
- `preferred_skills` join the semantic pool with their distance multiplied by a boost factor (0.7), so they win close matches.
- `capable_skills` join the same pool at their base distance.

Skills outside the three lists are never loaded for that agent. Guidance that applies to every agent belongs in [`rules/`](rules/README.md), not in a skill.

---

## 🧠 Repository Memory

The server stores repository summaries and action history separately for each
client project:

- **`describe_repo`** — generates a compressed, LLM-consumable repo overview via MCP sampling and writes it into the managed *Repository Memory* section of `CLAUDE.md`. Without sampling, or when the sampling call fails, it writes nothing and returns `needs_summary` (the prompt plus `repo_hash`, `repo_path` and `workspace_id`), and the caller persists its own summary with `write_repo_summary`. The shared HTTP daemon never samples, so a needed refresh over HTTP always returns `needs_summary`. Idempotent: re-runs are no-ops unless the repository fingerprint changes (top-level names, first- and second-level directory names, key manifest contents and the head of `README.md`), the managed section is missing, or `force_refresh=True`.
- **`log_interaction`** — end-of-turn logger. Appends `intent / action / outcome` entries (with optional files and tags) to `history.md` at the repo root; deduplicated by content hash; rotated to `history/YYYY-MM.md` when the file exceeds 512 KB. Also sends a Langfuse generation trace if keys are configured.
- **`read_history`** — returns recent entries by recency/`since` filter, or runs a lazy semantic search backed by the same `NumpyVectorStore` used for routing.

Over stdio, the project is `AGENTS_CLIENT_REPO_ROOT` when set, used as given;
otherwise the nearest `.git` or `CLAUDE.md` at or above the start directory, then
the start directory itself. The start directory is `CLAUDE_PROJECT_DIR`, which Claude Code
exports to the servers it starts, or else the launch directory. An inferred root
that is a filesystem root or, on Windows, lies inside the Windows directory is
refused with `workspace_required`. On Windows, for example, the Claude desktop app
starts the servers in `claude_desktop_config.json` in `C:\Windows\System32` without
a project hint; register Agents-Core with Claude Code instead, or set
`AGENTS_CLIENT_REPO_ROOT` in the entry's `env`. If the launch directory no longer
exists, memory falls back to the installation and workflows return an error.
Over HTTP, memory requires the registered `X-Agents-Workspace` header;
the daemon does not infer a project from its working directory. If a memory tool
returns `workspace_required` or `workspace_invalid`, routing remains available.
For HTTP, register the project with `migrate --workspace /absolute/project` (see
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
`history.md` and `history/`; check the client project's own ignore rules before
committing its action log.

---

## 📊 Observability

Selected routing, loading, retrieval, and memory operations are instrumented with
Langfuse. `log_interaction` records the answer and declared persona attribution;
its history and Langfuse results are reported separately. Set real Langfuse keys
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
