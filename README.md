# 🤖 Agents Framework

**Universal MCP Server for AI Agent Roles, Skills & Cognitive Implants**

A semantic router that dynamically loads specialized agent personas, domain skills, and cognitive reasoning implants based on user queries. Works with any MCP-compatible client (Claude Code, Cursor, Windsurf, and others).

---

## 🚀 Quick Start

### After Cloning

```bash
git clone <repository-url>
cd Agents

# Run initialization script
./scripts/init_repo.sh
```

The script will:
- ✅ Create Python virtual environment (`.venv/`)
- ✅ Install all dependencies
- ✅ Create `.env` configuration file
- ✅ Validate MCP server configuration

### Manual Setup

```bash
# Create and activate virtual environment
python3 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp env.example .env
# Edit .env with your API keys
```

---

## ⚙️ Configuration

### Required Environment Variables

Create `.env` file with:

```env
LANGFUSE_PUBLIC_KEY=pk-lf-... # Optional: observability
LANGFUSE_SECRET_KEY=sk-lf-... # Optional: observability
LANGFUSE_HOST=https://cloud.langfuse.com
ANTHROPIC_API_KEY=sk-ant-...  # Optional: for document OCR
AGENTS_DEBUG=0                # Set to 1 for JSON debug logging in logs/
```

> **Note**: Embeddings are handled locally by `fastembed` (ONNX Runtime). Model is selected during setup — no external API key is required for core routing.

### Background Auto-Update

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

Run a manual rebuild any time with `python -m src.reindex`.

---

## 🎯 How It Works

The server exposes MCP tools that any compatible client can call:

| Tool | Purpose |
|------|---------|
| `route_and_load(query)` | Semantic routing — finds the best agent, enriches its prompt with relevant skills & implants |
| `get_agent_context(agent_name, query)` | Direct agent loading when the target is already known |
| `refresh_persona_context(query, current_persona)` | Protocol 2: refresh skills/implants for the active role |
| `load_implants(query\|task_type)` | Load cognitive reasoning strategies by semantic query or preset bundle |
| `list_agents()` | Enumerate all available agents with metadata |
| `log_interaction(agent_name, query, response_content, intent?, action?, outcome?, files?, tags?)` | End-of-turn logger — appends to `history.md` (deduped by content hash) and, if configured, sends a Langfuse generation trace |
| `clear_session_cache()` | Reset session cache |
| `describe_repo(force_refresh=False)` | One-shot repo bootstrap — writes a structured summary into the managed Repository Memory section of CLAUDE.md |
| `read_history(limit?, since?, query?)` | Recent entries or lazy semantic recall over the action log |

### Persona continuity (protocol 2, opt-in)

Before each request, the model silently checks whether its active role fits the
task. If it does, it keeps that role without routing, candidate selection or
enrichment calls. A needed specialization change triggers routing; a known
requested role loads directly. Lost instructions restore the known role; a
justified refresh adds skills without reselecting it.

Version 2 returns separate persona, rules, skills and implants blocks, an
activation descriptor and an exact footer. Every successful switch, restore or
refresh replaces all four blocks, including changed or removed rules, while
preserving higher-priority instructions, conversation facts, goals, permissions
and tool results. This is logical replacement: MCP cannot physically delete old
messages. No history or cache clearing is required. Calls without `protocol_version=2`
retain the version 1 API and `context_hash` behavior. V2 never uses sampling.

The installer defaults to version 1. The [dialogue evaluation report](docs/persona-switch-eval-results.md)
records results and remaining gaps for each tested client/model; they do not
establish support for other applications or native context compaction. Opt in
explicitly:

```bash
AGENTS_PERSONA_PROTOCOL=2 ./scripts/init_repo.sh
```

On Windows, set `AGENTS_PERSONA_PROTOCOL=2` before running `scripts\init_repo.bat`.
Use the same setting on reruns. The checked-in `CLAUDE.md` also defaults to v1;
the global installer does not change this tracked file. To opt this checkout into
v2, explicitly replace its managed section:

```bash
.venv/bin/python scripts/_helpers/inject_claude_md.py CLAUDE.md scripts/templates/routing-protocol-core.md
```

On Windows, use `.venv\Scripts\python.exe` for the same command. To restore this
checkout to v1, use `scripts/templates/routing-protocol-v1.md` as the source.
Repository notes outside the markers are preserved. Managed instruction sections
are backed up and replaced by markers. Only exact known installer-generated
routing memory is migrated; edited reminders are preserved with a path-specific warning. Windows
does not create an absent memory reminder. Review your own project instructions
and memory for conflicting unconditional `route_and_load` requirements; these
are not automatically rewritten.

To roll back, restore the managed section and reminder from backups, preserving
later user edits, or rerun with `AGENTS_PERSONA_PROTOCOL=1`. Do not clear dialogue
history. A v2 client encountering an old server reports the mismatch once and
uses v1 until the server is updated.

See [routing, compatibility and migration](docs/routing_flow.md) and the
[client protocol](scripts/templates/routing-protocol-core.md) for the complete
contract. Behavioral support must be established for each client/model pair;
passing server tests alone does not establish it.

---

## 🏗️ Architecture

```
Agents/
├── agents/               # Agent personas (system prompts, 38 agents)
│   ├── software_engineer/
│   │   └── system_prompt.mdc
│   ├── common/           # Shared agent resources
│   ├── capabilities/     # Capability compositions (registry.yaml)
│   └── schemas/          # Validation schemas
├── skills/               # Reusable knowledge chunks (RAG)
│   └── skill-*.mdc
├── implants/             # Cognitive reasoning strategies (RAG)
│   └── implant-*.mdc
├── src/
│   ├── server.py         # MCP Server entrypoint (FastMCP)
│   ├── engine/
│   │   ├── router.py     # Semantic routing (cache-first)
│   │   ├── skills.py     # Skill retrieval (vector search)
│   │   ├── implants.py   # Implant retrieval (vector search)
│   │   ├── config.py     # Centralized configuration
│   │   ├── embedder.py   # FastEmbed wrapper (ONNX Runtime)
│   │   ├── vector_store.py # NumPy-based vector store
│   │   ├── enrichment.py # Tier-based context enrichment
│   │   ├── capabilities.py # Capability registry resolution
│   │   ├── context.py    # Context retrieval (history formatting)
│   │   └── language.py   # Language detection
│   └── utils/
│       ├── prompt_loader.py
│       ├── debug_logger.py     # Optional JSON debug logging
│       └── langfuse_compat.py  # Optional Langfuse layer
├── data/                 # Vector store cache (auto-initialized)
├── mcp.json              # MCP server configuration
├── pyproject.toml        # Python project metadata
└── requirements.txt
```

### Key Components

| Component | Description |
|-----------|-------------|
| **Agents** | Specialized personas with unique system prompts |
| **Skills** | Domain-specific knowledge chunks (retrieved via RAG) |
| **Implants** | Cognitive patterns & reasoning strategies |
| **Router** | Semantic matching + caching for fast agent selection |

---

## 🔌 MCP Client Configuration

### Claude Code (`.mcp.json` in project root)

```json
{
  "mcpServers": {
    "Agents-Core": {
      "command": ".venv/bin/python",
      "args": ["src/server.py"]
    }
  }
}
```

### Cursor (`mcp.json` in project root)

```json
{
  "mcpServers": {
    "Agents-Core": {
      "command": ".venv/bin/python",
      "args": ["src/server.py"]
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

The agent will be auto-discovered by the MCP server on next startup.

### Capabilities System

Instead of listing skills per agent, you can declare high-level capabilities:

```yaml
capabilities: [development, dev-security]
```

The enrichment pipeline resolves capabilities to skill bundles via `agents/capabilities/registry.yaml`. Available capabilities: `critical-analysis`, `content-structure`, `development`, `dense-summary`, `trust-weighted-research`, `bio-health`, `tech-documentation`, `dev-security`, `consultative-intake`, `creative-writing`, `psychology`, `3d-printing`, `data-investigation`, `epistemic-analysis`, `code-review`, `decision-making`, `product-thinking`, `temporal-research`, `performance-engineering`, `prompt-design`, `prompt-security`, `roblox-development`, `dev-tools`, `blender-scripting`, `health-optimization`, `consumer-research`, `visualization`, `child-psychology`.

---

## 🧠 Repository Memory

The server ships with a per-repo memory subsystem so each new Claude session does not have to re-explore the codebase from scratch:

- **`describe_repo`** — generates a compressed, LLM-consumable repo overview via MCP sampling and writes it into the managed *Repository Memory* section of `CLAUDE.md`. Idempotent: re-runs are no-ops unless the repo manifest changes or `force_refresh=True`.
- **`log_interaction`** — end-of-turn logger. Appends `intent / action / outcome` entries (with optional files and tags) to `history.md` at the repo root; deduplicated by content hash; rotated to `history/YYYY-MM.md` when the file exceeds 512 KB. Also sends a Langfuse generation trace if keys are configured.
- **`read_history`** — returns recent entries by recency/`since` filter, or runs a lazy semantic search backed by the same `NumpyVectorStore` used for routing.

The full design and step-by-step rationale lives in [`docs/memory-subsystem-spec.md`](docs/memory-subsystem-spec.md).

> ⚠️ **Privacy warning** — `history.md` captures raw prompts and responses. If you paste secrets (API keys, tokens, credentials) into Claude, they will land in this file. It is **gitignored by default** to keep them out of git history; if you want the action log visible in PRs, remove `history.md` / `history/` from `.gitignore` and review entries before pushing.

---

## 📊 Observability

The framework integrates with LangFuse for tracing:

- All tool calls are automatically traced
- Routing decisions are logged
- Cache hits/misses are tracked

Configure LangFuse in `.env` or leave blank for local-only operation.

---

## 🛠️ Development

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

Logs are written to `logs/{YYYY-MM-DD}/{HH-MM-SS.fff}_{tool}_{direction}.json`. Zero overhead when disabled.

---

## 📝 License

MIT
