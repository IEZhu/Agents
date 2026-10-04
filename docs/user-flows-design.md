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
| Concurrency | `expected_revision` (SHA-256 of the file) on every update; atomic writes under one lock, `flock` on POSIX and `LockFileEx` on Windows | Chat and editor edits conflict instead of overwriting each other, also across processes |
| History | Each overwritten or deleted text is kept in `.history/`; restore is read-then-save | Recovery without a database or git |
| Machine-local data | A group's `.repo.json` keeps only its normalized origin; `.repo.local.json` keeps this machine's clone path; a group without an origin is machine-local | A library shared between machines must not carry one machine's paths, and a key made from a path means nothing on another machine |
| Library files | `.agents-library.json` (format 1), `.gitignore` and `.gitattributes` (`* -text`) at the root, created by the first write and never overwritten | They mark the library and keep temporary files, machine-local files and line-ending conversion out of a repository that holds it |
| Change notifications | Each write passes the paths it changed to the listeners in `src/user_library.py`, also when it fails part way | A sync runner can react to writes instead of polling, and misses no file that changed |
| Publishing | None: no branches, commits or pushes | The flow lives in files; sharing through a repository is out of scope |
| Editor | Served by the existing daemon at `/ui`; a browser of the daemon's OS user signs in by itself, others with a one-use code from `python -m src.daemon flows-ui` | One installation to run, nothing to type; the browser never holds the MCP bearer token |

## Rejected alternative

The first proposal (2026-09-28, in this file's git history) used a per-user SQLite
database outside the installation with drafts, publication, run receipts and
metrics. It was dropped in favor of plain files. Its run ledger and outcome
metrics are therefore not implemented; `run_flow` still returns a
`needs_execution` handoff and does not record runs.

## Editor security

- Loopback `Host` only; changes also need a same-origin `Origin` and the
  `X-Agents-UI` header (not sendable by a cross-site form).
- Sign-in without a code succeeds only when the loopback connection belongs to a
  process of the daemon's OS user (Linux `/proc/net/tcp*`, Windows
  `GetExtendedTcpTable` with the process token and bind time, `lsof` elsewhere)
  and carries no forwarding header; any error refuses. Same-user relays (Docker
  Desktop, `ssh -R`, tunnels) count as the user; `flows-ui --auto off` ends every
  session and leaves only the one-use code (two minutes), always the fallback. Either becomes
  an HttpOnly, SameSite=Strict cookie scoped to `/ui`, signed with a key in the
  private state directory. It lasts 30 days from the last visit (renewed on each
  API response) and survives restarts; `flows-ui --revoke` replaces the key.
- The cookie cannot reach `/mcp` or administration; the bearer token does not
  open the editor API.
- Nonce-based Content Security Policy, no external assets, text rendered with
  `textContent`, `Cache-Control: no-store` on API responses.

## Limits and follow-ups

- Editing requires the HTTP daemon; stdio-only setups manage flows from chat.
- One library per installation. Several installations need
  `AGENTS_USER_FLOWS_DIR` pointed at one directory to share flows.
- History is kept indefinitely; pruning is manual.
- Sync of the library between machines through a private git repository is
  planned in #173; the machine-local split, library files and change
  notifications above prepare it (#169).
- Possible next steps: a run ledger with outcomes, and a line diff between a copy
  and its built-in in the editor.
