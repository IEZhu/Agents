# Session readiness banner

The optional installer covers all five Agents-Core client families. Their native
lifecycle and display contracts differ, so it installs a matching adapter rather
than treating every client as Claude Code.

| Client | Configuration | Trigger and delivery |
|---|---|---|
| Claude Code; local Claude Desktop Code tab | Claude profile `settings.json` | `SessionStart` matching startup/clear; native `systemMessage` |
| Codex CLI, desktop and local IDE sessions using that profile | Codex profile `hooks.json` | `SessionStart` matching startup/clear; native `systemMessage`; verified CLI 0.162.1 runs it at the first user turn |
| Cursor local IDE/CLI | `~/.cursor/hooks.json` | `sessionStart`; `additional_context` requests the banner in the first response |
| Google Antigravity app/CLI/IDE | `~/.gemini/config/hooks.json` | Named `SessionStart` handler list; `injectSteps` requests the banner in the first response |
| Ordinary Claude Desktop Chat | Platform-specific `claude_desktop_config.json` | Separate read-only `Agents-Readiness` MCP server requests tool use and display in the first response; no native clear hook |

"First response" delivery is model-controlled. It cannot force an idle empty chat
to display content, and a model may fail to follow the display instruction. It is
not equivalent to an immediate native UI banner. Cursor/Antigravity new-conversation
events are used where no dedicated clear source is documented. Cursor cloud agents
do not load the local user hook and currently do not support the same `sessionStart`.

The message contains the local date, time, timezone and UTC offset; session
directory; Git branch, detached commit, clean/changed state and upstream divergence
when available; user, hostname, local IPv4 address; operating system and architecture;
and the model and permission mode when the client supplies them. `Ready` means the
banner has run, not that MCP servers, authentication or remote services were checked.
Git's changed count counts porcelain records, including untracked directories,
rather than individual files inside those directories.

## Install

From the installation checkout, using Python 3.11 or newer:

```bash
.venv/bin/python scripts/install_session_readiness.py --dry-run
.venv/bin/python scripts/install_session_readiness.py --clients all
```

All clients are selected by default. Use `--clients claude,codex,cursor,antigravity,desktop`
or any subset to choose adapters. The installer honors `CLAUDE_CONFIG_DIR`,
`CODEX_HOME`, and the existing Claude Desktop path/override resolution in
`src/client_paths.py`. It copies the standalone standard-library runtime into
each native profile's `hooks/agents-core-session-readiness.py`. The Claude and
Codex groups match `^(startup|clear)$`; Cursor uses a direct `sessionStart` list;
Antigravity uses a direct `SessionStart` list under `agents-core-readiness`.
The recorded interpreter path must remain installed.
On Windows, native commands use an explicit PowerShell encoded command with
UTF-16LE command encoding, UTF-8 event forwarding and exit-code propagation, so
Git Bash, cmd and PowerShell do not reinterpret literal paths. The runtime decodes
event bytes explicitly as UTF-8 and emits ASCII-safe JSON escaping for Unicode
fields, independent of Python's configured standard-stream encoding. Live Windows execution is not tested
by the macOS suite. A disabled Antigravity entry containing unrelated handlers is
rejected without enabling those handlers; rename the conflicting entry before
installing readiness.

For ordinary Claude Desktop Chat, the runtime and `session_readiness_mcp.py` are
copied under `agents-core-readiness/hooks/` next to its configuration. The
`Agents-Readiness` entry uses the installer interpreter. Use the Agents-Core
virtual environment: this helper needs the installation's existing `mcp` package.
It exposes only `get_session_readiness(workspace?)`, with read-only annotations,
plus initialization instructions to use it once per fresh conversation. Existing
native snapshots take precedence to avoid duplication in inherited Code sessions.
Repeated installation updates the owned hook without duplicating it. Existing
hooks, permissions, models and Claude's custom status line remain in place. Changed
configuration files receive an exclusively created adjacent `.backup.<unique-suffix>`
copy through the shared backup helper; existing snapshots are never overwritten.
A failed backup stops before runtime or configuration changes.
The main Agents-Core installers do not enable this optional hook automatically.

Start a fresh client session after installation. In Codex, open `/hooks` and review
and trust the new hook before it can run; the installer does not manufacture a
trust decision or bypass managed-hook policy. If the desktop app has no hook review
control, use the CLI with the same `CODEX_HOME` to review the definition. Managed
configuration can prevent user hooks from running. Current installed-client
behavior must be checked separately from the script's tests. Restart Claude
Desktop and enable the new `Agents-Readiness` server/tools if the app prompts.
An existing Code-tab setup is covered by the Claude Code profile. Cursor reloads
its hook configuration; start a new conversation. In Antigravity, use a fresh
conversation and check Customizations > Hooks. Older clients that do not implement
the documented event need upgrading; installing configuration alone cannot add it.

## Output and limits

The runtime reads event JSON on stdin. Claude/Codex return `{"systemMessage":"..."}`;
these clients document it as a user-facing warning/message. Cursor returns only
`additional_context`: its `sessionStart` ignores `user_message`. Antigravity
returns `injectSteps` containing its nested system-message object. Those two
adapters ask the model to render a quoted snapshot once, with snapshot values
explicitly treated as data. Cursor background-agent sessions return `{}` without
probing or requesting a banner. This is a snapshot, not a persistent custom footer
or a live clock.
In the verified Codex CLI/app-server 0.162.1 installation, `SessionStart:clear`
runs on the first user turn in the new chat. Clearing the terminal alone therefore
does not immediately display the banner. A local protocol probe with a deliberately
unreachable loopback model provider confirmed that the installed hook completes
and returns its warning entry before any model response. Desktop rendering was
not visually verified. In Claude Code 2.1.296, the startup banner was observed in
the interactive terminal; immediate clear-event rendering was not established
by clearing an empty conversation.
Plain hook stdout in Claude/Codex would become model context instead of a visible
banner; those adapters return no `additionalContext`. Claude may discard a
SessionStart hook's result if another clear or conversation switch happens before
it finishes. Client rendering and placement vary.

Cursor uses `workspace_roots` and Antigravity uses `workspacePaths`/`modelName`.
When a workspace is missing, these adapters show `(workspace not bound)` instead
of accidentally reporting the directory where the global hook process starts.
The Desktop MCP helper accepts an explicit existing absolute directory or reads
local MCP roots when advertised, with a bounded wait. It never assumes that its
process directory is the chat's project. Without a root it reports the same
unbound state and omits Git. Host/IP always describe the local machine executing
the helper, which need not be a remote chat's frontend machine.

Git and route/interface probes share a 1.5-second budget and each subprocess has a
maximum 0.75-second timeout. IP discovery reads local state on macOS and Linux,
without external requests or DNS. Windows still displays the other fields, with
`IP: unavailable`. Missing route access or an offline host also displays
`IP: unavailable`. Failed Git probes omit the Git segment, without claiming a
clean working tree. No credentials, remote URLs, transcript content or public IP
services are read.

Preview the real output without clearing a conversation:

```bash
.venv/bin/python scripts/session_readiness.py --text < /dev/null
```

Use `--client cursor` or `--client antigravity` with event JSON on stdin to preview
those adapters. To remove an adapter, delete only its owned command from Claude/
Codex `SessionStart`, Cursor `sessionStart`, or Antigravity's named
`agents-core-readiness` entry. For Desktop Chat, remove only
`mcpServers.Agents-Readiness`. Remove copied files after unlinking the adapter.
Avoid restoring the entire backup after other settings have changed.

Official references: [Claude Code hooks](https://code.claude.com/docs/en/hooks#sessionstart),
[Codex hooks](https://learn.chatgpt.com/docs/hooks),
[Codex `/clear`](https://learn.chatgpt.com/docs/developer-commands?surface=cli),
[Cursor hooks](https://cursor.com/docs/hooks),
[Antigravity hooks](https://www.antigravity.google/docs/hooks), and
[Claude Chat versus Code/Cowork hooks](https://support.claude.com/en/articles/13837440-use-plugins-in-claude).
Cursor can also import Claude hook configurations; the imported native
`systemMessage` does not replace the Cursor-specific `additional_context` adapter.
Do not enable duplicate Cursor adapters through both user and project configuration.

## Verification

```bash
LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/test_session_readiness.py -q
```

The checks cover actual clean/dirty/detached Git repositories, no repository,
event directory and optional fields, JSON output, malformed input, bounded failed
probes, local IP parsing, terminal control characters, existing hooks/settings,
profile paths with spaces, backups and repeat installation. They do not establish
interactive rendering in a client. Additional checks cover all five configuration
shapes, preserving unrelated MCP servers, Cursor/Antigravity payloads and output,
background-session suppression, unbound workspaces, and a real stdio MCP handshake,
tool listing and tool calls. No test calls a model.
