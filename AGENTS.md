# Working on Agents-Core

Read [CLAUDE.md](CLAUDE.md) before starting work. Its managed section contains
the repository's persona protocol; its Repository notes describe the code. Use
that section as the shared routing contract for this checkout, including in
clients that discover `AGENTS.md` rather than `CLAUDE.md`. Do not maintain a second
copy here. Explicit session instructions and higher-priority instructions still
take precedence.

## Find the right source

- [README.md](README.md): installation, configuration and user-facing behavior.
- [Documentation map](docs/README.md): maintained references and historical plans.
- [Model workflows](flows/README.md): reusable task instructions to execute on request.
- [Routing reference](docs/routing_flow.md): protocol 2, client compatibility and migration.
- [Session playbook](docs/session-playbook.md): worktrees, reviews and evaluations.
- [Tests](tests/README.md): setup, focused checks and the regular suite.

Verify behavior against source and tests. Plans and old evaluation reports describe
their recorded revision; they do not override the current implementation.

## Make changes

- Inspect the branch, worktrees and existing changes first. Keep unrelated work
  intact; use an isolated worktree when the current checkout is busy.
- Use a `codex/` branch for new work unless the user or the invoked flow specifies
  another name; for example, [issue implementation](flows/issue-implementation.md)
  requires `claude/issue-<number>-<short-slug>`.
- Put reusable task instructions for models in `flows/<descriptive-name>.md`
  and add them to [flows/README.md](flows/README.md). Follow the selected flow
  when the user invokes it by path.
- MCP exposes installed flows through `list_flows()` and `run_flow(...)`.
  When a user requests one for another repository, execute the returned bundle
  against its `repo_path`. Source files come from the MCP installation; target
  instructions, edits and checks belong to the caller. `needs_execution` requires
  the current model to continue the work; it is not a completion result. Apply a
  `persona_activation` that `run_flow` returns as a switch before executing.
  Personal (`user:`) and repository (`repo:`) flows live in the installation's
  git-ignored `flows/.user`; `get_flow`, `save_flow` and `delete_flow` manage
  them, and `set_flow_persona` stores a flow's agent and components there,
  without touching tracked flows or the caller's working tree.
- Write all repository documentation in English, including AI instructions,
  plans, and reports. Retain other languages only for necessary passages such as
  verbatim quotations, language-specific examples or test data, and original
  names or literal output. Keep surrounding prose in English. Follow the
  [documentation language policy](flows/documentation-refresh.md#documentation-language).
- Update documentation when changing a public tool, configuration default,
  installer, command or AI instruction. The source-to-document map and review
  steps are in [documentation-refresh.md](flows/documentation-refresh.md).
- The source of the managed routing section is
  [routing-protocol-core.md](scripts/templates/routing-protocol-core.md).
  Edit the template first when changing that contract, then update the managed
  section with `scripts/_helpers/inject_claude_md.py`. A contract change also
  updates the MCP initialization instructions in `src/server.py`,
  `APPLY_INSTRUCTION` in `src/engine/persona.py` and, where it restates the
  change, [memory-routing.md](scripts/templates/memory-routing.md), after saving
  its previous copy as described in
  [legacy/README.md](scripts/templates/legacy/README.md). No test compares these
  restatements with the template. Keep repository notes and Repository Memory
  outside the routing markers.
- Treat `.env`, MCP client registrations, memory journals and generated indexes
  as local state. A documentation update does not require reinstalling clients
  or restarting the shared daemon.
- Run checks appropriate to the changed contract and report their actual result.
  Run only one heavy test or embedding process at a time.
- For PR/MR submission and review, follow [flows/pr-review.md](flows/pr-review.md):
  keep the English description aligned with every commit, handle review threads,
  and obtain fresh bot reviews before merging under its completion conditions.

## Repeat the documentation update

When the user points to [flows/documentation-refresh.md](flows/documentation-refresh.md),
read it and execute the workflow. It covers both human documentation and AI
instructions and finishes with a reviewable diff on a separate branch.
