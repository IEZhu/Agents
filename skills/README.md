# Skills Directory

Contains domain-specific knowledge modules that provide specialized capabilities to agents. Skills are reusable across multiple agents and loaded dynamically based on context.

## Concept

Skills are **knowledge modules** — compact chunks of domain expertise that agents can use. Unlike implants (reasoning patterns), skills provide **specific knowledge** about tools, techniques, and best practices.

## Skill vs Implant — Decision Test

> **Is this domain-specific KNOWLEDGE?** → Skill
> **Is this a domain-agnostic REASONING ALGORITHM?** → Implant

| Criterion | Skill | Implant |
|-----------|-------|---------|
| **Form** | Reference: Role → Rules → Concepts → Actions | Pattern: step 1 → step 2 → step 3 |
| **Scope** | Specific to ONE domain | Applies to ANY domain |
| **Example** | "SOLID, DRY, KISS" (clean code) | "Draft → Verify → Correct" (CoV) |
| **Teaches** | WHAT to know | HOW to think |
| **Frontmatter** | `compiled` (required), `globs` (optional), Role in description | `short_name`, `one_liner`, `globs: []` |

**Skills CAN reference implants** as "use this reasoning technique here" (e.g., `skill-analysis-critical` points to `implant-chain-of-verification`), but must NOT duplicate implant content.

## Structure

```
skills/
├── skill-dev-clean-code.mdc
├── skill-dev-debugging.mdc
├── skill-bio-protocols.mdc
├── skill-purchase-research.mdc
├── ... (other skills)
└── README.md
```

## Naming Convention

```
skill-{domain}-{capability}.mdc
```

Examples:
- `skill-dev-debugging.mdc` — Development domain, debugging capability
- `skill-bio-protocols.mdc` — Biology domain, protocols knowledge
- `skill-psy-cbt.mdc` — Psychology domain, CBT techniques

## Frontmatter Schema

```yaml
---
description: "Brief description with key concepts. Role: Persona."
globs: ["**/*.py", "**/*.ts"]  # File patterns for auto-activation
alwaysApply: false              # Whether to always include
---
## Role
Persona for this skill.

## Rules
- Rule 1
- Rule 2

## Concepts
- **Concept A**: Definition
- **Concept B**: Definition

## Actions
- `action()`: What it does
```

## Skill Categories

### Development

| Skill | Description |
|-------|-------------|
| `skill-dev-clean-code` | SOLID, DRY, KISS, YAGNI principles |
| `skill-dev-debugging` | Scientific debugging, root cause analysis, binary search |
| `skill-dev-security` | Secure coding, OWASP, input validation |
| `skill-mcp-development` | MCP server development best practices |
| `skill-blender-scripting` | Blender Python (bpy) scripting, manifold geometry, 3D printing |
| `skill-roblox-development` | Roblox Luau patterns, DataStore, anti-exploit, performance |

### Analysis & Research

| Skill | Description |
|-------|-------------|
| `skill-analysis-critical` | Critical thinking framework |
| `skill-reasoning-logic` | Logical reasoning, fallacy detection |
| `skill-dense-summarization` | High-density information extraction (80/20) |
| `skill-agnotology` | Study of ignorance/misinformation |
| `skill-temporal-validation` | Time-sensitive fact verification |
| `skill-fact-verification` | Source triangulation, chain-of-verification, anomaly detection |
| `skill-wayback-machine` | Temporal forensic layer via Archive.org Wayback Machine |

### Content & Communication

| Skill | Description |
|-------|-------------|
| `skill-content-structure` | BLUF, Minto Pyramid, MECE frameworks |
| `skill-tech-writing` | Technical documentation best practices |
| `skill-git-conventions` | Conventional Commits, atomic commits |
| `skill-clickup-markdown` | ClickUp-compatible markdown formatting |
| `skill-mermaid-best-practices` | Diagram creation with Mermaid |
| `skill-literary-devices` | Literary devices, tropes, sound symbolism |
| `skill-narrative-craft` | Story building, voice, pacing, emotional arcs |

### Domain-Specific

| Skill | Description |
|-------|-------------|
| `skill-bio-mechanism` | Biological mechanisms and pathways |
| `skill-bio-protocols` | Health optimization protocols |
| `skill-psy-cbt` | Cognitive Behavioral Therapy techniques |
| `skill-psy-nvc` | Nonviolent Communication framework |
| `skill-purchase-research` | Decision matrix methodology |
| `skill-3d-platforms` | 3D printing platforms knowledge |
| `skill-3d-print-search` | 3D model search strategies |
| `skill-fitness-programming` | Exercise programming, periodization, spine-safe biomechanics |

### Prompt Engineering & System

| Skill | Description |
|-------|-------------|
| `skill-prompt-engineering` | Prompt design methodology, evaluation, anti-patterns |
| `skill-prompt-techniques` | Mega-Prompting, Few-Shot, Tone Transfer, Directional Stimulus |
| `skill-prompt-security` | Sandwich Defense, Instructional Hierarchy, Delimiters, Negative Constraints |
| `skill-token-economy` | Minimize token usage, eliminate redundancy |
| `skill-error-recovery` | Universal error handling protocol |

## Loading Methods

### 1. Universal (core_skills.yaml)

Skills declared in `core_skills.yaml` at the repo root are auto-injected for every agent (with tier-policy gating). Use this for truly universal skills (e.g., output formatting). No per-agent listing needed.

### 2. Capability-Composed

Skills bundled inside a capability in `agents/capabilities/registry.yaml`. The agent references the capability name; the engine resolves it to skills + a directive at enrichment time. Prefer capabilities over raw skill lists when the same cluster repeats across multiple agents.

### 3. Per-Agent Preferred

Defined in agent's `preferred_skills` frontmatter for agent-specific skills not covered by core or a capability:

```yaml
preferred_skills:
  - "skill-dev-clean-code"
  - "skill-dev-debugging"
```

MCP server uses vector search to select relevant skills based on:
- Query content
- Agent's preferred skills (priority)
- Skill descriptions

### 4. Glob-Based Auto-Loading

Skills with `globs` patterns activate when matching files are in context:

```yaml
globs: ["**/*.py", "**/*.ts"]
```

### 5. Always-Apply

Skills with `alwaysApply: true` are always loaded:

```yaml
alwaysApply: true
```

## Creating a New Skill

1. **Create file**: `skills/skill-domain-name.mdc`

2. **Add content**:
   ```yaml
   ---
   description: "My Skill: what it provides. Key concepts: A, B, C. Role: Expert."
   globs: ["**/*.ext"]
   alwaysApply: false
   ---
   ## Role
   Expert in Domain: specific expertise.

   ## Rules
   - **Rule 1**: Explanation
   - **Rule 2**: Explanation

   ## Concepts
   - **Concept A**: Definition and usage
   - **Concept B**: Definition and usage

   ## Actions
   - `action_name()`: What this action does
   - `another_action()`: What this does
   ```

3. **Reference in agent** (only if not already covered by a capability):
   ```yaml
   # In agent's system_prompt.mdc
   preferred_skills:
     - "skill-domain-name"
   ```

4. **Restart MCP server**: To re-index skills

## Best Practices

- **Keep skills focused**: One domain/capability per file
- **Write dense descriptions**: Used for RAG matching
- **Use consistent structure**: Role → Rules → Concepts → Actions
- **Include practical actions**: Callable methods/procedures
- **Avoid overlap**: Skills should be complementary, not redundant
