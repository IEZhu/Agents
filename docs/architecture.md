# Architecture

Source of truth for behavior: `src/server.py`, `src/engine/`, `src/startup.py`,
`src/runtime_paths.py`.

This file shows the repository layout and names the components Agents-Core is
built from.

```
Agents/
├── .github/workflows/    # CI checks, vulnerability and release gates, issue-agent bridge
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
│   ├── personal_runtime.py # Personal startup hooks for the daemon and standalone stdio
│   ├── stdio_runtime.py  # Stdio run loop with supplied startup hook and log drain
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
│   ├── client_instructions.py # What a client's instruction files hold: the initialize pointer and drift warnings
│   ├── file_lock.py      # Stable sidecar file locks
│   ├── daemon/           # HTTP app, service control, workspaces, client migration, web UI and its sign-in
│   ├── mcp_servers/      # Optional document OCR server and the MCP server template
│   ├── engine/
│   │   ├── router.py     # Semantic routing (cache-first)
│   │   ├── persona.py    # Protocol 2 activation, restore, and refresh
│   │   ├── persona_bundle.py # Fresh instruction blocks and bundle revision
│   │   ├── protocol_text.py # Every protocol surface, rendered from the section files in protocol/
│   │   ├── readiness.py  # Background retrieval startup (warming_up)
│   │   ├── skills.py     # Skill retrieval (vector search)
│   │   ├── implants.py   # Implant retrieval (vector search)
│   │   ├── rules.py      # Shared rule loading
│   │   ├── config.py     # Centralized configuration
│   │   ├── embedder.py   # Dense ONNX embedding wrapper
│   │   ├── onnx_embedding.py # Dense ONNX adapter with the retained FastEmbed catalog
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

## Key Components

| Component | Description |
|-----------|-------------|
| **Agents** | Specialized personas with unique system prompts |
| **Skills** | Domain-specific knowledge chunks (retrieved via RAG) |
| **Implants** | Cognitive patterns & reasoning strategies |
| **Rules** | [Shared directives](../rules/README.md) included in every loaded bundle when enabled, unless a flow persona names an exact set |
| **Router** | Semantic matching + caching for fast agent selection |
| **Persona bundle** | Versioned role instructions, rules, skills, and implants retained by the client |
| **Flows** | [Markdown workflows](../flows/README.md) served to the caller's repository through `run_flow`, optionally with their own persona |
| **Memory** | Per-project summary and action history |
