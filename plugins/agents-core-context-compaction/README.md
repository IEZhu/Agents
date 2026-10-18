# Agents-Core Context Compaction

An optional Claude Code mod that requests native conversation compaction after a successful main conversation answer. Installing or loading it does not enable compaction. The shared configuration must contain literal `"enabled": true`.

The mod uses the existing Claude session. It reads no credentials, invokes no external processes or HTTP APIs, and submits no prompts. Native compaction can use the session's normal model allowance.

## Install

```text
/plugin install agents-core-context-compaction --marketplace IEZhu/Agents
/reload-plugins
```

For the shared configuration, Cursor adapter and separate explicit enable command,
see [the installation guide](https://github.com/IEZhu/Agents/blob/dev/docs/context-compaction.md).
Installing the plugin alone keeps automatic compaction disabled.

## Configuration

The default configuration is `~/.config/agents-core/context-compaction.json`. An absolute `AGENTS_CONTEXT_COMPACTION_CONFIG` environment variable takes precedence over the plugin's optional `configPath` setting, which takes precedence over that default. An empty `configPath` uses the default; an invalid environment override disables the mod. Missing, unreadable, oversized, invalid, or symlinked configuration is disabled.

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

Omitted known fields use these defaults. Unknown fields are rejected. `min_context_tokens` accepts integers from 10000 to 10000000, `min_turns` from 1 to 1000, and `cooldown_seconds` from 0 to 86400. Configuration is limited to 64 KiB. The shared `cursor_bindings` field is validated but has no effect on this mod.

The mod counts successful main answers since its last attempt or an observed successful compaction. Counters and cooldown are local to the loaded mod and reset when the mod reloads or the session changes. A refusal or failed attempt consumes the accumulated turns and starts the cooldown too. No retry timer is created.

## Native execution

After forwarding `turn.complete`, the mod schedules a one-shot clock callback outside the completion hook. It checks the current configuration again, the native context token count, worker activity, thresholds, and cooldown. A new main turn, worker spawn or worker model step invalidates pending work. Failed, refused, interrupted and subagent answers cannot trigger it. Configuration is checked immediately before dispatch as well, so disabling it cancels deferred work.

Terminal sessions use `$.session.compact()`. A native `{ skip }` result is respected. Exceptions and malformed results do not trigger another route.

Sessions whose native surfaces include Desktop and exclude the terminal use `$.command.run({ command: 'compact' })`. The mod first verifies that `compact` is a built-in command. This selects Desktop's native `/compact` route without relying on an undocumented error message. The command API can queue work until the host is idle; its return value does not certify that compaction succeeded. A failed command is not retried. Sessions that also expose the terminal use `$.session.compact()`.

The implementation targets the current [Mods API declarations](https://github.com/anthropics/claude-code/blob/main/mods/types/claude-code.d.ts) and was statically validated with Claude Code 2.1.302. See the official [mod reference](https://code.claude.com/docs/en/plugins/mods/reference) and [mod creation guide](https://code.claude.com/docs/en/plugins/mods/create). Organization policy may prevent installed user mods from observing `session.compact`; in that case an external compaction cannot reset this mod's counters through that hook.

## Offline verification

From the repository root:

```sh
node --test plugins/agents-core-context-compaction/tests/*.test.js
claude plugin validate ./plugins/agents-core-context-compaction
```

The Node tests use a fake Mods API and deterministic timers. Neither command starts a model session or performs compaction. The validator's hook and call inventory makes the mod's capabilities reviewable before installation.
