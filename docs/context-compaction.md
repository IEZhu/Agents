# Native context compaction

Agents-Core can request the signed-in Claude Code client's native compaction
between successful main turns. This is a separate optional installation and a
separate owner setting, **disabled by default**. It needs no separate model API
key. Native compaction uses the client's ordinary subscription, quota and network
connection. It summarizes the active model context so work can continue; it does
not delete the visible chat history or increase the model's context limit.

## Supported surfaces

| Client | Trigger and action | Requirements and limits |
|---|---|---|
| Claude Code terminal | A deferred `turn.complete` mod checks native session usage and requests `$.session.compact()` | Current Claude Code with mods; static validation is checked with 2.1.302. The plugin must be loaded into the session. |
| Claude Code in Desktop | The same mod chooses the native built-in `compact` command when the session reports Desktop without a terminal surface | Requires Desktop's mod support and native command availability. Ordinary Desktop Chat is a separate product surface and is unsupported. |
| Cursor IDE | **Unsupported.** The reserved adapter returns `unsupported` with reason `native_compaction_unavailable` | No verified, supported native action bound to an existing IDE chat is available to this adapter. Enabling the owner switch cannot enable this missing capability. |

Cursor automation is deferred. The former global keyboard transport has been
removed because a late focus change could send text or Return to unrelated input.
The adapter performs no Accessibility inspection, keyboard dispatch, clipboard
operation or client mutation. It never activates a window or chooses a chat.

Cursor's documented `stop.followup_message` submits a follow-up user message.
Static inspection of Cursor 3.24.12 confirmed that this path bypasses the chat
input's native `/summarize` interceptor. It is not a replacement for native
compaction. The inspected client has an internal conversation-bound summarize
action, but no supported external invocation route was verified. A future
transport requires a supported action confined to the intended chat and a live
native-compaction acceptance check. There is no global-key fallback.

## Install

From the Agents-Core checkout, using Python 3.11 or later:

```bash
python3 scripts/install_context_compaction.py --dry-run
python3 scripts/install_context_compaction.py
python3 -m src.context_compaction status
```

The default is `--clients claude`. The installer registers the separate
`agents-core-context-compaction` Claude plugin through Claude's native CLI using
this checkout's marketplace; the existing Agents-Core MCP launcher plugin remains
independent. A conflicting marketplace registration or an unsupported native CLI
command fails visibly rather than editing Claude's plugin registry. Use a current
Claude Code build. Reload plugins in an already open Claude Code session with
`/reload-plugins`.

Explicit `--clients cursor` or `--clients claude,cursor` installs Cursor's reserved
user-level `~/.cursor/hooks.json` `stop` handler and its private Python policy
runtime under `~/.cursor/hooks/`. **This does not enable Cursor compaction.** The
installer's JSON result reports `capabilities.cursor.status: "unsupported"` and
`reason: "native_compaction_unavailable"`, including in dry runs. Other hooks are
preserved; changed existing files have uniquely named sibling backups. The
interpreter used to run the installer must remain installed. The default install
does not create or update Cursor's hooks.

Installation creates the configuration with `enabled: false`. Reinstallation
preserves an existing owner setting; only explicit `--enable` or the command below
turns it on. A failed first installation cannot enable it. The shared switch
controls eligibility; it does not change which client capabilities are supported.

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

`AGENTS_CONTEXT_COMPACTION_CONFIG` selects an absolute alternate file. For a stable
explicit installation, use `--config /absolute/path/settings.json` with the
installer and management CLI. Claude also accepts its plugin's `configPath`
option. The environment override takes precedence over explicit client/CLI paths
and must be consistent across clients; an empty or relative override disables
loading. Missing, unreadable, oversized, malformed or symlinked configuration
disables the feature. Only a boolean `true` enables it. Management CLI updates use
atomic private files, and reject serialized settings larger than 64 KiB before
creating or replacing a configuration file.

Claude requires both the context-token and completed-turn thresholds. Refusals and
errors consume the cooldown to avoid repeated dispatch. Configuration ranges are
10,000 to 10,000,000 context tokens, 1 to 1,000 turns and 0 to 86,400 cooldown
seconds. Keep the conservative defaults for normal use. No adapter polls idle
sessions or starts an extra model worker.

`cursor_bindings` and the `bind-cursor` management command remain reserved policy
configuration. Bindings cannot make the native Cursor capability available. The
reserved stop hook retains per-conversation locking, deduplication, turn cadence
and cooldown; an otherwise eligible attempt records `unsupported`. It stores only
hashed conversation/turn identities, counters, attempt time and outcome under the
configuration directory's `context-compaction-state/`. Corrupt state skips the
attempt instead of forgetting cooldown. With the feature disabled, remove that
conversation's hashed JSON state to reset it; do not remove an active `.lock` file.

## Execution and failure behavior

Claude lets the host finish the successful main turn before the deferred action,
then rechecks the switch, usage, cadence and idle eligibility. Aborted/error and
subagent turns do not request compaction. A pending/native attempt suppresses
recursion and duplicates; a failure is consumed by cooldown, with no retry timer.
Plugin reload resets its in-memory cadence conservatively. Desktop's native
command API can queue the command until idle; a dispatched command cannot be
revoked by subsequently disabling the setting, and dispatch does not certify
completed compaction. Organization-managed Claude Code can suppress user-installed
`session.compact` observation, so external native compactions may not reset the
mod's counters in that environment.

Cursor's hook always returns `{}` and never a follow-up prompt. No credentials,
client chat databases or transcripts are read or rewritten, and no application
bundle or debug port is modified. Disabled mode performs no UI action or runtime
state write. Native user-initiated compaction remains available independently.

## Claude Code's auto-compact window

The plugin acts only after a completed main turn, and only when none of the
session's subagents or teammates is pending, running or waiting. A long autonomous
run, such as working through a PR queue with reviewers and workers, can last for
hours inside one turn, or across turns that each end while a subagent is still
running: the plugin does not act then, and only Claude Code's own auto-compaction
applies. Without a configured window, a local session on a model with a native 1M
context window (the Fable models, Sonnet 5 and later, Haiku 5.5, Opus 4.7 and
later) compacts at about 967K tokens, unless `CLAUDE_CODE_DISABLE_1M_CONTEXT=1`
holds it at 200K. Claude Code sends the whole conversation with every request;
prompt caching makes the repeated part cheaper, but usage still grows with the
context.

One measured day was dominated by an autonomous PR-queue session on Opus with the
1M window, about 81% of the day's usage. Its main thread grew to 963K tokens before
it compacted. Across the day's transcripts, an estimated 70% of usage went to
requests with more than 150K tokens of context and 40% to requests with more than
300K (weighted by API price ratios), and a 300K window was estimated to save 10 to
21% of that session's usage. These are estimates from one day's transcripts,
dominated by one session, so take 300K as a starting point for long autonomous
sessions, not a measured optimum: a smaller window compacts more often, each
compaction is one more summarization request and a prompt-cache rebuild, and what
the summary leaves out is gone. Short sessions never reach the window, so it
changes nothing for them.

Four places set the window. `/autocompact` stores one for the session's model; the
other three apply to whichever model the session runs:

- `/autocompact 300k` saves the window for the session's model under
  `modelSettings` in your user settings (Claude Code 2.1.288 or later) and applies
  it to the session. A window set for that model or for every model in a
  higher-priority scope, such as managed settings, outranks it, and the command
  says so. `/autocompact auto` returns to the model's default.
- `claude --autocompact 300k` sets it for one launch without saving it; managed
  settings do not outrank the flag.
- `"autoCompactWindow": 300000` at the top level of a settings file sets it for
  every model; a window saved for a model in the same file takes precedence.
- `CLAUDE_CODE_AUTO_COMPACT_WINDOW=300000`, for scripts and cloud environments,
  takes precedence over all of the above unless Claude Code ignores its value as
  unusable. Write a plain token count: a `k` or `M` suffix needs Claude Code
  2.1.304 or later, and a small number such as `300` reads as 300 tokens and is
  raised to the 100K minimum with a warning. While Claude Code uses the variable,
  `/autocompact` reports the override without saving.

The window accepts 100,000 to 1,000,000 tokens and is capped at the model's
context window. A subagent can compact earlier than it otherwise would with
`autoCompactWindow` in its frontmatter (Claude Code 2.1.296 or later). Write a
plain integer such as `300000`, because frontmatter reads `300k` as 300 and ignores
it, and set a value below the window the subagent would otherwise inherit. A
usable `CLAUDE_CODE_AUTO_COMPACT_WINDOW` overrides the field.

A compaction can drop the active persona's instructions. When session readiness is
installed with its `compact` group, that group asks the model to restore them
([persona restore after compaction](session-readiness.md#persona-restore-after-compaction)).

## Disable and remove

`python3 -m src.context_compaction disable` stops future Claude attempts; it cannot
cancel an operation already dispatched. To remove Claude's plugin, use its native
`/plugin uninstall agents-core-context-compaction@agents-core` command. If the
reserved Cursor hook was explicitly installed, delete only the stop entry whose
command names `agents-core-context-compaction.py` from `~/.cursor/hooks.json`.
The disabled runtime snapshot and hashed cadence state can then be removed.
Uninstalling Agents-Core's MCP launcher alone does not remove this separate opt-in.

## Verification

Offline checks use a fake Claude Mods API, injected Cursor policy transports and
temporary configuration. Run the
[focused checks](../tests/README.md#checks-outside-pytest) before submission. They
verify policy, installation and dispatch decisions without changing real chats.
Cursor policy tests do not establish a usable native Cursor transport.

For a Claude live smoke check, use a disposable chat with recognizable task
constraints, install while disabled, explicitly enable the feature, and complete
enough normal turns to cross both thresholds. Verify the native compaction
indicator, then continue the task with the retained constraints. Check busy,
aborted/subagent and disabled cases, and turn the switch off afterward. This uses
normal client allowance and changes that disposable chat's model context.
Automated tests never perform this in the owner's existing chats.

Sources: [Claude mods reference](https://code.claude.com/docs/en/plugins/mods/reference),
[Claude mod creation and validation](https://code.claude.com/docs/en/plugins/mods/create),
[Claude Code auto-compact window](https://code.claude.com/docs/en/model-config#set-the-auto-compact-window)
and [default thresholds](https://code.claude.com/docs/en/model-config#default-auto-compact-thresholds),
[`CLAUDE_CODE_AUTO_COMPACT_WINDOW` values](https://code.claude.com/docs/en/model-config#claude_code_auto_compact_window-values),
[`autoCompactWindow`](https://code.claude.com/docs/en/settings-reference#autocompactwindow),
[subagent auto-compact window](https://code.claude.com/docs/en/sub-agents#subagent-auto-compact-window),
[why usage climbs in a long session](https://code.claude.com/docs/en/costs#why-usage-climbs-in-a-long-session),
[Cursor hooks](https://cursor.com/docs/hooks),
[Cursor native summarization announcement](https://cursor.com/changelog/1-6#summarization-triggers),
and static inspection of Cursor 3.24.12's native submit and stop-hook handlers.
