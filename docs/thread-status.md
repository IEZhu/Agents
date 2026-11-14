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
| Claude Code terminal and IDE sessions | `UserPromptSubmit` can set `hookSpecificOutput.sessionTitle` when an existing title is supplied | Model uses its native rename capability, or the installed helper calls the public Claude Agent SDK metadata API |
| Claude desktop app Code tab | Hook supplies identity and policy; the model sets the title with the app's `set_session_title` tool | Same app tool with `session_id: "self"`; no hook `sessionTitle` or SDK helper. After the user presses stop, an `asyncRewake` watcher wakes the model once to set `stopped` |
| Codex desktop | Hook supplies identity and policy; the model uses the host's native current-chat rename tool | Native tool when exposed; `Interrupt` can persist `stopped` through the public App Server metadata API |
| Codex CLI/IDE | Hook supplies identity and policy | Native tool when exposed, or the metadata helper with a known current session ID |
| Cursor Agents window | `sessionStart` supplies policy and conversation identity | Model-driven native rename, only when the surface exposes it |
| Cursor classic IDE sidebar | Context policy only | No verified public title-write API; report unavailable instead of writing its database |

Claude's native prompt hook sets a working prefix only for unmarked, `unknown`
or `working` titles. It preserves `needs_user`, `waiting`, `blocked`, `review`,
`paused`, `stopped`, `finishing`, `ready_to_close` and `closed` until the model
decides whether the new request actually resumes work. Session startup, resume
and compaction only deliver context, without claiming work has started.
Response-end hooks that set a status are deliberately absent: the end of a
response does not establish task completion. The interrupt watcher's `Stop`
handler only records where a turn stopped and changes no title. Subagent and
background events cannot rename the parent chat.

When the chat identity is known, Claude's prompt hook also reminds the model, once
per user turn, to mark a chapter when the request moves the work to a meaningfully
different phase of the session and the client exposes a chapter tool, such as
`mark_chapter` in the Claude desktop app. The session's first message gets no
chapter. Instruction files load once per session, so a rule there is easy to lose in
a long session; the per-turn line is not.

The desktop app starts Code-tab sessions with `CLAUDE_CODE_ENTRYPOINT=claude-desktop`.
Their sidebar title lives in the app's own session record. The app copies that title
into the CLI session as its custom title. The adapter uses the app's title API
instead of depending on CLI-side metadata writes being mirrored back. In that environment the hook omits `sessionTitle`
and the SDK command. It names the app's
`mcp__ccd_session_mgmt__get_session` and `mcp__ccd_session_mgmt__set_session_title`
tools with `session_id: "self"`, plus the ToolSearch query that loads them while
deferred. The app may ask the user to approve renaming a title the user set
manually. Titles it generated are replaced without asking.

Codex's `Interrupt` hook runs outside the cancelled model turn. It requires the
session and interrupted turn IDs, verifies that the public `thread/turns/list`
metadata still names that turn as the latest, reads the existing title, applies
`stopped`, and verifies the saved name. Turn lookup uses `itemsView: notLoaded`
and does not load conversation items. For a new chat interrupted before its title
is generated, it waits for the real `thread.name` within the same bounded deadline;
it never substitutes a placeholder or prompt text. The latest-turn check runs
after this wait, so a resumed turn still prevents a stale interrupt write. If the
name remains unavailable, the receipt records the timeout instead of claiming a
saved title. A monotonic timestamp before the script's other imports lets parsing,
lock waiting and receipt preparation consume the same budget. Loading the title
adapter also consumes the writer's allowance; an exhausted allowance starts no
App Server. The next prompt waits for the full Interrupt window before recording
its turn. The exit margin covers the final receipt and hook shutdown, but time
before Python begins, including Windows PowerShell startup, is outside this
clock; this is a bounded best-effort update, not a host-level timing guarantee.
This also covers an early Stop before `UserPromptSubmit` has recorded the turn.
A small per-session
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
running tool does not fire `PostToolUseFailure`. In the desktop Code tab, where
only the model can rename the chat, the Claude profile therefore gets a separate
interrupt watcher, `hooks/agents-core-thread-status-interrupt.py`. Its
`UserPromptSubmit` handler runs with `asyncRewake: true` and a six-hour timeout
and follows the turn's `transcript_path`. When the user stops a turn, Claude Code
writes its own interrupt record: a `user` record bound to the turn's `promptId`
whose content is one text block starting with `[Request interrupted by user`. The
watcher matches only that record for its own prompt, including one written just
before it started. It then waits one second so that a prompt typed right after
stop can supersede it, and exits with code 2. That exit wakes Claude for one turn
whose reminder first asks for exactly one `set_session_title` call with the
`stopped` prefix. A user message delivered into that turn, such as one sent right
after stop, is then handled normally, including its own status; without one, the
turn ends with no reply text. Every other path exits 0, including usage errors,
malformed input and unsupported environments, because exit 2 is the wake-up
signal. A separate runtime file keeps an older installer that replaces the main
runtime from leaving these handlers calling a mode it lacks. The watcher acts only
with `CLAUDE_CODE_ENTRYPOINT=claude-desktop` and a bundled CLI path naming Claude
Code 2.1.287 or later, the release that stops a missing `asyncRewake` script from
waking Claude repeatedly, and it ignores subagent and sidechain events.

The `Stop` handler only records where the turn stopped, as the transcript size. A
short turn can stop before its watcher registers, even in a session's first
watched turn; the handler then creates the session record, and the watcher keeps
that stop when it registers. Another `Stop` hook in the profile can block a stop,
and Claude Code then continues the same prompt with the same `promptId`, so a
`Stop` does not end the watcher. The watcher ends when no user record of its
prompt follows the stop within ten minutes. The blocking hook's feedback record or
a tool result of the continued turn keeps it running, and an interrupt after the
stop still wakes Claude; an interrupt followed by a normal stop does not. A newer
prompt also ends an older watcher, through its registration or, before its own
watcher starts, through its first transcript record: Claude Code stamps every
main-thread user record with the current prompt's ID. Records of a prompt the
session record already superseded do not count as newer: a stopped prompt's woken
turn keeps that prompt's ID, so its records can follow the next prompt's own,
even before that prompt's watcher registers.

One record per session in `hooks/agents-core-thread-status-interrupt-state/`,
locked with `flock` on POSIX and `msvcrt.locking` on Windows, serializes
registration, `Stop` records and the wake; every access, reads included, takes the
lock. A watcher that starts late, after a newer prompt registered or after the
transcript recorded one, neither replaces that prompt nor wakes into its turn, and
its own prompt cannot become current again. It looks for its prompt's records in
the transcript's last 64 KiB and in the 64 KiB before the newer prompt registered,
however long that prompt's turn has run since. Before exiting with 2, a watcher claims
its prompt's single wake in the record. Duplicate watchers of one prompt therefore
wake Claude once, and the woken turn, which keeps the stopped prompt's ID, starts no
second wake. In a chat's first turn the hook can start before Claude Code creates
the transcript; the watcher then waits up to two minutes for the file and otherwise
exits quietly.

This is the one case where a thread-status hook starts a model turn: each stop
costs a short model call over the conversation, and the woken turn appears in the
chat. The transcript record is Claude Code's internal format rather than a
documented contract. If it changes, the watcher stops matching and no title
changes, instead of a false `stopped`. Each watcher also stays alive, polling twice
a second, for up to ten minutes after every stop. Turns longer than six hours,
turns that no `UserPromptSubmit` starts, terminal and IDE sessions, and a model
that disregards the reminder keep the previous title. So does an interrupt in a
continued turn when the blocking hook ran for more than ten minutes, or when its
feedback reached the transcript before the `Stop` handler read the size and the
continuation then wrote no tool result for ten minutes. A late watcher can still
wake when neither the session record nor the transcript shows the newer prompt by
the end of its one-second settle window, for example while that prompt's
synchronous hooks are still running. A late watcher whose prompt's last user
record lies more than 64 KiB before the newer prompt's registration can still
replace that prompt. If a message sent right after stop is carried into the woken
turn and Claude Code stamps that turn's later records, including an interrupt
record, with the stopped prompt's ID, a stop during that work keeps the previous
title, and the message's own watcher can keep polling until the next prompt.
Cursor documents an aborted stop status, but no verified general title-write API,
so Cursor has no automatic button-stop adapter. Do not infer interruption from
ordinary tool failures or claim unsupported title changes.

The always-on `thread-status` rule reaches new persona activations and explicit
refreshes, unless rules are disabled or an exact flow selection omits it.
Installed lifecycle hooks supply their own compact policy, including to an
existing persona. Semantic status selection and native tool invocation remain
model-controlled; these hooks are not a background process that classifies every
old conversation. Unsupported clients do not get a false success indicator.

## Install

Normal setup offers emoji thread-status hooks for detected Claude Code, Codex and Cursor profiles. `install.sh` uses `--yes`, which accepts both this option and session readiness; interactive `init_repo` asks for each separately. Use `--skip-thread-status`, `--skip-hooks` or `--skip-mcp` to opt out. Setup installs the pinned Claude title SDK in its persistent virtual environment when Claude's status option is selected. If that optional SDK installation fails, setup warns and still installs the stdlib hook; SDK-based CLI title writes remain unavailable until retry succeeds. Claude Desktop Code tabs use the app's own title tools. Existing hooks and other settings are preserved; absent clients are skipped.

To install or refresh this feature separately, use a stable Python 3.11+ interpreter. The standalone hook needs only the standard
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

For Claude the installer also copies `agents-core-thread-status-interrupt.py` and
adds the watcher's `UserPromptSubmit` (`asyncRewake`) and `Stop` handlers, each in
its own group. If the profile's existing `Stop` entries are not standard hook
groups, it leaves the watcher out and reports `interrupt_watcher: false`; the
other thread-status hooks still install.

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
`UserPromptSubmit`, Codex `Interrupt`, or Cursor `sessionStart`, and commands
referencing `agents-core-thread-status-interrupt.py` from Claude `UserPromptSubmit`
and `Stop`. Preserve other handlers, then delete
the copied runtimes, `agents_core_thread_titles.py`, `agents_core_codex_titles.py`
and their own turn-record directories after checking no hook still references them.
The main uninstaller removes unchanged registrations and copied runtimes belonging to the selected installation, preserving modified or shared adapters and reporting retained turn state. A separate hook environment belongs to that environment and is preserved. Cleanup trusts the selected home directory and its system ancestors, so a linked home layout does not prevent removing owned hooks. Linked profile directories and runtime files below home are retained.

## Verification and sources

```bash
PYTHONDONTWRITEBYTECODE=1 LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/test_thread_status.py tests/test_thread_status_interrupt.py tests/test_thread_status_installer.py tests/test_uninstall_thread_status.py tests/test_codex_thread_titles.py tests/test_rules.py tests/test_subagent.py -q -p no:cacheprovider
```

Tests use temporary profiles and fake SDKs; they check preservation, idempotence,
event/output compatibility, stale-interrupt races, bounded transport, failure
readback and worker exclusion. The interrupt watcher tests cover record matching
through the transcript follower, the reminder's contract, supersession by
registration or by a newer prompt's transcript record, a prompt typed within the
settle window, an interrupt written before the watcher started, a stop that
another hook blocks (with its feedback recorded before or after the `Stop`
handler), retirement after a normal stop, including one recorded before the
session's first watcher, a normal end after an interrupt, a late watcher that
must yield to a newer prompt, also when its records precede the lookback, a newer
prompt after a turn longer than the lookback, a superseded prompt's record after
the current prompt's own, two watchers of one prompt waking once, the session
lock, corrupt session records, the history and stop caps, a first-turn transcript
that appears late or never, version and entrypoint gating, and exit code 0 for
every failure path.
The `Thread status` workflow runs these adapter, hook and installer tests on
Linux, macOS and Windows, and the `Uninstall` workflow runs
`tests/test_uninstall_thread_status.py` there, including removal of the
watcher's handlers and runtime while another `Stop` hook stays. They do not
establish live client rendering, client trust or a real PowerShell/client
integration.

Primary contracts: [Claude hooks](https://code.claude.com/docs/en/hooks),
[Claude SDK metadata](https://code.claude.com/docs/en/agent-sdk/python#rename_session),
[Codex hooks](https://learn.chatgpt.com/docs/hooks),
[Codex App Server](https://learn.chatgpt.com/docs/app-server),
[Cursor hooks](https://cursor.com/docs/hooks), and
[Cursor's explanation of Agents-window renaming](https://forum.cursor.com/t/let-agents-set-their-own-chat-title-status-programmatically-e-g-dev-active/166385/5).
