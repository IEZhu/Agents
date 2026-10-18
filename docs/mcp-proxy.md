# HTTP MCP proxy

> **Pending integration — 2026-10-18.** This guide describes [PR #449](https://github.com/IEZhu/Agents/pull/449) at [`d067c315caf3`](https://github.com/IEZhu/Agents/tree/d067c315caf3c9877896315b96e68d4bb4a25c11). Its implementation is not included in this documentation branch. Use the commands only with a revision containing that implementation; the [integration checklist](documentation-audit-2026-10-18.md#parallel-feature-integration) records release prerequisites.

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

Machine-local app enablement confirms the exact definition revision. Saving through the UI or CLI confirms the new revision while preserving the existing app selection; stored credentials apply to the saved URL. Changes outside Agents-Core, such as hand edits or sync, remain unconfirmed until enabled again. Credentialed remote servers require HTTPS; HTTP is allowed for loopback hosts. A missing or unknown calling app sees only native Agents-Core tools; standalone stdio also exposes no proxy tools. Existing managed bridges carry the required app identity.

The MCP page displays each server's endpoint and expandable configuration. After a synced change, inspect that configuration and choose **Confirm this revision** to retain the existing local app selection. Choose **Connect** to refresh the catalog for the confirmed revision, then reconnect MCP in open chats. **Disable** remains available if the change should not be accepted. Confirmation does not add other apps selected in the add-server form.

Each installation publishes its app selections and definition revisions to `<personal-library>/mcp/machines/<id>.json`. Its opaque random ID and neutral label are kept in private daemon state; the default label contains no hostname. A configured sync machine label is reused when creating the identity. Only that installation writes its host file. Existing local connections are published on the next daemon start. The MCP page lists saved hosts; other hosts are read-only inventory. These files describe saved selections, not live upstream availability.

The existing private [library sync](user-sync.md) carries server definitions and host records in the `mcp` scope group. Excluding `mcp` keeps them local. Other files under `mcp`, including credentials and runtime caches, are never synced. Concurrent definition edits use the normal JSON conflict policy: keep the remote version and retain the local content in a conflict record. A received host record never grants local execution consent, even when its ID matches this installation; only private local confirmation can do that. After restoring a library onto a new computer, review and enable the desired servers and provide their tokens in that computer's native secret store.

Repository metadata is ordinary JSON in the private personal library, including server URLs and selected apps. Tokens remain solely in each host's native secret store and are neither published nor backed up through Git. This does not provide end-to-end encryption for library metadata.

Server aliases name shared definitions: using different URLs on different hosts requires separate aliases. Host records preserve app selections, without transport overrides. Upgrade every syncing installation before using the new `mcp` scope exclusion; older versions reject an unknown scope group. MCP JSON is validated again before publication, including hand edits; invalid records and credential-bearing definitions remain blocked even if their content hash was approved in the generic secret scanner.

Sync previews distinguish invalid MCP records from ordinary secret-scanner hits. Fix the invalid record or re-save its settings in Agents-Core; `scope --allow-secret` cannot authorize it. Invalid historical versions remain local even after the current record is repaired.

The runtime catalogs tools only on an explicit **Connect** operation. Ordinary `tools/list` reads the cached catalog and never connects upstream. Calls lazily open or reuse a session; idle sessions close after the configured timeout. Calls recheck app scope, revision and tool selection, including direct calls to guessed hidden names. Names containing unsupported characters or exceeding 32 characters are shortened deterministically; colliding exposed names are withheld.

The upstream receives only its own configured headers and declared secret headers. Downstream bearer credentials and workspace headers are not forwarded. Redirects and environment proxy settings are disabled. An upstream failure does not disable native tools. Upstream `CallToolResult` objects pass through with content, structured content, metadata and error flags intact. Unknown or hidden proxy names return an execution error, matching the installed SDK's normal tool-error handling.

App selection filters tools for trusted local clients. The calling app label is caller-supplied and does not isolate clients that hold the shared daemon credential. Disabling a server prevents new calls; already accepted work may finish.

## Initial scope

This is the functional HTTP slice of [epic #306](https://github.com/IEZhu/Agents/issues/306), with per-host connection inventory and library sync. Stdio/SSE upstreams, OAuth, registry search, client-configuration import, profiles, workspace scopes, compact mode, prompts/resources and per-upstream endpoints remain in the epic. Large results follow the client's ordinary MCP result handling; no proxy-specific file-pointer conversion is performed.

The original [proxy plan](mcp-proxy-plan.md) remains the broader design. Source and tests of the implemented HTTP slice take precedence over proposed features.
