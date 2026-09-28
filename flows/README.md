# Model workflows

`flows/` contains reusable Markdown instructions that a model follows to complete
a defined task. Each flow describes its inputs, execution steps, checks, and
expected result. Start with [AGENTS.md](../AGENTS.md) for repository instructions
and the [documentation map](../docs/README.md) for supporting references.

## Available flows

| Flow | Purpose | Result |
|---|---|---|
| [Documentation refresh](documentation-refresh.md) | Check and update documentation for people and AI against the implementation | Reviewed documentation changes on a separate branch, with validation results |

## Run a flow

Point the model to the file in the repository and ask it to execute the workflow:

```text
Run flows/documentation-refresh.md.
```

Add optional scope, a starting revision, or a branch when needed:

```text
Run flows/documentation-refresh.md for routing documentation and AI instructions.
Use main as the base and codex/docs-routing-refresh as the branch.
```

Use an absolute file path and identify the target repository when the file is in
another checkout. The model reads and executes the flow in the current session,
using the defaults in that file for omitted optional inputs.

A Markdown flow requires no automatic registration. Adding a file here does not
create an MCP tool, a slash command, or a scheduled task. The active conversation
and repository instructions still apply when executing it.

## Author a flow

Keep one workflow per `.md` file. Use a descriptive lowercase name with hyphens,
such as `documentation-refresh.md`, and add it to the catalog above.

Each flow must specify:

1. **Goal and result:** the task it completes and the output the user receives.
2. **Inputs and defaults:** required context, optional scope, and default choices.
3. **Steps:** ordered actions with links to authoritative sources and instructions.
4. **Validation:** appropriate checks and how to report unavailable or failed checks.
5. **Done criteria:** observable conditions for completion and the final report.

Write flows in English under the
[documentation language policy](documentation-refresh.md#documentation-language).
Link to shared protocols and references instead of maintaining duplicate copies.
Before adding or updating a flow, review its examples, relative links, and
completion criteria so another model can execute it from the file path alone.
