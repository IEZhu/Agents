# Documentation map

Start with the [project README](../README.md) for the overview and [install and connect](install.md) for setup. For work
on this repository, read [CONTRIBUTING.md](../CONTRIBUTING.md),
[AGENTS.md](../AGENTS.md) and [CLAUDE.md](../CLAUDE.md).

All repository documentation must be in English. Keep other languages only in
necessary passages, such as quotations and language-specific examples, with
English explanations around them. The
[documentation language policy](../flows/documentation-refresh.md#documentation-language)
also applies to AI instructions, plans, and reports.

## Contents

- [Set up and connect](#set-up-and-connect)
- [Use the server](#use-the-server)
- [Delegate work and coordinate sessions](#delegate-work-and-coordinate-sessions)
- [Run the shared service](#run-the-shared-service)
- [Internals and trust](#internals-and-trust)
- [Scheduled self-development](#scheduled-self-development)
- [Work on this repository](#work-on-this-repository)
- [AI instruction sources](#ai-instruction-sources)
- [Evaluation guides](#evaluation-guides)
- [Plans, research and recorded results](#plans-research-and-recorded-results)

## Set up and connect

Installation, client configuration, updates, and what each client supports.

| Need | Document | Source of truth for behavior |
|---|---|---|
| Install Agents-Core, connect a client and keep it updated | [Install and connect](install.md) | `install.sh`, `scripts/init_repo.sh`, `scripts/init_repo.bat`, `scripts/install_instructions.py`, `src/client_paths.py`, `src/mcp_registry.py`, `src/update_channel.py` |
| Choose a client and see which features it supports | [Features and client support](features.md) | `src/client_paths.py`, `src/client_instructions.py`, `src/engine/model_tiers.py`, `src/mcp_registry.py`, `scripts/install_*.py` and the guides each row links |
| Decide when to choose Agents-Core and make the first MCP calls | [Agent guide](agent-guide.md), [compact discovery index](../llms.txt) | `src/server.py`, `src/schemas/protocol.py`, `docs/routing_flow.md`, `docs/subagent-contract.md`, `llms-install.md`; the installed tool schemas govern each version |
| Configure the server and its optional integrations | [Configuration and observability](configuration.md) | `env.example`, `src/engine/config.py`, `src/model_migration.py`, `src/engine/embedding_prompts.py`, `src/utils/langfuse_compat.py` |
| Install or refresh global client instructions | [Instruction installation and updates](install.md#codex-instruction-installation) | `scripts/install_instructions.py`, `scripts/_helpers/install_codex_instructions.py`, `scripts/_helpers/inject_claude_md.py`, `scripts/_helpers/migrate_routing_memory.py`, `src/client_paths.py`, `scripts/templates/` |
| Keep a standalone installation updated | [Standalone stdio auto-update](install.md#standalone-stdio-auto-update), [model switch on update](configuration.md#model-switch-on-update) | `src/self_update.py`, `src/environments.py`, `src/daemon/update_dependencies.py`, `src/startup.py`, `src/model_migration.py`, `src/reindex.py`, `src/engine/config.py`, `tests/test_environments.py` |
| Show local operational information through native startup/`/clear` hooks in Claude/Codex, session-start first-response delivery in Cursor/Antigravity, or the MCP first-response fallback in Claude Desktop Chat; ask Claude/Codex to restore the Agents-Core persona after a compaction | [Session readiness](session-readiness.md) | `scripts/session_readiness.py`, `scripts/session_readiness_mcp.py`, `scripts/install_session_readiness.py`, `tests/test_session_readiness.py` |
| Opt in to native context compaction between completed Claude Code turns without a separate API key; choose Claude Code's own auto-compact window for long autonomous turns; inspect the reserved, unsupported Cursor adapter | [Native context compaction](context-compaction.md), [plugin README](../plugins/agents-core-context-compaction/README.md) | `plugins/agents-core-context-compaction/`, `src/context_compaction/`, `scripts/install_context_compaction.py`, `tests/test_context_compaction*.py` |
| Remove Agents-Core completely | [Uninstall](install.md#uninstall) | `src/uninstall.py`, `scripts/uninstall.sh`, `scripts/uninstall.ps1`, `tests/test_uninstall.py`, `.github/workflows/uninstall.yml` |

## Use the server

The public tools, the persona and memory layers, components, flows, and per-turn records.

| Need | Document | Source of truth for behavior |
|---|---|---|
| Find every public MCP tool and its maintained behavior reference | [MCP tool index](mcp-tools.md) | `src/server.py`, `src/artifacts/tools.py`, `src/artifacts/service.py`, `src/local_inference/tools.py`; `scripts/check_docs.py` checks tool-row coverage and local navigation |
| Understand persona continuity and routing | [Routing](routing_flow.md) | `src/server.py`, `src/engine/persona.py`, `src/engine/persona_bundle.py`, `src/engine/router.py`, `src/schemas/protocol.py` |
| Choose or add agents | [Agent catalog](../agents/README.md), [creation guide](../agents/README.md#creating-a-new-agent) | `agents/*/system_prompt.mdc`, `agents/common/agent-schema.json` |
| Understand skills, implants and universal rules | [Skills](../skills/README.md), [Implants](../implants/README.md), [Universal rules](../rules/README.md) | `src/engine/skills.py`, `src/engine/implants.py`, `src/engine/rules.py`, `rules/rule-*.mdc`, `src/engine/enrichment.py`, `src/component_toggles.py` (web UI switches) |
| Subscribe to private or team rules by URL | [URL-subscribed user rules](user-rules.md) | `src/user_rules.py`, `src/rule_sources.py`, `src/engine/rules.py`, `src/server.py`, `src/component_catalog.py` |
| Understand the optional intent classifier | [Intent classifier](intent-classifier.md) | `src/engine/intent.py`, `src/engine/config.py` |
| Run or add reusable model workflows | [Workflow catalog](../flows/README.md), [flow editor](shared-mcp-daemon.md#flow-editor) | `flows/*.md`, `src/flows.py`, `src/user_flows.py`, `src/flow_persona.py`, `src/server.py`, `src/daemon/workspaces.py`, `src/daemon/flows_ui.py`, `src/component_toggles.py`, `src/component_catalog.py`, `src/engine/config.py` (stdio target repository) |
| Reduce repeated model work without weakening flow gates | [Bounded flow execution](flow-execution.md) | `scripts/dev/watch_github_run.py`, `tests/test_watch_github_run.py`, source-bound task evidence |
| Use the repository description and turn history | [Repository Memory](memory.md), [memory and errors](shared-mcp-daemon.md#memory-and-errors) | `src/memory/`, `src/memory/describer.py`, `src/memory/config.py`, `src/server.py`, `src/daemon/workspaces.py`, `src/engine/config.py` |
| Read what Claude Code and Codex keep in their native memory, from any client | [Shared memory](shared-memory.md) | `src/memory/shared.py`, `src/memory/native.py`, `src/server.py`, `tests/test_shared_memory.py` |
| Keep checked facts with their verdicts, reuse them while they hold and see when they go stale or are contradicted | [Validated claims](validated-claims.md) | `src/claims/`, `src/estimation/`, `src/engine/warmup_estimate.py`, `src/server.py`, `tests/test_claims.py`, `tests/test_estimation_kalman.py` |
| Understand durable memory acceptance and retry protection | [Memory acceptance (MEM-01)](memory-acceptance.md) | `src/memory/history.py`, `src/server.py`, `tests/test_memory_acceptance.py` |
| Write a turn's numbers and read them back in a later run (the thread-close record, retrospectives) | [Turn records](turn-records.md) | `src/memory/record.py`, `src/server.py`, `src/memory/history.py`, `scripts/thread_close_miss_rate.py`, `tests/test_turn_record.py`, `tests/test_thread_close_miss_rate.py` |
| Maintain emoji prefixes in current chat titles, including final handoffs and user interruption, with client capability limits | [Thread status](thread-status.md) | `src/thread_status.py`, `src/codex_thread_titles.py`, `scripts/thread_status.py`, `scripts/thread_status_interrupt.py`, `scripts/install_thread_status.py`, `rules/rule-thread-status.mdc`, `tests/test_thread_status*.py`, `tests/test_codex_thread_titles.py`, `.github/workflows/thread-status.yml` |
| Sync the personal library between machines | [User library sync](user-sync.md) | `src/user_sync/`, `src/user_library.py`, `src/user_flows.py`, `src/component_toggles.py`, `src/memory/history.py`, `tests/test_user_sync*.py`, `.github/workflows/user-sync.yml` |
| Run the optional document OCR MCP server | [Document OCR server](../src/mcp_servers/document_ocr/README.md) | `src/mcp_servers/document_ocr/server.py`, `agents/document_ocr_expert/system_prompt.mdc`, `env.example` |

## Delegate work and coordinate sessions

Bounded workers, the inputs they share, and coordination between parallel sessions.

| Need | Document | Source of truth for behavior |
|---|---|---|
| Delegate a bounded task and validate its result | [Subagent contract and API](subagent-contract.md) | `src/engine/subagent.py`, `src/engine/subagent_text.py`, `src/engine/subagent_exchange.py`, `src/engine/subagent_validation.py`, `src/schemas/subagent.py`, `src/server.py`, `agents/*/subagent.mdc`, `rules/subagents/` |
| Run delegated workers on lighter models and measure the outcome | [Model tiers for delegated workers](subagent-model-tiers.md) | `src/engine/model_tiers.py`, `src/engine/model_catalogs.py`, `src/engine/subagent_ledger.py`, `scripts/subagent_tiers_report.py` |
| Share persistent exact task inputs through pinned manifests | [Shared artifacts](shared-artifacts.md) | `src/artifacts/`, `src/schemas/artifacts.py`, `src/engine/subagent.py`, `rules/subagents/rule-worker-artifacts.mdc`, `src/local_inference/service.py`, `tests/test_artifact_store.py`, `tests/test_artifact_service.py`, `tests/test_subagent_artifacts.py` |
| Coordinate intersecting assignments, exchange messages, await notifications and track requests across AI MCP clients | [Agent cooperation](agent-cooperation/README.md), [mailbox workflow](agent-cooperation/runtime.md#mailbox-workflow), [request tracking and event waits](agent-cooperation/runtime.md#request-tracking-and-event-waits), [protocol](agent-cooperation/protocol.md), [validation results](agent-cooperation/validation-results.md) | `src/cooperation/`, `src/server.py`, existing `bridge/stdio.mjs`, `rules/rule-agent-cooperation.mdc`, `rules/subagents/rule-worker-cooperation.mdc`; one enabled installed authority, with deterministic checks distinguished from model trials |

## Run the shared service

The daemon, its runtime boundary, optional local generation, upstream MCP servers, and cloud sessions.

| Need | Document | Source of truth for behavior |
|---|---|---|
| Operate the shared MCP daemon on macOS or Windows | [Daemon operations](shared-mcp-daemon.md) | `src/daemon/`, `src/client_paths.py`, `bridge/stdio.mjs`, `scripts/daemon_smoke.py` |
| Locate Personal startup composition shared by daemon and standalone stdio | [Personal runtime boundary](personal-runtime-boundary.md) | `src/personal_runtime.py`, `src/stdio_runtime.py`, `src/daemon/runtime.py`, `src/daemon/app.py`, `src/server.py`, `tests/test_personal_runtime.py`, `tests/test_runtime_composition.py` |
| Use optional shared local generation and KV reuse | [Local inference operations](shared-mcp-daemon.md#local-inference), [historical contract](shared-kv-mcp/START_HERE.md), [source reconciliation](shared-kv-mcp/reconciliation-report.md), [acceptance report](shared-kv-mcp/implementation-report.md), [Redis live results](shared-kv-mcp/redis-live-report.md), [resident deployment](shared-kv-mcp/resident-deployment-report.md) | `src/local_inference/`, `src/daemon/app.py`, `src/server.py`, `tests/test_local_inference_*.py` |
| Connect a manually configured HTTP and stdio MCP upstream and sync its host inventory | [MCP proxy](mcp-proxy.md) | `src/mcp_registry.py`, `src/mcp_secrets.py`, `src/daemon/mcp_proxy/`, `src/daemon/mcp_ui.py`, `src/daemon/mcp_cli.py`, `src/user_sync/scope.py` |
| Run Agents-Core in Claude Code cloud sessions | [Cloud environment with Agents-Core](cloud-runs.md#cloud-environment-with-agents-core) | `scripts/setup_cloud_env.sh`, `install.sh`, `scripts/init_repo.sh`, `tests/test_setup_cloud_env.py` |
| Set up and use the cloud issue agent (`/agent` commands) | [Issue agent setup](cloud-runs.md#issue-agent), [issue agent flow](../flows/issue-agent.md) | `flows/issue-agent.md`, `flows/issue-plan.md`, `flows/issue-implementation.md`, `scripts/templates/issue-agent-bridge.yml`, `.github/workflows/issue-agent-bridge.yml`, `tests/test_issue_agent_bridge.py` |

## Internals and trust

The source layout, instruction seals, and the bounds on tool metadata.

| Need | Document | Source of truth for behavior |
|---|---|---|
| Understand the source layout and the components behind the tools | [Architecture](architecture.md) | `src/server.py`, `src/engine/`, `src/startup.py`, `src/runtime_paths.py` |
| Verify instruction seals, exact bytes and protected inventories; manage local scoped trust and accepted policy; runtime enforcement remains planned | [Instruction-signing core API](instruction-signing-core.md) | `src/instruction_signing/`, `tests/test_instruction_signing.py`, `tests/test_instruction_signing_authority.py`, `tests/test_instruction_signing_interop.py`, `tests/test_instruction_inventory.py`, `.github/workflows/instruction-signing.yml` |
| Enroll scoped signing keys, revoke trust and accept authenticated coverage policy | [Local signing authority](instruction-signing-authority.md) | `src/instruction_signing/authority.py`, `src/instruction_signing/storage.py`, `src/instruction_signing/__main__.py`, `tests/test_instruction_signing_authority.py` |
| Bound MCP metadata output and derive native UI targets from current observations | [Tool inspection safety](mcp-tool-safety.md) | `scripts/dev/mcp_metadata_guard.mjs`, its generated code-mode adapter, `tests/mcp_metadata_guard.test.mjs`, `tests/test_mcp_tool_safety.py` |

## Scheduled self-development

Setting up the autodev schedule, what one run does, and what a run may reach.

| Need | Document | Source of truth for behavior |
|---|---|---|
| Set up and run the autodev schedule: prerequisites and installation, the modes (scheduled dry-runs, supervised real runs with `run-now`, the automatic mode with `autodev ready` and the owner's acceptance), the settings page, cost, limitations and the owner's steps in order | [Autodev guide](autodev.md), [Autodev runner](autodev-runner.md), [design](autodev-plan.md) | `src/schedules/` (commands: `cli.py`, `runner.py`; the automatic mode: `automatic.py`; trusted repository and bundles: `repos.py`, `gitsafe.py`), `src/daemon/autoupdate.py` (run lease), `src/daemon/autodev_ui.py` (settings page), `tests/test_schedules_*.py`, `tests/fixtures/autodev_gate.json` |
| Know what one autodev run does (the steps of each mode, a stage session, the checks between stages, the records, the report and the owner's live steps) | [Autodev run sequence](autodev-sequence.md) | `src/schedules/sequence.py`, `src/schedules/candidates.py`, `src/schedules/stages.py`, `src/schedules/preconditions.py`, `src/schedules/run_state.py`, `src/schedules/report.py`, `src/schedules/integrity.py`, `src/flow_execution.py`, `tests/test_schedules_sequence.py`, `tests/fixtures/autodev_select_synthetic.json` |
| Check what autodev pushes and sends out (protected paths, outgoing-text scan) | [Autodev guards](autodev-guards.md) | `src/schedules/paths.py`, `src/schedules/scan.py`, `.github/autodev-protected-paths`, `.github/autodev-control-paths`, `tests/test_schedules_paths.py`, `tests/test_schedules_scan.py` |
| Know what autodev may do on GitHub (the run-bound broker: push, PR, comments, labels, threads, reads) | [Autodev broker](autodev-broker.md) | `src/schedules/broker.py`, `tests/test_schedules_broker.py` |
| Know what an autodev Claude session may reach and how the runner proves it (argv, sandbox settings, environment, login, probes, decoys) | [Autodev sandbox](autodev-sandbox.md) | `src/schedules/sandbox.py`, `src/schedules/probes.py`, `src/schedules/decoys.py`, `tests/test_schedules_sandbox.py`, `tests/test_schedules_probes.py`, `tests/test_schedules_decoys.py`, `tests/fixtures/fake_claude.py` |
| Know how autodev runs Codex and when it falls back to the Claude worker (outer Seatbelt profile, argv, environment, proxy, probes, Q8/Q14) | [Autodev Codex](autodev-codex.md) | `src/schedules/codex_sandbox.py`, `src/schedules/codex_proxy.py`, `src/schedules/codex_probes.py`, `tests/test_schedules_codex.py`, `tests/fixtures/fake_codex.py` |
| Know how autodev starts a delegated review or pre-mortem and what it accepts (prepared packages, materials from git objects, the Claude and Codex workers, validation, the review policy, records and decisions) | [Autodev workers](autodev-workers.md) | `src/schedules/workers.py`, `src/schedules/worker_launch.py`, `src/schedules/materials.py`, `src/schedules/sandbox.py` (`build_worker`), `tests/test_schedules_workers.py`, `tests/fixtures/fake_worker.py` |
| Know what each autodev stage does and how a run proceeds (the stage flows, their output schemas, the autodev mode of `pr-review`) | [Autodev flow](../flows/autodev.md) | `flows/autodev.md`, `flows/autodev-gate.md`, `flows/autodev-select.md`, `flows/autodev-implement.md`, `flows/code-review-pass.md`, `flows/change-premortem.md`, `flows/pr-review.md` (autodev mode), `scripts/dev/pr_threads.py` (`--mode autodev`), `tests/test_flows.py`, `tests/test_pr_threads.py` |
| Measure the autodev pipeline and decide what to change (run outcomes, cost, review convergence, lead time, rework) | [Autodev retrospective](../flows/autodev-retro.md) | `src/schedules/records.py`, `src/schedules/report.py`, `src/daemon/state.py` (state directory), `docs/autodev-sequence.md` (records) |
| Run the phase 1a acceptance of autodev (#349) and the phase 1b one (#350): the owner's live steps with their commands, what to record and the pass criteria, and the results table | [Autodev acceptance](autodev-acceptance.md) | `src/schedules/cli.py`, `src/schedules/runner.py`, `src/schedules/sequence.py`, `src/schedules/probes.py`, `src/schedules/broker.py`, `src/schedules/integrity.py`, `src/daemon/control.py` (`update` and the run lease) |

## Work on this repository

The session playbook, tests and documentation checks, the review, release and distribution flows.

| Need | Document | Source of truth for behavior |
|---|---|---|
| Work in this repository | [Session playbook](session-playbook.md) | Current repository and task constraints |
| Run tests | [Test guide](../tests/README.md) | `pyproject.toml`, `tests/conftest.py`, `scripts/run_tests.sh`, `tests/` |
| Read the vulnerability gate and turn on debug logging | [Development reference](development.md) | `.github/workflows/vulnerability-scan.yml`, `src/utils/debug_logger.py`, `src/engine/config.py` |
| Interpret the README quality badges and update their numeric documentation metric | [Quality checks](quality-checks.md) | `.github/workflows/shared-runtime.yml`, `.github/workflows/instruction-signing.yml`, `.github/workflows/documentation-quality.yml`, `.github/workflows/user-sync.yml`, `.github/workflows/vulnerability-scan.yml`, `scripts/check_docs.py`, `docs/badges/mcp-api-docs.svg` |
| Refresh documentation for people and AI | [Repeatable workflow](../flows/documentation-refresh.md) | The sources and checks listed in that workflow |
| Find contradictions, gaps, ambiguity, broken references and terminology drift in the instructions and documentation of the current branch | [Consistency audit flow](../flows/consistency-audit.md) | `scripts/check_docs.py` for links, and code, tests and schemas over the branch's documents, committed or not, where they disagree; a single document's claim that disagrees with code is handed to documentation refresh |
| Review and merge a PR/MR | [PR/MR review flow](../flows/pr-review.md) | Live reviews, current-head checks, and repository merge rules |
| Take several open PRs/MRs to the target branch in sequence | [PR/MR queue flow](../flows/pr-queue.md) | The chain simulation, integrated-tree runs, the target's rulesets, and `pr-review` per item |
| Close a conversation without losing its work | [Thread close flow](../flows/thread-close.md) | `scripts/dev/thread_inventory.py`, the live repository and GitHub state, `log_interaction` |
| Improve or add a skill, implant or agent, from free sources or from a defect in this installation's own run | [Open-source tuning flow](../flows/open-source-tuning.md) | `skills/README.md`, `implants/README.md`, `agents/README.md`, `agents/common/agent-schema.json`, `evals/ablation/CASES.md`, `tests/test_skill_freshness.py`, `tests/test_subagent.py` |
| Install the shared review configuration and review discipline in a repository | [Review configuration](review-config.md), [rollout flow](../flows/review-config-rollout.md) | `scripts/install_review_config.py`, `scripts/templates/review/`, `tests/test_install_review_config.py` |
| Prepare the shared calendar version and promote dev to main | [Calendar releases](releases.md) | `VERSION`, `src/calendar_version.py`, `src/version.py`, `scripts/bump_plugin_version.py`, `.github/workflows/release-version.yml`, `.github/workflows/vulnerability-scan.yml`, `scripts/release_version.py`, `tests/test_bump_plugin_version.py`, `tests/test_release_version.py`, `tests/test_version.py`, `tests/test_workflow_permissions.py`, `tests/test_vulnerability_gate.py` |
| Build, validate and release the Claude plugin | [Claude plugin](claude-plugin.md), [plugin README](../plugins/agents-core/README.md) | `VERSION`, `plugins/agents-core/`, `scripts/bump_plugin_version.py`, `.github/workflows/release-version.yml`, `tests/test_bump_plugin_version.py`, `src/server.py` (`TOOL_HINTS`), `tests/test_tool_annotations.py` |
| Find every submitted directory, its status and follow-up link | [Submission ledger](distribution.md#submitted-destinations), [MCP installation instructions](../llms-install.md) | `.claude-plugin/marketplace.json`, `glama.json`, `plugins/agents-core/`, linked directory requirements and submission records |
| Build and publish the Glama container | [Glama listing and container releases](glama.md), [submitted build fields](glama-build.json) | Glama admin build/test and release results, pinned source commit, `src/engine/readiness.py`, `src/startup.py`, `requirements.txt` |

## AI instruction sources

| File | Purpose |
|---|---|
| [AGENTS.md](../AGENTS.md) | Short entry point for AI contributors; links to the shared protocol and working instructions |
| [CLAUDE.md](../CLAUDE.md) | Tracked routing section plus repository-specific notes |
| [flows/](../flows/README.md) | Reusable task instructions invoked by file path or through `run_flow` for the caller's repository |
| [src/engine/protocol/](../src/engine/protocol/), `src/engine/protocol_text.py` | The one source of the protocol 2 text: a file per section with its variants, and the renderer of every surface below and of the procedures that the worker, cooperation and local-inference tool descriptions carry (`python -m src.engine.protocol_text --write`) |
| `src/client_instructions.py` | Decides when the MCP initialization answers a Codex session with the pointer instead of the full instructions, and a persona `SUCCESS` with the section pointer, from the managed sections of the checkouts' `AGENTS.md` files, and words the warnings about outdated sections |
| [routing-protocol-core.md](../scripts/templates/routing-protocol-core.md) | Protocol 2 client instruction template, generated |
| `src/server.py` (`FastMCP` `instructions`), `src/engine/persona.py` (`APPLY_INSTRUCTION`) | Protocol 2 summary sent at initialization (a Codex session whose checkouts hold the current section gets the pointer instead), and the apply instruction returned with a conversation's first `SUCCESS` and a restore (later ones carry the pointer, and so does every `SUCCESS` but a restore where the client holds the managed section), both rendered at import |
| [memory-routing.md](../scripts/templates/memory-routing.md) | Matching installer reminder for client memory, generated; [legacy/](../scripts/templates/legacy/README.md) keeps earlier generated copies for migration |
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
- Cold-start routing: `python -m evals.scripts.measure_cold_ranker` compares the
  keyword, embedding and combined rankers of a semantic-cache miss on the labelled
  routing sets and scores resolving the top agent directly per margin, the basis of
  `ROUTER_COLD_RESOLVE_MARGIN` ([routing](routing_flow.md#a-cache-miss),
  [`measure_cold_ranker.py`](../evals/scripts/measure_cold_ranker.py)).
- Context size: `python evals/scripts/context_size_audit.py` (`--json`,
  `--profile`, `--embedder`) reports, without a model, how many characters
  activation puts into a client's context for each `AGENTS_CLIENT_PROFILE`: the
  managed template, the server instructions and their first 4,096 characters, the
  `SUCCESS` instruction and its pointer, a `ROUTE_REQUIRED` answer, each block of a
  first activation, and each tool's description and schema
  ([`context_size_audit.py`](../evals/scripts/context_size_audit.py)).
- [Telemetry export and analysis](../evals/telemetry/README.md).
- [Dataset sources](../evals/datasets/SOURCES.md).
- [Cloud sessions](cloud-runs.md): the September 2026 eval-sweep procedure (check
  account and service setup before reusing it), the [eval routine](cloud-runs.md#eval-routine)
  of the A/B eval flow, and the maintained setup and operations reference for the
  cloud [issue agent](cloud-runs.md#issue-agent) and for a
  [cloud environment with Agents-Core](cloud-runs.md#cloud-environment-with-agents-core).

## Plans, research and recorded results

Each of these documents opens with a **Status:** line: what it is, its date, how much of
it is implemented, and, where a maintained reference exists, where current behavior is
documented. Recorded measurements of something never adopted have no such reference and
say so by leaving it out. The exception is
the two generated reports at the end of the last list: a run of the script that writes
one would overwrite such a line, so the entry notes its provenance instead. They all
retain their original scope, measurements and limitations; the maintained references
above are authoritative for current commands and behavior.

### Proposals and implementation contracts

- [MCP proxy plan](mcp-proxy-plan.md)
- [Delegation reliability plan](delegation-reliability-plan.md), with its
  [verifier accuracy measurement](verifier-accuracy-measurement.md) for validated claims
- [Autodev plan](autodev-plan.md), with its [review appendix](autodev-plan-review.md)
- [Instruction signing specification](instruction-signing-spec.md)
- [Layer sensitivity proposal](layer-sensitivity-plan.md)
- [Subagent implementation plan](subagent-implementation-plan.md)
- [Shared local inference and KV cache handoff](shared-kv-mcp/START_HERE.md)

### Implemented design records

- [Personal flow library](user-flows-design.md)
- [Persona switching plan](persona-switch-plan.md)
- [Memory subsystem specification](memory-subsystem-spec.md)

### Recorded audits and validations

- [Documentation audit 2026-10-25](documentation-audit-2026-10-25.md)
- [Documentation audit 2026-10-18](documentation-audit-2026-10-18.md)
- [Legal reference audit 2026-11-07](legal-reference-audit-2026-11-07.md)
- [Subagent validation results](subagent-validation-results.md)
- [Daemon validation](shared-mcp-daemon-validation.md)
- [Persona switching results](persona-switch-eval-results.md)

### Research and recorded measurements

- [Query decomposition research](query-decomposition-research.md)
- [Ablation results](../evals/ablation/RESULTS.md)
- [English pivot A/B](english-pivot-eval-results.md)
- [Outbound timing A/B](outbound-timing-eval-results.md)
- [Embedding models A/B](embedding-models-eval-results.md)
- [Evaluation baseline](../evals/reports/baseline.md) — the committed comparison base for
  `./scripts/eval.sh diff`, generated by `python -m evals.runners.run_all --baseline`, so it
  carries no status line of its own. The baseline in the tree is the one recorded in
  `c389797` (2026-05-04, PR #48): its title names that run's date and commit `47d195d`, and
  it does not record the embedding model behind its skill-retrieval figures, which predate
  the `harrier-oss-v1-270m` default (#178). A later run of that command replaces the file,
  its title and those figures with its own.
- [Preferred implant A/B](../evals/reports/implants_preferred_ab.md) — the run recorded in
  commit `7e6fd79` (2026-05-23), which says nothing about later revisions. It is generated
  by `evals/scripts/compare_implants.py`, which overwrites the file with a report carrying
  no run date or commit, so a regenerated file is a different run and this entry is where
  that provenance lives.

Commit hashes recorded in these reports may refer to unpublished branch
revisions; `git cat-file -t <hash>` shows whether one is available locally.

Keep dates and measured values in these reports attached to their original runs.
Do not update a historical result by replacing its counts with a new test run;
only a deliberate `./scripts/eval.sh baseline` update replaces the evaluation
baseline.
