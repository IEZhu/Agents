# Implants Directory

Contains cognitive reasoning strategies ("implants") that augment agent reasoning capabilities. Based on prompt engineering research and advanced reasoning frameworks.

## Concept

Implants are **cognitive patterns** that enhance how agents think and reason. Unlike skills (domain knowledge), implants provide **meta-cognitive strategies** — ways of approaching problems rather than specific knowledge.

## Implant vs Skill — Decision Test

> **Is this a domain-agnostic REASONING ALGORITHM?** → Implant
> **Is this domain-specific KNOWLEDGE?** → Skill

| Criterion | Implant | Skill |
|-----------|---------|-------|
| **Form** | Pattern: step 1 → step 2 → step 3 | Reference: Role → Rules → Concepts → Actions |
| **Scope** | Applies to ANY domain | Specific to ONE domain |
| **Example** | "Draft → Verify → Correct" (CoV) | "SOLID, DRY, KISS" (clean code) |
| **Teaches** | HOW to think | WHAT to know |
| **Frontmatter** | `short_name`, `one_liner`, `triggers` (required), `globs: []` | `compiled` (required), `keywords` and `globs` (optional), Role in description |

**NOT implants** (move to skills):
- Prompt engineering techniques (how to write prompts) → `skill-prompt-techniques`
- Security practices (sandwich defense, delimiters) → `skill-prompt-security`
- Domain-specific protocols (fact verification) → `skill-fact-verification`
- Output brevity rules → `skill-caveman-tokenomics`

## Structure

```
implants/
├── implant-chain-of-note.mdc
├── implant-self-consistency.mdc
├── implant-skeleton-of-thought.mdc
├── ... (other implants)
└── README.md
```

## Naming Convention

```
implant-{technique-name}.mdc
```

## Frontmatter Schema

```yaml
---
description: "Brief description of the reasoning technique"
globs: []              # Editor metadata; MCP retrieval does not use this field
alwaysApply: false     # Editor metadata; MCP retrieval does not use this field
short_name: MyTechnique     # CamelCase short name for display
one_liner: brief action phrase  # Lowercase, no period, describes what it does
triggers:                      # Required: user-side task phrases (see retrieval settings below)
  - compare conflicting sources
---
## Pattern
1. **Step One**: Description of first step
2. **Step Two**: Description of second step
3. ...

## When to Use
- Scenario where this technique is most effective
- Another appropriate use case

## Limitations
- Known failure mode or constraint
- When NOT to use this technique
```

## Implant Categories

### Chain-of-Thought Variants

Techniques that structure sequential reasoning:

| Implant | Description |
|---------|-------------|
| `chain-of-note` | RAG annotation — annotate relevance before synthesis |
| `chain-of-code` | Pseudocode-driven reasoning for logic puzzles and multi-step math |
| `chain-of-draft` | Compressed reasoning: 3-5 word thesis steps instead of full CoT, to save tokens |
| `chain-of-symbol` | Symbolic reasoning for logic problems |
| `chain-of-table` | Tabular reasoning for structured data |
| `chain-of-verification` | Draft-verify-correct cycle to reduce hallucinations |
| `contrastive-cot` | Compare correct vs incorrect reasoning paths |
| `self-harmonized-cot` | Multiple perspectives harmonized into one |
| `reverse-cot` | Work backwards from conclusion to premises |
| `take-a-deep-breath` | Zero-shot CoT trigger |
| `dr-cot` | Dynamic Recursive CoT — recurse deeper only when needed |
| `cumulative-reasoning` | Build conclusions by accumulating verified evidence one step at a time |

### Meta-Cognition

Techniques for self-reflection and improvement:

| Implant | Description |
|---------|-------------|
| `self-consistency` | Generate multiple answers, select consensus |
| `complexity-based-prompting` | Generate several reasoning chains and keep the most detailed sound one |
| `self-discover` | Discover own reasoning patterns |
| `metacognitive-prompting` | Reflect on thinking process |
| `recursion-of-thought` | Recursive problem decomposition |
| `reflexion` | Self-critique Actor-Critic-Reflector cycle for high-stakes tasks |
| `step-back-prompting` | Abstract principle first, then apply to specifics |
| `active-prompting` | Quick answer with a confidence check; switch to full CoT only when confidence is low |
| `automatic-reasoning` | Alternates reasoning with tool calls (calc, search) |
| `maieutic-prompting` | Socratic method — explanation tree to find logical contradictions |
| `rephrase-and-respond` | Clarifying ambiguous requests before answering |
| `system-2-attention` | Input cleaning — removes bias/flattery before answering |
| `self-refine` | Generate, critique and refine without external feedback |
| `iteration-budget` | Stop after three failed fixes and re-examine the mental model |
| `uncertainty-quantification` | Stakes-based confidence threshold; separate checked, recalled and inferred claims |

### Structured Thinking

Techniques for organizing complex reasoning:

| Implant | Description |
|---------|-------------|
| `skeleton-of-thought` | Outline first, then fill details |
| `graph-of-thoughts` | Non-linear reasoning graphs |
| `layer-of-thoughts` | Hierarchical reasoning layers |
| `logic-of-thought` | Formal logical reasoning: propositions → inference → conclusion |
| `program-of-thoughts` | Write executable code to solve calculations |
| `thread-of-thought` | Maintain coherent reasoning thread |
| `buffer-of-thoughts` | Extract a reusable meta-template for repetitive task types, then instantiate it |
| `narrative-of-thought` | Story-based reasoning |
| `output-automata` | Structuring output as a Finite State Machine (FSM) or script |
| `tree-of-thought` | Explore several reasoning paths with evaluation and backtracking |
| `chain-of-abstraction` | Abstract away details progressively to reveal the core structure |
| `causal-reasoning` | Distinguish correlation from causation with systematic checks |
| `second-order-thinking` | Map cascading consequences beyond the immediate effects |

### Verification & Safety

Techniques for ensuring correctness and safety:

| Implant | Description |
|---------|-------------|
| `constitutional-critique` | Ethical review against principles |
| `verify-assumptions` | Check one to three load-bearing facts before a design or fix recommendation |
| `regression-first` | Localize a regression ("it worked before") before proposing a fix |
| `premortem` | Assume the plan failed, find the causes, then mitigate |
| `steel-man` | Build the strongest opposing argument before criticizing |
| `multi-agent-debate` | Generate distinct perspectives, let them critique each other, synthesize |

> **Moved to skills**: `fact-verification` → `skill-fact-verification`, security patterns (sandwich-defense, instructional-hierarchy, delimiters, negative-constraints) → `skill-prompt-security`

### Generation Strategies

Techniques for producing better outputs:

| Implant | Description |
|---------|-------------|
| `analogical-prompting` | Reasoning by analogy to known cases |
| `generated-knowledge` | Generate context before answering |
| `role-play-expert` | Expert persona for reasoning style (not facts — see Warning) |
| `output-priming` | Provide answer opening to steer completion format |
| `dynamic-few-shot` | Select task-relevant examples from a pool |

> **Moved to skills**: prompt engineering techniques (mega-prompting, few-shot-selection, directional-stimulus, emotion-prompting, simulated-interaction, tone-transfer) → `skill-prompt-techniques`

### Decomposition

Techniques for breaking down complex problems:

| Implant | Description |
|---------|-------------|
| `least-to-most-prompting` | Solve simpler sub-problems first |
| `plan-and-solve-plus` | Atomic planning before execution for multi-step tasks |
| `contextual-compression` | Compress context to essentials |
| `prompt-chaining` | Breaking task into sequence of LLM calls |
| `decomposed-prompting` | Split a complex task into simpler sub-prompts and combine the results |
| `react` | Interleave reasoning with tool actions and observations |

### Efficiency

> **Moved to skills**: output brevity → `skill-caveman-tokenomics`

## Activation Methods

### 1. Agent preferences and semantic retrieval

An agent's `preferred_implants` list loads by canonical ID in declaration order,
then semantic search fills remaining slots. These preferences are direct loads,
not the distance boost used for `preferred_skills`.

```yaml
preferred_implants:
  - implant-chain-of-verification
  - implant-step-back-prompting
```

With the default policy, `standard` starts with 2 slots and `deep` with 3. A longer
preferred list raises the budget, up to `MAX_PREFERRED_IMPLANTS` (5). An inferred
`lite` tier is promoted to `standard` for agents with declared implants; an
explicit `lite` tier loads none. A persona bundle is retained across turns and
always uses the tier-based budget. On the per-query enrichment path used by the
evaluation harnesses, the optional intent classifier can waive promotion for a
greeting and change per-query budgets.

Semantic retrieval embeds the query and role, then selects candidates below
`IMPLANTS_RELEVANCE_THRESHOLD` (default `0.85`). Optional settings in
`src/engine/config.py` change this behavior:

| Setting | Default | Alternative |
|---|---|---|
| `IMPLANT_INDEX_MODE` | `legacy`: description + body | `triggers`: description + triggers + When to Use |
| `IMPLANT_GATING` | `legacy`: absolute distance cutoff | `zscore`: candidates must stand out from the query's distance distribution |
| `IMPLANT_NEED_GATE` | `off` | `intent`: require a positive implant budget; applies only to the per-query evaluation path, not to persona bundles |

With `IMPLANT_GATING=zscore`, a candidate's distance must be at least
`IMPLANT_GATE_Z` (default `1.5`, range 0–5) standard deviations below the query's
mean distance, and `IMPLANT_TRIGGER_BOOST` (default `0.85`, range 0–1) multiplies
an implant's distance when one of its `triggers` occurs in the query as a whole
phrase, ignoring case. Invalid or out-of-range values fall back to the default.

The alternatives affect semantic selection or per-query need, not the meaning of
editor fields `globs` and `alwaysApply`. A protocol 2 bundle is updated through
`refresh_persona_context`; changing the current question alone does not reload it.

### 2. Explicit via MCP Tool

```python
# Request specific reasoning strategy
load_implants(task_type="debugging")
# Returns: chain-of-code, reflexion, react

load_implants(task_type="analysis")
# Returns: step-back-prompting, chain-of-verification

load_implants(task_type="creative")
# Returns: analogical-prompting, generated-knowledge

load_implants(task_type="planning")
# Returns: plan-and-solve-plus, skeleton-of-thought
```

### 3. Direct Query

```python
load_implants(query="How do I debug this race condition?", limit=3)
# Returns implants relevant to debugging
```

`task_type` selects a fixed bundle; `query` selects by semantic relevance, with
`limit=5` by default. Returned implants are supplementary context; this tool does
not replace a protocol 2 persona descriptor or its footer.

## Creating a New Implant

1. **Create file**: `implants/implant-my-technique.mdc`

2. **Add content** (all sections except Example are **required**):
   ```yaml
   ---
   description: "My Technique: brief explanation of when and why to use it"
   globs: []
   alwaysApply: false
   short_name: MyTechnique
   one_liner: brief action phrase describing what it does
   triggers:                 # Required; tests/test_implant_gating.py checks every implant
     - compare conflicting sources
   ---
   ## Pattern
   1. **Step One**: First step of the technique
   2. **Step Two**: Second step
   3. ...

   ## When to Use
   - Scenario 1 where this technique excels
   - Scenario 2...

   ## Limitations
   - Known failure mode or constraint
   - When NOT to use this technique

   ## Example (optional)
   Input: "..."
   Process: ...
   Output: "..."
   ```

3. **Attach when needed**: Add its canonical ID to an agent's `preferred_implants`
   for a direct preference. Otherwise it participates in semantic retrieval.

4. **Rebuild the index and reload the context**: Server startup detects changed
   implant files and rebuilds the index. For a shared daemon, run
   `.venv/bin/python -m src.daemon restart`
   ([service control](../docs/shared-mcp-daemon.md#service-control)); its warmup
   rebuilds changed indexes with the model pinned in the service's `service.json`
   before it reports ready. A manual `python -m src.reindex` does not replace
   that step: it embeds with `.env`'s `EMBEDDING_MODEL`, and the daemon rebuilds
   again when the model fingerprint differs. For standalone stdio, reconnect the
   server, or rebuild manually with the installation's interpreter
   (`python -m src.reindex`) while its service and stdio readers are stopped.
   Refresh retained protocol 2 bundles to deliver the updated text.

## Best Practices

- **Keep implants atomic**: One technique per file
- **Be explicit**: Clear step-by-step instructions
- **Include examples**: When the pattern is complex
- **Use sparingly**: Too many implants increase context size

## Switching implants off

The local web UI (`python -m src.daemon flows-ui`, tab *Implants*) lists every implant and can switch it off for the whole installation without touching the file. A switched-off implant is skipped in persona bundles and by `load_implants`. See [Flow editor](../docs/shared-mcp-daemon.md#flow-editor).
