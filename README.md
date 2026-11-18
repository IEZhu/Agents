# 🤖 Agents-Core

**Specialist roles, repeatable workflows and project memory for your AI coding assistant.**

Agents-Core is one local [MCP](https://modelcontextprotocol.io) server for Claude Code, Codex, Cursor, Claude Desktop, Antigravity and other MCP clients. It gives your current model a task-relevant expert persona with skills, shared rules and optional reasoning patterns called cognitive implants, Markdown workflows it executes with its own tools, bounded and validated delegated workers, and a memory of each project. The core server needs no external API key and runs on your machine.

**Documentation**: [documentation map](docs/README.md) · [agent guide](docs/agent-guide.md) · [features and client support](docs/features.md) · [install and connect](docs/install.md) · [install in another MCP client](llms-install.md)

[![Shared runtime (dev)](https://github.com/IEZhu/Agents/actions/workflows/shared-runtime.yml/badge.svg?branch=dev&event=push)](https://github.com/IEZhu/Agents/actions/workflows/shared-runtime.yml?query=branch%3Adev+event%3Apush)
[![Instruction signing tests (dev)](https://github.com/IEZhu/Agents/actions/workflows/instruction-signing.yml/badge.svg?branch=dev&event=push)](https://github.com/IEZhu/Agents/actions/workflows/instruction-signing.yml?query=branch%3Adev+event%3Apush)
[![Documentation checks (dev)](https://github.com/IEZhu/Agents/actions/workflows/documentation-quality.yml/badge.svg?branch=dev&event=push)](https://github.com/IEZhu/Agents/actions/workflows/documentation-quality.yml?query=branch%3Adev+event%3Apush)
[![MCP API reference coverage](docs/badges/mcp-api-docs.svg)](docs/mcp-tools.md)
[![User sync (dev)](https://github.com/IEZhu/Agents/actions/workflows/user-sync.yml/badge.svg?branch=dev&event=push)](https://github.com/IEZhu/Agents/actions/workflows/user-sync.yml?query=branch%3Adev+event%3Apush)
[![Vulnerability scan (dev)](https://github.com/IEZhu/Agents/actions/workflows/vulnerability-scan.yml/badge.svg?branch=dev&event=push)](https://github.com/IEZhu/Agents/actions/workflows/vulnerability-scan.yml?query=branch%3Adev+event%3Apush)

```bash
curl -fsSL https://raw.githubusercontent.com/IEZhu/Agents/main/install.sh | bash
```

One command installs the server on macOS or Linux (Python 3.11+, about 1.1 GB model download), registers it with the clients it finds and writes the protocol section into their instruction files; its defaults also add session hooks and, on macOS, the shared background service with cooperation. [Install and connect](docs/install.md) describes what it changes, Windows, the Claude Code plugin, manual setup, uninstalling and updates.

## Contents

- [What you get](#-what-you-get) and [supported clients](#-supported-clients)
- [When to use Agents-Core](#when-to-use-agents-core)
- [Quick start](#-quick-start)
- [First call](#first-call)
- [How it works](#-how-it-works)
- [Documentation](#-documentation), [checks](#checks) and [development](#development)

## ✨ What you get

| | Feature | What it does for you |
|---|---|---|
| 🎭 | [Specialist personas](docs/features.md#core-features) | Routes each task to an expert role (reviewer, architect, technical writer and more) with its skills and rules, and keeps that role across turns |
| 🔁 | [Repeatable workflows](flows/README.md) | PR review, documentation refresh, thread close and your own procedures, run by your model against your repository |
| 🤝 | [Delegated workers](docs/subagent-contract.md) | Bounded tasks for fresh reviewers or parallel workers, a fixed result format and validation, simple work on a lighter model |
| 🧠 | [Project memory](docs/memory.md) | A short description of each repository and a searchable log of earlier turns |
| 🔌 | [One server for every client](docs/features.md#client-integration) | A shared local service on macOS and Windows, managed instruction files, and your other MCP servers through one connection |
| 🔄 | [Sync and updates](docs/features.md#running-and-maintaining-the-installation) | Your personal flows and settings on every machine, and releases or every merged change through the update channel |

## 💻 Supported clients

| Client | Status | Connects through |
|---|:-:|---|
| Claude Code (CLI and desktop Code tab) | ✅ | Installer, [Claude Code plugin](plugins/agents-core/README.md) or the shared service; managed `CLAUDE.md` |
| Codex (CLI, desktop, IDE) | ✅ | Installer or the shared service; managed `AGENTS.md` |
| Cursor | ✅ | Installer or the shared service; protocol through MCP initialization |
| Antigravity | ✅ | Installer with a native plugin, or the shared service |
| Claude Desktop (chat) | ✅ | Installer or the shared service; protocol through MCP initialization |
| Other MCP clients, such as Cline | 🟡 | A hand-written stdio entry ([manual install](llms-install.md)) |

✅ means the installer sets the client up; what works in it differs. Claude Desktop chat starts outside any project, so workflows, repository memory and shared artifacts need a pinned project, and client hooks such as thread status, persona restore after compaction and native compaction exist only in some clients: see the [full feature matrix](docs/features.md).

## When to use Agents-Core

| Your task needs | Agents-Core provides | Entry point |
|---|---|---|
| A specialist approach to implementation, review, architecture or writing | A task-relevant persona with selected skills and rules | `route_and_load` or `get_agent_context` |
| A repeatable PR review, documentation refresh or other repository procedure | Markdown workflows for your current model to execute | `list_flows`, then `run_flow` |
| A bounded assignment for a reviewer or parallel worker | A compact role, acceptance criteria and a structured result contract | `get_subagent_context`, then `validate_subagent_result` |
| Context from earlier work on the same project | Repository description and interaction history | `describe_repo` and `read_history` |
| Shared exact inputs across workers | Persistent artifacts and pinned manifests | [Shared artifacts](docs/shared-artifacts.md) |

The core server needs no external API key. Setup requires Python 3.11+ and downloads a local embedding model (about 1.1 GB with the default configuration). Clients connect through standalone stdio or the shared local service; the Claude Code plugin launches a separately installed server. Native worker execution, file edits and shell commands remain the host client's responsibility. See the [agent guide](docs/agent-guide.md) for selection examples, data handling and limits.

## 🚀 Quick Start

The one-command installer above covers macOS and Linux. Everything else lives in one guide:

- [Install and connect Agents-Core](docs/install.md): what the installer changes, Windows setup, cloning for development, manual setup, per-client MCP configuration, the shared service, updates and uninstalling.
- [Claude Code plugin](plugins/agents-core/README.md): launcher prerequisites, setup and privacy policy.
- [Install in another MCP client](llms-install.md): a hand-written stdio entry, including Cline.
- [Configuration and observability](docs/configuration.md): environment variables, optional integrations and tracing.
- [User library sync](docs/user-sync.md): your personal flows, components and history on every machine.

## First call

Ask your assistant to use Agents-Core, and it activates a role before answering:

```text
Use Agents-Core and review this diff as a code reviewer.
```

The client calls `route_and_load` (or `get_agent_context` when the role is already
known), applies the returned persona, rules, skills and implants blocks, and ends
its answer with the bundle footer. A repeatable procedure goes through
`list_flows` and then `run_flow`:

```text
Use Agents-Core to run the pr-review flow on https://github.com/<owner>/<repo>/pull/<n>, no-merge.
```

The [agent guide](docs/agent-guide.md) shows the first calls with their expected
responses; [docs/mcp-tools.md](docs/mcp-tools.md) is the index of every public
tool with its maintained reference.

## 🎯 How It Works

The server exposes MCP tools that any compatible client can call. The
[MCP tool index](docs/mcp-tools.md) lists every one of them with its maintained
behaviour reference, and the [protocol API](docs/routing_flow.md#api) gives
parameters and responses. The server also registers MCP prompts (slash commands):
`ask` routes like `route_and_load`, one prompt per agent `routing.trigger_command`
and per `routing.aliases` entry loads that agent directly, and `describe_repo`
drives the repository memory bootstrap.

Shared task inputs travel as [persistent artifacts](docs/shared-artifacts.md):
publish once, pin the manifest into a worker's task contract, and the parent
validates the unchanged result and verifies the artifact evidence itself. The
optional [cooperation runtime](docs/agent-cooperation/README.md), disabled by
default, lets different AI clients on the same installed HTTP daemon exchange
messages and coordinate claims. The shared daemon can also proxy manually
configured HTTP and stdio MCP servers through the existing entry, so their tools
appear as `<alias>__<tool>` after reconnecting ([MCP proxy](docs/mcp-proxy.md)).

[Architecture](docs/architecture.md) shows the source layout and the key
components behind those tools.

### Workflows in the caller's repository

Ask the model to use Agents-Core to run `documentation-refresh` or `pr-review` in
the current repository. It calls `list_flows()` to discover flows and
`run_flow(flow="documentation-refresh")` to get the instructions; for review it
passes the PR/MR URL and constraints such as `no-merge` in `request`.

The flow files stay in the MCP installation, while inspection, edits, tests and
PR/MR actions target the caller's repository. The tool returns instructions and
the current model executes them with its own permissions and tools, so
`needs_execution` does not mean the work is complete. Most built-in flows name a
persona in their frontmatter, and `run_flow` then also returns a
`persona_activation` to apply as a switch. Users keep personal (`user:`) and
per-repository (`repo:`) flows in the installation's git-ignored `flows/.user`.
See the [workflow guide](flows/README.md#through-agents-core-mcp) for the response
contract, target resolution and authoring rules, and
[choose a flow's agent and components](flows/README.md#choose-a-flows-agent-and-components)
for persona selection.

### Persona continuity

Before each request the model silently checks whether its active role still fits
the task, and keeps it without routing when it does. Every successful switch,
restore or refresh replaces all four returned blocks — persona, rules, skills and
implants — while preserving higher-priority instructions, conversation facts,
goals and permissions. Protocol 2 has been the only protocol since 2026-09-29.

- [Routing, compatibility and migration](docs/routing_flow.md) is the complete
  contract, including [client decisions](docs/routing_flow.md#client-decisions)
  and [installation, migration and rollback](docs/routing_flow.md#installation-migration-and-rollback).
- [Session readiness](docs/session-readiness.md) and
  [thread status](docs/thread-status.md) cover the optional lifecycle hooks and
  their client limits; [native context compaction](docs/context-compaction.md) is
  a separate opt-in feature.
- [Worker contracts](docs/subagent-contract.md) and
  [model tiers](docs/subagent-model-tiers.md) cover delegated work and the
  lighter models simpler assignments run on.
- The [client protocol template](scripts/templates/routing-protocol-core.md) is
  generated from the section files in `src/engine/protocol/`; never edit a
  generated surface by hand.

## 📚 Documentation

- [Start here as an agent](docs/agent-guide.md): task-to-tool guide, first-call examples and operating limits.
- [Documentation map](docs/README.md): current guides, reference material, and evaluation reports.
- [Compact documentation index](llms.txt): direct Markdown links for agents discovering the project.
- [Model workflows](flows/README.md): reusable Markdown instructions, including the [documentation refresh process](flows/documentation-refresh.md).
- [Instruction signing](docs/instruction-signing-core.md): local key/trust controls and accepted coverage policy; runtime enforcement is still planned.
- [AI contributor instructions](AGENTS.md) and [Claude instructions](CLAUDE.md).

## Checks

Every badge at the top of this page, and the numeric documentation metric behind
it, is described in [quality checks](docs/quality-checks.md). The test guide is
[tests/README.md](tests/README.md), and
[docs/development.md](docs/development.md) covers the vulnerability gate and
debug logging.

## Development

Contributors start at [AGENTS.md](AGENTS.md), which states the worktree, branch
and review rules, and continue with the
[documentation refresh flow](flows/documentation-refresh.md) for documentation
changes. [Calendar releases](docs/releases.md) covers branches, versions and the
`dev` → `main` promotion, [tests/README.md](tests/README.md) the suite and its
focused selections, and [docs/development.md](docs/development.md) the
vulnerability gate and debug logging. New agents, skills and tiers are described
in the [agent catalog](agents/README.md#creating-a-new-agent).

## 📝 License

MIT — see [LICENSE](LICENSE).
