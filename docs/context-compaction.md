# Native context compaction

Agents-Core can request the signed-in client's native compaction between
successful main turns. This is a separate optional installation and a separate
owner setting, **disabled by default**. It needs no separate model API key.
Native compaction still uses the client's ordinary subscription, quota and
network connection. It summarizes the active model context so work can continue;
it does not delete the visible chat history or increase the model's context limit.

## Supported surfaces

| Client | Trigger and action | Requirements and limits |
|---|---|---|
| Claude Code terminal | A deferred `turn.complete` mod checks native session usage and requests `$.session.compact()` | Current Claude Code with mods; static validation is checked with 2.1.302. The plugin must be loaded into the session. |
| Claude Code in Desktop | The same mod chooses the native built-in `compact` command when the session reports Desktop without a terminal surface | Requires Desktop's mod support and native command availability. Ordinary Desktop Chat is a separate product surface and is unsupported. |
| Cursor IDE on macOS (experimental) | A completed `stop` hook checks cadence, then a bound foreground sidebar chat receives native `/summarize` through Accessibility | Explicit conversation/window/chat binding, accessible UI classes, focused empty chat input and proof that the agent is idle. Other layouts and unsupported platforms fail closed. Live AX selector compatibility has not been verified. |

Cursor's hook API exposes no documented operation that compacts an already open
IDE chat. Returning `/summarize` as `followup_message` submits ordinary message
text through the stop-hook path, so this adapter never returns a follow-up.
The UI adapter is intentionally narrow and may stop working after a Cursor UI
change. It never activates a background window or chooses a different chat.

## Install

From the Agents-Core checkout, using Python 3.11 or later:

```bash
python3 scripts/install_context_compaction.py --dry-run
python3 scripts/install_context_compaction.py
python3 -m src.context_compaction status
```

Select only one client with `--clients claude` or `--clients cursor`. The installer
registers the separate `agents-core-context-compaction` Claude plugin through
Claude's native CLI using this checkout's marketplace; the existing Agents-Core
MCP launcher plugin remains independent. A conflicting marketplace registration
or an unsupported native CLI command fails visibly rather than editing Claude's
plugin registry. Use a current Claude Code build. Reload plugins in an already
open Claude Code session with `/reload-plugins`.

Cursor receives a user-level `~/.cursor/hooks.json` `stop` handler and a private
Python runtime snapshot under `~/.cursor/hooks/`. Other hooks are preserved;
changed existing files have uniquely named sibling backups. The interpreter used
to run the installer is embedded in the hook command and must remain installed.
Cursor watches its hooks configuration; restart the app if it has not reloaded.
On macOS, Accessibility authorization must already allow the local hook's
automation host to inspect and control Cursor. Denied access skips the action.

Installation creates the configuration with `enabled: false`. Reinstallation
preserves an existing owner setting; only an explicit `--enable` or the command
below turns it on. A failed first installation cannot enable it.

## Owner setting

The default shared file is
`~/.config/agents-core/context-compaction.json`:

```json
{
  "schema_version": 1,
  "enabled": false,
  "min_context_tokens": 100000,
  "min_turns": 5,
  "cooldown_seconds": 900,
  "cursor_bindings": []
}
```

```bash
python3 -m src.context_compaction enable
python3 -m src.context_compaction disable
```

`AGENTS_CONTEXT_COMPACTION_CONFIG` selects an absolute alternate file. For a
stable explicit installation, use `--config /absolute/path/settings.json` with
the installer and management CLI; the installer passes that path to both clients.
Claude also accepts its plugin's `configPath` option. The environment override
takes precedence over explicit client/CLI paths and must be consistent across
the clients; an empty or relative override disables loading. Missing, unreadable, oversized,
malformed or symlinked configuration disables the feature. Only a boolean `true`
enables it. Updates written by the management CLI use atomic private files.

Claude requires both the context-token and completed-turn thresholds; Cursor
uses the turn threshold because its ordinary `stop` input contains no reliable
context-token measurement. `min_context_tokens` therefore applies only to Claude.
The shared cooldown applies to attempts, including refusals and errors, to avoid
repeated dispatch. Configuration ranges are 10,000–10,000,000 context tokens,
1–1,000 turns and 0–86,400 cooldown seconds. Keep the conservative defaults for
normal use. Neither adapter polls idle sessions or starts an extra model worker.

### Bind an open Cursor chat

Give the target window and chat distinct stable titles. Bind its exact
`conversation_id` from its Cursor hook input to those visible titles:

If the ID is not available in your hook diagnostics, leave compaction disabled
and temporarily append `--capture-identity /absolute/path/cursor-chat-id.json`
to this adapter's installed stop command in `~/.cursor/hooks.json`. Complete a
normal turn in the intended chat, read that small JSON file, and remove the
temporary argument immediately. This explicit setup mode writes only the
conversation ID and never dispatches compaction; no prompt, response, email or
transcript is recorded. Delete the diagnostic file after binding. Normal disabled
hooks do not collect IDs or write state. Avoid other Cursor chats during this
one-off capture so their events cannot replace the captured ID.

```bash
python3 -m src.context_compaction bind-cursor \
  --conversation-id 'the-conversation-id' \
  --window-title 'exact Cursor window title' \
  --chat-title 'exact active chat title'
```

Bindings are explicit owner configuration. Duplicate conversation IDs or duplicate
window/chat label pairs are rejected. At most 100 bindings are supported. A title
change requires updating the binding. Leave the intended sidebar chat open and
its empty input focused; the adapter skips when you switch windows, write a draft,
start another turn or use a layout it cannot identify. Cloud/background agents
and remote workspace hooks are unsupported.

## Execution and failure behavior

Claude lets the host finish the successful main turn before the deferred action,
then rechecks the switch, usage, cadence and idle eligibility. Aborted/error and
subagent turns do not request compaction. A pending/native attempt suppresses
recursion and duplicates; a failure is consumed by the cooldown, with no retry
timer. Plugin reload resets its in-memory cadence conservatively. Desktop's native
command API can queue the command until idle; a dispatched command cannot be
revoked by subsequently disabling the setting, and command dispatch does not
certify completed compaction. Organization-managed Claude Code can suppress
user-installed `session.compact` observation, so external native compactions may
not reset the mod's counters in that environment.

Cursor serializes each conversation with a nonblocking OS lock and stores only
hashed conversation/turn identities, counters, attempt time and outcome under
the configuration directory's `context-compaction-state/`. It rereads the switch
before dispatch, rechecks the selected UI immediately before typing and does not
overwrite drafts. A narrow race between the last Accessibility read and OS key
dispatch cannot be eliminated. This is why the adapter requires an explicit
binding and a foreground focused input.

An unreadable or corrupt cadence file skips compaction rather than forgetting
its cooldown. With compaction disabled, remove that conversation's hashed JSON
cadence file to reset it; do not remove an active `.lock` file.

Cursor reports `submitted` after dispatch; this is **not proof of completed
compaction**. UI failures after typing can have an unknown submission outcome and
still consume cooldown. The hook always returns `{}`. No credentials, client
chat databases or transcripts are read or rewritten, and no application bundle
or debug port is modified. Disabled mode performs no UI action or runtime-state
write. Native user-initiated compaction remains available independently.

## Disable and remove

`python3 -m src.context_compaction disable` stops future attempts in both adapters;
it cannot cancel a native operation already dispatched. To remove Claude's plugin,
use its native `/plugin uninstall agents-core-context-compaction@agents-core`
command. To remove the Cursor registration, delete only the stop entry whose
command names `agents-core-context-compaction.py` from `~/.cursor/hooks.json`.
The disabled runtime snapshot and hashed cadence state can then be removed.
Uninstalling Agents-Core's MCP launcher alone does not remove this separate opt-in.

## Verification

Offline checks use fake native APIs/UI and temporary configuration. Run the
[focused checks](../tests/README.md#checks-outside-pytest) before submission.
They establish policy and dispatch behavior, not live UI compatibility or the
quality of a model-generated summary.

For a live smoke check, use a disposable chat with recognizable task constraints,
install while disabled, bind Cursor if applicable, explicitly enable the feature,
and complete enough normal turns to cross the thresholds. Verify the client's
native compaction indicator and then continue the task with the retained
constraints. Check that a busy session, a draft, a different window/chat and the
disabled switch prevent dispatch. Turn the switch off afterward. This consumes
normal client usage and changes that chat's model context; automated tests never
perform it in the owner's existing chats.

Sources: [Claude mods reference](https://code.claude.com/docs/en/plugins/mods/reference),
[Claude mod creation and validation](https://code.claude.com/docs/en/plugins/mods/create),
[Cursor hooks](https://cursor.com/docs/hooks), and the inspected Cursor 3.24.12
sidebar implementation. The source-backed UI selectors are recorded beside the
adapter and must be reverified when Cursor's accessibility tree changes.
