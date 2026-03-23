# Cursor Agents Framework

A modular AI agent architecture for Cursor IDE that enables dynamic routing to specialized agent personas with contextual skills and cognitive reasoning patterns.

## Overview

This `.cursor/` directory contains the complete agent framework that can be deployed to any repository where you want to use AI-assisted development with specialized personas.

```
.cursor/
├── agents/        # Agent personas with system prompts (32 agents)
├── capabilities/  # Capability compositions (registry.yaml)
├── commands/      # Slash commands for agent routing
├── implants/      # Cognitive reasoning strategies
├── skills/        # Domain-specific knowledge modules
├── rules/         # Global routing and environment rules
└── state/         # Runtime state (MCP status, etc.)
```

See individual README files in each directory for detailed documentation.

---

## 🚀 Deployment to Another Repository

After setting up the Agents repository, you can deploy this framework to any other project.

### Prerequisites

1. **Initialize the Agents repo first**:
   ```bash
   cd /path/to/Agents
   ./scripts/init_repo.sh
   ```

2. **Ensure MCP server is configured** in `~/.cursor/mcp.json`

### Method 1: Manual Copy

Copy the `.cursor/` folder to your target repository:

```bash
# From Agents repo
cp -r .cursor /path/to/your-project/

# The folder includes:
# - All agent personas
# - Slash commands
# - Skills and implants
# - Routing rules
```

### Method 2: Using /install_repo Command

In Cursor, use the built-in installation command:

```
/install_repo /path/to/target-repo
```

This will:
1. Copy `.cursor/` directory
2. Preserve any existing project-specific rules
3. Configure environment references

### Post-Deployment

After copying, restart Cursor IDE to:
- Load new agent rules
- Activate slash commands
- Enable routing

**Note**: The MCP server (`Agents-Core`) runs from the original Agents repository. The copied `.cursor/` folder contains only the agent definitions, not the server code.

---

## Architecture

### Request Flow (MCP Enabled)

```
User Query
    │
    ▼
┌──────────────────────────┐
│   route_and_load(query)  │  → Single-hop routing
│   ┌────────────────────┐ │
│   │ Semantic Cache      │ │  → Cosine distance < 0.05?
│   │ Meta-Query Detect   │ │  → Greeting/short → universal_agent
│   │ Session TTLCache    │ │  → context_hash delta mode
│   └────────────────────┘ │
└──────────────────────────┘
    │
    ├── SUCCESS_SAMPLED → Agent response ready
    ├── SUCCESS → Enriched system prompt returned
    └── ROUTE_REQUIRED → Candidates list for client
    │
    ▼
┌──────────────────────────┐
│   Tier-Based Enrichment  │
│   lite:     no extras    │
│   standard: 2 skills +   │
│             2 implants   │
│   deep:     4+ skills +  │
│             3 implants   │
└──────────────────────────┘
    │
    ▼
┌──────────────────────────┐
│   log_interaction()      │  → Trace to Langfuse (optional)
└──────────────────────────┘
```

### Request Flow (Static Mode / Fallback)

When MCP is unavailable:

```
User Query
    │
    ▼
┌─────────────────────┐
│  Read 00-router.mdc │  → Manual routing table
└─────────────────────┘
    │
    ▼
┌─────────────────────┐
│  Load Agent Files   │  → Read system_prompt.mdc + static_skills
└─────────────────────┘
    │
    ▼
┌─────────────────────┐
│   Agent Execution   │
└─────────────────────┘
```

---

## Directory Documentation

| Directory | README | Description |
|-----------|--------|-------------|
| `agents/` | [README](agents/README.md) | Agent personas and system prompts |
| `capabilities/` | — | Capability compositions (registry.yaml) |
| `commands/` | [README](commands/README.md) | Slash commands for Cursor |
| `implants/` | [README](implants/README.md) | Cognitive reasoning strategies |
| `skills/` | [README](skills/README.md) | Domain-specific knowledge |

---

## Creating New Components

### New Agent
```bash
# Use command
/new_agent

# Or manually:
mkdir -p .cursor/agents/my_agent
# Create system_prompt.mdc with frontmatter
# Create .cursor/rules/10-my-agent.mdc
# Create .cursor/commands/my_cmd.md
```

### New Skill
```bash
# Create .cursor/skills/skill-domain-name.mdc
# Add frontmatter: description, globs
# Reference in agent's preferred_skills
```

### New Implant
```bash
# Create .cursor/implants/implant-technique.mdc
# Add frontmatter: description
# Auto-selected via RAG
```

---

## License

MIT
