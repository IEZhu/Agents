# 🤖 Agents Framework

**Multi-persona AI Agent System powered by MCP (Model Context Protocol)**

A semantic router that dynamically loads specialized agents, skills, and cognitive implants based on user queries. Built for Cursor IDE with Human-AI symbiosis in mind.

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
```

> **Note**: Embeddings are handled locally by `sentence-transformers` (all-MiniLM-L6-v2). No external API key is required for core routing.

---

## 🎯 Usage in Cursor

### Available Commands

#### System & Routing

| Command | Agent | Purpose |
|---------|-------|---------|
| `/route` | Router | Check available agents |
| `/universal` | Universal Agent | General tasks & orchestration |
| `/new_agent` | Agent Builder | Create new agent personas |
| `/new_mcp` | MCP Builder | Create new MCP servers |
| `/install_repo` | Repo Installer | Deploy framework to another repo |
| `/init_repo` | HAI Architect | Initialize repository structure |

#### Development

| Command | Agent | Purpose |
|---------|-------|---------|
| `/dev` | Software Engineer | Code, debugging, refactoring |
| `/debug_ai` | AI Debugger | Debug AI/ML systems |
| `/security_audit` | Security Expert | Vulnerability analysis |
| `/ai_architect` | AI Senior Engineer | HAI system design |
| `/blender` | Blender Scripter | Python bpy scripts for 3D printing |
| `/roblox` | Roblox Studio Expert | Roblox game development |

#### Research & Analysis

| Command | Agent | Purpose |
|---------|-------|---------|
| `/research` | Deep Researcher | Deep dive research & summarization (80/20) |
| `/investigate` | Investigative Analyst | OSINT & fact-checking |
| `/analyse_data` | Data Analyst | Data analysis & statistics |
| `/forensic` | Data Forensic | Forensic data analysis, timelines |
| `/find_black_hole` | Black Hole Finder | Knowledge gap detection |
| `/purchase` | Purchase Researcher | Product research, decision matrix |

#### Documentation & Content

| Command | Agent | Purpose |
|---------|-------|---------|
| `/docs` | Tech Writer | Documentation writing |
| `/commit_en` / `/commit_ru` | Tech Writer | Git commit messages |
| `/draw` | Diagram Architect | Mermaid diagrams |
| `/semantic_parse` | Semantic Expert | Meeting/transcript analysis |
| `/presentation` | Presentation Coach | Slide structure & design |
| `/literary` | Literary Writer | Artistic prose creation |

#### Domain-Specific

| Command | Agent | Purpose |
|---------|-------|---------|
| `/doctor` | Medical Expert | Diagnosis & treatment protocols |
| `/bio_protocol` | Bio-Hacker | Health optimization |
| `/psy_session` | Psychologist | Psychological sessions |
| `/workout` | Fitness Coach | Spine-safe fitness programming |
| `/briefing` | Daily Briefing | Strategic verified news digest |
| `/site_audit` | Website Analyst | Website business & tech audit |
| `/insta_audit` | Instagram Analyst | Social media analysis |
| `/3dprint` | 3D Print Finder | 3D model search & print optimization |
| `/ocr` | Document OCR Expert | Text extraction from images/PDF |
| `/alerts` | Alerts Describer | Infrastructure alert documentation |

### How Routing Works

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
├── .cursor/
│   ├── agents/           # Agent personas (system prompts, 32 agents)
│   ├── capabilities/     # Capability compositions (registry.yaml)
│   ├── skills/           # Reusable capabilities (RAG)
│   ├── implants/         # Cognitive strategies (RAG)
│   ├── commands/         # Slash commands
│   └── rules/            # Routing rules & agent rules
├── src/
│   ├── server.py         # MCP Server entrypoint (FastMCP)
│   ├── engine/
│   │   ├── router.py     # Semantic routing (cache-first)
│   │   ├── skills.py     # Skill retrieval (ChromaDB)
│   │   ├── implants.py   # Implant retrieval (ChromaDB)
│   │   ├── config.py     # Centralized configuration
│   │   ├── chroma.py     # ChromaDB client singleton
│   │   ├── enrichment.py # Tier-based context enrichment
│   │   └── capabilities.py # Capability registry resolution
│   └── utils/
│       ├── prompt_loader.py
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

## 🔌 MCP Integration

The framework runs as an MCP server, providing tools to Cursor:

| Tool | Purpose |
|------|---------|
| `route_and_load` | Single-hop routing + enriched prompt (primary) |
| `get_agent_context` | Direct agent loading when target is known |
| `load_implants` | Load implants by semantic query or task_type |
| `log_interaction` | Observability logging |
| `clear_session_cache` | Reset session cache |

### MCP Configuration (`mcp.json`)

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

---

## 🧠 Creating New Agents

Use the `/new_agent` command or manually:

1. Create directory: `.cursor/agents/<agent_name>/`
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

3. Create rule: `.cursor/rules/10-my-agent.mdc`
4. Create command: `.cursor/commands/my_command.md`

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

### Testing

```bash
# Test specific agent
/route "How do I fix this bug?"

# Check semantic routing
/route "Tell me about supplements for sleep"
```

---

## 📝 License

MIT

---

> **Built with HAI principles**: Human-AI Symbiosis, Cognitive Ergonomics, Adaptive Interaction
