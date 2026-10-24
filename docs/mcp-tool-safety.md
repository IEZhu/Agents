# Inspect MCP metadata through a checked output boundary

Use `scripts/dev/mcp_metadata_guard.mjs` in a Node runtime, or its generated
no-import adapter `scripts/dev/mcp_metadata_guard_code_mode.js` in code mode.
These helpers are opt-in orchestration boundaries; they do not intercept other
tool calls or change the JSON-RPC transport. Keep required instructions and
persona descriptors/footers complete. A small metadata return is not a read of
the underlying instructions.

## Bounded-return gate by client class

This section is the one statement of the bounded-return gate; flows and guides
link here instead of restating it. The gate covers every tool call that can
return a catalogue, protocol, context bundle or large status object, flow
discovery included.

For every client: keep what a step prints, emits or forwards within an
8,000-character return budget, and when the selection would exceed it, return
only the source reference, identity, status and counts. When a required
instruction or evidence source is too large to return whole, read it separately
in bounded ranges until complete; truncation never counts as a completed read.
Keep persona descriptors and footers intact.

**Orchestrated hosts** (Codex code mode, or a Node orchestrator around tool
calls) receive a tool result before the model does. The caller can pass each
result through the guard, and this gate requires it to:

- Load the trusted adapter before the first large call, as described in
  [code mode before the first large call](#code-mode-before-the-first-large-call),
  which also covers the first flow discovery.
- Before each call the gate covers, choose the output field allowlist, then pass
  the result to `guardMetadata`, or to `guardThreadMetadata` for native thread
  metadata, with a `retain` callback that keeps the complete source in the
  task's existing evidence area (in memory for sensitive metadata).
- Never forward an unfiltered result or catalogue to the model. The guard parses
  JSON strings, checks the serialized envelope against the budget and refuses
  instead of truncating.
- Read each required persona block separately rather than combining it with a
  long flow or catalogue in one return.

**Direct-call clients** call MCP tools natively and receive each result without
passing it through the guard; Claude Code and Cursor work this way as Agents-Core
installs them. Their native MCP results, flow discovery included, reach model
context unguarded: never report such a result as guarded or as retained by the
guard. Such a client must still:

- choose the narrowest tool arguments (identifiers, scopes, filters, limits)
  so that results stay small;
- save large shell, `gh` or API output to a file in the task's evidence area and
  print only fields that a script selects from the saved file (the Node module
  can do that selection);
- apply the same 8,000-character budget to what it prints or forwards to a
  worker, report or comment.

## What is enforced and what is opt-in

**Enforced** for a result that the caller hands to `guardMetadata` or
`guardThreadMetadata`: the complete source is retained before decoding, and a
retention failure or invalid reference refuses output; the MCP envelope and the
JSON shape are validated; `guardMetadata` projects only allowlisted scalars and
array counts, and `guardThreadMetadata` only its
[closed item projection](#native-thread-metadata); selected strings are
redacted; the whole serialized `{sourceRef, metadata}` envelope is checked
against the budget before `emit`. A refusal once a usable source reference
exists carries a fixed code and that reference, and its message names both. A
refusal before a usable reference exists carries only the code, and its
`sourceRef` is null. Exactly four codes do so: `invalid_guard_options` (a
malformed `retain`, `emit`, `maxCharacters` or `secrets` option, refused before
`retain` runs), and `source_retention_failed`, `invalid_source_reference` and
`sensitive_source_reference`, which come after `retain` was called, so the
source may already be stored without a reference that can be shown. No refusal
includes the raw input or a sensitive reference, and there is no raw fallback.

**Opt-in, not enforced:** nothing routes a tool result to the guard. No
Agents-Core component installs the guard in any client, and the transport is
unchanged; automatic routing is tracked in
[issue #506](https://github.com/IEZhu/Agents/issues/506). The helper does not
intercept native-client, terminal or third-party responses. A result is guarded
only when the caller's orchestration passes it there: Codex code mode with the
recipe below, or a Node orchestrator that injects retention and emission. A
direct-call client follows the
[gate's direct-call rules](#bounded-return-gate-by-client-class); for it the
guard is a voluntary filter for output it saved first. The field allowlist is
the caller's choice: the guard cannot stop a selected field from carrying prompt
text, and it does not detect unknown secrets. The host's `retain` callback owns
storage and access control. The tests below check the enforced part and run the
documented recipe; they do not show that a client used it.

## Retain, decode, project, then emit

`guardMetadata(raw, options)` retains the complete original response before
decoding, projects explicit field paths to scalar values or array counts, then
serializes the entire `{sourceRef, metadata}` envelope and checks its size
before calling `emit`. The limit defaults to 8,000 UTF-16 code units (JavaScript
`String.length`), including escaped strings and the reference; a positive lower
limit is allowed. A larger limit, malformed `secrets`, unsafe projection, invalid
source reference, retention failure or oversized envelope throws before
emission. There is no raw fallback. Errors have fixed codes and never include
parser input or callback error messages; once a usable source reference exists,
a refusal's message names it, so an uncaught refusal still shows where the
source is. On refusal, use the retained reference and narrower fields or bounded
source reads; do not print the raw error's inputs or retry a write.

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
sensitive strings as `secrets` too, an array of non-empty strings. This is not a
general secret detector: unknown opaque values cannot reliably be recognized.
Select only public metadata, or supply known sensitive values. Source references
must be opaque identifiers, never derived from credentials, URLs with
credentials or payload text. Redaction applies before output; audit files must
contain only this safe projection. Retain sensitive originals only in a
task-local in-memory store, with an opaque reference; do not write them to
evidence files. A durable private evidence store is suitable only for sources
already established as nonsecret. The `retain` callback owns preservation and
access control; throwing or returning an invalid reference refuses output. Its
receipt is not independent proof that the host stored the source correctly.

## Code mode before the first large call

The trusted installation root is the directory above `flows/` in a built-in
flow's `source_path`; keep the target `repo_path` separate. Only `list_flows`,
`get_flow` and `run_flow` return that path, so the root can be unknown before
flow discovery. When the owner or the task already names the root, load the
adapter **before** any catalogue, `run_flow` or context call, flow discovery
included. Otherwise the first discovery call is exempt: call `list_flows` with
`scope: 'builtin'`, emit only one built-in flow's `source_path` from it, and
load the adapter before any further large call, including `run_flow`.

In Codex code mode, the following recipe uses the exposed shell tool to read a
trusted local file; no filesystem or import API inside the isolate is assumed.
Set the absolute path from inspected installation metadata. Do not interpolate
tool payloads into code or execute a payload as a bootstrap.
`tests/mcp_metadata_guard.test.mjs` runs this block verbatim, replacing only
`/absolute/Agents-Core`, so keep it runnable.

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

Known sensitive values are matched against the original selected string. All overlapping match spans are suppressed, including partial overlaps and repeated self-overlaps.

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

Native thread summaries accept top-level `turns` or nested `thread.turns`. An explicitly present top-level property always wins. Malformed values and accessors are refused rather than replaced with nested data.

## Regenerate and check the adapter

The code-mode file is generated from the ESM source, not maintained separately.
From the source checkout, regenerate it after editing the module:

```bash
python3 - <<'PY'
from pathlib import Path
source = Path('scripts/dev/mcp_metadata_guard.mjs').read_text(encoding='utf-8')
body = '\n'.join(line.removeprefix('export ') for line in source.splitlines())
exports = 'DEFAULT_MAX_CHARACTERS, MetadataGuardError, decodeObject, decodeMcpObject, projectMetadata, redactCredentials, summarizeThread, guardMetadata, guardThreadMetadata'
Path('scripts/dev/mcp_metadata_guard_code_mode.js').write_text(
    '// Generated from mcp_metadata_guard.mjs; see docs/mcp-tool-safety.md.\n'
    + 'return (function () {\n' + body
    + '\nreturn Object.freeze({' + exports + '});\n})()\n', encoding='utf-8', newline='\n')
PY
node --test tests/mcp_metadata_guard.test.mjs
```

`tests/test_mcp_tool_safety.py` runs these Node checks in the regular pytest
suite (explicitly skipped if Node is unavailable). The same synthetic scenarios
run through both exports and the actual no-import adapter, including lossless
retention, exact serialized thresholds, malformed/truncated JSON, closed native
item projection and forbidden credential sentinels. No real history or
clipboard is read.

The code-mode recipe above also runs verbatim in a Codex-like isolate whose
shell tool reads the real adapter file. Its Agents-Core results use the legacy
`structuredContent.result` wrapper that FastMCP returns for a string-valued
tool. Flow discovery emits only the bounded envelope and retains the whole
result; a malformed result refuses with its source reference in the error text
the model sees; a truncated adapter load stops before the first tool call.
Through the same loader, a `run_flow` bundle keeps its request, flow body and
persona blocks out of the output, and selecting its oversized body refuses with
the source reference.
