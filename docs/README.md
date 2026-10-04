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
| Install and configure Agents-Core | [Project README](../README.md) | `install.sh`, `scripts/init_repo.sh`, `scripts/init_repo.bat`, `src/client_paths.py`, `pyproject.toml`, `env.example`, `src/engine/config.py`, `src/model_migration.py`, `src/engine/embedding_prompts.py` |
| Keep a standalone installation updated | [Standalone stdio auto-update](../README.md#standalone-stdio-auto-update), [model switch on update](../README.md#model-switch-on-update) | `src/self_update.py`, `src/startup.py`, `src/model_migration.py`, `src/reindex.py`, `src/engine/config.py` |
| Install or refresh global client instructions | [Instruction installation and updates](../README.md#codex-instruction-installation) | `scripts/install_instructions.py`, `scripts/_helpers/install_codex_instructions.py`, `scripts/_helpers/inject_claude_md.py`, `scripts/_helpers/migrate_routing_memory.py`, `src/client_paths.py`, `scripts/templates/` |
| Understand persona continuity and routing | [Routing](routing_flow.md) | `src/server.py`, `src/engine/persona.py`, `src/engine/persona_bundle.py`, `src/engine/router.py`, `src/schemas/protocol.py` |
| Operate the shared macOS MCP daemon | [Daemon operations](shared-mcp-daemon.md) | `src/daemon/`, `src/client_paths.py`, `bridge/stdio.mjs`, `scripts/daemon_smoke.py` |
| Use repository memory | [Repository Memory](../README.md#-repository-memory), [memory and errors](shared-mcp-daemon.md#memory-and-errors) | `src/memory/`, `src/server.py`, `src/daemon/workspaces.py`, `src/engine/config.py` |
| Choose or add agents | [Agent catalog](../agents/README.md), [creation guide](../README.md#-creating-new-agents) | `agents/*/system_prompt.mdc`, `agents/common/agent-schema.json` |
| Run the optional document OCR MCP server | [Document OCR server](../src/mcp_servers/document_ocr/README.md) | `src/mcp_servers/document_ocr/server.py`, `agents/document_ocr_expert/system_prompt.mdc`, `env.example` |
| Understand skills, implants and universal rules | [Skills](../skills/README.md), [Implants](../implants/README.md), [Universal rules](../rules/README.md) | `src/engine/skills.py`, `src/engine/implants.py`, `src/engine/rules.py`, `rules/rule-*.mdc`, `src/engine/enrichment.py`, `src/component_toggles.py` (web UI switches) |
| Understand the optional intent classifier | [Intent classifier](intent-classifier.md) | `src/engine/intent.py`, `src/engine/config.py` |
| Work in this repository | [Session playbook](session-playbook.md) | Current repository and task constraints |
| Run or add reusable model workflows | [Workflow catalog](../flows/README.md), [flow editor](shared-mcp-daemon.md#flow-editor) | `flows/*.md`, `src/flows.py`, `src/user_flows.py`, `src/flow_persona.py`, `src/server.py`, `src/daemon/workspaces.py`, `src/daemon/flows_ui.py`, `src/component_toggles.py`, `src/component_catalog.py`, `src/engine/config.py` (stdio target repository) |
| Run Agents-Core in Claude Code cloud sessions | [Cloud environment with Agents-Core](cloud-runs.md#cloud-environment-with-agents-core) | `scripts/setup_cloud_env.sh`, `install.sh`, `scripts/init_repo.sh`, `tests/test_setup_cloud_env.py` |
| Set up and use the cloud issue agent (`/agent` commands) | [Issue agent setup](cloud-runs.md#issue-agent), [issue agent flow](../flows/issue-agent.md) | `flows/issue-agent.md`, `flows/issue-plan.md`, `flows/issue-implementation.md`, `scripts/templates/issue-agent-bridge.yml`, `.github/workflows/issue-agent-bridge.yml`, `tests/test_issue_agent_bridge.py` |
| Run tests | [Test guide](../tests/README.md) | `pyproject.toml`, `tests/conftest.py`, `scripts/run_tests.sh`, `tests/` |
| Refresh documentation for people and AI | [Repeatable workflow](../flows/documentation-refresh.md) | The sources and checks listed in that workflow |
| Review and merge a PR/MR | [PR/MR review flow](../flows/pr-review.md) | Live reviews, current-head checks, and repository merge rules |
| Close a conversation without losing its work | [Thread close flow](../flows/thread-close.md) | `scripts/dev/thread_inventory.py`, the live repository and GitHub state, `log_interaction` |

## AI instruction sources

| File | Purpose |
|---|---|
| [AGENTS.md](../AGENTS.md) | Short entry point for AI contributors; links to the shared protocol and working instructions |
| [CLAUDE.md](../CLAUDE.md) | Tracked routing section plus repository-specific notes |
| [flows/](../flows/README.md) | Reusable task instructions invoked by file path or through `run_flow` for the caller's repository |
| [routing-protocol-core.md](../scripts/templates/routing-protocol-core.md) | Protocol 2 client instruction template |
| `src/server.py` (`FastMCP` `instructions`), `src/engine/persona.py` (`APPLY_INSTRUCTION`) | Protocol 2 summary sent to every MCP client at initialization, and the apply instruction returned with each `SUCCESS` bundle; keep both consistent with `routing-protocol-core.md` |
| [memory-routing.md](../scripts/templates/memory-routing.md) | Matching installer reminder for client memory; [legacy/](../scripts/templates/legacy/README.md) keeps earlier generated copies for migration |
| [`agents/`](../agents/README.md), [`skills/`](../skills/README.md), [`implants/`](../implants/README.md), [`rules/`](../rules/README.md) | Runtime prompt content, selected and assembled by the server |

Protocol 2 is the only protocol; protocol 1 was removed on 2026-09-29. See
[routing](routing_flow.md) before copying a call example.

## Evaluation guides

- Regression and bench harness: `./scripts/eval.sh help` lists the deterministic
  routing, retrieval and tier evals, the baseline commands and the MCP-vs-vanilla
  `bench`; like every `eval.sh` command, it first needs `.venv`, `venv` or a pyenv
  Python. The [session playbook](session-playbook.md#evals) gives the
  prerequisites.
- [Local and hosted model runners](../evals/LOCAL_MODELS.md).
- [A/B eval flow](../flows/ab-eval.md): the whole cycle for one change, from case
  set to recommendation, on hosted models and Opus.
- [Component ablation workflow](../evals/ablation/README.md) and
  [case format](../evals/ablation/CASES.md).
- Semantic-cache routing of the embedding model:
  `python -m evals.runners.run_cache_routing` (leave-one-out nearest neighbours,
  precision at fixed coverage per language). It and
  `python -m evals.runners.run_retrieval` take `--dataset` (repeatable) for
  hand-written sets such as `evals/datasets/routing_ru.jsonl`.
- [Telemetry export and analysis](../evals/telemetry/README.md).
- [Dataset sources](../evals/datasets/SOURCES.md).
- [Cloud sessions](cloud-runs.md): the September 2026 eval-sweep procedure (check
  account and service setup before reusing it), the [eval routine](cloud-runs.md#eval-routine)
  of the A/B eval flow, and the maintained setup and operations reference for the
  cloud [issue agent](cloud-runs.md#issue-agent) and for a
  [cloud environment with Agents-Core](cloud-runs.md#cloud-environment-with-agents-core).

## Plans, research and recorded results

These documents retain their original scope, measurements and limitations. Use
the maintained references above for current commands and behavior.

| Document | Status / scope |
|---|---|
| [Personal flow library](user-flows-design.md) | Implemented decision record: file-based personal and repository flows, chat management and the local editor; the current reference is the [flow guide](../flows/README.md#personal-and-repository-flows) |
| [Persona switching plan](persona-switch-plan.md) | Implemented design; current contracts are in the [routing reference](routing_flow.md) |
| [Persona switching results](persona-switch-eval-results.md) | Measurements of the revisions named in the report |
| [Memory subsystem specification](memory-subsystem-spec.md) | Implemented design with deviations in Appendix C; current behavior is in the repository memory references above |
| [Daemon validation](shared-mcp-daemon-validation.md) | Recorded checks and measurements of the initial daemon release, September 2026 |
| [Layer sensitivity proposal](layer-sensitivity-plan.md) | Partially implemented proposal (opt-in implant settings, default off); measurements tied to its recorded branch/model |
| [Query decomposition research](query-decomposition-research.md) | Research synthesis and analysis of its recorded dataset |
| [Ablation results](../evals/ablation/RESULTS.md) | Recorded experiment results |
| [English pivot A/B](english-pivot-eval-results.md) | Recorded 2026-10-02 runs of `rule-english-pivot` on Qwen 3.8 27B, Gemini 3.8 Flash and Opus 5.5 |
| [Embedding models A/B](embedding-models-eval-results.md) | Recorded 2026-10-03 retrieval runs of eight embedding configurations (seven models) and an answer A/B of harrier-oss-v1-270m against e5-large; harrier-oss-v1-270m became the default with #178 |
| [Evaluation baseline](../evals/reports/baseline.md) | Committed comparison base for `./scripts/eval.sh diff`; its title records the run date and commit `47d195d`, a PR #48 revision that GitHub still serves but that is not on `main` (the file was last committed in `c389797`, 2026-05-04, PR #48). It does not record the embedding model behind its skill retrieval figures, which predate the `microsoft/harrier-oss-v1-270m` default (#178) |
| [Preferred implant A/B](../evals/reports/implants_preferred_ab.md) | Recorded run added in commit `7e6fd79` (2026-05-23) by `evals/scripts/compare_implants.py`; not a guarantee about later revisions |

Commit hashes recorded in these reports may refer to unpublished branch
revisions; `git cat-file -t <hash>` shows whether one is available locally.

Keep dates and measured values in these reports attached to their original runs.
Do not update a historical result by replacing its counts with a new test run;
only a deliberate `./scripts/eval.sh baseline` update replaces the evaluation
baseline.
