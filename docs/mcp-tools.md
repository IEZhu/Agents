# MCP tool index

Use this index to find the maintained behavior reference for every tool served by
the main Agents-Core server. Start with the [agent guide](agent-guide.md) for a
task-oriented introduction. The connected server's `tools/list` schema supplies
the exact arguments and defaults for the installed revision; this page does not
replace that schema or grant permission to call a mutating tool.

The registrations live in [server.py](../src/server.py),
[artifact tools](../src/artifacts/tools.py) and
[local inference tools](../src/local_inference/tools.py). The optional
[document OCR server](../src/mcp_servers/document_ocr/README.md) has its own
connection and tools. Agent slash commands and `/ask` are MCP prompts rather
than tools; see [routing](routing_flow.md).

The static index and its badge count native registered tools. The
[MCP proxy](mcp-proxy.md) adds app-scoped HTTP/stdio aliases from cached upstream
catalogs, such as `blincho__get_status`; those dynamic names are not part of this
denominator. Their schemas and availability depend on the confirmed upstream
revision, the local app selection and an MCP reconnect. The proxy reuses the
existing daemon endpoint; dynamic aliases do not add native registrations.

## Personas and discovery

| Tool | Purpose and boundary | Reference |
|---|---|---|
| `list_agents` | Inspect installed role metadata; does not activate a persona | [Agent catalog](../agents/README.md) |
| `route_and_load` | Initial role selection or a needed specialization switch; finish `ROUTE_REQUIRED` with a load | [Routing protocol](routing_flow.md) |
| `get_agent_context` | Load an explicitly selected role, or restore it with the retained descriptor | [Routing protocol](routing_flow.md) |
| `refresh_persona_context` | Refresh components for the same role; replace a complete changed bundle | [Routing protocol](routing_flow.md) |
| `load_implants` | Retrieve cognitive strategies by query or task type; does not activate a persona | [Implants](../implants/README.md) |
| `clear_session_cache` | Administrative shared-cache reset; unnecessary for role switches | [Routing protocol](routing_flow.md) |

## Repository context and turn history

Pass the caller's absolute `workspace` where supported. HTTP uses the registered
workspace header; a `workspace` argument does not rebind that connection. Stdio
has its own root-selection rules. See [workspace targeting](routing_flow.md).

| Tool | Purpose and boundary | Reference |
|---|---|---|
| `describe_repo` | Read context or return `needs_summary`; does not write project files | [Repository memory](../README.md#-repository-memory) |
| `write_repo_summary` | Explicitly persist a description using its repository fingerprint; refuses installation/source roots | [Repository memory](../README.md#-repository-memory) |
| `read_history` | Read recent, semantic or exact-entry history; inspect returned workspace and errors | [Memory operations](shared-mcp-daemon.md#memory-and-errors) |
| `log_interaction` | Record the exact composed answer (without the time line) and the verbatim request, both with secrets and capability values replaced by safe references and prohibited personal details omitted, together with the retained persona attribution, before delivery | [Routing protocol](routing_flow.md) |

## Reusable flows and user rules

Flow mutations concern personal/repository copies or overlays. Editing a built-in
source file for a repository contribution belongs in a development worktree.
URL rules are separate subscriptions; changes do not alter already issued
persona bundles. Read before editing and use the current revision for tools
that require optimistic concurrency.

| Tool | Purpose and boundary | Reference |
|---|---|---|
| `list_flows` | Discover built-in, personal and repository procedures and overrides | [Workflow catalog](../flows/README.md) |
| `get_flow` | Read current text, upstream text or a saved version before an edit | [Workflow library](../flows/README.md) |
| `run_flow` | Return a user-requested procedure bound to its target; `needs_execution` requires the current model to execute it | [Workflow execution](../flows/README.md) |
| `save_flow` | Save complete Markdown to a personal or repository flow with the expected revision | [Workflow library](../flows/README.md) |
| `delete_flow` | Remove a requested local flow; saved history remains | [Workflow library](../flows/README.md) |
| `set_flow_persona` | Set or reset the user's agent/component overlay without copying built-in text | [Flow personas](../flows/README.md) |
| `list_user_rules` | List subscriptions and cached refresh errors without fetching sources | [User rules](user-rules.md) |
| `get_user_rule` | Read accepted content, revision and cached versions | [User rules](user-rules.md) |
| `subscribe_user_rule` | Fetch and validate a requested HTTPS MDC rule; token values stay outside arguments | [User rules](user-rules.md) |
| `refresh_user_rule` | Explicitly fetch an existing subscription; a failed fetch preserves accepted content | [User rules](user-rules.md) |
| `set_user_rule_enabled` | Enable or disable an accepted subscription without a fetch | [User rules](user-rules.md) |
| `rollback_user_rule` | Restore a cached content hash without a fetch; preserve the enabled state | [User rules](user-rules.md) |
| `delete_user_rule` | Remove the subscription and cached versions; local rollback is unavailable | [User rules](user-rules.md) |

## Delegated tasks and exact artifacts

Preparation does not launch a worker. The host launches it with the receipt's
`launch` text (inline delivery: the compact package), and the parent keeps the
receipt's `contract_revision` (inline: the original contract). Structural
validation and artifact integrity checks have different purposes; neither
proves that a worker's conclusions are correct.

| Tool | Purpose and boundary | Reference |
|---|---|---|
| `get_subagent_context` | Prepare one scoped assignment and its contract; by default store the package in a private file and return a receipt with the worker's `launch` text; plain-text or JSON result format; optional model-tier selection | [Worker contract](subagent-contract.md), [model tiers](subagent-model-tiers.md) |
| `validate_subagent_result` | Validate every returned/revised result (text or JSON, or a `result_ref` to a result file or worker transcript) against the contract stored at preparation (`contract_revision`) or retained from an inline package | [Worker contract](subagent-contract.md) |
| `artifact_publish` | Publish immutable UTF-8 task data in the workspace store | [Shared artifacts](shared-artifacts.md) |
| `artifact_manifest_create` | Pin selected artifacts, hashes and purposes for a task | [Shared artifacts](shared-artifacts.md) |
| `artifact_manifest_get` | Read the exact active pinned manifest | [Shared artifacts](shared-artifacts.md) |
| `artifact_read` | Read verified UTF-8 byte slices through `next_offset` until `eof` | [Shared artifacts](shared-artifacts.md) |
| `artifact_manifest_verify` | Recheck access, active lifetime and every pinned artifact before acceptance | [Shared artifacts](shared-artifacts.md) |
| `artifact_manifest_release` | End future access without deleting stored bytes | [Shared artifacts](shared-artifacts.md) |

## Cooperation between executions

The owner must enable one shared daemon authority. Capabilities remain private.
Declarations and resource claims coordinate participants; they do not fence
filesystem writes or grant host permissions. Complete request/response schemas
are available as `agents-core://cooperation/schema` from the connected server.

| Tool | Purpose and boundary | Reference |
|---|---|---|
| `cooperation_register` | Register a bounded execution scope and retain its private capability | [Runtime operations](agent-cooperation/runtime.md) |
| `cooperation_read` | Read paginated public state/events at a consistent revision | [Runtime operations](agent-cooperation/runtime.md) |
| `cooperation_apply` | Apply an authenticated revision/generation-checked metadata action | [Runtime operations](agent-cooperation/runtime.md) |
| `cooperation_send` | Persist an authorized private message; delivery is not consent | [Mailbox workflow](agent-cooperation/runtime.md#mailbox-workflow) |
| `cooperation_receive` | Read pending private inbox messages without acknowledging them | [Mailbox workflow](agent-cooperation/runtime.md#mailbox-workflow) |
| `cooperation_ack` | Acknowledge delivery without accepting work, artifacts or agreements | [Mailbox workflow](agent-cooperation/runtime.md#mailbox-workflow) |
| `cooperation_request_status` | Inspect request delivery separately from declared processing state | [Request tracking](agent-cooperation/runtime.md#request-tracking-and-event-waits) |
| `cooperation_request_update` | Update a request with its current revision and a stable operation ID | [Request tracking](agent-cooperation/runtime.md#request-tracking-and-event-waits) |
| `cooperation_wait` | Await bounded durable notifications with a separate cursor; never wakes an idle model | [Event waits](agent-cooperation/runtime.md#request-tracking-and-event-waits) |

## Optional local completions

The shared HTTP daemon brokers these tools only when the owner enables and
configures the capability. Status reads start no model. Context and grant
capabilities stay outside prompts and logs. Generation is one bounded textual
completion, with explicit polling and cancellation; it is not a native worker
or a tool-execution loop.

| Tool | Purpose and boundary | Reference |
|---|---|---|
| `local_inference_status` | Read enablement, readiness, capabilities and owner limits | [Local inference operations](shared-mcp-daemon.md#local-inference) |
| `local_context_create` | Capture immutable shared text, allowed files or a pinned manifest without starting a model | [Local inference operations](shared-mcp-daemon.md#local-inference) |
| `local_context_grant` | Create a separate bounded access grant; caps do not reserve shared credits | [Local inference operations](shared-mcp-daemon.md#local-inference) |
| `local_context_revoke` | Revoke a child grant and fence its active work | [Local inference operations](shared-mcp-daemon.md#local-inference) |
| `local_generate` | Submit one idempotently identified bounded completion and receive its job ID | [Local inference operations](shared-mcp-daemon.md#local-inference) |
| `local_generation_result` | Read retained job state/output; disconnecting a poll does not cancel it | [Local inference operations](shared-mcp-daemon.md#local-inference) |
| `local_generation_cancel` | Explicitly cancel a job for all pollers | [Local inference operations](shared-mcp-daemon.md#local-inference) |
| `local_context_release` | Release a context and fence its jobs and future access | [Local inference operations](shared-mcp-daemon.md#local-inference) |

## Keep the index current

From the repository root, run:

```bash
.venv/bin/python scripts/check_docs.py
```

The check compares these tool rows with production decorators and the artifact
and local-inference registration lists, without importing the server or starting
models. It also rejects a stale [MCP API docs badge](badges/mcp-api-docs.svg);
regenerate it with `--update-badge` after changing tool/index coverage. The
[quality guide](quality-checks.md) defines the metric's limits.
The checker also checks tracked Markdown links, images and heading/explicit HTML
anchors. Supply paths to include newly created untracked documents before
staging. It does not fetch external URLs, validate arbitrary HTML navigation or
prove semantic documentation coverage; use the
[documentation refresh](../flows/documentation-refresh.md) for that review.
