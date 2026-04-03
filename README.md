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

> **Note**: Embeddings are handled locally by `sentence-transformers` (all-MiniLM-L6-v2). No external API key is required for core routing.

---

## 🎯 How It Works

The server exposes MCP tools that any compatible client can call:

| Tool | Purpose |
|------|---------|
| `route_and_load(query)` | Semantic routing — finds the best agent, enriches its prompt with relevant skills & implants |
| `get_agent_context(agent_name, query)` | Direct agent loading when the target is already known |
| `load_implants(query\|task_type)` | Load cognitive reasoning strategies by semantic query or preset bundle |
| `list_agents()` | Enumerate all available agents with metadata |
| `log_interaction(...)` | Observability logging (Langfuse) |
| `clear_session_cache()` | Reset session cache |

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
├── agents/               # Agent personas (system prompts, 31 agents)
│   ├── ai_debugger/
│   │   └── system_prompt.mdc
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
│   │   ├── skills.py     # Skill retrieval (ChromaDB)
│   │   ├── implants.py   # Implant retrieval (ChromaDB)
│   │   ├── config.py     # Centralized configuration
│   │   ├── chroma.py     # ChromaDB client singleton
│   │   ├── enrichment.py # Tier-based context enrichment
│   │   ├── capabilities.py # Capability registry resolution
│   │   ├── context.py    # Context retrieval (history formatting)
│   │   └── language.py   # Language detection
│   └── utils/
│       ├── prompt_loader.py
│       ├── debug_logger.py     # Optional JSON debug logging
│       └── langfuse_compat.py  # Optional Langfuse layer
├── chroma_db/            # Vector database (auto-initialized)
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

The enrichment pipeline resolves capabilities to skill bundles via `agents/capabilities/registry.yaml`. Available capabilities: `critical-analysis`, `content-structure`, `development`, `dense-summary`, `trust-weighted-research`, `bio-health`, `tech-documentation`, `dev-security`, `consultative-intake`, `creative-writing`, `psychology`, `3d-printing`, `data-investigation`, `epistemic-analysis`.

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
