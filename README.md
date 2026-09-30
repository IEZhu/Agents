# 🤖 Agents Framework

**Universal MCP Server for AI Agent Roles, Skills & Cognitive Implants**

A local MCP server that loads specialized agent personas, domain skills, shared rules, and cognitive reasoning implants. Clients can connect through standalone stdio processes or a shared macOS HTTP service. Protocol 2 keeps a fitting persona in the conversation and routes only when selection is needed.

- [Documentation map](docs/README.md): current guides, reference material, and evaluation reports.
- [AI contributor instructions](AGENTS.md) and [Claude instructions](CLAUDE.md).
- [Model workflows](flows/README.md): reusable Markdown instructions, including
  the [documentation refresh process](flows/documentation-refresh.md).

---

## 🚀 Quick Start

### After Cloning

Use Python 3.11 or newer, as required by [pyproject.toml](pyproject.toml).
An existing `.venv` must also use a supported version. If it uses an older Python,
choose to recreate it when the installer asks; setup stops if you decline.

```bash
git clone <repository-url>
cd Agents

# Run initialization script
./scripts/init_repo.sh
```

The interactive script creates or reuses `.venv/`, installs dependencies, creates
`.env`, selects and downloads an embedding model, and builds the skill and implant
indexes. It can also configure detected Cursor, Claude Code, and Claude Desktop
clients and install protocol 2 instructions in the global Claude configuration.
When Codex is detected, it automatically installs the same protocol in Codex's
global instructions. This instruction update is separate from connecting Codex to
MCP; see [Codex instruction installation](#codex-instruction-installation).
Use `./scripts/init_repo.sh --help` for the available skip options. For the shared
macOS service and client connections, continue with [MCP client configuration](#-mcp-client-configuration).

For an existing installation, refresh global Codex and Claude instructions with
`python3 scripts/install_instructions.py`. This standalone command needs Python
3.11 or newer and does not rerun setup; see the [instruction update options](#codex-instruction-installation).

### Manual Setup

```bash
# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

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

The core router needs no external API key. Keep optional integration keys empty
unless you use the corresponding service. Configure `.env` using [env.example](env.example):

```env
LANGFUSE_PUBLIC_KEY=          # Optional: observability
LANGFUSE_SECRET_KEY=          # Optional: observability
LANGFUSE_HOST=https://cloud.langfuse.com
ANTHROPIC_API_KEY=            # Optional: for document OCR
AGENTS_DEBUG=0                # Set to 1 for per-call JSON debug logs
```

Embeddings run locally through FastEmbed (ONNX Runtime); the initial model download
requires network access. Standalone stdio defaults to
`sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`. The shared daemon
uses a cached `intfloat/multilingual-e5-large` snapshot selected by its controller.

| Setting | Default | Purpose |
|---|---|---|
| `EMBEDDING_MODEL` | Multilingual MiniLM above | Standalone embedding model |
| `FASTEMBED_CACHE_DIR` | `~/.cache/fastembed` | Persistent model cache |
| `AGENTS_CLIENT_REPO_ROOT` | Nearest `.git` or `CLAUDE.md` at or above `CLAUDE_PROJECT_DIR` or the process working directory; otherwise that directory, but never a filesystem root or the Windows directory | Explicit stdio memory target |
| `RULES_ENABLED` | `1` | Include shared rules in loaded context |
| `INTENT_CLASSIFIER_ENABLED` | `0` | Enable the optional intent-based enrichment classifier |
| `AGENTS_USER_FLOWS_DIR` | `flows/.user` in the installation | Location of [personal and repository flows](flows/README.md#personal-and-repository-flows) |

Routing thresholds and enrichment settings are defined in
[src/engine/config.py](src/engine/config.py). HTTP memory uses a registered
workspace header instead of `AGENTS_CLIENT_REPO_ROOT`.

### Updates

The shared daemon has its own update controller. Run
`.venv/bin/python -m src.daemon update` for a manual update; automatic daemon
updates are opt-in through `.venv/bin/python -m src.daemon auto-update enable`.
See [service updates and recovery](docs/shared-mcp-daemon.md#updates-and-recovery).
The daemon disables the standalone updater described below.

#### Standalone stdio auto-update

The server can keep itself current. Updates are **two-phase** — prepared in the
background, activated on the next idle start — so the live install is never mutated
mid-session:

1. **Prepare** (background): a daemon thread (non-blocking, so it never delays
   serving) fetches the target branch and, if the install is fast-forwardable,
   builds the new version's vector stores in an isolated git worktree under
   `data/.prepared/<sha>`, then writes a marker. The live tree and stores are
   untouched.
2. **Activate** (next idle start): if a valid prepared update exists and no other
   server session is using this installation, the server
   fast-forwards the live tree (local, no network) and atomically moves the
   pre-built stores into `data/` — the expensive embedding already happened in
   phase 1. Existing sessions hold a shared installation lock for their lifetime;
   overlapping starts keep serving the current version and leave the update
   pending. A start during activation waits before importing application code.
   After waiting, it reloads both the bootstrap and server code under the lease.
   Background preparation uses a separate lock and does not delay startup.
   Git/reindex children inherit the relevant leases, so an orphan worker remains
   protected until it exits even if its server process has already stopped.
   Timed-out commands have their process group terminated. A separate inherited
   completion pipe keeps rollback and lease release waiting for any remaining
   descriptor holders, including detached descendants; this safety wait can
   exceed the command timeout.

It is **safe by default**:

- acts **only** when the checked-out branch is `AGENTS_AUTO_UPDATE_BRANCH` (default
  `main`) — a **no-op on feature branches**, so local development is never touched;
- only when the working tree is clean, and only **fast-forward** (never merge, rebase,
  or switch branches);
- a failed staged build discards the worktree and leaves the install as-is; crash
  recovery also uses the store's torn-pair detection and content-hash re-embed;
- staging roots, worktree paths, and store artifacts must not contain symlinks;
  redirected paths are rejected before reading, moving, or pruning their targets;
- store activation invalidates all hashes before moving files and publishes new
  hashes only after all pairs have moved. Any batch failure invalidates all hashes
  again; if that cannot be completed, the recovery journal keeps startup blocked;
- source paths in staged metadata are rebased to the live checkout before
  publication, while the vector files and their matching save versions are retained;
- offline/fetch/build failures leave the current version available. A failed
  activation merge (including a timeout after HEAD moved) restores the old tree.
  If rollback or re-exec fails, or an unexpected activation error leaves the tree's
  state unknown, startup stops instead of serving mixed versions.
  Dependencies are **not** auto-installed.

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
| `route_and_load(query)` | Semantic routing when selection is requested; returns a loaded role or candidates for the client to choose |
| `get_agent_context(agent_name, query)` | Direct agent loading when the target is already known |
| `refresh_persona_context(query, current_persona)` | Refresh skills/implants for the active role without reselecting it |
| `load_implants(query\|task_type)` | Load cognitive reasoning strategies by semantic query or preset bundle |
| `list_agents()` | Enumerate all available agents with metadata |
| `list_flows(scope="all")` | Discover built-in, personal (`user:`) and repository (`repo:`) Markdown workflows, with IDs and content revisions |
| `run_flow(flow, request="", repo_path=None)` | Load a workflow for the caller's repository; returns `needs_execution` for the current model to carry out using its tools |
| `get_flow` / `save_flow` / `delete_flow` | Manage personal and repository flows from chat, with history and conflict detection; stored in the git-ignored `flows/.user` ([details](flows/README.md#personal-and-repository-flows)) |
| `log_interaction(agent_name, query, response_content, intent?, action?, outcome?, files?, tags?)` | End-of-turn logger — appends to `history.md` (deduped by content hash) and, if configured, sends a Langfuse generation trace |
| `clear_session_cache()` | Stdio only: administrative reset of the shared enrichment cache; not required for persona changes. For HTTP, use `.venv/bin/python -m src.daemon clear-cache` |
| `describe_repo(repo_path=None, force_refresh=False)` | One-shot repo bootstrap — writes a structured summary into the managed Repository Memory section of CLAUDE.md via sampling; without sampling, or when the sampling call fails, it returns `needs_summary` with the prompt and writes nothing until `write_repo_summary` is called |
| `write_repo_summary(summary, repo_hash, repo_path=None, workspace_id=None)` | Persists the summary when `describe_repo` returns `needs_summary` (no sampling, or sampling failed); pass its `repo_hash`, `repo_path` and `workspace_id` back unchanged |
| `read_history(limit?, since?, query?)` | Recent entries or lazy semantic recall over the action log |

### Workflows in the caller's repository

Ask the model to use Agents-Core to run `documentation-refresh` or `pr-review`
in the current repository. It calls `list_flows()` to discover flows and
`run_flow(flow="documentation-refresh")` to get the instructions. For review,
pass the PR/MR URL and constraints in `request`, such as `no-merge`.

The flow files stay in the MCP installation. Inspection, edits, tests and PR/MR
actions target the caller's repository. HTTP requires a registered workspace in
`X-Agents-Workspace`; stdio uses `AGENTS_CLIENT_REPO_ROOT`, `CLAUDE_PROJECT_DIR` or
the server's working directory, never a filesystem root or the Windows directory.
An optional `repo_path` must stay within that workspace. A missing
HTTP workspace is an error, even when `repo_path` is supplied.

The tool reads and returns instructions; the current model executes them with
its existing permissions and tools. `needs_execution` does not mean the work is
complete. See the [workflow guide](flows/README.md#through-agents-core-mcp) for
the response contract, target resolution, and authoring rules.

Besides the built-in flows, users keep personal (`user:`) and per-repository
(`repo:`) flows in the installation's git-ignored `flows/.user`. Ask the model to
save, change, restore or delete one; it uses `get_flow`, `save_flow` and
`delete_flow`. A bare flow name resolves `repo:`, then `user:`, then the built-in.
With the shared daemon, `.venv/bin/python -m src.daemon flows-ui` opens a local
[flow editor](docs/shared-mcp-daemon.md#flow-editor).

### Persona continuity

Before each request, the model silently checks whether its active role fits the
task. If it does, it keeps that role without routing, candidate selection or
enrichment calls. A needed specialization change triggers routing; a known
requested role loads directly. Lost instructions restore the known role; a
justified refresh rebuilds its context without reselecting it.

The server returns separate persona, rules, skills and implants blocks, an
activation descriptor and an exact footer. Every successful switch, restore or
refresh replaces all four blocks, including changed or removed rules, while
preserving higher-priority instructions, conversation facts, goals, permissions
and tool results. This is logical replacement: MCP cannot physically delete old
messages. No history or cache clearing is required. The server never samples an
answer.

This is protocol 2, the only protocol since 2026-09-29; the installers made it the
default on 2026-09-26. Clients pass `protocol_version=2` (also the default); any
other value returns `ERROR` without loading a persona. Under the removed protocol 1, 96% of
routed turns in 30 days of telemetry returned ROUTE_REQUIRED, and when the turn
continued a conversation the model re-picked the agent already active 73% of the
time, re-sending its full prompt ([analysis](evals/telemetry/README.md)); in the
[dialogue evaluation](docs/persona-switch-eval-results.md), version 2 made no
selection calls on continuing turns and switched roles correctly in every completed
case. That report also records the remaining gaps for each tested client/model;
they do not establish support for other applications or native context compaction.

Both installers apply the protocol to detected Codex global instructions as well
as the Claude instruction setup. The checked-in `CLAUDE.md` uses the same
template; the global installer does not change this tracked file. After editing
the template, refresh this checkout's managed section:

```bash
.venv/bin/python scripts/_helpers/inject_claude_md.py CLAUDE.md scripts/templates/routing-protocol-core.md
```

On Windows, use `.venv\Scripts\python.exe` for the same command.
Repository notes outside the markers are preserved. Managed instruction sections
are backed up and replaced by markers. For each managed instruction or routing
memory file, the helper keeps the three newest generated timestamp backups;
unchanged instructions create no new backup. Backup creation reserves a distinct
name even when successive writes have the same timestamp. Named manual backups
and other backup formats are preserved. This limit does not apply to MCP configuration
backups. Only exact known installer-generated
routing memory is migrated, including reminders written for protocol 1
([legacy copies](scripts/templates/legacy/README.md)); edited reminders are preserved with a path-specific warning. Windows
does not create an absent memory reminder. Review your own project instructions
and memory for conflicting unconditional `route_and_load` requirements; these
are not automatically rewritten.

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
├── agents/               # Agent personas, discovered from system_prompt.mdc
│   ├── software_engineer/
│   │   └── system_prompt.mdc
│   ├── common/           # Shared resources and agent-schema.json
│   └── schemas/          # Additional schemas
├── skills/               # Reusable knowledge chunks (RAG)
│   └── skill-*.mdc
├── implants/             # Cognitive reasoning strategies (RAG)
│   └── implant-*.mdc
├── rules/                # Shared directives (rule-*.mdc)
├── src/
│   ├── server.py         # FastMCP tools, prompts, and standalone entrypoint
│   ├── startup.py        # Installation leases and isolated stdio indexes
│   ├── self_update.py    # Standalone staged updates
│   ├── reindex.py        # Skill and implant index rebuild
│   ├── flows.py          # Built-in flow catalog and run_flow bundles
│   ├── user_flows.py     # Personal and repository flows (flows/.user)
│   ├── daemon/           # HTTP app, service control, workspaces, client migration, flow editor
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
├── scripts/templates/    # Versioned client instruction templates
├── tests/                # Deterministic and opt-in integration tests
├── evals/                # Routing, enrichment, and client dialogue evaluations
├── docs/                 # Guides and reference documents
├── flows/                # Markdown workflows served to caller repositories through MCP
├── data/                 # Installation indexes and leased stdio state (ignored)
├── pyproject.toml        # Python project metadata
└── requirements.txt
```

### Key Components

| Component | Description |
|-----------|-------------|
| **Agents** | Specialized personas with unique system prompts |
| **Skills** | Domain-specific knowledge chunks (retrieved via RAG) |
| **Implants** | Cognitive patterns & reasoning strategies |
| **Rules** | Shared directives included in every loaded bundle when enabled |
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
generated Claude routing reminder and its entry in `~/.claude/memory/MEMORY.md`,
but does not create an absent reminder.

This command updates global instructions, that reminder and its memory-index
entry, and their managed backups. It does not install dependencies, change `.env`
or vector indexes, register MCP connections, or change
service configuration. The shared daemon migration below configures Codex's MCP
connection on macOS. Refreshing instructions alone does not require restarting the
daemon.

### Shared macOS service

On macOS, one shared daemon can serve Codex, Claude Code, Cursor and Claude Desktop.
Before the first migration, follow the maintenance and baseline steps in
[service operations](docs/shared-mcp-daemon.md#installation-and-client-migration).
The service requires a locally cached e5-large model; choose that model during
setup if you plan to use the daemon. The Desktop bridge requires Node 22 or newer.

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

Client migration honors `CLAUDE_CONFIG_DIR` and `CODEX_HOME` from the environment
of the controller command. Use `--client-config CLIENT=/absolute/config/file`
for an explicit configuration file, including alternate Cursor or Claude Desktop
configurations. For example:

```bash
CLAUDE_CONFIG_DIR="$HOME/.claude-work" .venv/bin/python -m src.daemon migrate --clients claude
.venv/bin/python -m src.daemon migrate --clients cursor --client-config cursor=/absolute/profile/mcp.json
.venv/bin/python -m src.daemon audit --workspace /absolute/path/to/project
```

Explicit files take precedence over environment overrides and default paths.
With `--workspace`, Codex and Cursor retain their project configuration paths
unless an explicit file is supplied.
Audit includes standard locations, active profiles, and files remembered from
successful migrations; arbitrary inactive profiles need an explicit path. See
[alternate client configurations](docs/shared-mcp-daemon.md#alternate-client-configurations)
for supported targets, environment variables, and workspace behavior.

The configurations below are for explicit standalone stdio use (including platforms
without the macOS service). Stop the shared daemon before a full stdio rollback.
Replace both absolute installation paths below. Set `AGENTS_CLIENT_REPO_ROOT` to
the client project when its launch directory is not reliable.

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
---
# My Agent System Prompt

## Identity
You are an expert in X...
```

Validate the frontmatter with `.venv/bin/python scripts/validate_agents.py`.
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

Skills outside the three lists are never loaded for that agent. Guidance that applies to every agent belongs in `rules/`, not in a skill.

---

## 🧠 Repository Memory

The server stores repository summaries and action history separately for each
client project:

- **`describe_repo`** — generates a compressed, LLM-consumable repo overview via MCP sampling and writes it into the managed *Repository Memory* section of `CLAUDE.md`. Without sampling, or when the sampling call fails, it writes nothing and returns `needs_summary` (the prompt plus `repo_hash`, `repo_path` and `workspace_id`), and the caller persists its own summary with `write_repo_summary`. Idempotent: re-runs are no-ops unless the repo manifest changes or `force_refresh=True`.
- **`log_interaction`** — end-of-turn logger. Appends `intent / action / outcome` entries (with optional files and tags) to `history.md` at the repo root; deduplicated by content hash; rotated to `history/YYYY-MM.md` when the file exceeds 512 KB. Also sends a Langfuse generation trace if keys are configured.
- **`read_history`** — returns recent entries by recency/`since` filter, or runs a lazy semantic search backed by the same `NumpyVectorStore` used for routing.

Over stdio, the project is resolved from `AGENTS_CLIENT_REPO_ROOT`, then the
nearest `.git` or `CLAUDE.md` at or above the start directory, then the start
directory itself. The start directory is `CLAUDE_PROJECT_DIR`, which Claude Code
exports to the servers it starts, or else the launch directory. A filesystem root
or a directory inside the Windows directory is refused with `workspace_required`.
The Claude desktop app, for example, starts the servers in
`claude_desktop_config.json` in `C:\Windows\System32` without a project hint;
register Agents-Core with Claude Code instead, or set `AGENTS_CLIENT_REPO_ROOT` in
the entry's `env`. Over HTTP, memory requires the registered `X-Agents-Workspace` header;
the daemon does not infer a project from its working directory. If a memory tool
returns `workspace_required` or `workspace_invalid`, routing remains available.
Register the project before retrying memory operations; do not retry logging in a loop.

Source history and `CLAUDE.md` stay in the project. The summary hash is stored in
the project's `data/memory/`. Derived router and history indexes live in private
daemon state or in leased `data/stdio/` slots for standalone processes. These
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
its history and Langfuse results are reported separately. Configure the Langfuse
keys in `.env`, or leave them blank for local-only operation. Set
`LANGFUSE_TRACING_ENABLED=false` when running checks that should not send traces.

---

## 🛠️ Development

Read [AGENTS.md](AGENTS.md) for contributor instructions, [the documentation
refresh process](flows/documentation-refresh.md) for documentation work, and
[tests/README.md](tests/README.md) for the test matrix.

Store reusable task instructions for models in `flows/` and list them in
[flows/README.md](flows/README.md). To run one, point the model to its Markdown
file, for example: `Run flows/documentation-refresh.md.`
From another repository, use `run_flow(flow="documentation-refresh")` through
Agents-Core MCP; no copy of the flow file is needed in that repository.
For review through merge, use `Run flows/pr-review.md for <PR or MR URL>.`
The [PR/MR flow](flows/pr-review.md) covers concise English descriptions, bot
review cycles, replies, and a final report in the request's language.

### Validation

Run these commands from the checkout root with its environment installed:

```bash
LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/ -q
.venv/bin/python scripts/validate_agents.py
```

The default pytest configuration excludes `slow` tests. Use
`LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/ -m '' -q` to
include them. Bridge changes also require `node --test bridge/test.mjs`.
The test configuration isolates derived indexes in temporary storage; tests that
load embeddings still need the selected model to be available.

### Running Server Manually

```bash
source .venv/bin/activate
python src/server.py
```

### Debug Logging

Enable detailed per-call JSON logging:

```bash
AGENTS_DEBUG=1 python src/server.py
```

Standalone debug logs are written under the client project's `logs/`; HTTP
debug logs are written under the daemon's private `debug/` directory. Files use
`{YYYY-MM-DD}/{HH-MM-SS.fff}_{uuid}_{tool}_{direction}.json`. Logging is disabled
unless `AGENTS_DEBUG=1` (or `true`).

---

## 📝 License

MIT
