# Task-scoped worker rules

These rules belong to `agents-core/subagent/1` worker packages prepared by
`get_subagent_context`. They do not change the protocol 2 conversation rule
loader and are not added to ordinary persona bundles.

The worker builder loads the five mandatory files below by sorted path and requires their names.
It also adds `worker-artifacts` when the request supplies `artifact_manifest`:

| Name | Responsibility |
|---|---|
| `worker-scope` | Assignment boundaries, operation permissions, fixed specialization, and delegation limits |
| `worker-input` | Harmless assumptions versus critical dependencies and `input_required` |
| `worker-completion` | Acceptance criteria, effort limits, partial results, and stop conditions |
| `worker-result` | Exact JSON envelope, traceable evidence, and truthful check status |
| `worker-cooperation` | Parent-mediated intersection handling, authorized sidecar use, valid claims, and unchanged final envelopes |
| `worker-artifacts` (conditional) | Complete pinned `artifact_read` of required inputs, unavailable-input handling, and authorized output manifests |

Each file has YAML frontmatter with a unique `name` matching its
`rule-<name>.mdc` filename and a nonempty `description`, followed by a nonempty
English Markdown body. The five-rule collection is mandatory for every worker preparation; the artifact rule is additionally required when a manifest is supplied.
Missing or invalid required instructions fail preparation instead of yielding an
incomplete package. Keep changes short: every worker receives the mandatory rules.

Worker scope is independent of universal presentation rules. The builder also
retains installation-enabled non-presentation universal rules, including honesty
and uncertainty, while excluding `answer-timestamp` and `language-match`.
It excludes universal `agent-cooperation` with the reason `worker_equivalent`
because the mandatory worker rule supplies its equivalent within the assignment.
Turning conversation rules off does not remove `worker-cooperation`; if mandatory
instructions exceed the character budget, preparation returns an error instead
of dropping or cropping them.
Language can be specified in the assignment; the returned artifact is always
the selected JSON envelope. Do not add applicability fields to universal rules.

Use the supplied result schema as the structural authority. Do not duplicate
its full definition in these files or invent another result format. Keep rule
text consistent with status invariants and evidence references. The parent
retains the authoritative contract, validates the result structurally, and
assesses evidence and acceptance criteria before integration. These prompts
cannot enforce runtime budgets, permissions, or the truth of reported checks.

For the full contract and authoring requirements, see
[Task-scoped subagents](../../docs/subagent-contract.md) and the
[agent profile guide](../../agents/README.md#compact-worker-profiles).
