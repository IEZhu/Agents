# Emoji thread status

One leading emoji in a conversation title describes the task's current state.
The remaining title stays intact. Runtime `active`, `idle`, unread flags and age
are not evidence that the user's task is complete.

| Canonical status | Prefix | Meaning |
|---|---|---|
| `working` | 🔄 | Work is in progress |
| `needs_user` | 🙋 | Required user input, a decision or permission |
| `waiting` | ⏳ | An external event is already underway |
| `blocked` | 🚧 | An obstacle needs action |
| `review` | 👀 | Required human review or acceptance |
| `paused` | ⏸️ | Work was explicitly put on hold |
| `stopped` | 🛑 | Confirmed user interruption; requested work remains unfinished |
| `finishing` | 🧹 | Required work remains from a closure audit; the task is unfinished |
| `ready_to_close` | 🏁 | Requested work and required checks are complete; no required steps remain |
| `closed` | ✅ | Explicitly closed or confirmed archived |
| `unknown` | ❔ | Insufficient evidence |

The usual completion path is `working → review → ready_to_close → closed`.
Skip review when no acceptance step is required. An optional suggestion after a
complete result does not create unfinished work. A new work request reopens the
task as `working`, including after `closed`; a status question does not. Changing
the prefix does not itself archive a conversation or authorize another action.

The attention statuses `needs_user`, `review` and `ready_to_close` are final
handoffs. First finish all currently possible autonomous work; no AI-owned check,
subagent, installation or internal review may still be active. A bot review or
CI run already underway uses `waiting`, and work on its results uses `working`.
Use `review` only when a human must review or accept the result. If essential
input prevents further progress, finish independent work before handing off as
`needs_user`. The whole task need not be complete for that input handoff.
An optional question during ongoing work does not establish a handoff. Never
set an attention status and then continue autonomous work.

At a closure-audit handoff, use `finishing` when required obligations remain,
unless essential input or required human approval calls for `needs_user`/`review`.
This means the remaining required work needs to be finished; the task is still
unfinished. Resume implementation as `working`, and use `working`/`waiting` for
AI review, CI and subagents. Verify or explicitly resolve each required task
before `ready_to_close`; an owner or a proposed follow-up alone does not resolve
it. `closed` still requires explicit closure or confirmed archival. A completed
audit report does not establish that the audited work is complete, and optional
suggestions alone do not require `finishing`.

`stopped` records an actual user interruption, not an idle session, a failed
tool, or the ordinary end of a response. It differs from an explicit pause and
from closure. A new work request resumes `working`.

## Client support

| Surface | Start of work | Later semantic transitions |
|---|---|---|
| Claude Code and its local Code sessions | `UserPromptSubmit` can set `hookSpecificOutput.sessionTitle` when an existing title is supplied | Model uses its native rename capability, or the installed helper calls the public Claude Agent SDK metadata API |
| Codex desktop | Hook supplies identity and policy; the model uses the host's native current-chat rename tool | Native tool when exposed; `Interrupt` can persist `stopped` through the public App Server metadata API |
| Codex CLI/IDE | Hook supplies identity and policy | Native tool when exposed, or the metadata helper with a known current session ID |
| Cursor Agents window | `sessionStart` supplies policy and conversation identity | Model-driven native rename, only when the surface exposes it |
| Cursor classic IDE sidebar | Context policy only | No verified public title-write API; report unavailable instead of writing its database |

Claude's native prompt hook sets a working prefix only for unmarked, `unknown`
or `working` titles. It preserves `needs_user`, `waiting`, `blocked`, `review`,
`paused`, `stopped`, `finishing`, `ready_to_close` and `closed` until the model
decides whether the new request actually resumes work. Session startup, resume and compaction only
deliver context, without claiming work has started. Ordinary response-end hooks
are deliberately absent: the end of a response does not establish task completion. Subagent and
background events cannot rename the parent chat.

Codex's `Interrupt` hook runs outside the cancelled model turn. It requires the
session and interrupted turn IDs, verifies that the public `thread/turns/list`
metadata still names that turn as the latest, reads the existing title, applies
`stopped`, and verifies the saved name. Turn lookup uses `itemsView: notLoaded`
and does not load conversation items. This also covers an early Stop before
`UserPromptSubmit` has recorded the turn. A small per-session
record under the adapter's own `hooks/agents-core-thread-status-state/` directory
tracks the prompt turn ID and the last interrupt receipt. Its lock serializes
the interrupt write with the next prompt, while the public turn check rejects
an older interruption even when no prompt record exists. When the prompt record
differs, the adapter also requires that record to appear after the interrupted
turn in the newest-first metadata page (at most 32 turns). Missing or lagging
metadata cannot authorize a write over a locally observed newer prompt; an
unconfirmed order is skipped. The receipt records
`running`, `persisted`, `skipped`, or `failed`, including a bounded error message
on failure, and survives the next prompt for diagnosis. Missing IDs, a stale
interrupt or a failed metadata operation cannot produce a success message. The helper starts no model
turn. It does not prove that an already open desktop sidebar refreshed.

Claude's documented `Stop` event excludes user interruption, and cancelling a
running tool does not fire `PostToolUseFailure`. Cursor documents an aborted
stop status, but no verified general title-write API. These clients therefore
have no automatic button-stop adapter here; do not infer interruption from
ordinary tool failures or claim unsupported title changes.

The always-on `thread-status` rule reaches new persona activations and explicit
refreshes, unless rules are disabled or an exact flow selection omits it.
Installed lifecycle hooks supply their own compact policy, including to an
existing persona. Semantic status selection and native tool invocation remain
model-controlled; these hooks are not a background process that classifies every
old conversation. Unsupported clients do not get a false success indicator.

## Install

Use a stable Python 3.11+ interpreter. The standalone hook needs only the standard
library. Claude's later SDK writes additionally need `claude-agent-sdk==0.2.168`
in that same interpreter. The optional `thread-status` package extra supplies it.
Prefer a separate persistent environment when the Agents-Core checkout is a live
installation that must stay unchanged:

```bash
python3 -m venv ~/.local/share/agents-core/thread-status-venv
~/.local/share/agents-core/thread-status-venv/bin/python -m pip install 'claude-agent-sdk==0.2.168'
~/.local/share/agents-core/thread-status-venv/bin/python scripts/install_thread_status.py --clients all --dry-run
~/.local/share/agents-core/thread-status-venv/bin/python scripts/install_thread_status.py --clients all
```

On Windows, use a persistent venv and its `Scripts/python.exe` for the same
installer commands. Windows hook commands use the existing encoded PowerShell
adapter. The installer honors `CLAUDE_CONFIG_DIR` and `CODEX_HOME`; Cursor uses
`~/.cursor/hooks.json`. It copies the runtime and formatter into each profile's
`hooks/`, together with the Codex metadata adapter, so it does not depend on a development worktree remaining present.
Its interpreter must remain installed. It updates only owned handlers, preserves
other hooks and settings, backs up every changed existing file before writes,
and refuses symlink paths. Repeating it does not duplicate handlers.

Start a fresh session. Review and trust changed Codex hook definitions through
the client's `/hooks` interface where required; installation does not fabricate
a trust decision. Managed policy or an older client can prevent hooks from
running. Existing idle sessions are not woken or renamed in bulk. No daemon
restart or live installation source edit is required to use the copied hooks.

## Explicit helper and limits

The hook context gives Claude an argument array for its current session. Its
helper performs a metadata-only read, rename and readback using public SDK APIs,
without sending a prompt or invoking a model:

```bash
python /path/to/profile/hooks/agents-core-thread-status.py --client claude --session-id CURRENT_UUID --directory /absolute/project --set-status ready_to_close
```

A successful result confirms `persisted: true`, with
`live_ui_refresh_verified: false`: SDK persistence does not prove that an already
open interface refreshed. Failures return nonzero and do not claim application.
The helper never picks the newest session as a substitute for an unknown ID.
It consumes no transcript paths and contains no client storage-format writer.

For a candidate title without a client mutation:

```bash
python scripts/thread_status.py --client codex --title 'Review API changes' --set-status ready_to_close
```

This returns `applied: false`. With a known current session UUID instead of
`--title`, `--client codex --session-id CURRENT_UUID --set-status stopped` uses
the public App Server's metadata-only `thread/read` and `thread/name/set` methods.
The installed `codex` executable must be available on the hook's PATH. Matching
readback establishes persistence, not live UI refresh; the overall operation is
bounded and never calls `thread/resume` or `turn/start`. Native host tools remain
preferred for model-driven transitions. A hook cannot invoke a model-only tool
just because it is installed.

On Windows the metadata adapter launches the native `codex.exe` directly. It
resolves supported npm shim/package layouts without executing their `.cmd`,
PowerShell or Node wrappers, so terminating the owned process cannot leave an
npm wrapper's server running after the turn-record lock is released. Unsupported
launcher layouts fail visibly; lifecycle hooks still deliver policy context.

To remove the opt-in hooks, remove only commands referencing
`agents-core-thread-status.py` from Claude/Codex `SessionStart` and
`UserPromptSubmit`, Codex `Interrupt`, or Cursor `sessionStart`. Preserve other handlers, then delete
the copied runtime, `agents_core_thread_titles.py`, `agents_core_codex_titles.py`
and its own turn-record directory after checking no hook still references them.
The main uninstaller currently inventories readiness adapters,
not this separate opt-in feature; remove these adapters before a full uninstall.

## Verification and sources

```bash
PYTHONDONTWRITEBYTECODE=1 LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/test_thread_status.py tests/test_thread_status_installer.py tests/test_codex_thread_titles.py tests/test_rules.py tests/test_subagent.py -q -p no:cacheprovider
```

Tests use temporary profiles and fake SDKs; they check preservation, idempotence,
event/output compatibility, stale-interrupt races, bounded transport, failure
readback and worker exclusion.
The `Thread status` workflow runs these adapter, hook and installer tests on
Linux, macOS and Windows. They do not establish live client rendering, client
trust or a real PowerShell/client integration.

Primary contracts: [Claude hooks](https://code.claude.com/docs/en/hooks),
[Claude SDK metadata](https://code.claude.com/docs/en/agent-sdk/python#rename_session),
[Codex hooks](https://learn.chatgpt.com/docs/hooks),
[Codex App Server](https://learn.chatgpt.com/docs/app-server),
[Cursor hooks](https://cursor.com/docs/hooks), and
[Cursor's explanation of Agents-window renaming](https://forum.cursor.com/t/let-agents-set-their-own-chat-title-status-programmatically-e-g-dev-active/166385/5).
