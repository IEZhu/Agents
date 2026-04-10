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
| **Frontmatter** | `short_name`, `one_liner`, `globs: []` | `compiled` (required), `globs` (optional), Role in description |

**NOT implants** (move to skills):
- Prompt engineering techniques (how to write prompts) → `skill-prompt-techniques`
- Security practices (sandwich defense, delimiters) → `skill-prompt-security`
- Domain-specific protocols (fact verification) → `skill-fact-verification`
- Token usage rules → `skill-token-economy`

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
globs: []              # File patterns for auto-activation (usually empty)
alwaysApply: false     # Whether to always include this implant
short_name: MyTechnique     # CamelCase short name for display
one_liner: brief action phrase  # Lowercase, no period, describes what it does
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
| `chain-of-draft` | Iterative drafting for complex outputs |
| `chain-of-symbol` | Symbolic reasoning for logic problems |
| `chain-of-table` | Tabular reasoning for structured data |
| `chain-of-verification` | Draft-verify-correct cycle to reduce hallucinations |
| `contrastive-cot` | Compare correct vs incorrect reasoning paths |
| `self-harmonized-cot` | Multiple perspectives harmonized into one |
| `reverse-cot` | Work backwards from conclusion to premises |
| `take-a-deep-breath` | Zero-shot CoT trigger |
| `dr-cot` | Dynamic Recursive CoT — recurse deeper only when needed |

### Meta-Cognition

Techniques for self-reflection and improvement:

| Implant | Description |
|---------|-------------|
| `self-consistency` | Generate multiple answers, select consensus |
| `self-discover` | Discover own reasoning patterns |
| `metacognitive-prompting` | Reflect on thinking process |
| `recursion-of-thought` | Recursive problem decomposition |
| `reflexion` | Self-critique Actor-Critic-Reflector cycle for high-stakes tasks |
| `step-back-prompting` | Abstract principle first, then apply to specifics |
| `active-prompting` | Actively select most informative examples |
| `automatic-reasoning` | Alternates reasoning with tool calls (calc, search) |
| `maieutic-prompting` | Socratic method — explanation tree to find logical contradictions |
| `rephrase-and-respond` | Clarifying ambiguous requests before answering |
| `system-2-attention` | Input cleaning — removes bias/flattery before answering |

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
| `buffer-of-thoughts` | Working memory management |
| `narrative-of-thought` | Story-based reasoning |
| `output-automata` | Structuring output as a Finite State Machine (FSM) or script |

### Verification & Safety

Techniques for ensuring correctness and safety:

| Implant | Description |
|---------|-------------|
| `constitutional-critique` | Ethical review against principles |

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
| `complexity-based-prompting` | Order by complexity |
| `contextual-compression` | Compress context to essentials |
| `prompt-chaining` | Breaking task into sequence of LLM calls |

### Efficiency

> **Moved to skills**: `token-economy` → `skill-token-economy`

## Activation Methods

### 1. Automatic (RAG)

Implants are automatically selected based on query relevance:
- Query is embedded
- Vector search returns relevant implants
- Top matches are injected into context

### 2. Explicit via MCP Tool

```python
# Request specific reasoning strategy
load_implants(task_type="debugging")
# Returns: chain-of-code, reflexion

load_implants(task_type="analysis")
# Returns: step-back-prompting, chain-of-verification

load_implants(task_type="creative")
# Returns: analogical-prompting, generated-knowledge

load_implants(task_type="planning")
# Returns: plan-and-solve, skeleton-of-thought
```

### 3. Direct Query

```python
load_implants(query="How do I debug this race condition?")
# Returns implants relevant to debugging
```

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

3. **Restart MCP server**: To re-index implants

## Best Practices

- **Keep implants atomic**: One technique per file
- **Be explicit**: Clear step-by-step instructions
- **Include examples**: When the pattern is complex
- **Use sparingly**: Too many implants increase context size
