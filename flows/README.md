# Model workflows

`flows/` contains reusable Markdown instructions that a model follows to complete
a defined task. Each flow describes its inputs, execution steps, checks, and
expected result. Start with [AGENTS.md](../AGENTS.md) for repository instructions
and the [documentation map](../docs/README.md) for supporting references.

## Available flows

| Flow | Purpose | Result |
|---|---|---|
| [Documentation refresh](documentation-refresh.md) | Check and update documentation for people and AI against the implementation | Reviewed documentation changes on a separate branch, with validation results |
| [PR/MR review](pr-review.md) | Handle review findings, update the description, and repeat bot reviews | A merged request or a precise blocker, with a report in the invocation language |
| [Issue agent](issue-agent.md) | Entry point of the cloud issue agent: verify an `@agent` command, keep state in the issue, dispatch | The command's result and an updated state comment |
| [Issue plan](issue-plan.md) | `@agent plan` / `replan`: validate requirements, ask questions, write a versioned plan with a pre-mortem | A Markdown plan comment or open questions |
| [Issue implementation](issue-implementation.md) | `@agent run_plan` / `run`: branch, implement, self-review, independent review, pre-mortem, PR | A pull request handed to PR/MR review, never merged |

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

`flow` accepts a catalog ID, `ID.md`, or `flows/ID.md`. `request` carries scope,
URLs and constraints as text. The default target is the caller's workspace;
optional `repo_path` selects an existing directory within that workspace.
Absolute paths outside it and escaping symlinks are rejected.

- **HTTP / shared daemon:** the connection must supply a registered workspace
  UUID in `X-Agents-Workspace`, including when `repo_path` is provided. A global
  connection without it can list flows but cannot start one. See
  [workspace setup](../docs/shared-mcp-daemon.md).
- **Stdio:** the target comes from `AGENTS_CLIENT_REPO_ROOT`, then the nearest
  `.git` or `CLAUDE.md` above the server's working directory, then that directory.
  Set the variable when the client launches MCP from another directory. An
  unavailable working directory fails instead of falling back to the installation.

`list_flows` returns `status="success"` and a `flows` array. Each item contains
`id`, `title`, `source_path`, a SHA-256 `revision` and its `source`; see
[personal and repository flows](#personal-and-repository-flows) for the other
sources and fields. `run_flow` returns
`status="needs_execution"`, the same metadata under `flow`, the full `content`,
`repo_path`, `workspace_id` (null on stdio), `request`, and execution `instruction`.
The client model must continue through the flow's completion criteria using its
own tools. Loading the bundle reads files only: it does not perform the workflow,
start a background job, sample a model, or grant permission for extra actions.
The active conversation and target repository instructions still apply.

Source Markdown links resolve relative to `flow.source_path` in the installation.
Operational paths, edits, branches, tests and PR/MR actions belong to `repo_path`.
Read the target's own `AGENTS.md` and other applicable instructions. Source
references explain Agents-Core and do not impose its project conventions on
another repository. Use the target's tools when installation helpers are absent
from the client filesystem. If the target is inaccessible, report the blocker.

Both tools return `status="error"` with `error` on failure. Invalid flow names,
missing files and unreadable UTF-8 report `flow_invalid`, `flow_not_found`, or
`flow_unreadable`; a missing catalog reports `flows_unavailable` when listed.
Missing or invalid HTTP identity reports `workspace_required` or
`workspace_invalid`. Correct the selection or connection before trying again.

Adding a valid Markdown file exposes it through these generic MCP tools on the
next call, without registering another tool or restarting the server. It does
not create a slash command or a scheduled task. Content is read fresh each time;
the revision identifies the exact instructions supplied to the model.

## Personal and repository flows

Besides the built-in flows in this directory, each user keeps their own flows,
managed from chat or the [local editor](../docs/shared-mcp-daemon.md#flow-editor):

| Source | ID | Stored in the installation | Visible |
|---|---|---|---|
| Built-in | `pr-review` (also `builtin:pr-review`) | `flows/<id>.md`, tracked in git | Everywhere, read-only |
| Personal | `user:<id>` | `flows/.user/common/<id>.md` | In every repository |
| Repository | `repo:<id>` | `flows/.user/repos/<repo-key>/<id>.md` | Only in that repository |

`flows/.user` is ignored by git (the repository ignores every dot-directory), so
saving a flow never dirties or switches a branch, neither in this installation nor
in the caller's repository. Installation updates fast-forward and leave it alone.
`AGENTS_USER_FLOWS_DIR` moves the library elsewhere. The repository key is the
normalized `origin` remote without credentials, for example
`github.com-owner-project`, so clones of one remote share their flows; without a
remote it is the folder name plus a path hash. `repo:` flows need the caller's
workspace, like `run_flow`.

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
never edited in place: saving the same ID with `override=true` creates a local copy
that replaces it for this user (`user:`) or this repository (`repo:`). The listing
marks the built-in `overridden_by`. When the built-in text later changes, the copy
reports `upstream_changed` until it is saved again; deleting the copy restores the
built-in. Without `override=true`, reusing a built-in ID is rejected
(`flow_shadows_builtin`), so a bare name never silently changes meaning.

Every save or delete keeps the previous text in `flows/.user/.history`; restoring
is `get_flow(flow, version=...)` followed by `save_flow` with that text. An update
with an outdated `expected_revision` returns `flow_conflict` with the current
revision instead of overwriting a change made from another chat or the editor.
Writes are atomic and serialized by a lock, so concurrent clients are safe.

## Author a flow

Keep one workflow per top-level `.md` file. Use lowercase letters, digits and
single hyphens between words, such as `documentation-refresh.md`, and add it to
the catalog above. `README.md` is documentation, not a runnable flow. Flows must
be nonempty UTF-8 files of at most 256 KiB; source symlinks must stay within
`flows/`. Absolute paths, traversal and nested source directories are rejected.

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
