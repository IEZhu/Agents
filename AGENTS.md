# Working on Agents-Core

Read [CLAUDE.md](CLAUDE.md) before starting work. Its managed section contains
the repository's persona protocol; its Repository notes describe the code. Use
that section as the shared routing contract for this checkout, including in
clients that discover `AGENTS.md` rather than `CLAUDE.md`. Re-read the managed
section before the first answer and after any context compaction. Do not maintain
a second copy here. Explicit session instructions and higher-priority instructions
still take precedence.

## Find the right source

- Before the first repository exploration or edit, call `describe_repo` with the
  target checkout's `repo_path` and the caller's authorized `workspace`. Read its
  summary preview or context bundle. On `needs_summary`, follow the returned
  persistence instructions only within the authorized scope; preserve
  `workspace_id`, `repo_path` and `repo_hash` unchanged when writing. Never
  persist a summary into the live installation or deployed runtime source.
- [README.md](README.md): installation, configuration and user-facing behavior.
- [Documentation map](docs/README.md): maintained references and historical plans.
- [Model workflows](flows/README.md): reusable task instructions to execute on request.
- [Routing reference](docs/routing_flow.md): protocol 2, client compatibility and migration.
- [Session playbook](docs/session-playbook.md): worktrees, reviews and evaluations.
- [Tests](tests/README.md): setup, focused checks and the regular suite.

Verify behavior against source and tests. Plans and old evaluation reports describe
their recorded revision; they do not override the current implementation.

## Make changes

- Inspect the branch, worktrees and existing changes first, and keep unrelated
  work intact.
- Never change a live installation checkout in place. The daemon runs from it,
  and the updaters fast-forward it only while it is on
  `AGENTS_AUTO_UPDATE_BRANCH` (its update channel in `.env`) with no tracked
  changes, switching to it only from the previous setting, so a feature branch or
  a tracked edit there silently stops updates, and an untracked file that an
  incoming commit adds makes the fast-forward fail. Do not edit, commit, stash,
  reset or switch branches in it; reading, running its tools, `git fetch` and
  `git worktree add` are fine. If it is already off its update branch, has
  tracked changes or holds untracked files an update would overwrite, tell the
  user instead of repairing it.
- Make every change in a linked worktree on its own branch, cut from `origin/dev`
  with `--no-track`, for example
  `git worktree add --no-track -b codex/<slug> .worktrees/<slug> origin/dev`.
  Keep worktrees out of temporary directories and session scratchpads, which get
  cleaned with any unpushed work. Open the PR against `dev`, the integration
  branch; `main` takes release PRs from `dev`
  ([release procedure](docs/releases.md#procedure)). Remove your worktree once its work
  is pushed or merged and it is clean.
- For Teams/Hosted work, use `origin/team` as the feature branch base and `team`
  as the PR target; promotion into `dev` is a separate reviewed PR, as described
  in the [release procedure](docs/releases.md#procedure).
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
- The protocol text has one source, the section files in
  [src/engine/protocol/](src/engine/protocol/), composed by
  `src/engine/protocol_text.py` into every surface: the template
  [routing-protocol-core.md](scripts/templates/routing-protocol-core.md) (the
  managed section of `CLAUDE.md` and `AGENTS.md`), the MCP initialization
  instructions, the `instruction` of a `SUCCESS`, the pointers sent instead of
  a text the client already holds (#433) and the installer's
  [memory-routing.md](scripts/templates/memory-routing.md). Edit the section
  that states the rule, never a generated surface, then run
  `.venv/bin/python -m src.engine.protocol_text --write`, which regenerates the
  template, the reminder and this checkout's managed section. On every run, not
  only when the reminder changes, the command keeps the released reminder (the
  one at the merge base with `dev`) as the next legacy copy when migration would
  not recognize it ([legacy/README.md](scripts/templates/legacy/README.md)); add
  any name it prints where the tests list legacy reminders.
  `tests/test_protocol_text.py` fails on a stale file and on a sentence the
  source states in two places, `tests/test_log_interaction_contract.py` on a
  server instruction that leaves Claude Code's windows, and
  `tests/test_instruction_pointers.py` when a pointer of
  `src/client_instructions.py` does not name a rule of the full instruction.
  Keep repository notes and Repository Memory outside the routing markers.
- Treat `.env`, MCP client registrations, memory journals and generated indexes
  as local state. A documentation update does not require reinstalling clients
  or restarting the shared daemon.
- Run checks appropriate to the changed contract and report their actual result.
  Run only one heavy test or embedding process at a time.
- In GitHub issue and PR descriptions, keep each prose paragraph and list item on one physical line. Do not hard-wrap text to a fixed width; preserve meaningful paragraph, list, table and code-block boundaries.
- Before the first PR/MR submission or review action, call `list_flows`, then
  `run_flow(flow="builtin:pr-review", ...)` with the target worktree's
  `repo_path`, authorized `workspace`, retained `current_persona` and the user's
  constraints, including `no-merge` when only submission is requested. Apply its
  `persona_activation` through the shared routing contract. If that flow is
  already running for this target, continue it. Reading the Markdown alone does
  not activate the flow.
- For PR/MR submission and review, follow [flows/pr-review.md](flows/pr-review.md):
  keep the English description aligned with every commit, handle review threads,
  and obtain fresh bot reviews before merging under its completion conditions.

## Repeat the documentation update

When the user points to [flows/documentation-refresh.md](flows/documentation-refresh.md),
read it and execute the workflow. It covers both human documentation and AI
instructions and finishes with a reviewable diff on a separate branch.
