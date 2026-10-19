# Inspect MCP metadata through a checked output boundary

Use `scripts/dev/mcp_metadata_guard.mjs` in a Node runtime, or its generated
no-import adapter `scripts/dev/mcp_metadata_guard_code_mode.js` in code mode.
These helpers are opt-in orchestration boundaries; they do not intercept other
tool calls or change the JSON-RPC transport. Keep required instructions and
persona descriptors/footers complete. A small metadata return is not a read of
the underlying instructions.

## Retain, decode, project, then emit

`guardMetadata(raw, options)` retains the complete original response before
decoding, projects explicit field paths to scalar values or array counts, then
serializes the entire `{sourceRef, metadata}` envelope and checks its size
before calling `emit`. The limit defaults to 8,000 UTF-16 code units (JavaScript
`String.length`), including escaped strings and the reference; a positive lower
limit is allowed. A larger limit, unsafe projection, invalid source reference,
retention failure or oversized envelope throws before emission. There is no raw
fallback. Errors have fixed codes and never include parser input or callback
error messages. On refusal, use the retained reference and narrower fields or
bounded source reads; do not print the raw error's inputs or retry a write.

`decodeObject` accepts a complete JSON object string or a plain object.
`decodeMcpObject` explicitly handles MCP CallToolResult envelopes: structured
content wins over text; the legacy single `structuredContent.result` JSON
string is unwrapped once; otherwise exactly one JSON text block is required.
Pass `mcp: true` only for such an envelope. Direct payloads keep their `result`
and `content` properties as data. Nulls, arrays, scalars, empty/malformed JSON,
explicit client truncation markers and tool errors fail closed. A syntax error
does not establish truncation or server unavailability. Do not enumerate a raw
resource string with `Object.keys`; decode and validate its shape first.

The allowlist rejects credential-bearing field paths. Selected strings are
redacted for contextual credential assignments and Bearer values; pass known
sensitive strings as `secrets` too. This is not a general secret detector:
unknown opaque values cannot reliably be recognized. Select only public
metadata, or supply known sensitive values. Source references must be opaque
identifiers, never derived from credentials, URLs with credentials or payload
text. Redaction applies before output; audit files must contain only this safe
projection. Retain sensitive originals only in a task-local in-memory store,
with an opaque reference; do not write them to evidence files. A durable private
evidence store is suitable only for sources already established as nonsecret.
The `retain` callback owns preservation and access control; throwing or returning
an invalid reference refuses output. Its receipt is not independent proof that
the host stored the source correctly.

## Code mode before the first large call

Derive the trusted installation root from the built-in flow's `source_path`
(`flows/` is directly below the root), keeping the target `repo_path` separate.
Load the adapter **before** calling a catalogue, `run_flow` or context tool.
In Codex code mode, the following recipe uses the exposed shell tool to read a
trusted local file; no filesystem or import API inside the isolate is assumed.
Set the absolute path from inspected installation metadata. Do not interpolate
tool payloads into code or execute a payload as a bootstrap.

```javascript
const loaded = await tools.exec_command({
  cmd: "cat '/absolute/Agents-Core/scripts/dev/mcp_metadata_guard_code_mode.js'",
  max_output_tokens: 12000,
});
if (loaded.exit_code !== 0 || loaded.output.includes('tokens truncated')) {
  throw new Error('Trusted metadata adapter unavailable; do not emit the raw tool result.');
}
const guard = new Function(loaded.output)(); // trusted local source only
// In a later isolate, retain the trusted adapter source with store/load and
// compile it again. Do not try to store function objects.
const raw = await tools.mcp__Agents_Core__list_flows({workspace: '/absolute/target'});
await guard.guardMetadata(raw, {
  mcp: true,
  fields: {status: {path: ['status']}, count: {path: ['flows'], count: true}},
  retain: value => { store('flowCatalogueSource', value); return 'store:flowCatalogueSource'; },
  emit: text,
});
```

For Node, import the same API from the `.mjs` module and inject the host's
retention/emission functions. A source stored in memory is available only while
that host's task store survives: secure necessary nonsecret evidence before a
reset. For `run_flow` retain its original `persona_activation`, descriptor,
footer and all instruction blocks. Read required fields separately with an
explicit offset, next offset and end marker, within the return budget, until
complete; compare the assembled bytes with the retained source. Never apply a
truncated activation or count a summary as having read the flow. This helper
does not grant extra access to a referenced source.

## Native thread metadata

Use `guardThreadMetadata(raw, options)` for a native `read_thread` response,
with `mcp: true` when the host wraps it in MCP text. Its closed projection keeps
only a validated thread UUID, turn count, `agentMessage` type/closed phase pairs
and an omitted-item count. It copies no message body, preview, reasoning,
command, tool arguments or capability fields. `includeOutputs: false` and
per-item length options are not redaction guarantees. One size check covers the
whole envelope, so many individually small items can still refuse output.
This metadata adapter is not an authorization-text reader. When exact user
authorization is needed, use the minimum authorized redacted source range
through the host's documented interface; do not broaden this projection.

## Derive UI indices from the latest observation

An accessibility (AX) element index belongs to one observation, not permanently
to the app. First obtain the latest AX state and inspect its returned tree. Only
then derive the element index from that tree, perform one intended action, and
inspect the resulting state to verify the effect. If another observation or a UI
change intervenes, inspect the new tree and derive the target again.

Never batch `getAXState({emit:false, disableDiffing:true})` with a `click(index)`
chosen from an earlier observation. A hidden refresh does not validate a stale
index, even if the old and new trees happen to use the same number. Keep the
observation separate from choosing the target; return or otherwise inspect the
current tree before the click. After every action, verify the observed state
instead of treating a successful click call as proof that the intended control
was activated.

Tool argument selectors also need the server's current schema. For TokenSave's
default served project use `tokensave_status({})`, without `graph_root`; a root
selector is for an explicitly different initialized project. Follow the
[project selection and freshness checks](flow-execution.md#select-an-indexed-project-explicitly)
without initializing an index to repair a lookup.

## Regenerate and check the adapter

The code-mode file is generated from the ESM source, not maintained separately.
From the source checkout, regenerate it after editing the module:

```bash
python3 - <<'PY'
from pathlib import Path
source = Path('scripts/dev/mcp_metadata_guard.mjs').read_text()
body = '\n'.join(line.removeprefix('export ') for line in source.splitlines())
exports = 'DEFAULT_MAX_CHARACTERS, MetadataGuardError, decodeObject, decodeMcpObject, projectMetadata, redactCredentials, summarizeThread, guardMetadata, guardThreadMetadata'
Path('scripts/dev/mcp_metadata_guard_code_mode.js').write_text(
    '// Generated from mcp_metadata_guard.mjs; see docs/mcp-tool-safety.md.\n'
    + 'return (function () {\n' + body
    + '\nreturn Object.freeze({' + exports + '});\n})()\n')
PY
node --test tests/mcp_metadata_guard.test.mjs
```

`tests/test_mcp_tool_safety.py` runs these Node checks in the regular pytest
suite (explicitly skipped if Node is unavailable). The same synthetic scenarios
run through both exports and the actual no-import adapter, including lossless
retention, exact serialized thresholds, malformed/truncated JSON, closed native
item projection and forbidden credential sentinels. No real history or
clipboard is read.
