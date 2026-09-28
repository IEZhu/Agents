# Documentation map

Start with the [project README](../README.md) for installation and usage. For work
on this repository, read [AGENTS.md](../AGENTS.md) and [CLAUDE.md](../CLAUDE.md).

All repository documentation must be in English. Keep other languages only in
necessary passages, such as quotations and language-specific examples, with
English explanations around them. The
[documentation language policy](../flows/documentation-refresh.md#documentation-language)
also applies to AI instructions, plans, and reports.

## Maintained references

| Need | Document | Source of truth for behavior |
|---|---|---|
| Install and configure Agents-Core | [Project README](../README.md) | `scripts/init_repo.sh`, `scripts/init_repo.bat`, `pyproject.toml`, `env.example`, `src/engine/config.py` |
| Install or refresh global client instructions | [Instruction installation and updates](../README.md#codex-instruction-installation) | `scripts/install_instructions.py`, `scripts/_helpers/install_codex_instructions.py`, `scripts/_helpers/inject_claude_md.py`, `scripts/templates/` |
| Understand persona continuity and routing | [Routing](routing_flow.md) | `src/server.py`, `src/engine/persona.py`, `src/engine/persona_bundle.py`, `src/schemas/protocol.py` |
| Operate the shared macOS MCP daemon | [Daemon operations](shared-mcp-daemon.md) | `src/daemon/`, `bridge/stdio.mjs`, `scripts/daemon_smoke.py` |
| Choose or add agents | [Agent catalog](../agents/README.md), [creation guide](../README.md#-creating-new-agents) | `agents/*/system_prompt.mdc`, `agents/common/agent-schema.json` |
| Understand skills and implants | [Skills](../skills/README.md), [Implants](../implants/README.md) | `src/engine/skills.py`, `src/engine/implants.py`, `src/engine/enrichment.py` |
| Understand the optional intent classifier | [Intent classifier](intent-classifier.md) | `src/engine/intent.py`, `src/engine/config.py` |
| Work in this repository | [Session playbook](session-playbook.md) | Current repository and task constraints |
| Run or add reusable model workflows | [Workflow catalog](../flows/README.md) | `flows/*.md`, `src/flows.py`, `src/server.py`, `src/daemon/workspaces.py` |
| Run tests | [Test guide](../tests/README.md) | `pyproject.toml`, `tests/conftest.py`, `scripts/run_tests.sh`, `tests/` |
| Refresh documentation for people and AI | [Repeatable workflow](../flows/documentation-refresh.md) | The sources and checks listed in that workflow |
| Review and merge a PR/MR | [PR/MR review flow](../flows/pr-review.md) | Live reviews, current-head checks, and repository merge rules |

## AI instruction sources

| File | Purpose |
|---|---|
| [AGENTS.md](../AGENTS.md) | Short entry point for AI contributors; links to the shared protocol and working instructions |
| [CLAUDE.md](../CLAUDE.md) | Tracked routing section plus repository-specific notes |
| [flows/](../flows/README.md) | Reusable task instructions invoked by file path or through `run_flow` for the caller's repository |
| [routing-protocol-core.md](../scripts/templates/routing-protocol-core.md) | Installer's default protocol 2 instruction template |
| [routing-protocol-v1.md](../scripts/templates/routing-protocol-v1.md) | Explicit version 1 compatibility template |
| [memory-routing-v2.md](../scripts/templates/memory-routing-v2.md), [memory-routing-v1.md](../scripts/templates/memory-routing-v1.md) | Matching installer reminders for client memory |
| `agents/`, `skills/`, `implants/`, `rules/` | Runtime prompt content, selected and assembled by the server |

The installer defaults to protocol 2; the MCP API and slash prompts retain version
1 defaults unless version 2 is requested explicitly. See [routing](routing_flow.md)
before copying a call example.

## Evaluation guides

- [Local and hosted model runners](../evals/LOCAL_MODELS.md).
- [Component ablation workflow](../evals/ablation/README.md) and
  [case format](../evals/ablation/CASES.md).
- [Telemetry export and analysis](../evals/telemetry/README.md).
- [Dataset sources](../evals/datasets/SOURCES.md).
- [Cloud evaluation sessions](cloud-runs.md): operational experience from the
  September 2026 sweep; check account and service setup before reusing it.

## Plans, research and recorded results

These documents retain their original scope, measurements and limitations. Use
the maintained references above for current commands and behavior.

| Document | Status / scope |
|---|---|
| [Persona switching plan](persona-switch-plan.md) | Implemented design; current contracts are in the routing reference |
| [Persona switching results](persona-switch-eval-results.md) | Measurements of the revisions named in the report |
| [Memory subsystem specification](memory-subsystem-spec.md) | Implemented design with deviations in Appendix C; current transport behavior is in the routing and daemon references |
| [Daemon validation](shared-mcp-daemon-validation.md) | Recorded checks and measurements from September 2026 |
| [Layer sensitivity proposal](layer-sensitivity-plan.md) | Proposal and measurements tied to its recorded branch/model |
| [Query decomposition research](query-decomposition-research.md) | Research synthesis and analysis of its recorded dataset |
| [Ablation results](../evals/ablation/RESULTS.md) | Recorded experiment results |
| [Evaluation baseline](../evals/reports/baseline.md), [preferred implant A/B](../evals/reports/implants_preferred_ab.md) | Recorded runs, not a guarantee about later revisions |

Keep dates and measured values in these reports attached to their original runs.
Do not update a historical result by replacing its counts with a new test run.
