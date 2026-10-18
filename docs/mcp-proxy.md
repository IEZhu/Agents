# HTTP MCP proxy

Agents-Core exposes manually configured Streamable HTTP MCP servers through its existing daemon endpoint. Tool names use `<alias>__<tool>`, for example `blincho__get_status`. Reconnect MCP in open chats after connecting a server or changing its catalog.

## Connect a server

Open **MCP** in the local web UI, or visit `http://127.0.0.1:8765/ui/mcp` after signing in. Enter an alias, HTTP URL, optional bearer token and allowed apps, then choose **Add and connect**. Tokens are write-only and stored in the native OS secret store; the form clears its token field when submitted. Missing or locked native stores produce an error, never a credential file fallback.

The CLI uses the running daemon and supports `list`, `add`, `connect` and `disable`:

```bash
.venv/bin/python -m src.daemon mcp list
.venv/bin/python -m src.daemon mcp add blincho --url http://127.0.0.1:47831/mcp --apps codex,claude-code --bearer
.venv/bin/python -m src.daemon mcp connect blincho
.venv/bin/python -m src.daemon mcp disable blincho
```

With `--bearer`, the command reads one line from standard input. Supply it through a private pipe or terminal input; do not put a credential in command arguments or shell history. The native-store adapter accepts at most 1024 bytes of base64-encoded compact JSON for all secret headers of one server. Oversized records fail explicitly.

## Storage and consent

Definitions live in `<personal-library>/mcp/servers/<alias>.json`. The library is selected by `AGENTS_USER_FLOWS_DIR` or the installation's `flows/.user`. Only Streamable HTTP is accepted. Credentials cannot be embedded in headers, URL user information, credential query parameters or credential extension fields. Secret declarations name HTTP headers; values are stored separately.

Machine-local app enablement confirms the exact definition revision. Saving through the UI or CLI confirms the new revision while preserving the existing app selection; stored credentials apply to the saved URL. Changes outside Agents-Core, such as hand edits or future sync, remain unconfirmed until enabled again. Credentialed remote servers require HTTPS; HTTP is allowed for loopback hosts. A missing or unknown calling app sees only native Agents-Core tools; standalone stdio also exposes no proxy tools. Existing managed bridges carry the required app identity.

The runtime catalogs tools only on an explicit **Connect** operation. Ordinary `tools/list` reads the cached catalog and never connects upstream. Calls lazily open or reuse a session; idle sessions close after the configured timeout. Calls recheck app scope, revision and tool selection, including direct calls to guessed hidden names. Names containing unsupported characters or exceeding 32 characters are shortened deterministically; colliding exposed names are withheld.

The upstream receives only its own configured headers and declared secret headers. Downstream bearer credentials and workspace headers are not forwarded. Redirects and environment proxy settings are disabled. An upstream failure does not disable native tools. Upstream `CallToolResult` objects pass through with content, structured content, metadata and error flags intact. Unknown or hidden proxy names return an execution error, matching the installed SDK's normal tool-error handling.

## Initial scope

This is the functional HTTP slice of [epic #306](https://github.com/IEZhu/Agents/issues/306). Stdio/SSE upstreams, OAuth, registry search, client-configuration import, library sync for `mcp`, profiles, workspace scopes, compact mode, prompts/resources and per-upstream endpoints remain in the epic. Definitions are local until MCP-specific sync support lands. Large results follow the client's ordinary MCP result handling; no proxy-specific file-pointer conversion is performed.

The original [proxy plan](mcp-proxy-plan.md) remains the broader design. Source and tests of the implemented HTTP slice take precedence over proposed features.
