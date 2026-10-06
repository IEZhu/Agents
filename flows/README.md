# Model workflows

`flows/` contains reusable Markdown instructions that a model follows to complete
a defined task. Each flow describes its inputs, execution steps, checks, and
expected result. Start with [AGENTS.md](../AGENTS.md) for repository instructions
and the [documentation map](../docs/README.md) for supporting references.

## Available flows

| Flow | Purpose | Result |
|---|---|---|
| [Documentation refresh](documentation-refresh.md) | Check and update documentation for people and AI against the implementation | Reviewed documentation changes on a separate branch, with validation results |
| [PR/MR review](pr-review.md) | Review the diff, handle bot findings one push per round until they converge, and keep the description current | A merged request or a precise blocker, with a report in the invocation language |
| [Issue agent](issue-agent.md) | Verify an owner's `/agent` comment, keep trusted state in the issue, and dispatch; bot reviews never trigger a session | The command's result and an updated state comment |
| [Issue plan](issue-plan.md) | `/agent plan` / `replan`: validate requirements, ask questions, write a versioned plan with a pre-mortem | A Markdown plan comment or open questions |
| [Issue implementation](issue-implementation.md) | `/agent run_plan` / `run`: implement, self-review, independent review, pre-mortem, PR and the full bot review cycle in the same session | A pull request with review results or a precise blocker, left unmerged |
| [Thread close](thread-close.md) | Close a conversation: inventory what it produced, verify each result against the current state with evidence levels (V0 claimed to V4 accepted), audit the thread for its own errors and secure unsaved work | A report with per-artifact levels; in apply mode one `#thread-close` history entry and memory updates at V2 or above |
| [A/B eval](ab-eval.md) | Evaluate a change (rule, skill, implant, prompt, setting or embedding model) on hosted models and Opus with blind judging and statistics | A recorded result, a recommendation and an unmerged pull request |

Built-in flows declare their persona: `documentation-refresh` runs as
`tech_writer`, `issue-plan` as `system_architect`, `issue-implementation` as
`software_engineer`, `pr-review` as `code_reviewer`, `thread-close` as
`investigative_analyst` and `ab-eval` as `ai_senior_engineer`, each with an exact list of skills and implants (see
[choosing a flow's agent](#choose-a-flows-agent-and-components)). The
`issue-agent` dispatcher has none.

The three issue flows run in a Claude Code cloud routine that a GitHub Actions
bridge starts for the owner's `/agent` comments, not from a local request. See
[issue agent setup](../docs/cloud-runs.md#issue-agent).

## Run a flow

Point the model to the file in the repository and ask it to execute the workflow:

```text
Run flows/documentation-refresh.md.
```

To handle a pull request or merge request through merge:

```text
Run flows/pr-review.md for <PR or MR URL>.
```

Add `no-merge` to stop before merging.

Add optional scope, a starting revision, or a branch when needed:

```text
Run flows/documentation-refresh.md for routing documentation and AI instructions.
Use main as the base and codex/docs-routing-refresh as the branch.
```

Use an absolute file path and identify the target repository when the file is in
another checkout. The model reads and executes the flow in the current session,
using the defaults in that file for omitted optional inputs.

### Through Agents-Core MCP

The caller does not need a local copy of `flows/`. Ask the model:

```text
Use Agents-Core to run documentation-refresh in this repository.
```

The model discovers installed workflows with `list_flows()` and calls:

```text
run_flow(flow="documentation-refresh")
run_flow(flow="flows/pr-review.md", request="Review <PR or MR URL>. no-merge")
```

`flow` accepts an ID from `list_flows` (bare or scope-qualified, such as
`user:<id>`), `ID.md`, or `flows/ID.md`. `request` carries scope, URLs and
constraints as text. The default target is the caller's workspace;
optional `repo_path` selects an existing directory within that workspace.
Absolute paths outside it and escaping symlinks are rejected.

- **HTTP / shared daemon:** the connection must supply a registered workspace
  UUID in `X-Agents-Workspace`, including when `repo_path` is provided. A global
  connection without it can list flows but cannot start one. See
  [workspace setup](../docs/shared-mcp-daemon.md#installation-and-client-migration).
- **Stdio:** the target is `AGENTS_CLIENT_REPO_ROOT` when set; otherwise the
  nearest `.git` or `CLAUDE.md` at or above `CLAUDE_PROJECT_DIR` (exported by
  Claude Code) or the server's working directory. A `CLAUDE_PROJECT_DIR` without
  a marker is used as named; a working directory without a marker is refused
  with `workspace_required`. A root (including the override) that is a filesystem
  root, the home directory, or a system or program directory (such as
  `%SystemRoot%` or `%ProgramFiles%` on Windows, `/usr` or `/etc` on POSIX) is
  refused with `workspace_unsafe`. Set the variable to one project's directory
  when the client launches MCP from another directory. An unavailable working
  directory fails instead of falling back to the installation.

`list_flows` returns `status="success"` and a `flows` array. Each item contains
`id`, `title`, `source_path`, a SHA-256 `revision` and its `source`; built-in
items also carry `qualified_id` (`builtin:<id>`). See
[personal and repository flows](#personal-and-repository-flows) for the other
sources and fields. A flow that cannot be loaded is left out of `flows` and
reported in an `issues` array of `{id, error}` entries. `run_flow` returns
`status="needs_execution"`, the flow's metadata under `flow`, the full `content`,
`repo_path`, `workspace_id` (null on stdio), `request`, and execution `instruction`.
Its `flow` has the listing's fields, except that `flow.id` is always
scope-qualified (`builtin:<id>` for a built-in) and the listing-only
`qualified_id` and `overridden_by` are absent.
`run_flow` and `get_flow` declare `anthropic/maxResultSizeChars` in their
`tools/list` entries: 500,000 characters, Claude Code's ceiling, derived from the
256 KiB flow limit. Claude Code therefore keeps their results in the conversation;
without the declaration it saves a result over 50,000 characters, such as
`pr-review` with its persona bundle, to a file the model has to read in parts.
The client model must continue through the flow's completion criteria using its
own tools. Loading the bundle reads files only: it does not perform the workflow,
start a background job, sample a model, or grant permission for extra actions.
The active conversation and target repository instructions still apply.

Source Markdown links resolve relative to `flow.source_path`. For `user:` and
`repo:` flows that file is in the personal library, not beside the built-ins; see
[personal and repository flows](#personal-and-repository-flows). Operational
paths, edits, branches, tests and PR/MR actions belong to `repo_path`. Read the
target's own `AGENTS.md` and other applicable instructions. Source
references explain Agents-Core and do not impose its project conventions on
another repository. Use the target's tools when installation helpers are absent
from the client filesystem. If the target is inaccessible, report the blocker.

On failure, `run_flow` returns `status="error"` with an `error` string that starts
with a code, usually followed by a colon and an explanation: `flow_invalid` for
an invalid name or an empty or oversized file, `flow_not_found` for a missing
flow, `flow_unreadable` for a file that cannot be read as UTF-8, and
`workspace_required`, `workspace_unsafe` or `workspace_invalid` for a missing,
unsafe or invalid caller workspace. Match the code prefix, not the whole string; the flow management
errors below use the same format. An unusable `repo_path` (outside the
workspace, through an escaping symlink, or not an existing directory) is an
exception: it returns the uncoded message
`repo_path must be an existing directory within workspace`. `list_flows` rejects
an unknown `scope` with `flow_invalid`, but a missing workspace does not fail it:
it still lists built-in and personal flows and reports `repo.status="unavailable"`.
Correct the selection or connection before trying again.

Adding a valid Markdown file exposes it through these generic MCP tools on the
next call, without registering another tool or restarting the server. It does
not create a slash command or a scheduled task. Content is read fresh each time;
the revision identifies the exact instructions supplied to the model.

## Choose a flow's agent and components

A flow can name the agent it runs as and, optionally, the exact skills,
implants and rules of that agent's bundle. The flow declares a default in YAML
frontmatter before its title:

```markdown
---
persona:
  agent: code_reviewer
  skills: [skill-dev-clean-code]  # exactly these
  implants: []                    # none
  # rules omitted: the usual rules apply
---
# Review a pull request
```

An omitted list keeps the agent's own selection for that kind (retrieval and
the agent's declared skills and implants, minus components switched off in the
web UI). A list is exact: nothing is retrieved or added, the agent's skill policy
does not restrict it, an empty list loads nothing, and components switched off
in the web UI still load when a flow names them. Rules are named as in the
footer (`no-fabrication`), skills and implants by file ID (`skill-web-search`,
`implant-iteration-budget`); unknown IDs are rejected on save.

Each user can replace that choice for any flow, built-in ones included, without
copying its text: `set_flow_persona(flow, agent, skills, implants, rules)` or the
**Persona** panel of the [flow editor](../docs/shared-mcp-daemon.md#flow-editor).
The choice is stored in `flows/.user/personas/` and replaces the whole
frontmatter declaration; calling it without `agent` runs the flow without a
persona, and `reset=true` restores the frontmatter. It returns `status` `saved`
or `reset` with the flow's metadata; components without `agent`, or `reset=true`
with any of them, are `flow_invalid`. `list_flows` and `get_flow`
report the effective `persona` and `persona_source` (`frontmatter`, `overlay` or
null). An invalid declaration or an unreadable overlay does not hide the flow:
it is listed with `persona_error`, and `run_flow` refuses it with `flow_invalid`.
Saving a choice repairs an invalid declaration or a malformed overlay;
`reset=true` removes an overlay, including a symlinked one without touching its
target, which a saved choice cannot replace. `run_flow` also refuses a persona that
names an agent or component that no longer exists. Frontmatter is recognized only as a
closed block that parses as a YAML mapping, so a flow may still open with a
Markdown rule (`---`); a block that mentions `persona:` but is not valid YAML is
`flow_invalid`.

The persona is role guidance under the flow, not a replacement for it: the
flow's steps, permissions and required outputs (comments, reports, reply style)
take precedence over the persona's own workflow rules, clarifying questions and
`## Output Format`. The persona contributes domain expertise and judgment.

`run_flow` then also returns `persona_activation`, a protocol 2 response for that
persona built from the flow's title and `request`. Pass `current_persona` to
`run_flow` and apply the activation as a switch before executing the flow:
`SUCCESS` replaces the four blocks and footer, `NO_CHANGE` keeps them, and
`ERROR` keeps the previous persona and must be reported. While retrieval is still
starting, that `ERROR` starts with `warming_up`; call `run_flow` again after a few
seconds ([startup and readiness](../docs/routing_flow.md#startup-and-readiness)). The activation is
compared by `bundle_revision`, not by agent name: the same agent with other
components is a new activation. A flow's choice never trains the router cache
that `route_and_load` shares between users. A flow without a persona returns no
activation. A later `refresh_persona_context`, or a restore with
`get_agent_context(force_reload=True)`, rebuilds the agent's default bundle, not
the flow's exact lists.

### Without Agents-Core MCP

A session without the MCP server, or one told not to use it (such as the cloud
routine of the issue agent), gets no `persona_activation`, but can load the same persona from a checkout of
this repository. Read every file from the default branch (for example
`git show origin/main:agents/<agent>/system_prompt.mdc`), never from a pull
request's working tree: a PR may change the persona files, and its author must
not choose the reviewer's instructions. Read the flow's frontmatter, then, in
this order and each without its frontmatter:

1. `agents/<agent>/system_prompt.mdc`;
2. each `skills/<id>.mdc` from `persona.skills`, else the agent's `core_skills`;
3. each `implants/<id>.mdc` from `persona.implants`, else the agent's
   `preferred_implants`;
4. each rule from `persona.rules` (`rules/rule-<name>.mdc`), else every
   `rules/rule-*.mdc`, in the rules' `priority` order.

Follow them as role guidance under the flow, the user's request and the target
repository's instructions (see the precedence above). Keep the framing the MCP
bundle adds around these files (`src/engine/rules.py`, `src/engine/implants.py`):
each rule's description is part of it, and where persona, skill or implant text
conflicts with a rule, the rule wins; implants are reasoning patterns to use only
where they help, never claims about checks that were not run. The persona belongs to the
flow whose steps are being executed: switch when a flow calls another one, and
return to the caller's persona, or to none, when that flow's steps are done.
This is a manual fallback: there is no descriptor, footer or `log_interaction`
attribution, and none should be invented. Exact lists name the same components
as the MCP bundle, but the content can differ: MCP renders a skill's short
`compiled` text at the standard tier, while the files hold the full bodies. An
omitted list loads fewer components than MCP, which also retrieves by relevance,
and the default rules ignore the web UI's switches, which do not exist there.

## Personal and repository flows

Besides the built-in flows in this directory, each user keeps their own flows,
managed from chat or, with the shared macOS daemon, the
[local editor](../docs/shared-mcp-daemon.md#flow-editor):

| Source | ID | Stored in the installation | Visible |
|---|---|---|---|
| Built-in | `pr-review` (also `builtin:pr-review`) | `flows/<id>.md`, tracked in git | Everywhere, read-only |
| Personal | `user:<id>` | `flows/.user/common/<id>.md` | In every repository |
| Repository | `repo:<id>` | `flows/.user/repos/<repo-key>/<id>.md` | Only in that repository |

`flows/.user` is ignored by git (the repository ignores every dot-directory), so
saving a flow never dirties or switches a branch, neither in this installation nor
in the caller's repository. Installation updates fast-forward and leave it alone.
`AGENTS_USER_FLOWS_DIR` moves the library, including persona choices and the web
UI's component switches (`components.json`), elsewhere; use an absolute path,
because a relative value is resolved against the server's working directory (the
client's launch directory over stdio). The repository key is the
normalized `origin` remote without credentials, for example
`github.com-owner-project`, so clones of one remote share their flows; without a
remote it is the folder name plus a path hash. Each repository folder keeps the
origin in `.repo.json` and this machine's clone path in `.repo.local.json`; a
folder without an origin belongs to this machine only. The library root holds
`.agents-library.json`, `.gitignore` and `.gitattributes`, which prepare it for
[sync between machines](../docs/user-sync.md). Once sync is set up (setup offers it;
later `python -m src.user_sync setup`, or the Sync page of the daemon's settings),
personal and repository flows, persona choices, the switches and the flows'
history reach your other machines whenever both sides sync: with the shared
daemon at its fetch interval (5 minutes by default) and shortly after a save,
one cycle for a burst of saves; without it at the interval of the scheduled run
that setup offers (`python -m src.user_sync schedule enable`, 5 minutes by
default), and otherwise only in the cycle that a save made through Agents-Core
starts about ten seconds later.
Repository folders without an origin stay on this machine.
`repo:` flows need the caller's workspace, like `run_flow`; without one,
`get_flow`, `save_flow` and `delete_flow` return `repo_scope_unavailable` for them.

Ask in chat, for example "save this as my flow", "save it only for this
repository", "change pr-review for me" or "restore the previous version". The
model uses these tools and says which scope it chose:

| Tool | Purpose |
|---|---|
| `list_flows(scope="all")` | `all`, `builtin`, `user` or `repo`; `repo` reports whether the repository scope is available |
| `get_flow(flow, version=None)` | Text, revision and saved versions; for a local copy of a built-in, `upstream` holds the current built-in text |
| `save_flow(flow, content, scope="user", expected_revision=None, override=False)` | Create (no revision) or update (revision from `get_flow`) a personal or repository flow |
| `delete_flow(flow, expected_revision)` | Delete a personal or repository flow; its text stays in history |

A bare name resolves `repo:`, then `user:`, then the built-in. Built-in flows are
never edited in place; saving or deleting `builtin:<id>` returns `flow_read_only`.
Saving the same ID with `override=true` creates a local copy that replaces it for
this user (`user:`) or this repository (`repo:`). The listing marks the built-in
`overridden_by` and the copy `overrides`. When the built-in text later changes,
the copy reports `upstream_changed` until it is saved again; deleting the copy
restores the built-in. Without `override=true`, reusing a built-in ID is rejected
(`flow_shadows_builtin`), so a bare name never silently changes meaning;
`override=true` without a built-in of that ID is `flow_invalid`. Pass
`override=true` on every save of the copy while the built-in exists; a plain save
is accepted only after the built-in is removed, and it drops the copy's
`overrides` mark.

A copy, like every `user:` and `repo:` flow, lives in the personal library, not
beside the built-ins, and its `source_path` points there. Relative links kept
from the built-in text, such as `../AGENTS.md` or `documentation-refresh.md`,
therefore resolve against the copy's location and miss their targets. Follow
them from the built-in's `source_path` (from `list_flows` or
`get_flow("builtin:<id>")`), or change them to absolute paths when editing the
copy.

`save_flow` returns `status` `created`, `saved` or `unchanged` with the flow
metadata; `delete_flow` returns `status="deleted"` with the scoped `id` and the
archived `version`, and also removes that flow's persona choice. Every save or delete keeps the previous text in
`flows/.user/.history`. To restore, read the text with
`get_flow(flow, version=...)` and pass it to `save_flow` with the current
revision from `get_flow(flow)`, not the old version's `revision`; omit
`expected_revision` when the flow was deleted, and pass `override=true` for a copy
of a built-in. A deleted flow and its history list disappear from `list_flows`,
`get_flow(flow)` and the editor, so keep the `version` that `delete_flow` returns;
otherwise find the archived file under `.history/common/<id>/` or
`.history/repos/<repo-key>/<id>/` in the library. An update with an outdated
`expected_revision` returns `flow_conflict` with the current revision instead of
overwriting a change made from another chat or the editor.
Writes are atomic, and a file lock (`flock` on macOS and Linux, `LockFileEx` on
Windows) serializes them across processes, so concurrent clients get
`flow_conflict` instead of overwriting each other. On a Windows file system
without byte-range locks, such as some network shares, the lock covers only one
server process, so avoid editing the same flow there from two clients at once.

## Author a flow

Keep one workflow per top-level `.md` file. Use lowercase letters, digits and
single hyphens between words, such as `documentation-refresh.md`, and add it to
the catalog above. `list_flows` silently skips a top-level file whose name breaks
this rule, and `run_flow` rejects such a name with `flow_invalid`; after adding a
flow, check that `list_flows()` shows it without a `persona_error`. `README.md` is documentation, not a
runnable flow. Flows must be nonempty UTF-8 files of at most 256 KiB; source
symlinks must stay within `flows/`. Absolute paths, traversal and nested source
directories are rejected.

Each flow must specify:

1. **Goal and result:** the task it completes and the output the user receives.
2. **Inputs and defaults:** required context, optional scope, and default choices.
3. **Steps:** ordered actions with links to authoritative sources and instructions.
4. **Validation:** appropriate checks and how to report unavailable or failed checks.
5. **Done criteria:** observable conditions for completion and the final report.

Write flows in English under the
[documentation language policy](documentation-refresh.md#documentation-language).
Link to shared protocols and references instead of maintaining duplicate copies.
Before adding or updating a flow, review its examples, relative links, and
completion criteria so another model can execute it from the file path alone.
Write steps for the caller's repository. Mark project-specific examples and
checks explicitly, and keep source references separate from target operations.
