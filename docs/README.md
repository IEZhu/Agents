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
| Interpret the README quality badges and update their numeric documentation metric | [Quality checks](quality-checks.md) | `.github/workflows/shared-runtime.yml`, `.github/workflows/instruction-signing.yml`, `.github/workflows/documentation-quality.yml`, `scripts/check_docs.py`, `docs/badges/mcp-api-docs.svg` |
| Find every public MCP tool and its maintained behavior reference | [MCP tool index](mcp-tools.md) | `src/server.py`, `src/artifacts/tools.py`, `src/artifacts/service.py`, `src/local_inference/tools.py`; `scripts/check_docs.py` checks tool-row coverage and local navigation |
| Decide when to choose Agents-Core and make the first MCP calls | [Agent guide](agent-guide.md), [compact discovery index](../llms.txt) | `src/server.py`, `src/schemas/protocol.py`, `docs/routing_flow.md`, `docs/subagent-contract.md`, `llms-install.md`; the installed tool schemas govern each version |
| Install and configure Agents-Core | [Project README](../README.md) | `install.sh`, `scripts/init_repo.sh`, `scripts/init_repo.bat`, `src/client_paths.py`, `pyproject.toml`, `env.example`, `src/engine/config.py`, `src/model_migration.py`, `src/engine/embedding_prompts.py` |
| Remove Agents-Core completely | [Uninstall](../README.md#uninstall) | `src/uninstall.py`, `scripts/uninstall.sh`, `scripts/uninstall.ps1`, `tests/test_uninstall.py`, `.github/workflows/uninstall.yml` |
| Build, validate and release the Claude plugin | [Claude plugin](claude-plugin.md), [plugin README](../plugins/agents-core/README.md) | `VERSION`, `plugins/agents-core/`, `scripts/bump_plugin_version.py`, `.github/workflows/release-version.yml`, `tests/test_bump_plugin_version.py`, `src/server.py` (`TOOL_HINTS`), `tests/test_tool_annotations.py` |
| Find every submitted directory, its status and follow-up link | [Submission ledger](distribution.md#submitted-destinations), [MCP installation instructions](../llms-install.md) | `.claude-plugin/marketplace.json`, `glama.json`, `plugins/agents-core/`, linked directory requirements and submission records |
| Build and publish the Glama container | [Glama listing and container releases](glama.md), [submitted build fields](glama-build.json) | Glama admin build/test and release results, pinned source commit, `src/engine/readiness.py`, `src/startup.py`, `requirements.txt` |
| Keep a standalone installation updated | [Standalone stdio auto-update](../README.md#standalone-stdio-auto-update), [model switch on update](../README.md#model-switch-on-update) | `src/self_update.py`, `src/startup.py`, `src/model_migration.py`, `src/reindex.py`, `src/engine/config.py` |
| Install or refresh global client instructions | [Instruction installation and updates](../README.md#codex-instruction-installation) | `scripts/install_instructions.py`, `scripts/_helpers/install_codex_instructions.py`, `scripts/_helpers/inject_claude_md.py`, `scripts/_helpers/migrate_routing_memory.py`, `src/client_paths.py`, `scripts/templates/` |
| Show local operational information through native startup/`/clear` hooks in Claude/Codex, session-start first-response delivery in Cursor/Antigravity, or the MCP first-response fallback in Claude Desktop Chat; ask Claude/Codex to restore the Agents-Core persona after a compaction | [Session readiness](session-readiness.md) | `scripts/session_readiness.py`, `scripts/session_readiness_mcp.py`, `scripts/install_session_readiness.py`, `tests/test_session_readiness.py` |
| Maintain emoji prefixes in current chat titles, including final handoffs and user interruption, with client capability limits | [Thread status](thread-status.md) | `src/thread_status.py`, `src/codex_thread_titles.py`, `scripts/thread_status.py`, `scripts/install_thread_status.py`, `rules/rule-thread-status.mdc`, `tests/test_thread_status*.py`, `tests/test_codex_thread_titles.py`, `.github/workflows/thread-status.yml` |
| Prepare the shared calendar version and promote dev to main | [Calendar releases](releases.md) | `VERSION`, `src/calendar_version.py`, `src/version.py`, `scripts/bump_plugin_version.py`, `.github/workflows/release-version.yml`, `scripts/release_version.py`, `tests/test_bump_plugin_version.py`, `tests/test_release_version.py`, `tests/test_version.py` |
| Understand persona continuity and routing | [Routing](routing_flow.md) | `src/server.py`, `src/engine/persona.py`, `src/engine/persona_bundle.py`, `src/engine/router.py`, `src/schemas/protocol.py` |
| Delegate a bounded task and validate its result | [Subagent contract and API](subagent-contract.md) | `src/engine/subagent.py`, `src/engine/subagent_validation.py`, `src/schemas/subagent.py`, `src/server.py`, `agents/*/subagent.mdc`, `rules/subagents/` |
| Run delegated workers on lighter models and measure the outcome | [Model tiers for delegated workers](subagent-model-tiers.md) | `src/engine/model_tiers.py`, `src/engine/model_catalogs.py`, `src/engine/subagent_ledger.py`, `scripts/subagent_tiers_report.py` |
| Verify instruction seals, exact bytes and protected inventories; runtime enforcement remains planned | [Instruction-signing core API](instruction-signing-core.md) | `src/instruction_signing/`, `tests/test_instruction_signing.py`, `tests/test_instruction_signing_interop.py`, `tests/test_instruction_inventory.py`, `.github/workflows/instruction-signing.yml` |
| Share persistent exact task inputs through pinned manifests | [Shared artifacts](shared-artifacts.md) | `src/artifacts/`, `src/schemas/artifacts.py`, `src/engine/subagent.py`, `rules/subagents/rule-worker-artifacts.mdc`, `src/local_inference/service.py`, `tests/test_artifact_store.py`, `tests/test_artifact_service.py`, `tests/test_subagent_artifacts.py` |
| Coordinate intersecting assignments, exchange messages, await notifications and track requests across AI MCP clients | [Agent cooperation](agent-cooperation/README.md), [mailbox workflow](agent-cooperation/runtime.md#mailbox-workflow), [request tracking and event waits](agent-cooperation/runtime.md#request-tracking-and-event-waits), [protocol](agent-cooperation/protocol.md), [validation results](agent-cooperation/validation-results.md) | `src/cooperation/`, `src/server.py`, existing `bridge/stdio.mjs`, `rules/rule-agent-cooperation.mdc`, `rules/subagents/rule-worker-cooperation.mdc`; one enabled installed authority, with deterministic checks distinguished from model trials |
| Operate the shared MCP daemon on macOS or Windows | [Daemon operations](shared-mcp-daemon.md) | `src/daemon/`, `src/client_paths.py`, `bridge/stdio.mjs`, `scripts/daemon_smoke.py` |
| Use optional shared local generation and KV reuse | [Local inference operations](shared-mcp-daemon.md#local-inference), [historical contract](shared-kv-mcp/START_HERE.md), [source reconciliation](shared-kv-mcp/reconciliation-report.md), [acceptance report](shared-kv-mcp/implementation-report.md), [Redis live results](shared-kv-mcp/redis-live-report.md), [resident deployment](shared-kv-mcp/resident-deployment-report.md) | `src/local_inference/`, `src/daemon/app.py`, `src/server.py`, `tests/test_local_inference_*.py` |
| Use repository memory | [Repository Memory](../README.md#-repository-memory), [memory and errors](shared-mcp-daemon.md#memory-and-errors) | `src/memory/`, `src/server.py`, `src/daemon/workspaces.py`, `src/engine/config.py` |
| Choose or add agents | [Agent catalog](../agents/README.md), [creation guide](../README.md#-creating-new-agents) | `agents/*/system_prompt.mdc`, `agents/common/agent-schema.json` |
| Run the optional document OCR MCP server | [Document OCR server](../src/mcp_servers/document_ocr/README.md) | `src/mcp_servers/document_ocr/server.py`, `agents/document_ocr_expert/system_prompt.mdc`, `env.example` |
| Understand skills, implants and universal rules | [Skills](../skills/README.md), [Implants](../implants/README.md), [Universal rules](../rules/README.md) | `src/engine/skills.py`, `src/engine/implants.py`, `src/engine/rules.py`, `rules/rule-*.mdc`, `src/engine/enrichment.py`, `src/component_toggles.py` (web UI switches) |
| Subscribe to private or team rules by URL | [URL-subscribed user rules](user-rules.md) | `src/user_rules.py`, `src/rule_sources.py`, `src/engine/rules.py`, `src/server.py`, `src/component_catalog.py` |
| Understand the optional intent classifier | [Intent classifier](intent-classifier.md) | `src/engine/intent.py`, `src/engine/config.py` |
| Work in this repository | [Session playbook](session-playbook.md) | Current repository and task constraints |
| Reduce repeated model work without weakening flow gates | [Bounded flow execution](flow-execution.md) | `scripts/dev/watch_github_run.py`, `tests/test_watch_github_run.py`, source-bound task evidence |
| Run or add reusable model workflows | [Workflow catalog](../flows/README.md), [flow editor](shared-mcp-daemon.md#flow-editor) | `flows/*.md`, `src/flows.py`, `src/user_flows.py`, `src/flow_persona.py`, `src/server.py`, `src/daemon/workspaces.py`, `src/daemon/flows_ui.py`, `src/component_toggles.py`, `src/component_catalog.py`, `src/engine/config.py` (stdio target repository) |
| Sync the personal library between machines | [User library sync](user-sync.md) | `src/user_sync/`, `src/user_library.py`, `src/user_flows.py`, `src/component_toggles.py`, `src/memory/history.py`, `tests/test_user_sync*.py`, `.github/workflows/user-sync.yml` |
| Set up and run the autodev schedule: prerequisites and installation, the modes (scheduled dry-runs, supervised real runs with `run-now`, the automatic mode with `autodev ready` and the owner's acceptance), the settings page, cost, limitations and the owner's steps in order | [Autodev guide](autodev.md), [Autodev runner](autodev-runner.md), [design](autodev-plan.md) | `src/schedules/` (commands: `cli.py`, `runner.py`; the automatic mode: `automatic.py`; trusted repository and bundles: `repos.py`, `gitsafe.py`), `src/daemon/autoupdate.py` (run lease), `src/daemon/autodev_ui.py` (settings page), `tests/test_schedules_*.py`, `tests/fixtures/autodev_gate.json` |
| Run the phase 1a acceptance of autodev (#349) and the phase 1b one (#350): the owner's live steps with their commands, what to record and the pass criteria, and the results table | [Autodev acceptance](autodev-acceptance.md) | `src/schedules/cli.py`, `src/schedules/runner.py`, `src/schedules/sequence.py`, `src/schedules/probes.py`, `src/schedules/broker.py`, `src/schedules/integrity.py`, `src/daemon/control.py` (`update` and the run lease) |
| Run Agents-Core in Claude Code cloud sessions | [Cloud environment with Agents-Core](cloud-runs.md#cloud-environment-with-agents-core) | `scripts/setup_cloud_env.sh`, `install.sh`, `scripts/init_repo.sh`, `tests/test_setup_cloud_env.py` |
| Set up and use the cloud issue agent (`/agent` commands) | [Issue agent setup](cloud-runs.md#issue-agent), [issue agent flow](../flows/issue-agent.md) | `flows/issue-agent.md`, `flows/issue-plan.md`, `flows/issue-implementation.md`, `scripts/templates/issue-agent-bridge.yml`, `.github/workflows/issue-agent-bridge.yml`, `tests/test_issue_agent_bridge.py` |
| Know what one autodev run does (the steps of each mode, a stage session, the checks between stages, the records, the report and the owner's live steps) | [Autodev run sequence](autodev-sequence.md) | `src/schedules/sequence.py`, `src/schedules/candidates.py`, `src/schedules/stages.py`, `src/schedules/preconditions.py`, `src/schedules/run_state.py`, `src/schedules/report.py`, `src/schedules/integrity.py`, `src/flow_execution.py`, `tests/test_schedules_sequence.py`, `tests/fixtures/autodev_select_synthetic.json` |
| Check what autodev pushes and sends out (protected paths, outgoing-text scan) | [Autodev guards](autodev-guards.md) | `src/schedules/paths.py`, `src/schedules/scan.py`, `.github/autodev-protected-paths`, `.github/autodev-control-paths`, `tests/test_schedules_paths.py`, `tests/test_schedules_scan.py` |
| Know what autodev may do on GitHub (the run-bound broker: push, PR, comments, labels, threads, reads) | [Autodev broker](autodev-broker.md) | `src/schedules/broker.py`, `tests/test_schedules_broker.py` |
| Know what an autodev Claude session may reach and how the runner proves it (argv, sandbox settings, environment, login, probes, decoys) | [Autodev sandbox](autodev-sandbox.md) | `src/schedules/sandbox.py`, `src/schedules/probes.py`, `src/schedules/decoys.py`, `tests/test_schedules_sandbox.py`, `tests/test_schedules_probes.py`, `tests/test_schedules_decoys.py`, `tests/fixtures/fake_claude.py` |
| Know how autodev runs Codex and when it falls back to the Claude worker (outer Seatbelt profile, argv, environment, proxy, probes, Q8/Q14) | [Autodev Codex](autodev-codex.md) | `src/schedules/codex_sandbox.py`, `src/schedules/codex_proxy.py`, `src/schedules/codex_probes.py`, `tests/test_schedules_codex.py`, `tests/fixtures/fake_codex.py` |
| Know how autodev starts a delegated review or pre-mortem and what it accepts (prepared packages, materials from git objects, the Claude and Codex workers, validation, the review policy, records and decisions) | [Autodev workers](autodev-workers.md) | `src/schedules/workers.py`, `src/schedules/worker_launch.py`, `src/schedules/materials.py`, `src/schedules/sandbox.py` (`build_worker`), `tests/test_schedules_workers.py`, `tests/fixtures/fake_worker.py` |
| Know what each autodev stage does and how a run proceeds (the stage flows, their output schemas, the autodev mode of `pr-review`) | [Autodev flow](../flows/autodev.md) | `flows/autodev.md`, `flows/autodev-gate.md`, `flows/autodev-select.md`, `flows/autodev-implement.md`, `flows/code-review-pass.md`, `flows/change-premortem.md`, `flows/pr-review.md` (autodev mode), `scripts/dev/pr_threads.py` (`--mode autodev`), `tests/test_flows.py`, `tests/test_pr_threads.py` |
| Measure the autodev pipeline and decide what to change (run outcomes, cost, review convergence, lead time, rework) | [Autodev retrospective](../flows/autodev-retro.md) | `src/schedules/records.py`, `src/schedules/report.py`, `src/daemon/state.py` (state directory), `docs/autodev-sequence.md` (records) |
| Run tests | [Test guide](../tests/README.md) | `pyproject.toml`, `tests/conftest.py`, `scripts/run_tests.sh`, `tests/` |
| Refresh documentation for people and AI | [Repeatable workflow](../flows/documentation-refresh.md) | The sources and checks listed in that workflow |
| Inspect the scope, evidence and limitations of the October 18 documentation audit | [Documentation audit](documentation-audit-2026-10-18.md) | Recorded base revision, source/test evidence and actual validation results; a dated audit, not a promise of future freshness |
| Review and merge a PR/MR | [PR/MR review flow](../flows/pr-review.md) | Live reviews, current-head checks, and repository merge rules |
| Close a conversation without losing its work | [Thread close flow](../flows/thread-close.md) | `scripts/dev/thread_inventory.py`, the live repository and GitHub state, `log_interaction` |
| Improve or add a skill, implant or agent from free sources | [Open-source tuning flow](../flows/open-source-tuning.md) | `skills/README.md`, `implants/README.md`, `agents/README.md`, `agents/common/agent-schema.json`, `evals/ablation/CASES.md`, `tests/test_skill_freshness.py`, `tests/test_subagent.py` |

## Pending feature integration

The proxy guide was checked against its pinned neighboring PR on 2026-10-18.
Its implementation is absent from this documentation branch. Follow the
[integration checklist](documentation-audit-2026-10-18.md#parallel-feature-integration)
before promoting it to released behavior or running its commands.

| Reader task | Guide | Pinned implementation and evidence |
|---|---|---|
| Connect HTTP MCP upstreams, confirm revisions, store credentials and sync host inventory | [HTTP MCP proxy](mcp-proxy.md) | [PR #449 at d067c315caf3](https://github.com/IEZhu/Agents/tree/d067c315caf3c9877896315b96e68d4bb4a25c11): `src/mcp_registry.py`, `src/mcp_secrets.py`, `src/daemon/mcp_proxy/`, `src/daemon/mcp_cli.py`, `src/daemon/mcp_ui.py`, MCP and sync tests |

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
| [Shared local inference and KV cache handoff](shared-kv-mcp/START_HERE.md) | Historical implementation contract and recorded base; the [reconciliation](shared-kv-mcp/reconciliation-report.md) identifies integrated source and evidence limits, while maintained operations are linked above |
| [MCP proxy plan](mcp-proxy-plan.md) | Proposal of 2026-10-12 for epic #306. The narrower HTTP/host-inventory slice is documented in the [pending integration guide](mcp-proxy.md); registry/import, OAuth, other upstream transports and the remaining epic scope are not established by that slice |
| [Subagent validation results](subagent-validation-results.md) | Recorded contract tests, default-budget coverage and controlled context-size measurements; model quality/cost evaluation remains pending |
| [Subagent implementation plan](subagent-implementation-plan.md) | Issue #291: compact profiles, task-scoped context, worker rules and structural validation; current API is the [subagent contract](subagent-contract.md) |
| [Autodev plan](autodev-plan.md) | Proposal of 2026-10-12, version 5. Phase 0 is done, and the code of phase 1a (#338 to #348) and phase 1b (#350, the automatic mode) is implemented; their acceptance runs wait for the owner ([acceptance](autodev-acceptance.md)), and phases 2 to 6 are not implemented. The design: a self-development pipeline (selecting a task, implementing it with review until clean and a pre-mortem, then a PR and `pr-review`) driven by a local scheduled Python runner, plus per-flow execution profiles (AI, model, effort), flow contracts and linear pipelines; ends with the checklist plan. The [review appendix](autodev-plan-review.md) records every finding of its three design reviews and the decision on it |
| [Instruction signing specification](instruction-signing-spec.md) | Implementation contract (#233/#234), with #235 primitives implemented and runtime enforcement planned: an unchanged OMS envelope for instruction and system-file protection, trusted startup, bounded unsigned-change review, verified recovery, and proposed repository/global instruction discovery, per-request gates and one exact-snapshot owner decision across chats (#463–#468) |
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
