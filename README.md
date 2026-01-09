# 🤖 Agents Framework

**Multi-persona AI Agent System powered by MCP (Model Context Protocol)**

A semantic router that dynamically loads specialized agents, skills, and cognitive implants based on user queries. Built for Cursor IDE with Human-AI symbiosis in mind.

---

## 🚀 Quick Start

### 1. Clone and Initialize

```bash
git clone https://github.com/WonderMr/Agents
cd Agents

# Run initialization script
./scripts/init_repo.sh
```

### 2. Configure API Keys

Edit `.env` file created by the script:

```env
OPENAI_API_KEY=sk-...           # Required: for embeddings
LANGFUSE_PUBLIC_KEY=pk-lf-...   # Optional: observability
LANGFUSE_SECRET_KEY=sk-lf-...   # Optional: observability
```

### 3. Restart Cursor IDE

To load MCP server and agent rules.

### 4. Test

```
/route "How do I fix this bug?"
```

---

## 📦 What init_repo.sh Does

The initialization script performs these steps:

| Step | Description |
|------|-------------|
| **1. Python Check** | Finds suitable Python (3.10-3.12), prefers 3.11 for ML compatibility |
| **2. Environment Config** | Creates `.env` from `env.example`, merges missing keys if exists |
| **3. Virtual Environment** | Creates `.venv/` with selected Python version |
| **4. Dependencies** | Installs all packages from `requirements.txt` |
| **5. MCP Integration** | Merges `mcp.json` into `~/.cursor/mcp.json` (global Cursor config) |
| **6. ChromaDB** | Prepares vector database path (initialized on first run) |
| **7. Validation** | Counts agents, skills, implants, commands |

### Script Flags

```bash
./scripts/init_repo.sh --skip-env      # Skip .env creation
./scripts/init_repo.sh --skip-chroma   # Skip ChromaDB check
./scripts/init_repo.sh --help          # Show help
```

### Testing

Run language detection tests:

```bash
./scripts/run_tests.sh                 # Run all language detection tests
./scripts/run_tests.sh -k russian      # Run only Russian detection test
./scripts/run_tests.sh --verbose       # Verbose output
```

The test script automatically detects and uses:
- `.venv/` or `venv/` if present
- pyenv Python (prefers 3.12.4)
- Verifies `langdetect` and `pytest` are installed

### What Gets Installed

```
pydantic>=2.0          # Data validation
openai>=1.0            # OpenAI API client
chromadb>=0.4          # Vector database for RAG
langfuse>=2.0          # Observability/tracing
sentence-transformers  # Embeddings
python-dotenv          # Environment management
mcp>=0.1.0             # Model Context Protocol
pdf2image, Pillow      # Document OCR support
anthropic>=0.18.0      # Claude API (optional)
```

---

## 🔄 Deploying to Another Repository

After initializing the Agents repo, you can use the agent framework in **any other project**.

### Method 1: Copy .cursor Directory

```bash
# From your target project
cp -r /path/to/Agents/.cursor /path/to/your-project/
```

This copies:
- ✅ All agent personas (`agents/`)
- ✅ Slash commands (`commands/`)
- ✅ Cognitive implants (`implants/`)
- ✅ Domain skills (`skills/`)
- ✅ Routing rules (`rules/`)

### Method 2: Use /install_repo Command

In Cursor IDE (from Agents repo):

```
/install_repo /path/to/target-repo
```

### Important Notes

1. **MCP Server Location**: The MCP server (`Agents-Core`) runs from the original Agents repo. The `.cursor/` folder only contains agent definitions.

2. **Global MCP Config**: The `~/.cursor/mcp.json` file points to the Agents repo. This is configured once during `init_repo.sh`.

3. **Restart Required**: After copying `.cursor/`, restart Cursor to load new rules.

4. **Project-Specific Rules**: You can add project-specific rules in the target repo's `.cursor/rules/` without affecting the Agents repo.

### Architecture After Deployment

```
~/.cursor/
└── mcp.json          # Points to Agents repo MCP server

/path/to/Agents/      # Source repository
├── src/server.py     # MCP server (runs here)
├── .venv/            # Python environment
└── .cursor/          # Agent definitions (source)

/path/to/your-project/  # Target repository
└── .cursor/            # Copied agent definitions
    ├── agents/
    ├── commands/
    ├── skills/
    └── rules/
```

---

## 🎯 Usage in Cursor

### Available Commands

| Command | Agent | Purpose |
|---------|-------|---------|
| `/route` | Router | Check available agents |
| `/universal` | Universal Agent | General tasks |
| `/dev` | Software Engineer | Development tasks |
| `/debug_ai` | AI Debugger | Debug AI systems |
| `/security_audit` | Security Expert | Security analysis |
| `/docs` | Tech Writer | Documentation |
| `/research` | Deep Researcher | Deep dive research |
| `/investigate` | Investigative Analyst | Fact-checking, OSINT |
| `/doctor` | Medical Expert | Clinical analysis |
| `/bio_protocol` | Bio-Hacker | Health protocols |
| `/psy_session` | Psychologist | Psychological support |
| `/draw` | Diagram Architect | Mermaid diagrams |
| `/purchase` | Purchase Researcher | Product research |
| `/briefing` | Daily Briefing | News digest |
| `/3dprint` | 3D Print Finder | 3D model search |
| `/insta_audit` | Instagram Analyst | Social media analysis |
| `/site_audit` | Website Analyst | Website audit |
| `/ocr` | Document OCR Expert | Text from images/PDF |
| `/forensic` | Data Forensic | Leak analysis |
| `/new_agent` | Agent Builder | Create new agents |
| `/new_mcp` | MCP Builder | Create MCP servers |

### How Routing Works

```
User Query → Semantic Router → Cache Check → Agent Selection → Context Enrichment → Response
```

1. **User Query** → Semantic Router analyzes intent
2. **Cache Check** → Fast path if query pattern is cached
3. **Agent Selection** → Best-fit agent loaded dynamically
4. **Context Enrichment** → Skills + Implants injected via RAG
5. **Response** → Agent-specific system prompt applied

---

## 🏗️ Architecture

```
Agents/
├── .cursor/
│   ├── agents/           # Agent personas (system prompts)
│   ├── skills/           # Reusable capabilities (RAG)
│   ├── implants/         # Cognitive strategies (RAG)
│   ├── commands/         # Slash commands
│   └── rules/            # Cursor rules
├── src/
│   ├── server.py         # MCP Server entrypoint
│   ├── engine/
│   │   ├── router.py     # Semantic routing logic
│   │   ├── skills.py     # Skill retrieval (ChromaDB)
│   │   └── implants.py   # Implant retrieval (ChromaDB)
│   └── mcp_servers/      # Additional MCP servers
├── scripts/
│   └── init_repo.sh      # Initialization script
├── chroma_db/            # Vector database (auto-created)
└── requirements.txt
```

### Key Components

| Component | Description |
|-----------|-------------|
| **Agents** | Specialized personas with unique system prompts |
| **Skills** | Domain-specific knowledge (retrieved via RAG) |
| **Implants** | Cognitive patterns & reasoning strategies |
| **Router** | Semantic matching + caching for fast selection |
| **ChromaDB** | Vector store for skills/implants embeddings |

---

## 🔌 MCP Integration

The framework runs as an MCP server, providing tools to Cursor:

| Tool | Purpose |
|------|---------|
| `get_routing_info` | Check cache / get available agents |
| `get_agent_context` | Load agent prompt + update cache |
| `get_context` | Retrieve dynamic skills + implants |
| `get_relevant_implants` | Query cognitive implants |
| `get_reasoning_strategy` | Load task-specific reasoning |
| `log_interaction` | Observability logging to Langfuse |

### MCP Configuration

After `init_repo.sh`, your `~/.cursor/mcp.json` contains:

```json
{
  "mcpServers": {
    "Agents-Core": {
      "command": "/absolute/path/to/Agents/.venv/bin/python",
      "args": ["/absolute/path/to/Agents/src/server.py"]
    }
  }
}
```

---

## 🧠 Creating New Agents

Use `/new_agent` command or manually:

1. Create directory: `.cursor/agents/<agent_name>/`
2. Create `system_prompt.mdc`:

```yaml
---
identity:
  name: "my_agent"
  display_name: "My Agent"
  role: "Expert in X"
  tone: "Professional, Clear"
routing:
  domain_keywords: ["keyword1", "keyword2"]
  trigger_command: "/mycmd"
static_skills:
  - "skill-relevant.mdc"
---
# My Agent System Prompt

## Identity
You are an expert in X...

## Protocol
...
```

3. Create rule: `.cursor/rules/10-my-agent.mdc`
4. Create command: `.cursor/commands/mycmd.md`

---

## 📊 Observability

Integrates with LangFuse for tracing:

- All tool calls automatically traced
- Routing decisions logged
- Cache hits/misses tracked

Configure in `.env` or leave blank for local-only operation.

---

## 🛠️ Manual Setup (Alternative)

If you prefer not to use the script:

```bash
# Create virtual environment
python3.11 -m venv .venv
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt

# Configure environment
cp env.example .env
nano .env  # Add your API keys

# Configure MCP manually in ~/.cursor/mcp.json
```

---

## 📁 Documentation

| Directory | README |
|-----------|--------|
| `.cursor/` | [Overview & Deployment](.cursor/README.md) |
| `.cursor/agents/` | [Agent Personas](.cursor/agents/README.md) |
| `.cursor/commands/` | [Slash Commands](.cursor/commands/README.md) |
| `.cursor/implants/` | [Cognitive Implants](.cursor/implants/README.md) |
| `.cursor/skills/` | [Domain Skills](.cursor/skills/README.md) |

---

## 📝 License

MIT

