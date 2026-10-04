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
| **Frontmatter** | `compiled` (required), `keywords` and `globs` (optional), Role in description | `short_name`, `one_liner`, `triggers` (required), `globs: []` |

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
compiled: "Dense one-liner used only when the skill is rendered at standard tier (token-saving)."
keywords:                       # Phrases that boost preferred/capable skills
  - keyword phrase one          # in `SkillRetriever.retrieve()`.
  - keyword phrase two          # The retrieval embedding uses
                                # `description + keywords + body`, not `compiled`,
                                # truncated at 2048 tokens (or the model's lower limit).
                                # Matching: case-insensitive, non-word
                                # look-around (no `\w` immediately before /
                                # after the literal). Phrases containing
                                # punctuation (e.g. ``(CoVe)``) are fine.
as_of: "YYYY-MM-DD"             # Freshness fields for volatile facts;
review_after_days: 180          # required for skill-jurisdiction-*
sources:                        # (see below)
  - https://example.org/source
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

A skill that states volatile facts, such as tax rates, thresholds or versions,
declares `as_of` (the ISO date of the last check), `review_after_days` and a
`sources` list of URLs; every `skill-jurisdiction-*` skill must declare all three.
[`tests/test_skill_freshness.py`](../tests/test_skill_freshness.py) fails once
`as_of + review_after_days` has passed (180 days when `review_after_days` is
omitted). Re-check the figures against `sources`, then update `as_of`.

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
| `skill-code-generation` | Code generation with tests, documentation, error handling and validation |
| `skill-dev-api-design` | REST, GraphQL and gRPC API design: schemas, versioning, errors, pagination |
| `skill-dev-performance` | Profiling, bottleneck identification, optimization patterns, benchmarking |
| `skill-dev-testing` | Unit, integration, E2E and property-based testing; TDD/BDD workflow |
| `skill-system-design` | Distributed systems, scalability patterns, trade-offs, C4 model, capacity planning |
| `skill-ux-principles` | Gestalt laws, Nielsen heuristics, WCAG accessibility, design tokens, information architecture |

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
| `skill-confidence-markers` | Calibrated confidence markers (HIGH/MEDIUM/LOW + domain variants) on findings/recommendations/solutions — deliberate, not per-sentence |
| `skill-epistemic-method` | Epistemic gap analysis: semantic voids, time-slicing, cui bono |
| `skill-forensic-process` | Forensic data processing: timeline reconstruction, deduplication, fact extraction, triangulation |
| `skill-source-trust-tiers` | Source credibility tiers and triangulation |
| `skill-web-search` | When to search, tool-selection ladder, query formulation and search operators |
| `skill-mathematical-reasoning` | Step-by-step proofs, dimensional analysis, estimation, formal notation, verification |
| `skill-decision-frameworks` | Inversion, Eisenhower Matrix, reversibility test, devil's advocate |
| `skill-product-frameworks` | RICE, MoSCoW, user stories, PRD, OKR, Jobs-to-be-Done |

> **Note**: `skill-confidence-markers` holds the confidence-LABELING mechanics that used to live inside `rule-honest-uncertainty`. The rule keeps the universal honesty principle (calibrate to evidence; never imply you verified what you only recalled), while the per-context labeling discipline is **opt-in via `core_skills`** on agents that emit explicit markers (e.g. `security_expert`, `software_engineer`, `debate_moderator`) — rules stay universal, per-agent behavior lives in skills.

### Content & Communication

| Skill | Description |
|-------|-------------|
| `skill-tech-writing` | Technical documentation best practices |
| `skill-git-conventions` | Conventional Commits, atomic commits |
| `skill-clickup-markdown` | ClickUp-compatible markdown formatting |
| `skill-mermaid-best-practices` | Diagram creation with Mermaid |
| `skill-literary-devices` | Literary devices, tropes, sound symbolism |
| `skill-narrative-craft` | Story building, voice, pacing, emotional arcs |
| `skill-content-structure` | Form-matching: reads the register first; plain prose for conversational, story, spoken-script, manuscript and age-targeted answers (verse form for poems and songs), with no headers, bold lead-ins or bullet lists; BLUF/headers/MECE for analytical, reference, technical, troubleshooting and how-to answers |
| `skill-creative-craft` | Creative writing craft: show don't tell, point of view, subtext |
| `skill-report-formats` | Reusable analytical-report models: executive summary, key findings, risk assessment, decision matrix, recommendations |
| `skill-structured-output` | JSON Schema, XML tags, validated formats, parsing pipelines |
| `skill-caveman-tokenomics` | Terse, answer-first output without filler |
| `skill-pedagogy` | Socratic method, scaffolding, zone of proximal development, active recall, spaced repetition |
| `skill-consultative-intake` | Phased handling of ambiguous requests: clarify, confirm, then execute |

> **Note**: `skill-content-structure` was briefly promoted to an always-on `rule-content-structure`, then demoted back to a skill — its behavior is per-context (analytical vs creative vs manuscript), not a flat universal directive, so it does not belong in the rules layer. It is now **opt-in via `core_skills`** on analytical/technical/reference agents and is excluded from pure-prose/therapeutic agents (e.g. `literary_writer`, `psychologist`).

### Legal Jurisdictions

| Skill | Description |
|-------|-------------|
| `skill-legal-citation` | Verbatim statute citation, jurisdiction discipline, conflict-of-laws (lex specialis/posterior) |
| `skill-jurisdiction-co` | Colombia — Constitución 1991, SAS, DIAN, RUT, acción de tutela, ICA, Estatuto Tributario |
| `skill-jurisdiction-cy` | Cyprus — Cap. 113, corporate tax 15% (from 2026), IP Box ~3%, non-dom 17yr, DTT network 65+, EU acquis |
| `skill-jurisdiction-ge` | Georgia — Virtual Zone IT, micro-business 0%, Estonian-model profit tax, NAPR |
| `skill-jurisdiction-kz` | Kazakhstan — AIFC common law zone, Astana Hub, ТОО, НК РК |
| `skill-jurisdiction-mx` | Mexico — amparo, CFDI, PTU 10%, fideicomiso, SAT, RFC, federal vs estado |
| `skill-jurisdiction-ru` | Russia — ГК/НК/ТК/УК/КоАП, Plenums of Supreme Court, regional subjects |
| `skill-jurisdiction-rs` | Serbia — ЗОО, д.о.о./а.д., paušalno, EU acquis harmonization |
| `skill-jurisdiction-es` | Spain — IRPF/IS/IVA, NIE, autónomo, Ley Beckham, 17 Comunidades Autónomas |
| `skill-jurisdiction-us` | United States — federal + 50 states, IRC, USCIS, LLC/Corp, H-1B/EB visas, circuit splits |

All nine jurisdiction skills sit in the `lawyer` agent's `capable_skills` pool — loaded by keyword/semantic match when the user's query identifies a specific country.

### Domain-Specific

| Skill | Description |
|-------|-------------|
| `skill-bio-mechanism` | Biological mechanisms and pathways |
| `skill-bio-protocols` | Health optimization protocols |
| `skill-bio-protocol-design` | Mechanism-first protocol design: dosage, timing, duration, safety stops |
| `skill-psy-cbt` | Cognitive Behavioral Therapy techniques |
| `skill-psy-nvc` | Nonviolent Communication framework |
| `skill-psy-child-dev` | Child developmental psychology: age stages, play and narrative therapy, attachment theory |
| `skill-psy-digital-wellbeing` | Screen time, social media, cyberbullying and gaming for children and adolescents |
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
| `skill-error-recovery` | Universal error handling protocol |
| `skill-prompt-design-process` | Prompt design process: role, task and constraints, few-shot examples, evaluation loop |
| `skill-agent-handoff` | When and how to transfer tasks between specialized agents |
| `skill-agentic-loops` | Checkpoints, convergence criteria, budgets and error recovery for long-running agent loops |
| `skill-multi-step-planning` | Decompose goals into steps with dependencies, checkpoints and exit criteria |
| `skill-react-pattern` | Thought-Action-Observation loops for tool-using agents |

## Loading Methods (3-Tier Per-Agent Model)

Every agent declares three skill lists in its frontmatter. Skills not present
in any of the three are unavailable to that agent (explicit exclusion). A flow
persona's exact `skills` list replaces the three lists for that flow
([flow personas](../flows/README.md#choose-a-flows-agent-and-components)).

### 1. `core_skills` — Mandatory

Always loaded for the agent, regardless of tier. Use sparingly (0–3 items) —
only skills the agent cannot function without.

```yaml
core_skills:
  - skill-legal-citation
```

### 2. `preferred_skills` — Boosted Semantic Pool

Skills that participate in semantic retrieval with a `distance × 0.7` boost.
Loaded when the query semantically matches them. Typical size 3–8.

```yaml
preferred_skills:
  - skill-dev-clean-code
  - skill-dev-debugging
  - skill-dev-testing
```

### 3. `capable_skills` — Base Semantic Pool

Skills available with base distance, promoted by keyword match (`distance ×
0.85` when any `keywords:` entry literally appears in the query). Use for the
broader pool that may apply to sub-queries. Typical size 0–15. The same keyword
boost also applies to preferred skills, in addition to their preferred boost.

```yaml
capable_skills:
  - skill-prompt-security
  - skill-tech-writing
```

### Tier budgets and protocol lifetime

With the default tier policy, core skills are followed by up to 0 semantic skills
at `lite`, 2 at `standard`, and 4 at `deep`. The relevance cutoff is applied after
distance boosts (`SKILLS_RELEVANCE_THRESHOLD`, default `0.75`). `standard` renders
`compiled` text; `lite` and `deep` render the full skill body. Declared
`preferred_implants` can promote an inferred `lite` tier to `standard`.

Skills are selected when a persona bundle is activated or refreshed and are
retained across turns. The per-query enrichment path used by the evaluation
harnesses honours the optional `INTENT_CLASSIFIER_ENABLED=1` budgets (disabled by
default). The bundle uses the tier budgets above, even when the intent classifier
is enabled. Request
`refresh_persona_context` when a continuing task needs a different skill selection.

Universal rules are a separate layer: every bundle carries all enabled rules (or a
flow persona's exact list), without semantic retrieval. See the [rules reference](../rules/README.md).

## Creating a New Skill

1. **Create file**: `skills/skill-domain-name.mdc`

2. **Add content**:
   ```yaml
   ---
   description: "My Skill: what it provides. Key concepts: A, B, C. Role: Expert."
   compiled: "Dense one-liner rendered at standard tier (token-saving)."
   keywords:
     - canonical phrase one
     - canonical phrase two
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

   Add the [freshness fields](#frontmatter-schema) when the skill states rates,
   thresholds or other facts that change over time.

3. **Attach to one or more agents** by listing it in the agent's
   `core_skills`, `preferred_skills`, or `capable_skills`:
   ```yaml
   preferred_skills:
     - skill-domain-name
   ```

4. **Rebuild the index and reload the context**: Server startup detects changed
   skill files and rebuilds the index. For a shared daemon, run
   `.venv/bin/python -m src.daemon restart`
   ([service control](../docs/shared-mcp-daemon.md#service-control)); its warmup
   rebuilds changed indexes with the model pinned in the service's `service.json`
   before it reports ready. A manual `python -m src.reindex` does not replace
   that step: it embeds with `.env`'s `EMBEDDING_MODEL`, and the daemon rebuilds
   again when the model fingerprint differs. For standalone stdio, reconnect the
   server, or rebuild manually with the installation's interpreter
   (`python -m src.reindex`) while its service and stdio readers are stopped.
   A retained protocol 2 bundle needs `refresh_persona_context` to use the changes.

   In an installation checkout that receives updates (such as `~/.agents-core`
   on `main`), a new untracked skill file does not block updates. Changing
   tracked files, including an agent's skill lists in step 3, or committing
   locally makes the standalone updater, the daemon's `update` and `auto-update`,
   and `install.sh` skip or refuse updates until the change is removed or
   upstreamed; see [Updates](../README.md#updates).

## Best Practices

- **Keep skills focused**: One domain/capability per file
- **Write dense descriptions**: Used for RAG matching
- **Use consistent structure**: Role → Rules → Concepts → Actions
- **Include practical actions**: Callable methods/procedures
- **Avoid overlap**: Skills should be complementary, not redundant

## Switching skills off

With the shared daemon, the local web UI (`.venv/bin/python -m src.daemon flows-ui`, tab *Skills*) lists every skill and can switch it off for the whole installation without touching the file. A switched-off skill is skipped in persona bundles, including when an agent lists it in `core_skills`; a flow persona that names it still loads it. See [Flow editor](../docs/shared-mcp-daemon.md#flow-editor).
