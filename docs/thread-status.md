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
| `review` | 👀 | Required review or acceptance |
| `paused` | ⏸️ | Work was explicitly put on hold |
| `ready_to_close` | 🏁 | Requested work and required checks are complete; no required steps remain |
| `closed` | ✅ | Explicitly closed or confirmed archived |
| `unknown` | ❔ | Insufficient evidence |

The usual finishing path is `working → review → ready_to_close → closed`.
Skip review when no acceptance step is required. An optional suggestion after a
complete result does not create unfinished work. A new work request reopens the
task; a status question does not. Changing the prefix does not itself archive a
conversation or authorize another action.

## Client support

| Surface | Start of work | Later semantic transitions |
|---|---|---|
| Claude Code and its local Code sessions | `UserPromptSubmit` can set `hookSpecificOutput.sessionTitle` when an existing title is supplied | Model uses its native rename capability, or the installed helper calls the public Claude Agent SDK metadata API |
| Codex desktop | Hook supplies identity and policy; the model uses the host's native current-chat rename tool | Same native tool, when exposed |
| Codex CLI/IDE | Hook supplies identity and policy | Requires a supported rename capability on that surface; hooks do not create one |
| Cursor Agents window | `sessionStart` supplies policy and conversation identity | Model-driven native rename, only when the surface exposes it |
| Cursor classic IDE sidebar | Context policy only | No verified public title-write API; report unavailable instead of writing its database |

Claude's native hook does not reopen an already marked paused, waiting, review
or finished task merely because a prompt arrived. The model decides whether the
new request actually resumes work. Session startup, resume and compaction only
deliver context, without claiming work has started. Stop hooks are deliberately
absent: the end of a response does not establish task completion. Subagent and
background events cannot rename the parent chat.

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
`hooks/`, so it does not depend on a development worktree remaining present.
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

This returns `applied: false`. Codex's App Server supports `thread/name/set`, but
this helper deliberately does not launch a second server: its notification would
not establish that the current desktop process refreshed. Native host tools are
preferred. A hook cannot invoke a model-only tool just because it is installed.

To remove the opt-in hooks, remove only commands referencing
`agents-core-thread-status.py` from Claude/Codex `SessionStart` and
`UserPromptSubmit`, or Cursor `sessionStart`. Preserve other handlers, then delete
the copied runtime and `agents_core_thread_titles.py` after checking no hook still
references them. The main uninstaller currently inventories readiness adapters,
not this separate opt-in feature; remove these adapters before a full uninstall.

## Verification and sources

```bash
PYTHONDONTWRITEBYTECODE=1 LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/test_thread_status.py tests/test_thread_status_installer.py tests/test_rules.py tests/test_subagent.py -q -p no:cacheprovider
```

Tests use temporary profiles and fake SDKs; they check preservation, idempotence,
event/output compatibility, bounded input, failure readback and worker exclusion.
They do not establish live rendering, client trust or Windows execution.

Primary contracts: [Claude hooks](https://code.claude.com/docs/en/hooks),
[Claude SDK metadata](https://code.claude.com/docs/en/agent-sdk/python#rename_session),
[Codex hooks](https://learn.chatgpt.com/docs/hooks),
[Codex App Server](https://learn.chatgpt.com/docs/app-server),
[Cursor hooks](https://cursor.com/docs/hooks), and
[Cursor's explanation of Agents-window renaming](https://forum.cursor.com/t/let-agents-set-their-own-chat-title-status-programmatically-e-g-dev-active/166385/5).
