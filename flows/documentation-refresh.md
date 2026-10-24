---
persona:
  agent: tech_writer
  skills: [skill-content-structure, skill-tech-writing, skill-fact-verification, skill-git-conventions, skill-dense-summarization]
  implants: [implant-chain-of-verification, implant-skeleton-of-thought]
---
# Refresh documentation for people and AI

This is an executable instruction for an AI working on a target repository.
When the user requests this workflow, read this file and carry it through to a
verified set of changes. Deliver current documentation and AI instructions on a
separate branch, with a short validation report.

The `tech_writer` persona contributes documentation judgment. Its own protocol
does not override this flow: documentation follows the
[language policy](#documentation-language), and commits follow
[step 5](#5-complete-the-work).

This workflow ships in the Agents-Core [model workflow catalog](README.md).
When maintaining that source catalog, store reusable task instructions in
`flows/` and update the index when adding or renaming them. Running a flow for
another repository does not require copying the catalog there.

## Documentation language

Write all repository documentation in **English**, including human guides,
READMEs, AI instructions, workflow files, plans, and reports. The language of the
user's chat does not change this requirement.
Continue to follow the active conversation language rules when replying to the
user.

Use another language only where it is necessary to preserve meaning or behavior:
verbatim quotations, language-specific examples, multilingual test inputs and
expected outputs, original names or terms, and literal source or command output.
Keep these exceptions limited to the required passages. Write the surrounding
headings, explanations, and instructions in English.

Translate existing non-English narrative within the requested scope. An older
or historical document is not exempt merely because it was written in another
language: preserve its dates, findings, measurements, and necessary quotations
while translating its explanatory text. Check each retained non-English passage
for a concrete reason to keep it.

## How to invoke

Point the AI to this file and ask:

```text
Run flows/documentation-refresh.md.
```

Optionally specify the scope, starting revision, or branch:

```text
Run flows/documentation-refresh.md for routing documentation and AI instructions.
Use origin/dev as the base and codex/docs-routing-refresh as the branch.
```

This instruction runs in the current AI session. No separate installation or
command registration is required. If the file is in another working copy, give
its absolute path and identify the target repository.
Through Agents-Core MCP, call `run_flow(flow="documentation-refresh")` with
`current_persona` and apply its `persona_activation` (`tech_writer`) as a switch
first; use the returned `repo_path` as the target. The flow's source checkout supplies instructions
and reference links, while all inspection, edits and validation use the target.

## 1. Establish the scope and starting state

1. Read the target's `AGENTS.md`, `CLAUDE.md` and documentation index when present.
   For Agents-Core, the index is the [documentation map](../docs/README.md).
   Follow the active session instructions. This workflow does not change their
   priority.
2. Check `git status --short --branch`, `git worktree list`, and the current
   `HEAD`. Record the base revision for the final comparison.
   Determine the target's integration branch from its contributor instructions;
   do not assume `main`. For the live Agents-Core installation, inspect only:
   create a linked worktree with `--no-track` from `origin/dev`, never edit or
   switch the installation checkout, and never put unpushed work in a temporary
   directory. Fetch the base when available; record an unavailable fetch rather
   than presenting an old remote-tracking ref as freshly verified.
3. If no scope is specified, review all maintained documents in the target: user
   guides, component READMEs, AI instructions, any workflows or generated
   instruction templates, and the test guide. Check plans and reports for clear
   status labels, links, and compliance with the documentation language policy.
   Inventory tracked documents even when an index exists: an index can omit new
   pages or whole features. Use `git ls-files` and include component READMEs,
   discovery files such as `llms.txt`, CLI help and public schemas where present.
   Separate maintained guidance, executable AI instructions, historical records,
   generated material and local state. Preserve historical results; label their
   revision/status and link to current guidance instead of silently changing
   recorded outcomes. Do not rewrite immutable legacy template bytes that
   migration code recognizes by exact match.
4. Use the separate branch requested by the user. Otherwise follow the target's
   branch naming rules; if none exist, create `codex/docs-refresh-<YYYYMMDD>`
   from the current `HEAD`. Add a suffix if that
   name is taken. Use a different base when the user specifies one; when it is
   a remote-tracking ref, create the branch without an upstream
   (`git switch -c <branch> --no-track <ref>` or
   `git worktree add --no-track -b <branch> <path> <ref>`). Do not reuse
   an unfamiliar branch or discard existing changes.
5. If the current checkout is busy or contains unrelated changes, create an
   isolated worktree. Continue in the task's existing dedicated branch when one
   is already available. Do not create another branch on every repeated run.
6. When the user includes work still in neighboring branches, record each PR's
   full head SHA and integration status. Verify its guide against that pinned
   source and tests, label the feature as pending in the documentation branch,
   and make commands conditional on the implementation being present. Do not
   merge the implementation merely to document it. Before release, recheck the
   final integrated source, reconcile overlapping guides and remove pending
   labels only when the recorded prerequisites are satisfied.

Use these defaults when optional details are missing. State the selected scope
and branch, then continue working.

## 2. Verify facts against the implementation

For each material discrepancy, record the document, claim, supporting source or
test, and required correction. For a broad refresh, retain a dated coverage
record with the inspected revision, subsystem/public surface, reader task,
maintained reference, source/test evidence, disposition and validation result.
Use explicit dispositions: verified unchanged, corrected, added, historical,
out of scope or unverified with a reason. A focused refresh may keep this in the
final report. File counts or a resolving link alone do not establish coverage.

Inspect in both directions:

1. **Source to documentation:** enumerate public tools, CLI commands,
   configuration/defaults, transports and shipped subsystems from their actual
   registrations and entry points. Find a maintained reference for each reader
   task. A symbol name mentioned in prose does not prove its behavior is covered.
2. **Documentation to source:** verify each material claim and example against
   current implementation and tests. Inspect recent changes since a recorded
   documentation revision when available, but do not treat an incremental diff
   as a complete audit of older guidance.

Use the repository's configured code intelligence first when available. Check
its revision, branch and freshness; use current source when an index falls back
to another branch. Do not initialize indexes or scan unrelated repositories.
Batch independent reads. Delegate disjoint areas only through the
[prepared worker contract](README.md#delegated-workers), with one integrator;
validate returned results and independently assess evidence before accepting
their coverage claims.

Build a source-to-document map from the target's actual structure, manifests,
entry points, configuration and tests. The following table applies to Agents-Core;
for another project, replace these examples with its verified sources. Do not
assume it uses Python, MCP, routing templates or the Agents-Core test commands.

| What to check | Sources |
|---|---|
| Python version, dependencies, installation, client configuration paths | `install.sh`, `pyproject.toml`, `requirements.txt`, `uv.lock`, `scripts/init_repo.sh`, `scripts/init_repo.bat`, `scripts/_helpers/`, `src/client_paths.py`, `tests/test_installer_*.py`, `tests/test_inject_mcp.py` |
| Environment variables and defaults | `env.example`, `src/engine/config.py`; also `src/daemon/` for daemon settings, `src/client_paths.py` for `CLAUDE_CONFIG_DIR` and `CODEX_HOME`, `src/model_migration.py` for the default embedding model and `EMBEDDING_MODEL_GENERATION`, `src/engine/embedding_prompts.py` for `EMBEDDING_PROMPTS`, and `src/user_flows.py` and `src/component_toggles.py` for `AGENTS_USER_FLOWS_DIR` |
| MCP tools, parameters, statuses, and slash prompts | `src/server.py`, `src/schemas/protocol.py`, `src/schemas/tool_args.py`, `src/engine/readiness.py` (`warming_up`), `tests/test_readiness.py`, `tests/test_startup_handshake.py`, `tests/test_log_interaction_contract.py`, `tests/test_log_interaction_async.py` |
| Protocol 2 and bundle assembly | `src/engine/persona.py`, `src/engine/persona_bundle.py`, `src/version.py` (footer version), `tests/test_persona_protocol.py`, `tests/test_persona_bundle.py`, `tests/test_version.py` |
| Protocol text of every surface (managed section, MCP instructions, `SUCCESS` instruction, memory reminder) | `src/engine/protocol/`, `src/engine/protocol_text.py`, `tests/test_protocol_text.py`, `tests/test_cooperation_instructions.py`, `tests/test_log_interaction_contract.py` |
| Prepared workers, model tiers and structural result validation | `src/engine/subagent.py`, `src/engine/subagent_validation.py`, `src/engine/model_tiers.py`, `src/engine/model_catalogs.py`, `src/engine/subagent_ledger.py`, `src/schemas/subagent.py`, `agents/*/subagent.mdc`, `rules/subagents/`, `tests/test_subagent*.py` |
| Exact shared artifacts and pinned task manifests | `src/artifacts/`, `src/schemas/artifacts.py`, `src/engine/subagent.py`, `src/local_inference/service.py`, `tests/test_artifact*.py`, `tests/test_subagent_artifacts.py` |
| Routing, skills, implants, and rules | `src/engine/router.py`, `src/engine/enrichment.py`, `src/engine/skills.py`, `src/engine/implants.py`, `src/engine/rules.py`, `src/component_toggles.py` (web UI switches), `tests/test_component_toggles.py` |
| Embedding model, its prompts and the switch on update | `src/engine/embedder.py`, `src/engine/embedding_prompts.py`, `src/engine/fingerprint.py`, `src/model_migration.py`, `src/startup.py`, `tests/test_embedder.py`, `tests/test_embedding_prompts.py`, `tests/test_model_migration.py` |
| Agent catalog and metadata | `agents/*/system_prompt.mdc`, `agents/common/agent-schema.json`, `scripts/validate_agents.py` |
| Memory, history, and workspace isolation | `src/memory/`, `src/daemon/`, `src/server.py` (`describe_repo`, `write_repo_summary`), `tests/test_describer.py`, `tests/test_server_describe.py`, `tests/test_managed_section.py`, `tests/test_per_repo_memory.py`, `tests/test_daemon.py` |
| Agent cooperation, mailbox notifications and request tracking | `src/cooperation/`, `src/server.py`, `docs/agent-cooperation/runtime.md`, `docs/agent-cooperation/schemas.json`, `rules/rule-agent-cooperation.mdc`, `rules/subagents/rule-worker-cooperation.mdc`, `tests/test_cooperation_*.py`, `.github/workflows/shared-runtime.yml` |
| Instruction-signing primitives and planned enforcement | `src/instruction_signing/`, `docs/instruction-signing-core.md`, `docs/instruction-signing-spec.md`, `tests/test_instruction_signing.py`, `tests/test_instruction_signing_interop.py`, `tests/test_instruction_inventory.py`, `.github/workflows/instruction-signing.yml` |
| Stdio client repository root | `src/engine/config.py` (`get_client_repo_root`, `client_root_from_workspace`), `src/daemon/workspaces.py` (`client_context`, `resolve_client_context`, `workspace_inputs`), `tests/test_config_client_root.py`, `tests/test_persona_protocol.py` |
| Built-in, personal, and repository flows; flow personas; flow editor | `flows/*.md`, `src/flows.py`, `src/user_flows.py`, `src/flow_persona.py`, `src/component_catalog.py`, `src/component_toggles.py`, `src/daemon/flows_ui.py`, `src/daemon/flows_ui.html`, `src/daemon/peer.py`, `scripts/dev/`, `tests/test_flows.py`, `tests/test_user_flows.py`, `tests/test_server_flows.py`, `tests/test_flows_ui_page.py`, `tests/test_daemon_peer.py`, `tests/test_thread_inventory.py` |
| Cloud issue agent bridge | `scripts/templates/issue-agent-bridge.yml`, `.github/workflows/issue-agent-bridge.yml`, `tests/test_issue_agent_bridge.py` |
| Claude Code cloud environment | `scripts/setup_cloud_env.sh`, `tests/test_setup_cloud_env.py` |
| Updates and the Node stdio bridge | `src/self_update.py`, `src/startup.py`, `src/model_migration.py`, `src/daemon/autoupdate.py`, `src/daemon/update.py`, `bridge/`, `tests/test_self_update.py`, `tests/test_startup.py`, `tests/test_model_migration.py`, `tests/test_daemon_autoupdate.py`, `tests/test_daemon_update.py` |
| Update channels and release promotion | `VERSION`, `src/calendar_version.py`, `src/version.py`, `pyproject.toml`, `uv.lock`, `src/update_channel.py`, `src/env_file.py`, `src/daemon/updates_ui.py`, `scripts/bump_plugin_version.py`, `scripts/release_version.py`, `plugins/agents-core/.claude-plugin/plugin.json`, `.github/workflows/release-version.yml`, `tests/test_bump_plugin_version.py`, `tests/test_version.py`, `tests/test_update_channel.py`, `tests/test_env_file.py`, `tests/test_updates_ui.py`, `tests/test_release_version.py` |
| URL rules and personal library synchronization | `src/user_rules.py`, `src/rule_sources.py`, `src/user_sync/`, `src/user_library.py`, `src/user_flows.py`, `tests/test_user_rules.py`, `tests/test_rule_sources.py`, `tests/test_user_sync*.py` |
| Optional local inference, native backends and Redis snapshots | `src/local_inference/`, `src/daemon/app.py`, `src/server.py`, `tests/test_local_inference_*.py`, `tests/test_optional_inference_disabled.py`; distinguish deterministic fake-adapter checks from authorized native/live acceptance |
| Autodev scheduling, stages, brokers and sandboxed workers | `src/schedules/`, `src/flow_execution.py`, `src/daemon/autodev_ui.py`, `flows/autodev*.md`, `flows/code-review-pass.md`, `flows/change-premortem.md`, `flows/pr-review.md`, `tests/test_schedules_*.py`, `tests/test_flow_execution.py`; distinguish implemented phases, pending owner acceptance and future plans |
| Readiness hooks and compaction recovery | `scripts/session_readiness.py`, `scripts/session_readiness_mcp.py`, `scripts/install_session_readiness.py`, `tests/test_session_readiness.py` |
| Thread-title status and client lifecycle adapters, when included in the target revision | `src/thread_status.py`, `src/codex_thread_titles.py`, `scripts/thread_status.py`, `scripts/install_thread_status.py`, `rules/rule-thread-status.mdc`, formatter/installer/metadata tests; distinguish native title writes, persisted metadata and verified visible refresh |
| HTTP MCP proxy and per-host connection inventory, when included in the target revision | `src/mcp_registry.py`, `src/mcp_secrets.py`, `src/daemon/mcp_proxy/`, `src/daemon/mcp_ui.py`, `src/daemon/mcp_cli.py`, `src/user_sync/`, MCP registry/exposure/upstream/host/sync tests; distinguish shared definitions, local consent, host metadata and local-only credentials |
| Distribution, plugin metadata, optional servers and evaluation entry points | `plugins/agents-core/`, `.claude-plugin/`, `glama.json`, `src/mcp_servers/`, `scripts/eval.sh`, `evals/`, `docs/distribution.md`; external listing acceptance needs independent current evidence |
| Generated instructions and Codex discovery | `scripts/install_instructions.py`, `scripts/templates/`, `scripts/_helpers/install_codex_instructions.py`, `scripts/_helpers/inject_claude_md.py`, `scripts/_helpers/migrate_routing_memory.py`, `tests/test_install_instructions.py`, `tests/test_installer_instructions.py`, `tests/test_codex_instructions.py`, `tests/test_protocol_migration.py` |
| Validation commands | `pyproject.toml`, `requirements.txt`, `tests/conftest.py`, `scripts/run_tests.sh`, `.github/workflows/*.yml`, `tests/test_*.py` |

Verify names, paths, parameters, versions, defaults, and examples. Distinguish
installer defaults from API defaults, settings of a particular installation from
code defaults, and stdio behavior from HTTP behavior. A document is not evidence
of its own accuracy. Record disagreements between code and tests; do not change
product behavior merely to match the text.

## 3. Update the documents together

- **For people:** correct installation steps, commands, configuration,
  architecture, limitations, and links in the affected guides. Add new pages
  to the documentation map.
- **For model workflows:** preserve the target's established workflow location
  and index. Use `flows/` and `flows/README.md` when introducing workflows into
  a project without a convention, or when maintaining Agents-Core. Update
  invocation examples and incoming links when moving or renaming a workflow.
- **For AI:** check that the target's AI entry points agree, repository
  notes describe real modules, and any generated instruction templates agree
  with the code.
  Avoid maintaining separate copies of the full protocol in multiple documents.
- **Agents-Core only:** the protocol text has one source, the section files in
  `src/engine/protocol/`. Edit the section that states a rule, never a generated
  surface, then regenerate the template `routing-protocol-core.md`, the memory
  reminder and the managed section in this checkout:

  ```bash
  .venv/bin/python -m src.engine.protocol_text --write
  ```

  The command needs only the standard library; in a worktree without `.venv`, run
  `python3 -m src.engine.protocol_text --write` from the worktree root (Windows:
  `py -3`). When it replaces the memory reminder, it first keeps the released one
  (the reminder at the merge base with `dev`) as the next
  `scripts/templates/legacy/memory-routing-v<N>.md` and prints its name.
  The MCP initialization instructions and `APPLY_INSTRUCTION` are rendered from
  the same sections at import. Review the diff of every generated surface. Do not
  run the global installer just to synchronize Markdown. Change protocol text
  only when a contract change is in scope, then run the protocol checks in
  [step 4](#4-validate-the-result), including `tests/test_protocol_text.py`.
- **Agents-Core only:** when a change regenerates `memory-routing.md`, check that
  `--write` reported a new `scripts/templates/legacy/memory-routing-v<N>.md`, and add
  that file where tests list the existing legacy copies
  (`grep -rn memory-routing-v tests/`). The migration helper reads
  only `legacy/memory-routing-*.md` and recognizes generated reminders by exact
  match, so a template edit must not turn the previous generated file into
  unrecognized user content. When the `MEMORY.md` index line (`INDEX_ENTRY` in
  `scripts/_helpers/migrate_routing_memory.py`) changes, add the previous line
  to `LEGACY_INDEX_ENTRIES`. If migration logic needs to change, include that
  change explicitly in the scope and validation.
- Apply the [documentation language policy](#documentation-language) to every
  document in scope. Preserve a consistent style while translating non-English
  narrative. Keep historical plans tied to their original scope and add links
  to current guidance where needed.
- Avoid undated test counts, run durations, and catalog sizes. In reports, tie
  these values to the actual run and revision.
- Exclude `.env`, conversation journals, user MCP configurations, generated
  indexes, and local ignored documents from the commit.

## 4. Validate the result

Run the target project's checks from the worktree root. For Agents-Core, use its
Python environment; if reusing another checkout's environment, supply the
interpreter's absolute path and verify
that `src` imports resolve to the current worktree. Dependency setup is described
in the [test guide](../tests/README.md).

Minimum checks for every run:

```bash
git diff --check
git diff --stat
git status --short
```

Read the full diff, including new files: ordinary `git diff` omits untracked files.
Check local Markdown links and anchors, referenced paths, and examples against
function signatures and CLI options. Check that documentation prose is English
and each passage in another language is necessary under the language policy;
translate any passage that does not qualify.

For Agents-Core, run the offline navigation and public-tool index check from the
worktree root using the project environment:

```bash
.venv/bin/python scripts/check_docs.py
```

The helper uses `markdown-it-py` from the project environment. Its default file
set is tracked Markdown; pass newly created paths explicitly before staging,
then run the default check again once they are tracked. It checks Markdown
links/images, local heading/HTML anchors and the
[public MCP tool index](../docs/mcp-tools.md) against source registrations. It
does not fetch external URLs, check arbitrary HTML navigation or prove that the
linked prose accurately covers an interface. Review those separately and record
external claims that could not be confirmed.
The [quality guide](../docs/quality-checks.md) defines the README badges. After
adding/removing a main-server tool or correcting its index coverage, explicitly
regenerate its source-derived SVG with `--update-badge` and review that diff.
The default checker and documentation CI fail on stale badge data and never
rewrite it automatically.

Execute safe validation commands. Verify installation, migration, update, and
restart commands against source and tests without running them on the live
installation.

Choose additional checks based on the changed contract and the target's own
contributor guide or CI. These commands apply to Agents-Core:

| Change | Check |
|---|---|
| Routing instructions, Codex discovery, managed sections, reminders | `LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/test_protocol_text.py tests/test_cooperation_instructions.py tests/test_install_instructions.py tests/test_installer_instructions.py tests/test_codex_instructions.py tests/test_protocol_migration.py tests/test_installer_profiles.py -q` |
| Agent catalog or metadata | `.venv/bin/python scripts/validate_agents.py`; compare catalog rows with `identity` and `routing` metadata |
| Protocol or bundle assembly documentation | `LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/test_persona_protocol.py tests/test_persona_bundle.py -q` |
| Another single area, such as installers, flows, the issue agent bridge, the Node bridge or updates | The focused checks in [Choose tests by change](../tests/README.md#choose-tests-by-change) |
| Documentation checker or workflow changes | `.venv/bin/python -m pytest tests/test_check_docs.py tests/test_flows.py -q`; retain safe import isolation when reusing an environment |
| Behavior changes or several related subsystems | The regular suite described in `tests/README.md` |

For editorial changes, checking links, examples, language, and the diff is enough.
Run only one heavy test or embedding process at a time. Do not run paid evaluations
or install the daemon to validate prose.

If a check is unavailable or fails, investigate within the task's scope.
Distinguish a pre-existing issue from a problem introduced by your edits. Never
report an unperformed check as successful.

## 5. Complete the work

The workflow is complete when:

- Human documentation and AI instructions agree with verified sources in the
  selected scope.
- The coverage record accounts for every public surface/subsystem inventoried
  in scope, including verified unchanged areas and explicit unresolved gaps.
  A working link or a completed worker alone is not evidence of semantic coverage.
- Documentation in scope is in English, with other languages retained only in
  necessary passages covered by the language policy.
- New pages are reachable through the documentation map and relevant entry points.
- Links, examples, and appropriate checks pass, or the report clearly describes
  the remaining limitations.
- Changes are on a separate branch and unrelated work is preserved.

In the final response, state the branch and worktree path, main corrections, a
link to this file for future runs, validation results, and remaining issues.
For a broad refresh, link the dated coverage record and identify the inspected
base revision. Distinguish mechanical checks, source review and executed behavior
tests; never infer native/live acceptance from deterministic fixtures. If this
flow itself changed, execute its updated steps and review the final instructions
as well as the documentation they produced.
Commit, push, and create a PR when those actions are included in the user's
request. Before committing, review the staged diff and follow the target's commit
convention. In Agents-Core, use a Conventional Commit with type `docs` for
documentation changes.

If a later run finds that the documentation is already current, report what was
checked and that no discrepancies were found. Do not create redundant changes
or commits.
