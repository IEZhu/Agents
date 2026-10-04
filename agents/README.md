# Agent Catalog

Reference of available agents grouped by category, with their primary triggers
and roles. The MCP tool `list_agents` returns the same agents: each directory
`name` with the frontmatter `display_name`, `role` and `trigger_command`. It
omits the categories and `routing.aliases`, such as the legacy lawyer commands.

## Selecting and keeping a role

The installer writes **persona protocol 2** instructions. Route with
`route_and_load(query, protocol_version=2, current_persona=...)` for initial
selection or a change of specialization. Keep the active bundle locally while its
role fits. An explicitly named role can be loaded directly with
`get_agent_context(agent_name, query, protocol_version=2, current_persona=...)`.
Use `refresh_persona_context` when the same role needs updated skills or implants.

Protocol 1 was removed on 2026-09-29. See [the routing protocol](../docs/routing_flow.md) for response handling,
replacement rules, logging, and the unavailable-MCP fallback.

If no valid MCP bundle is retained and MCP is unavailable, read the selected
`agents/<name>/system_prompt.mdc` manually. Report manual fallback without
inventing MCP descriptors or loaded-component attribution.

## Agent source and metadata

Each agent lives in `agents/<name>/system_prompt.mdc`: YAML frontmatter declares
its identity and retrieval policy; the Markdown body supplies its role guidance.
The [schema](common/agent-schema.json) defines the fields:

| Field | Purpose |
|---|---|
| `identity` | Canonical `name`, `display_name`, competency `role`, and `tone` |
| `routing` | `domain_keywords`, primary `trigger_command`, and optional `aliases` |
| `core_skills` | Mandatory skills, loaded at every tier |
| `preferred_skills` | Semantic skill pool with a distance boost |
| `capable_skills` | Additional allowed skills selected by semantic and keyword match |
| `preferred_implants` | Optional ordered implant IDs loaded before semantic candidates, within the implant budget |
| `interaction_examples` | Optional examples reserved for future use; enrichment does not consume them |

Use canonical component IDs such as `skill-tech-writing` and
`implant-chain-of-verification`. Skills outside the three skill lists are excluded,
except in a flow persona's exact list.
See [skills](../skills/README.md) and [implants](../implants/README.md) for loading
budgets and authoring conventions. Universal directives belong in
[`rules/rule-*.mdc`](../rules/README.md);
per-agent guidance belongs in the agent or its skills. The former global
`core_skills.yaml` and `agents/capabilities/registry.yaml` mechanisms have been
removed.

Protocol 2 delivers separate persona, rules, skills, and implants blocks. A
successful switch, restore, or refresh replaces all four blocks, including empty
ones; it does not accumulate the previous role's instructions.

With the shared daemon, the local web UI (`.venv/bin/python -m src.daemon flows-ui`,
tab *Agents*) lists every agent
with its identity, routing fields, skills by tier, preferred implants and prompt
body. The tab is read-only: agents cannot be switched off there, and their files
are never changed. See [Flow editor](../docs/shared-mcp-daemon.md#flow-editor).

## Research & Analytics

| Agent | Trigger | Role |
|---|---|---|
| `deep_researcher` | `/research` | Deep Research & Literature Synthesis Specialist (topic deep-dives, source-weighted analysis) |
| `investigative_analyst` | `/investigate` | OSINT & Fact-Checking Expert |
| `data_analyst` | `/analyse_data` | Data Analysis & Pattern Recognition Expert |
| `data_forensic` | `/forensic` | Forensic Data Processor & Timeline Architect |
| `black_hole_finder` | `/find_black_hole` | Agnotological Detective / Epistemic Gap Analyst |
| `website_analyst` | `/site_audit` | Web Project Analyst: business models, traffic, monetization, stakeholders |
| `instagram_analyst` | `/instagram` | Social Media Profile Auditor |

## Development & Engineering

| Agent | Trigger | Role |
|---|---|---|
| `software_engineer` | `/dev` | Senior Full Stack Engineer — Code Implementation, Debugging & Refactoring |
| `code_reviewer` | `/review` | Code Review & PR Analyst (Security-Aware, Performance-Conscious) |
| `system_architect` | `/architect` | Distributed Systems & Architecture Designer — Scalability, Trade-offs, C4 Modeling |
| `mcp_builder` | `/new_mcp` | MCP Server Architect & Generator |
| `agent_builder` | `/new_agent` | Agent Prompt & Persona Designer for the Agents Framework |
| `security_expert` | `/security_audit` | Application & Infrastructure Security Analyst — Vulnerability Assessment, Threat Modeling, Zero-Trust |
| `prompt_engineer` | `/prompt` | Prompt Design & Optimization Specialist |
| `roblox_studio_expert` | `/roblox` | Full-Cycle Roblox Game Development: Scripts, Level Design, Optimization, Monetization |
| `blender_scripter` | `/blender` | Blender Python (bpy) Scripting Specialist — Procedural 3D-Printable Model Generation |

## Infrastructure & Operations

| Agent | Trigger | Role |
|---|---|---|
| `sysadmin` | `/sysadmin` | Linux Systems Administrator & Infrastructure Engineer (filesystems, networking, Docker, shell scripting) |
| `devops_engineer` | `/devops` | DevOps & Cloud Infrastructure Engineer (CI/CD, Kubernetes, Terraform, AWS/GCP) |
| `database_admin` | `/dba` | Database Administration & SQL Optimization Specialist (PostgreSQL, MySQL, Redis, MongoDB) |
| `alerts_describer` | `/alerts_describer` | Alert Runbook & Incident Card Documentation Specialist (Prometheus, Grafana) |

## Documentation & Content

| Agent | Trigger | Role |
|---|---|---|
| `tech_writer` | `/docs` | Technical Writer & Documentation Expert |
| `literary_writer` | `/literary` | Master of artistic prose — creator of elegant prose with lyrical nuances and deep meanings |
| `semantic_expert` | `/semantic_parse` | Meeting Transcript Analyst — Semantic Reconstruction, Decision Extraction, Action Items |
| `presentation_coach` | `/present` | Presentation Structure and Psychology Expert |
| `diagram_architect` | `/diagram` | Mermaid.js Visualization Specialist |

## Health & Psychology

| Agent | Trigger | Role |
|---|---|---|
| `medical_expert` | `/medical` | Clinical Reasoning & Medical Analysis Specialist — Differential Diagnosis, Drug Interactions, Lab Interpretation |
| `psychologist` | `/psy_session` | Cognitive-Behavioral Psychologist & Mental Health Consultant |
| `child_psychologist` | `/child_psy` | Child & Adolescent Psychologist specializing in the digital generation |
| `bio_hacker` | `/bio_protocol` | Biohacking & Supplement Protocol Designer — Sleep, Focus, Energy Optimization |
| `fitness_coach` | `/workout` | Scientific fitness coach focused on spine rehabilitation |

## Education & Science

| Agent | Trigger | Role |
|---|---|---|
| `education_tutor` | `/tutor` | Pedagogy Expert — Socratic Teaching, Scaffolded Learning & Knowledge Building |
| `math_scientist` | `/math` | Mathematical & Scientific Reasoning Specialist — Proofs, Computation, Modeling |

## AI & Strategy

| Agent | Trigger | Role |
|---|---|---|
| `ai_senior_engineer` | `/ai_architect` | Human-AI Interaction Designer & Agentic Systems Architect |
| `universal_agent` | `/universal` | Chief of Staff / Strategic Partner |
| `daily_briefing` | `/briefing` | Strategic analyst of daily verified news |
| `product_manager` | `/pm` | Product Thinking & Requirements Analyst |
| `debate_moderator` | `/debate` | Devil's Advocate & Decision Facilitator |

## Design

| Agent | Trigger | Role |
|---|---|---|
| `ux_designer` | `/ux` | UX/UI & Accessibility Expert — Usability, Design Systems, Information Architecture |

## Legal

| Agent | Trigger | Role |
|---|---|---|
| `lawyer` | `/lawyer` | Multi-Jurisdictional Legal Expert (Colombia, Cyprus, Georgia, Kazakhstan, Mexico, Russia, Serbia, Spain, United States) |

The single `lawyer` agent replaced nine country-specific clones (`colombian_lawyer`, `cypriot_lawyer`, `georgian_lawyer`, `kazakh_lawyer`, `mexican_lawyer`, `russian_lawyer`, `serbian_lawyer`, `spanish_lawyer`, `us_lawyer`). Per-jurisdiction knowledge now lives in `skill-jurisdiction-{co|cy|ge|kz|mx|ru|rs|es|us}` and is loaded into the `lawyer` agent's `capable_skills` pool — activated by country-specific keywords. Old trigger commands (`/co_lawyer`, `/cy_lawyer`, …, `/us_lawyer`) are preserved as routing aliases to `/lawyer`.

## Utilities

| Agent | Trigger | Role |
|---|---|---|
| `document_ocr_expert` | `/ocr` | Specialist in extracting text from PDF and images, including handwritten text |
| `purchase_researcher` | `/purchase` | Premium Product Advisor & Comparison Specialist |
| `3d_print_finder` | `/3dprint` | 3D Model Search Specialist & Print Optimization Advisor |
| `install_to_repo` | `/install_agents` | Agent Framework Installer & Repository Scaffolder |

---

**Total agents**: 43
**Updated**: 2026-09-30
