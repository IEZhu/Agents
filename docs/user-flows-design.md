# Personal flows: decision record

Status: implemented on 2026-09-29. The maintained reference is the
[flow guide](../flows/README.md#personal-and-repository-flows); the editor is
described in the [daemon reference](shared-mcp-daemon.md#flow-editor).

## Goal

Let a user create, change and use their own flows from any chat and from a local
editor, at two levels: for every repository ("agent level") or for one repository.
Built-in flows that ship with the installation must stay editable for the user
without dirtying or switching any git branch.

## Decisions

| Question | Decision | Why |
|---|---|---|
| Storage | Markdown files, one flow per file | The owner chose files over a database; flows stay readable, diffable and easy to back up |
| Location | `flows/.user/` in the installation, ignored by git | Every dot-directory is already ignored, so no working tree or branch is touched; fast-forward updates keep it |
| Levels | `user:<id>` in `common/`, `repo:<id>` in `repos/<repo-key>/` | One personal library with an optional per-repository layer; the target repository never receives files |
| Repository key | Normalized `origin` without credentials, else folder name plus path hash | Clones of one remote share flows; secrets in remote URLs are never stored |
| Built-in changes | A local copy saved with `override=true`, recorded in `<id>.meta.json` with the built-in revision it was based on | The tracked file stays untouched; the copy reports `upstream_changed` when the built-in moves on |
| Name resolution | Bare IDs: `repo:` > `user:` > built-in; reusing a built-in ID requires `override=true` | Specific beats general, and a bare name never silently changes meaning |
| Concurrency | `expected_revision` (SHA-256 of the file) on every update; atomic writes under one lock | Chat and editor edits conflict instead of overwriting each other |
| History | Each overwritten or deleted text is kept in `.history/`; restore is read-then-save | Recovery without a database or git |
| Publishing | None: no branches, commits or pushes | The flow lives in files; sharing through a repository is out of scope |
| Editor | Served by the existing daemon at `/ui`, entered with a one-use code from `python -m src.daemon flows-ui` | One installation to run; the browser never holds the MCP bearer token |

## Rejected alternative

The first proposal (2026-09-28, in this file's git history) used a per-user SQLite
database outside the installation with drafts, publication, run receipts and
metrics. It was dropped in favor of plain files. Its run ledger and outcome
metrics are therefore not implemented; `run_flow` still returns a
`needs_execution` handoff and does not record runs.

## Editor security

- Loopback `Host` only; changes also need a same-origin `Origin` and the
  `X-Agents-UI` header (not sendable by a cross-site form).
- A one-use code (two minutes) becomes an HttpOnly, SameSite=Strict cookie scoped
  to `/ui`, with 30-minute idle and eight-hour limits, kept in daemon memory.
- The cookie cannot reach `/mcp` or administration; the bearer token does not
  open the editor API.
- Nonce-based Content Security Policy, no external assets, text rendered with
  `textContent`, `Cache-Control: no-store` on API responses.

## Limits and follow-ups

- Editing requires the HTTP daemon; stdio-only setups manage flows from chat.
- One library per installation. Several installations need
  `AGENTS_USER_FLOWS_DIR` pointed at one directory to share flows.
- History is kept indefinitely; pruning is manual.
- Possible next steps: a run ledger with outcomes, a line diff between a copy and
  its built-in in the editor, and import/export of flows between machines.
