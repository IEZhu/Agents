# Features and client support

Agents-Core is one local MCP server. Its core features are MCP tools and
prompts, so any MCP client can call them; what differs between clients is how the
server is installed, how the protocol instructions reach the model, whether a
call belongs to a project and which client hooks exist. Each feature links to its
maintained guide.

**Legend:** ✅ supported · 🟡 partial, see the note under the table ·
❌ not supported · ➖ not applicable: the feature belongs to another client.

**Columns:** *Claude Code* is the CLI and the Code tab of the Claude desktop app;
*Claude Desktop* is the desktop app's ordinary chat; *Codex* covers the CLI,
desktop app and IDE extension, which share one profile; *Other MCP clients* are
clients with a hand-written stdio entry, such as Cline
([manual install](../llms-install.md)).

The cells come from the source and the guides linked in each row. ✅ means
implemented and documented; it does not mean every client was tested live.

## How each client connects

| Client | Connects through | Protocol instructions | Set up by |
|---|---|---|---|
| Claude Code | A stdio entry in the user scope of `~/.claude.json`, the [Claude Code plugin](../plugins/agents-core/README.md), or a bridge to the shared service | Managed section in `~/.claude/CLAUDE.md` | [Installer](../README.md#one-command-install-macos-and-linux) or plugin |
| Claude Desktop | The `Agents-Core-Desktop` stdio entry or a bridge to the shared service | MCP initialization text | Installer |
| Codex | A stdio entry in `config.toml` or a bridge to the shared service | Managed section in `AGENTS.md` | Installer |
| Cursor | A stdio entry in `mcp.json` or a bridge to the shared service | MCP initialization text | Installer |
| Antigravity | A stdio entry with a native plugin (startup rules and a workspace hook) or a bridge to the shared service | Plugin rules file | Installer |
| Other MCP clients | A hand-written stdio entry | MCP initialization text; copy the [routing template](../scripts/templates/routing-protocol-core.md) for persistent instructions | [Manual install](../llms-install.md) |

The [shared local service](../README.md#shared-macos-and-windows-service) runs
on macOS and Windows; on Linux every client starts its own standalone stdio
server.

## Core features

These are MCP tools and prompts, so every client that reaches the server can call them. The
differences come from the protocol instructions, the project a call belongs to
and the host's own worker tools.

| Feature | What you get | Claude Code | Claude Desktop | Codex | Cursor | Antigravity | Other MCP clients |
|---|---|:-:|:-:|:-:|:-:|:-:|:-:|
| [Specialist personas](routing_flow.md#api) | The right expert role, with its skills and rules, loaded for the task and kept across turns | ✅ | 🟡 ¹ | ✅ | 🟡 ¹ | 🟡 ¹ | 🟡 ¹ |
| [Slash commands](routing_flow.md#api) | `/ask` or a named specialist started from a command instead of a tool call | ✅ | ✅ | 🟡 ² | 🟡 ² | 🟡 ² | 🟡 ² |
| [Workflows](../flows/README.md#through-agents-core-mcp) | Built-in procedures, such as PR review or a documentation refresh, run against your repository with your model's own tools | ✅ | 🟡 ³ | ✅ | ✅ | ✅ | 🟡 ³ |
| [Personal and repository workflows](../flows/README.md#personal-and-repository-flows) | Your own procedures, saved globally or per project, each with the persona it runs as | ✅ | 🟡 ³ | ✅ | ✅ | ✅ | 🟡 ³ |
| [Delegated workers](subagent-contract.md#compact-exchange) | A bounded task for a fresh worker with a fixed result format, validated against its contract | ✅ | 🟡 ⁴ | ✅ | 🟡 ⁵ | 🟡 ⁴ | 🟡 ⁴ |
| [Worker model tiers](subagent-model-tiers.md#model-resolution) | Simple delegated tasks on a lighter model, moved up one tier at a time when a result falls short | ✅ | ❌ | ✅ | 🟡 ⁶ | ❌ | ❌ |
| [Repository memory](../README.md#-repository-memory) | A short description of each project, returned when work starts | ✅ | 🟡 ³ | ✅ | ✅ | ✅ | 🟡 ³ |
| [Turn log and history](shared-mcp-daemon.md#memory-and-errors) | Every answer recorded with its persona, and earlier turns of a project to read or search | ✅ | 🟡 ⁷ | ✅ | ✅ | ✅ | 🟡 ⁷ |
| [Shared artifacts](shared-artifacts.md#native-worker-workflow) | The same exact inputs for several workers, pinned by hash instead of pasted into every prompt | ✅ | 🟡 ³ | ✅ | ✅ | ✅ | 🟡 ³ |
| [Agent cooperation](agent-cooperation/runtime.md#enablement-and-authority) | Overlapping sessions, even in different clients, that message each other and claim work instead of colliding | ✅ ⁸ | 🟡 ³ ⁸ | ✅ ⁸ | ✅ ⁸ | ✅ ⁸ | 🟡 ³ ⁸ |
| [Rules from URLs](user-rules.md#mcp-operations) | A team rule file at an HTTPS address, added to every persona and worker package | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |
| [Local inference](shared-mcp-daemon.md#local-inference) | One bounded completion on a local model the owner configured | ✅ ⁹ | 🟡 ³ ⁹ | ✅ ⁹ | ✅ ⁹ | ✅ ⁹ | 🟡 ³ ⁹ |

1. The protocol reaches these clients through the MCP initialization text, or
   through the Antigravity plugin's rules; persona behavior was measured on
   Claude Code and Codex only.
2. The server offers the prompts to every client; whether a client shows them
   as slash commands depends on the client.
3. These tools need the project a call belongs to: an MCP root, a launch
   directory inside a project, `AGENTS_CLIENT_REPO_ROOT`, or the registered
   workspace over HTTP. The desktop chat starts servers outside any project.
4. These clients get no launch arguments, and their transcripts are not read:
   the host launches the worker with its own tool where it has one, and
   validation takes the result text or a result file.
5. Cursor gets launch arguments, but its transcripts are not read: validate the
   result text or a result file.
6. The light tier passes Cursor's default model; Cursor's acceptance of that
   value is not verified.
7. Without a project, turns go to the installation's fallback journal.
8. Needs the shared service (direct HTTP or its stdio bridge) with cooperation
   enabled; standalone stdio has no coordinator. Setup on macOS and Windows offers
   both, and the one-command install (`--yes`) accepts them; the server setting
   alone is off by default.
9. Owner-enabled and off by default; needs the shared service, and native
   inference runs on POSIX hosts only.

## Client integration

| Feature | What you get | Claude Code | Claude Desktop | Codex | Cursor | Antigravity | Other MCP clients |
|---|---|:-:|:-:|:-:|:-:|:-:|:-:|
| [One-command installer](../README.md#one-command-install-macos-and-linux) | The server installed and registered with every supported client it finds | ✅ | ✅ | ✅ | ✅ | ✅ | 🟡 ¹ |
| [Managed instruction files](routing_flow.md#installation-migration-and-rollback) | The persona protocol kept current in the client's own instruction file, your text left alone | ✅ | 🟡 ² | ✅ | 🟡 ² | ✅ | 🟡 ³ |
| [Claude Code plugin](../plugins/agents-core/README.md#setup) | The server launched from a marketplace plugin instead of a registration written by setup | ✅ ⁴ | ➖ | ➖ | ➖ | ➖ | ➖ |
| [Shared local service](../README.md#shared-macos-and-windows-service) | One background server holding the model for every client and session, on macOS and Windows | ✅ | ✅ | ✅ | ✅ | ✅ | 🟡 ⁵ |
| [MCP proxy](mcp-proxy.md#connect-a-server) | Your other MCP servers through the one Agents-Core connection, with per-app access and credentials in the OS secret store | ✅ ¹² | ✅ ¹² | ✅ ¹² | ✅ ¹² | 🟡 ⁶ ¹² | 🟡 ⁵ ¹² |
| [Session readiness banner](session-readiness.md#session-readiness-banner) | A snapshot of time, directory, Git state and host when a session starts | ✅ | 🟡 ⁷ | 🟡 ⁸ | 🟡 ⁷ | 🟡 ⁷ | ❌ |
| [Persona restore after compaction](session-readiness.md#persona-restore-after-compaction) | A reminder to reload the active persona right after the client compacts the conversation | ✅ | ❌ | 🟡 ⁹ | ❌ | ❌ | ❌ |
| [Emoji thread status](thread-status.md#client-support) | One emoji in the chat title for working, waiting, review or closed | ✅ | ❌ | 🟡 ¹⁰ | 🟡 ¹¹ | ❌ | ❌ |
| [Native context compaction](context-compaction.md#supported-surfaces) | Opt-in compaction between completed turns, through the client's own command | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| [Claude Code cloud sessions](cloud-runs.md#cloud-environment-with-agents-core) | A working server in a Claude Code cloud environment from one setup script | ✅ | ➖ | ➖ | ➖ | ➖ | ➖ |

1. The installer sets up the server; add the client's stdio entry by hand.
2. No instruction file of its own: the protocol arrives in the MCP
   initialization text.
3. Copy the routing template into the client's instruction file yourself.
4. macOS and Linux; on Windows, run the server installer instead.
5. A hand-written stdio entry that starts the server is handed to the shared
   service when one is installed.
6. The proxy accepts Antigravity as an app; no documented example or live check
   yet.
7. The model is asked to show the banner in its first answer; the desktop chat
   uses the separate read-only `Agents-Readiness` helper.
8. Needs review and trust of the hooks in Codex `/hooks`.
9. The hook is installed; not yet verified on an installed client.
10. The desktop app renames through its own tool; the CLI and IDE need an
    exposed rename tool or the metadata helper.
11. The Agents window only; the classic sidebar has no title write.
12. Needs the shared service (macOS and Windows); standalone stdio exposes no
    proxy tools.

## Running and maintaining the installation

These features do not depend on the client you chat in.

| Feature | What you get | Where it runs |
|---|---|---|
| [Personal library sync](user-sync.md#user-library-sync) | Your personal flows, persona choices and component switches kept identical on all your machines through one private Git repository you own | Any installation; off until you set it up |
| [Updates and update channels](../README.md#update-channel) | Releases from `main` or every merged change from `dev`, with a rollback path on the shared service | Standalone servers update themselves; the shared service has an opt-in updater |
| [Web settings page](shared-mcp-daemon.md#flow-editor) | A local browser page to edit flows, switch rules, skills and implants, run sync, choose the update channel and manage MCP connections | The shared service on macOS and Windows |
| [Autodev](autodev.md#autodev) | Scheduled, sandboxed self-development: a session implements an issue you marked ready in the configured repository and the runner opens a pull request only you can merge | macOS; runs the Claude Code CLI, with optional Codex reviews that fall back to Claude where its sandbox cannot run; off by default |
| [Cloud issue agent](cloud-runs.md#issue-agent) | `/agent` commands in a GitHub issue or pull request that plan, implement and review in a cloud session | A Claude Code cloud routine; per-repository setup |
| [Evaluations](README.md#evaluation-guides) | Deterministic routing and retrieval checks and hosted-model A/B runs before a change ships | Local scripts and hosted model APIs |
| [Observability](../README.md#-observability) | Traces of routing, loading, retrieval and memory calls in your own Langfuse project | Off until both Langfuse keys are set |
| [Instruction signing](instruction-signing-core.md) | Local signing and verification of instruction files; runtime enforcement is planned | Local command line |
| [Distribution](distribution.md#submitted-destinations) | The Claude Code plugin marketplace in this repository and a Glama directory listing | GitHub and Glama |
