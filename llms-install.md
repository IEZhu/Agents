# Install Agents-Core in an MCP client

Agents-Core is a local MCP server for specialist personas, reusable Markdown workflows and repository memory. The core server needs no external API key. Its source is [IEZhu/Agents](https://github.com/IEZhu/Agents); the license is MIT.

Before installing, use the [agent guide](docs/agent-guide.md) to match a task to the available tools, inspect first-call examples and check operating limits. [llms.txt](llms.txt) provides a compact documentation index.

These instructions cover standalone stdio setup for Cline and other clients that accept `mcpServers` JSON. The installer registers supported clients it detects, but does not configure Cline. The [project README](README.md) covers other clients, the shared service and Windows.

## 1. Install the local server

On macOS or Linux, use Git, curl and Python 3.11 or newer, with `pip3` on PATH. The default installation downloads an embedding model of about 1.1 GB from Hugging Face and builds local indexes. It needs network access during setup.

To install only the server before configuring this client, download and read the installer, then run it with client registration disabled:

```bash
curl -fsSL https://raw.githubusercontent.com/IEZhu/Agents/main/install.sh -o /tmp/agents-core-install.sh
bash /tmp/agents-core-install.sh --skip-mcp
```

The default destination is `~/.agents-core`. `--skip-mcp` skips client configuration and global routing instructions, but still installs dependencies and builds indexes. For an existing shared-service installation, use its documented update procedure instead of rerunning setup while it serves sessions.

On Windows, clone the repository and follow [After Cloning](README.md#after-cloning), using `scripts\init_repo.bat --skip-mcp`. The standalone interpreter is then `.venv\Scripts\python.exe` in that clone; the plugin's shell launcher is not supported on Windows.

## 2. Add one MCP entry

Open the client's MCP server configuration. In Cline, use its MCP Servers configuration editor. Merge the following entry into the existing `mcpServers` object, preserving all other entries. Replace the two installation paths with absolute paths for the actual installation. JSON does not expand `~` or `$HOME`.

```json
{
  "mcpServers": {
    "Agents-Core": {
      "command": "/absolute/path/to/.agents-core/.venv/bin/python",
      "args": ["/absolute/path/to/.agents-core/src/server.py"]
    }
  }
}
```

On Windows, use the absolute `.venv\Scripts\python.exe` path and escape backslashes in JSON. Keep tool approval under the client's existing policy.

On NixOS, add `LD_LIBRARY_PATH` with `/run/current-system/sw/share/nix-ld/lib` in the entry's `env` when using Nix Python, as described in [After Cloning](README.md#after-cloning).

For a configuration dedicated to one project, add `"env": {"AGENTS_CLIENT_REPO_ROOT": "/absolute/path/to/project"}` to the server entry. This override takes precedence over `workspace`, so memory and workflow tools cannot select another repository through `workspace` while it is set. Use a separate entry with a matching override for each project; omit the override when `workspace` should select the repository. Do not point the override at your home directory or the server installation. Pass the selected project's absolute path as `workspace` to memory and workflow tools.

## 3. Connect and verify

Reload or reconnect MCP in the client. Confirm that the `Agents-Core` server connects and exposes tools, then call `list_agents`. If retrieval reports `warming_up`, wait for startup to finish and retry. Call `route_and_load` with a task, `protocol_version=2` and `current_persona=null`; if it returns `ROUTE_REQUIRED`, complete selection with `get_agent_context`. Apply only a complete successful bundle and follow its persona continuity and logging instructions.

For persistent client instructions, use the managed protocol from [routing-protocol-core.md](scripts/templates/routing-protocol-core.md). Do not overwrite unrelated project instructions.

Record whether the client actually completed installation and connected successfully. File validation alone does not establish a successful Cline installation, which the [Cline Marketplace submission rules](https://github.com/cline/mcp-marketplace#how-to-submit-your-mcp-server) require.

## Troubleshooting and removal

- Missing executable: confirm setup finished and both absolute paths refer to the same installation.
- Missing dependencies or model: inspect the setup output; use the project's documented repair or update procedure.
- Wrong repository memory target: when `AGENTS_CLIENT_REPO_ROOT` is set, correct that entry's project path and reconnect; otherwise pass the selected project's absolute `workspace`.
- Duplicate tools in Claude Code: keep either its direct `Agents-Core` registration or the plugin, following the [plugin setup guide](plugins/agents-core/README.md#setup).

To disconnect this client, remove only its `Agents-Core` entry. To remove the installation, follow [Uninstall](README.md#uninstall), starting with the dry run. The [plugin Privacy Policy](plugins/agents-core/README.md#privacy-policy) describes local storage, optional tracing and optional Git sync.
