# Choose and use Agents-Core

Agents-Core supplies specialist context and reusable procedures to an AI assistant through MCP. The assistant keeps its current model and performs the work with its existing tools. Start here to decide whether it fits the user's task; use [installation instructions](../llms-install.md) when a local server is needed.

## Match the task to a tool

| Need or example request | Start with | What to expect |
|---|---|---|
| "Help implement this feature" or "review this design" | `route_and_load` | A complete persona bundle, or `ROUTE_REQUIRED` with candidates for the caller to choose from |
| "Use the code reviewer role" | `get_agent_context` with `agent_name="code_reviewer"` | Direct activation of the named role |
| "Which specialist roles are installed?" | `list_agents` | The installed catalog; discovery alone does not activate a role |
| "Run the PR review procedure" or "refresh these docs using our procedure" | `list_flows`, then user-requested `run_flow` | Markdown instructions bound to the caller's repository; the current model continues execution |
| "Give a worker a bounded review task" | `get_subagent_context` | A compact worker context, the original contract to retain and a fixed result schema |
| "What context do we have from earlier work here?" | `describe_repo` or `read_history` | Repository context or existing journal entries; an empty history is possible |
| "Give several workers the same exact input" | Artifact publication and manifest tools | Persistent UTF-8 inputs pinned by identity and hash; see [shared artifacts](shared-artifacts.md) |

Use the connected server's tool schemas for the installed version. Catalogs and examples in this repository describe their own revision. The [agent catalog](../agents/README.md) and [workflow catalog](../flows/README.md) let a discovering assistant inspect the available content before installation.

## A first persona call

After the server connects, call `list_agents` to inspect the installed catalog. For a conversation without an active persona, the MCP arguments for `route_and_load` can be:

```json
{
  "query": "Review this Python change for correctness and missing tests.",
  "protocol_version": 2,
  "current_persona": null
}
```

If the response is `ROUTE_REQUIRED`, select the best canonical candidate and call `get_agent_context` in the same turn. For a response listing `code_reviewer`, the arguments are:

```json
{
  "agent_name": "code_reviewer",
  "query": "Review this Python change for correctness and missing tests.",
  "protocol_version": 2,
  "current_persona": null
}
```

Apply only a complete `SUCCESS`: its `replaces_activation_id` must match the active activation, or be null for this initial activation. Replace the four returned instruction blocks together and retain the exact `persona` descriptor and `footer`. `NO_CHANGE` keeps the existing bundle; an error does not activate a role. Follow the returned application and logging instructions before answering. See the [full protocol](routing_flow.md) for restore, refresh and unavailable-server behavior.

For later requests that fit the active role, keep it locally without another routing or enrichment call. When a switch is needed, pass the retained descriptor verbatim as `current_persona`; null in these examples means there was no prior activation. A prepared worker uses its separate task package and result schema.

## Run a repeatable procedure

For an ordinary review request, keep the work within review scope. The built-in `pr-review` procedure includes fixes, pushes, replies and merge when authorized; invoke it only within the user's requested scope, carrying `review-only` or `no-merge` where applicable. Reading this guide does not authorize that procedure.

Configure the connection for the caller's project before using repository workflows or history. A stdio client declaring MCP roots can select a project within those roots through `workspace`, unless `AGENTS_CLIENT_REPO_ROOT` overrides it. Without roots, stdio uses its configured or process-derived project. HTTP uses its registered workspace through `X-Agents-Workspace`; passing another `workspace` argument does not rebind it. See [workspace targeting](routing_flow.md).

Call `list_flows` with the selected project's absolute `workspace` to discover built-in, personal and repository workflows. Carry the user's scope and constraints into `run_flow`. This example explicitly selects the built-in documentation procedure after the connection is bound to that project:

```json
{
  "flow": "builtin:documentation-refresh",
  "request": "Refresh this repository's documentation to match the current source.",
  "workspace": "/absolute/path/to/project",
  "current_persona": null
}
```

Replace the path with the actual caller project, and replace null with the retained descriptor when a persona is already active. If `persona_activation` is returned, apply it before executing the workflow. `needs_execution` means the current model must continue the returned instructions using its tools. The tool does not itself run commands, edit files, start a background job or complete the task. Check that the returned `repo_path` matches the intended project before executing; correct a mismatched connection first. The [workflow guide](../flows/README.md) explains scoped IDs and personal overrides.

## Delegate a bounded assignment

Use `get_subagent_context` with a canonical role, one task, concrete `acceptance_criteria`, relevant `context`, explicit `scope` and `non_goals`, and the required `result_type`. Rate the assignment with `complexity` (`low`, `standard` or `high`) and pass `client`, the host you run in (`claude`, `codex` or `cursor`), so the package names a lighter model for simpler work ([model tiers](subagent-model-tiers.md)); omit `complexity` to keep your own model.

1. Require `status: prepared` and retain the original `contract`.
2. Forward `worker_context` unchanged, plus only necessary task materials, to a fresh native worker. When the package has `execution.spawn`, launch the worker with exactly those arguments. The host launches it and enforces permissions and runtime limits. Keep the parent's persona unchanged.
3. Pass every returned or revised output unchanged to `validate_subagent_result` with the retained contract.
4. Require `valid: true`, then independently assess source evidence, reported checks and acceptance criteria. Structural validity alone does not verify correctness. A returned `escalation` names the `complexity` for a new attempt with a new `attempt_id`.

If preparation, validation or native spawning is unavailable, report that limitation and do practical work in the parent. The [worker contract](subagent-contract.md) covers result statuses and retries. Pinned [artifact manifests](shared-artifacts.md) support shared exact input without copying it into every assignment.

## Requirements, data and limits

- **Runtime:** Python 3.11+. The default installation downloads an embedding model of about 1.1 GB from Hugging Face and builds local indexes. Setup needs network access; the core server needs no external API key.
- **Connection:** a local stdio process or the shared local service. The Claude Code plugin launches a separately installed server on macOS or Linux. Windows uses direct server setup. Follow the [installation guide](../llms-install.md) for paths, supported setup and connection verification.
- **Execution:** the host client provides file access, shell commands, native workers and its model. Personas and workflow text guide that model; they do not enforce a sandbox or guarantee task quality. Optional local inference has separate [configuration and limits](shared-mcp-daemon.md#local-inference).
- **Workspace:** pass the selected project's absolute `workspace` to memory and workflow tools, with the transport-specific binding described above. Check the returned target before using repository context. An explicit stdio `AGENTS_CLIENT_REPO_ROOT` takes precedence. A shared installation directory is not the caller's project.
- **Persistence:** `describe_repo` reads context; `write_repo_summary` explicitly writes a managed summary. `log_interaction` writes turn attribution and `read_history` reads existing entries. User libraries, artifacts and cooperation records have their own local stores. Read each tool's contract before a write.
- **Network and privacy:** setup/model downloads and configured updates use the network. Optional tracing, library sync, subscribed rule sources and configured inference have separate data paths. Inspect the [privacy policy](../plugins/agents-core/README.md#privacy-policy), [artifact storage contract](shared-artifacts.md) and relevant configuration before enabling them. The host model provider's own data handling still applies.
- **Publication:** the [distribution ledger](distribution.md) distinguishes published listings from pending submissions and packaging work. Installation commands, a plugin manifest or a directory submission do not establish acceptance in another catalog.

The compact discovery index is [llms.txt](../llms.txt). Contributors should start with [AGENTS.md](../AGENTS.md); the complete reference map is [docs/README.md](README.md).
