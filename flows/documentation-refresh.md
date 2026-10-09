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
Use main as the base and codex/docs-routing-refresh as the branch.
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
3. If no scope is specified, review all maintained documents in the target: user
   guides, component READMEs, AI instructions, any workflows or generated
   instruction templates, and the test guide. Check plans and reports for clear
   status labels, links, and compliance with the documentation language policy.
   If no index exists, inventory the tracked documents before making changes.
   Preserve their historical results.
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

Use these defaults when optional details are missing. State the selected scope
and branch, then continue working.

## 2. Verify facts against the implementation

For each material discrepancy, record the document, claim, supporting source or
test, and required correction. A working table in your notes is sufficient; a
separate report in the repository is optional.

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
| Routing, skills, implants, and rules | `src/engine/router.py`, `src/engine/enrichment.py`, `src/engine/skills.py`, `src/engine/implants.py`, `src/engine/rules.py`, `src/component_toggles.py` (web UI switches), `tests/test_component_toggles.py` |
| Embedding model, its prompts and the switch on update | `src/engine/embedder.py`, `src/engine/embedding_prompts.py`, `src/engine/fingerprint.py`, `src/model_migration.py`, `src/startup.py`, `tests/test_embedder.py`, `tests/test_embedding_prompts.py`, `tests/test_model_migration.py` |
| Agent catalog and metadata | `agents/*/system_prompt.mdc`, `agents/common/agent-schema.json`, `scripts/validate_agents.py` |
| Memory, history, and workspace isolation | `src/memory/`, `src/daemon/`, `tests/test_per_repo_memory.py`, `tests/test_daemon.py` |
| Stdio client repository root | `src/engine/config.py` (`get_client_repo_root`, `client_root_from_workspace`), `src/daemon/workspaces.py` (`client_context`, `resolve_client_context`, `workspace_inputs`), `tests/test_config_client_root.py`, `tests/test_persona_protocol.py` |
| Built-in, personal, and repository flows; flow personas; flow editor | `flows/*.md`, `src/flows.py`, `src/user_flows.py`, `src/flow_persona.py`, `src/component_catalog.py`, `src/component_toggles.py`, `src/daemon/flows_ui.py`, `src/daemon/flows_ui.html`, `src/daemon/peer.py`, `scripts/dev/`, `tests/test_flows.py`, `tests/test_user_flows.py`, `tests/test_server_flows.py`, `tests/test_flows_ui_page.py`, `tests/test_daemon_peer.py`, `tests/test_thread_inventory.py` |
| Cloud issue agent bridge | `scripts/templates/issue-agent-bridge.yml`, `.github/workflows/issue-agent-bridge.yml`, `tests/test_issue_agent_bridge.py` |
| Claude Code cloud environment | `scripts/setup_cloud_env.sh`, `tests/test_setup_cloud_env.py` |
| Updates and the Node stdio bridge | `src/self_update.py`, `src/startup.py`, `src/model_migration.py`, `src/daemon/autoupdate.py`, `src/daemon/update.py`, `bridge/`, `tests/test_self_update.py`, `tests/test_startup.py`, `tests/test_model_migration.py`, `tests/test_daemon_autoupdate.py`, `tests/test_daemon_update.py` |
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
- **Agents-Core only:** when shared routing text changes, edit its source in
  `scripts/templates/`
  first. When changing the `routing-protocol-core.md` template, synchronize
  the managed section in this checkout:

  ```bash
  .venv/bin/python scripts/_helpers/inject_claude_md.py CLAUDE.md scripts/templates/routing-protocol-core.md
  ```

  The helper uses only the standard library; in a worktree without `.venv`, run
  it with `python3` (Windows: `py -3`). It preserves other text and creates a
  backup when it makes a change.
  Review the diff and exclude the backup from the changes. Do not run the global
  installer just to synchronize Markdown.
  The MCP initialization instructions in `src/server.py` and `APPLY_INSTRUCTION`
  in `src/engine/persona.py` restate the same contract for MCP clients: compare
  them with `routing-protocol-core.md` and report any disagreement. Change them
  only when a contract change is in scope, then run the protocol checks in
  [step 4](#4-validate-the-result).
- **Agents-Core only:** when changing `memory-routing.md`, first copy its
  previous bytes into `scripts/templates/legacy/` as the next unused
  `memory-routing-v<N>.md`, and add that file where tests list the existing
  legacy copies (`grep -rn memory-routing-v tests/`). The migration helper reads
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

Execute safe validation commands. Verify installation, migration, update, and
restart commands against source and tests without running them on the live
installation.

Choose additional checks based on the changed contract and the target's own
contributor guide or CI. These commands apply to Agents-Core:

| Change | Check |
|---|---|
| Routing instructions, Codex discovery, managed sections, reminders | `LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/test_install_instructions.py tests/test_installer_instructions.py tests/test_codex_instructions.py tests/test_protocol_migration.py tests/test_installer_profiles.py -q` |
| Agent catalog or metadata | `.venv/bin/python scripts/validate_agents.py`; compare catalog rows with `identity` and `routing` metadata |
| Protocol or bundle assembly documentation | `LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/test_persona_protocol.py tests/test_persona_bundle.py -q` |
| Another single area, such as installers, flows, the issue agent bridge, the Node bridge or updates | The focused checks in [Choose tests by change](../tests/README.md#choose-tests-by-change) |
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
- Documentation in scope is in English, with other languages retained only in
  necessary passages covered by the language policy.
- New pages are reachable through the documentation map and relevant entry points.
- Links, examples, and appropriate checks pass, or the report clearly describes
  the remaining limitations.
- Changes are on a separate branch and unrelated work is preserved.

In the final response, state the branch and worktree path, main corrections, a
link to this file for future runs, validation results, and remaining issues.
Commit, push, and create a PR when those actions are included in the user's
request. Before committing, review the staged diff and follow the target's commit
convention. In Agents-Core, use a Conventional Commit with type `docs` for
documentation changes.

If a later run finds that the documentation is already current, report what was
checked and that no discrepancies were found. Do not create redundant changes
or commits.
