# Refresh documentation for people and AI

This is an executable instruction for an AI working on the Agents-Core repository.
When the user requests this workflow, read this file and carry it through to a
verified set of changes. Deliver current documentation and AI instructions on a
separate branch, with a short validation report.

This workflow is part of the [model workflow catalog](README.md). Store reusable
task instructions in `flows/` and keep the catalog up to date when adding or
renaming them.

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

## 1. Establish the scope and starting state

1. Read `AGENTS.md` and `CLAUDE.md`, then the [documentation map](../docs/README.md).
   Follow the active session instructions. This workflow does not change their
   priority.
2. Check `git status --short --branch`, `git worktree list`, and the current
   `HEAD`. Record the base revision for the final comparison.
3. If no scope is specified, review all maintained documents in the map: user
   guides, component READMEs, AI instructions, workflows in `flows/`,
   routing/memory templates, and the test guide. Check plans and reports for clear
   status labels, links, and compliance with the documentation language policy.
   Preserve their historical results.
4. Use the separate branch requested by the user. By default, create
   `codex/docs-refresh-<YYYYMMDD>` from the current `HEAD`; add a suffix if that
   name is taken. Use a different base when the user specifies one. Do not reuse
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

| What to check | Sources |
|---|---|
| Python version, dependencies, installation | `pyproject.toml`, `requirements.txt`, `uv.lock`, `scripts/init_repo.sh`, `scripts/init_repo.bat` |
| Environment variables and defaults | `env.example`, `src/engine/config.py`; also `src/daemon/` for daemon settings |
| MCP tools, parameters, statuses, and slash prompts | `src/server.py`, `src/schemas/protocol.py` |
| Protocol 2, bundle assembly, and v1 compatibility | `src/engine/persona.py`, `src/engine/persona_bundle.py`, `tests/test_persona_protocol.py`, `tests/test_persona_bundle.py` |
| Routing, skills, implants, and rules | `src/engine/router.py`, `src/engine/enrichment.py`, `src/engine/skills.py`, `src/engine/implants.py`, `src/engine/rules.py` |
| Agent catalog and metadata | `agents/*/system_prompt.mdc`, `agents/common/agent-schema.json`, `scripts/validate_agents.py` |
| Memory, history, and workspace isolation | `src/memory/`, `src/daemon/`, `tests/test_per_repo_memory.py`, `tests/test_daemon.py` |
| Generated instructions and Codex discovery | `scripts/templates/`, `scripts/_helpers/install_codex_instructions.py`, `scripts/_helpers/inject_claude_md.py`, `scripts/_helpers/migrate_routing_memory.py`, `tests/test_codex_instructions.py`, `tests/test_protocol_migration.py` |
| Validation commands | `pyproject.toml`, `tests/conftest.py`, `scripts/run_tests.sh`, `tests/test_*.py` |

Verify names, paths, parameters, versions, defaults, and examples. Distinguish
installer defaults from API defaults, settings of a particular installation from
code defaults, and stdio behavior from HTTP behavior. A document is not evidence
of its own accuracy. Record disagreements between code and tests; do not change
product behavior merely to match the text.

## 3. Update the documents together

- **For people:** correct installation steps, commands, configuration,
  architecture, limitations, and links in the affected guides. Add new pages
  to the documentation map.
- **For model workflows:** keep reusable task instructions in `flows/`, register
  them in `flows/README.md`, and update invocation examples and incoming links
  whenever a workflow moves or is renamed.
- **For AI:** check that `AGENTS.md` points to one shared protocol, repository
  notes describe real modules, and routing/memory templates agree with the code.
  Avoid maintaining separate copies of the full protocol in multiple documents.
- When shared routing text changes, edit its source in `scripts/templates/`
  first. When changing the default `routing-protocol-core.md` template, synchronize
  the managed section in this checkout, which uses protocol 2:

  ```bash
  .venv/bin/python scripts/_helpers/inject_claude_md.py CLAUDE.md scripts/templates/routing-protocol-core.md
  ```

  Editing the v1 compatibility template alone does not require switching the
  checkout to v1. Preserve a different protocol explicitly selected by the user.
  The helper preserves other text and creates a backup when it makes a change.
  Review the diff and exclude the backup from the changes. Do not run the global
  installer just to synchronize Markdown.
- When changing `memory-routing-v1.md` or `memory-routing-v2.md`, verify migration
  of the previous generated reminder: the helper recognizes known text by exact
  match. A template edit must not turn the previous generated file into
  unrecognized user content. If migration logic needs to change, include that
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

Run commands from the worktree root. Use its Python environment; if reusing
another checkout's environment, supply the interpreter's absolute path and verify
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

Choose additional checks based on the changed contract:

| Change | Check |
|---|---|
| Routing instructions, Codex discovery, managed sections, reminders | `LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/test_codex_instructions.py tests/test_protocol_migration.py tests/test_managed_section.py -q` |
| Agent catalog or metadata | `.venv/bin/python scripts/validate_agents.py`; compare catalog rows with `identity` and `routing` metadata |
| Protocol or bundle assembly documentation | `LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/test_persona_protocol.py tests/test_persona_bundle.py -q` |
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
request. Before committing, review the staged diff and use a Conventional Commit
with type `docs` for documentation changes.

If a later run finds that the documentation is already current, report what was
checked and that no discrepancies were found. Do not create redundant changes
or commits.
