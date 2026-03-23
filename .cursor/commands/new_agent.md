# /new_agent

Starts the interactive process to create a new agent persona.
See rules: `.cursor/rules/10-agent-builder.mdc`
See rules: `.cursor/rules/00-router.mdc`
See rules: `.cursor/rules/99-environment.mdc`

Profile: **Agent Builder**
Action:
1.  Read `.cursor/agents/common/agent-schema.json`.
2.  Read `.cursor/capabilities/registry.yaml` to know available capabilities.
3.  Interview the user to fill the schema (including `capabilities`).
4.  Generate agent files: system_prompt.mdc (with capabilities in frontmatter), rule, command.
5.  If new skills are created, include `compiled:` field in frontmatter.
