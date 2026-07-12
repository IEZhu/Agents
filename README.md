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
background, activated on the next start — so the live install is never mutated
mid-session:

1. **Prepare** (background): a daemon thread (non-blocking, so it never delays
   serving) fetches the target branch and, if the install is fast-forwardable,
   builds the new version's vector stores in an isolated git worktree under
   `data/.prepared/<sha>`, then writes a marker. The live tree and stores are
   untouched.
2. **Activate** (next start): if a valid prepared update exists, the server
   fast-forwards the live tree (local, no network) and atomically moves the
   pre-built stores into `data/` — the expensive embedding already happened in
   phase 1, so startup stays fast. For per-session stdio servers that's simply
   the next spawn.

It is **safe by default**:

- acts **only** when the checked-out branch is `AGENTS_AUTO_UPDATE_BRANCH` (default
  `main`) — a **no-op on feature branches**, so local development is never touched;
- only when the working tree is clean, and only **fast-forward** (never merge, rebase,
  or switch branches);
- a failed staged build discards the worktree and leaves the install as-is; crash
  windows self-heal via the store's torn-pair detection and content-hash re-embed;
- any error (offline, lock held by another process, timeout) is logged and the server
  keeps serving the current code. Dependencies are **not** auto-installed.

```env
AGENTS_AUTO_UPDATE=1                     # 0 to disable
AGENTS_AUTO_UPDATE_REMOTE=origin
AGENTS_AUTO_UPDATE_BRANCH=main           # only updates when this branch is checked out
AGENTS_AUTO_UPDATE_TIMEOUT=30            # seconds per git op
AGENTS_AUTO_UPDATE_INTERVAL=900          # throttle network checks (0 = every start)
AGENTS_AUTO_UPDATE_REINDEX_TIMEOUT=600   # seconds allowed for the staged index build
AGENTS_AUTO_UPDATE_STAGING=1             # 0 = legacy in-place update (ff + reindex, rollback on failure)
AGENTS_AUTO_UPDATE_STAGING_DIR=          # staging parent (default data/.prepared; same filesystem as data/)
```

With `AGENTS_AUTO_UPDATE_STAGING=0` the updater falls back to the legacy in-place
path: fast-forward the live tree and rebuild the stores right there, rolling back
to the previous commit if the rebuild fails.

Run a manual rebuild any time with `python -m src.reindex`.

---

## 🎯 How It Works

The server exposes MCP tools that any compatible client can call:

| Tool | Purpose |
|------|---------|
| `route_and_load(query)` | Semantic routing — finds the best agent, enriches its prompt with relevant skills & implants |
| `get_agent_context(agent_name, query)` | Direct agent loading when the target is already known |
| `load_implants(query\|task_type)` | Load cognitive reasoning strategies by semantic query or preset bundle |
| `list_agents()` | Enumerate all available agents with metadata |
| `log_interaction(agent_name, query, response_content, intent?, action?, outcome?, files?, tags?)` | End-of-turn logger — appends to `history.md` (deduped by content hash) and, if configured, sends a Langfuse generation trace |
| `clear_session_cache()` | Reset session cache |
| `describe_repo(force_refresh=False)` | One-shot repo bootstrap — writes a structured summary into the managed Repository Memory section of CLAUDE.md |
| `read_history(limit?, since?, query?)` | Recent entries or lazy semantic recall over the action log |

### Routing Flow

1. **`route_and_load(query)`** → Single-hop routing via semantic cache
2. **Meta Detection** → Greetings/short queries auto-route to `universal_agent`
3. **Cache Hit** → Returns enriched prompt (SUCCESS) or sampled response (SUCCESS_SAMPLED)
4. **Cache Miss** → Returns ROUTE_REQUIRED with agent candidates for client selection
5. **Tier-Based Enrichment** → lite (no extras) / standard (2 skills + 2 implants) / deep (4+ skills + 3 implants)
6. **Multi-Turn** → `context_hash` enables delta optimization on follow-up queries

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
