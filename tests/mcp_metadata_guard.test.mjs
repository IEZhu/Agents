import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
import {fileURLToPath} from 'node:url';
import test from 'node:test';
import * as moduleApi from '../scripts/dev/mcp_metadata_guard.mjs';

const root = new URL('../', import.meta.url);
const moduleSource = readFileSync(new URL('scripts/dev/mcp_metadata_guard.mjs', root), 'utf8');
const adapter = readFileSync(new URL('scripts/dev/mcp_metadata_guard_code_mode.js', root), 'utf8');
const names = 'DEFAULT_MAX_CHARACTERS, MetadataGuardError, decodeObject, decodeMcpObject, projectMetadata, redactCredentials, summarizeThread, guardMetadata, guardThreadMetadata';
const generated = '// Generated from mcp_metadata_guard.mjs; see docs/mcp-tool-safety.md.\nreturn (function () {\n' +
  moduleSource.split('\n').filter((line, i, lines) => i < lines.length - 1 || line !== '')
    .map(line => line.replace(/^export /u, '')).join('\n') +
  '\nreturn Object.freeze({' + names + '});\n})()\n';

test('code-mode adapter is the exact generated trusted module, not a separate implementation', () => {
  assert.equal(adapter, generated);
});

// Exercise every scenario through BOTH the module and the actual no-import adapter.
for (const [runtime, api] of [['module', moduleApi], ['code-mode', new Function(adapter)()]]) {
  const fields = {status: {path: ['status']}, count: {path: ['entries'], count: true}};
  function host(extra = {}) {
    const sources = new Map();
    const emitted = [];
    return {sources, emitted, options: {fields, retain: raw => {sources.set('source:1', raw); return 'source:1';},
      emit: text => emitted.push(text), ...extra}};
  }
  test(`${runtime}: huge source is retained whole, only allowlisted scalars/counts escape`, async () => {
    const raw = {status: 'ok', entries: Array(50000).fill('FORBIDDEN_RAW_SENTINEL'), hidden: 'FORBIDDEN_RAW_SENTINEL'};
    const h = host();
    const receipt = await api.guardMetadata(raw, h.options);
    assert.equal(h.sources.get(receipt.sourceRef), raw);
    assert.deepEqual(JSON.parse(h.emitted[0]), {sourceRef: 'source:1', metadata: {status: 'ok', count: 50000}});
    assert.ok(!h.emitted[0].includes('FORBIDDEN'));
  });
  test(`${runtime}: serialized envelope, escaped strings and UTF-16 boundaries are checked before emission`, async () => {
    const raw = {status: '"\n😀', entries: []};
    const h = host();
    const receipt = await api.guardMetadata(raw, h.options);
    assert.equal(receipt.characters, h.emitted[0].length);
    const exact = host({maxCharacters: receipt.characters});
    await api.guardMetadata(raw, exact.options);
    const short = host({maxCharacters: receipt.characters - 1});
    await assert.rejects(api.guardMetadata(raw, short.options), error =>
      error.code === 'output_budget_exceeded' && error.sourceRef === 'source:1');
    assert.equal(short.emitted.length, 0);
    assert.equal(short.sources.get('source:1'), raw);
  });
  test(`${runtime}: invalid budgets and retention failures emit nothing`, async () => {
    for (const maxCharacters of [0, -1, 0.5, NaN, Infinity, '8000', 8001]) {
      const h = host({maxCharacters});
      await assert.rejects(api.guardMetadata({}, h.options), {code: 'invalid_guard_options'});
      assert.equal(h.emitted.length, 0);
    }
    for (const retain of [() => {throw new Error('FORBIDDEN');}, () => 'x'.repeat(257), () => ({raw: 'FORBIDDEN'})]) {
      const h = host({retain});
      await assert.rejects(api.guardMetadata({}, h.options), error => !String(error).includes('FORBIDDEN'));
      assert.equal(h.emitted.length, 0);
    }
  });
  test(`${runtime}: empty, malformed, truncated and non-object roots never reach projection`, async () => {
    for (const raw of ['', ' ', '{"status":', '{…40 tokens truncated…}', '{"x":"…400 characters truncated…"}',
      'null', '[]', 'true', '1', '"text"', null, [], true, 1]) {
      const h = host();
      await assert.rejects(api.guardMetadata(raw, h.options), error =>
        error.name === 'MetadataGuardError' && !/unavailable|offline|server down/iu.test(String(error)));
      assert.equal(h.sources.get('source:1'), raw);
      assert.equal(h.emitted.length, 0);
    }
    assert.throws(() => api.decodeObject('{"status":"FORBIDDEN"'), error => !String(error).includes('FORBIDDEN'));
  });
  test(`${runtime}: deterministic MCP structured/text/legacy wrappers and tool errors`, () => {
    assert.deepEqual(api.decodeMcpObject({structuredContent: {status: 'ok'}, content: [{text: 'bad'}]}), {status: 'ok'});
    assert.deepEqual(api.decodeMcpObject({structuredContent: {result: '{"status":"ok"}'}}), {status: 'ok'});
    assert.deepEqual(api.decodeMcpObject(JSON.stringify({content: [{type: 'text', text: '{"status":"ok"}'}]})), {status: 'ok'});
    for (const raw of [{isError: true, structuredContent: {}}, {isError: 'true', structuredContent: {}},
      {structuredContent: []}, {content: []}, {content: [{type: 'text', text: '{}'}, {type: 'text', text: '{}'}]},
      {content: [{type: 'image', text: '{}'}]}, {content: [{type: 'text', text: 'null'}]}]) {
      assert.throws(() => api.decodeMcpObject(raw), {name: 'MetadataGuardError'});
    }
    assert.deepEqual(api.decodeObject({result: 'literal data', content: []}), {result: 'literal data', content: []});
  });
  test(`${runtime}: no inherited properties, accessors, collection values or sensitive paths`, () => {
    let calls = 0;
    const raw = {get status() {calls++; throw Error('FORBIDDEN');}, entries: []};
    assert.throws(() => api.projectMetadata(raw, fields), {code: 'accessor_not_allowed'});
    assert.equal(calls, 0);
    for (const path of [['__proto__'], ['constructor'], ['capability'], ['nested', 'access_token']]) {
      assert.throws(() => api.projectMetadata({}, {bad: {path}}), {code: 'invalid_field_path'});
    }
    for (const status of [{}, [], undefined, NaN, Infinity, () => 1, 1n]) {
      assert.throws(() => api.projectMetadata({status, entries: []}, fields), {name: 'MetadataGuardError'});
    }
  });
  test(`${runtime}: selected contextual credentials and known sensitive values are redacted before emission`, async () => {
    const h = host({secrets: ['OPAQUE_SENTINEL']});
    await api.guardMetadata({status: 'Bearer AUTH_SENTINEL capability=CAP_SENTINEL OPAQUE_SENTINEL', entries: []}, h.options);
    assert.ok(!/AUTH_SENTINEL|CAP_SENTINEL|OPAQUE_SENTINEL/u.test(h.emitted[0]));
    assert.equal(h.sources.get('source:1').status.includes('OPAQUE_SENTINEL'), true);
    const badRef = host({retain: () => 'source:OPAQUE_SENTINEL', secrets: ['OPAQUE_SENTINEL']});
    await assert.rejects(api.guardMetadata({status: 'ok', entries: []}, badRef.options), {code: 'sensitive_source_reference'});
    assert.equal(badRef.emitted.length, 0);
  });
  test(`${runtime}: native read_thread item/field allowlist excludes arguments despite includeOutputs=false`, async () => {
    const forbidden = 'FORBIDDEN_CAPABILITY_SENTINEL';
    const raw = {includeOutputs: false, preview: forbidden,
      thread: {id: '01a15116-6fb4-7e10-9144-0c9450edd574', preview: forbidden},
      turns: [{id: forbidden, items: [{type: 'agentMessage', phase: 'commentary', text: forbidden},
        {type: 'mcpToolCall', arguments: {capability: forbidden}}, {type: 'reasoning', text: forbidden},
        {type: 'commandExecution', command: forbidden}, {type: 'userMessage', text: forbidden}]}]};
    const h = host();
    await api.guardThreadMetadata({content: [{type: 'text', text: JSON.stringify(raw)}]}, {...h.options, mcp: true});
    assert.deepEqual(JSON.parse(h.emitted[0]).metadata, {threadId: raw.thread.id, turnCount: 1,
      items: [{type: 'agentMessage', phase: 'commentary'}], omittedItems: 4});
    assert.ok(!h.emitted[0].includes(forbidden));
    const huge = host();
    raw.turns[0].items = Array(1000).fill({type: 'agentMessage', phase: 'final'});
    await assert.rejects(api.guardThreadMetadata(raw, huge.options), {code: 'output_budget_exceeded'});
    assert.equal(huge.emitted.length, 0);
  });
  test(`${runtime}: quoted credentials and authentication schemes suppress the complete string`, async () => {
    for (const status of ['password="PREFIX_SENTINEL SUFFIX_SENTINEL"',
      "password='PREFIX_SENTINEL SUFFIX_SENTINEL'", 'Authorization: Basic BASIC_PAYLOAD_SENTINEL',
      'Bearer AUTH_SENTINEL with suffix SUFFIX_SENTINEL', 'secret: "PREFIX_SENTINEL\\nSUFFIX_SENTINEL"']) {
      const h = host();
      await api.guardMetadata({status, entries: []}, h.options);
      assert.equal(JSON.parse(h.emitted[0]).metadata.status, '[redacted]');
      assert.ok(!h.emitted[0].includes('SENTINEL'));
      assert.equal(h.sources.get('source:1').status, status);
    }
  });

  test(`${runtime}: known sensitive values redact the union of all overlapping occurrences`, async () => {
    for (const [status, secrets, expected] of [
      ['abcdef', ['abc', 'abcdef'], '[redacted]'],
      ['abcdef', ['abcdef', 'abc'], '[redacted]'],
      ['abcdef', ['abcd', 'cdef'], '[redacted]'],
      ['ababa', ['aba'], '[redacted]'],
      ['abcXabcdef', ['abc', 'abcdef'], '[redacted]X[redacted]'],
      ['axb a.b', ['a.b'], 'axb [redacted]'],
    ]) {
      const h = host({secrets});
      await api.guardMetadata({status, entries: []}, h.options);
      assert.equal(JSON.parse(h.emitted[0]).metadata.status, expected);
      assert.equal(h.sources.get('source:1').status, status);
    }
  });
  test(`${runtime}: nested thread turns are supported only when the top-level own property is absent`, async () => {
    const raw = {thread: {id: '01a15116-6fb4-7e10-9144-0c9450edd574', turns: [
      {items: [{type: 'agentMessage', phase: 'final', text: 'FORBIDDEN'}, {type: 'userMessage', text: 'FORBIDDEN'}]},
    ]}};
    const nested = host();
    await api.guardThreadMetadata(raw, nested.options);
    assert.deepEqual(JSON.parse(nested.emitted[0]).metadata, {threadId: raw.thread.id,
      turnCount: 1, items: [{type: 'agentMessage', phase: 'final'}], omittedItems: 1});
    assert.ok(!nested.emitted[0].includes('FORBIDDEN'));
    const top = host();
    await api.guardThreadMetadata({...raw, turns: []}, top.options);
    assert.equal(JSON.parse(top.emitted[0]).metadata.turnCount, 0);
  });
  test(`${runtime}: malformed explicit top-level turns and getters never fall back or execute`, async () => {
    const nested = {id: '01a15116-6fb4-7e10-9144-0c9450edd574', turns: []};
    for (const turns of [undefined, null, {}, 'FORBIDDEN']) {
      const h = host();
      await assert.rejects(api.guardThreadMetadata({thread: nested, turns}, h.options), {code: 'expected_thread_turns'});
      assert.equal(h.emitted.length, 0);
    }
    let invoked = 0;
    for (const raw of [
      {thread: nested, get turns() {invoked++; return [];}},
      {thread: {id: nested.id, get turns() {invoked++; return [];}}},
    ]) {
      const h = host();
      await assert.rejects(api.guardThreadMetadata(raw, h.options), {code: 'accessor_not_allowed'});
      assert.equal(h.emitted.length, 0);
    }
    assert.equal(invoked, 0);
  });
}

// The documented code-mode path: the recipe in docs/mcp-tool-safety.md runs verbatim (only the
// installation-root placeholder is replaced) in a Codex-like isolate whose shell tool reads the
// real adapter file, against Agents-Core-shaped CallToolResults.
const rootPath = fileURLToPath(root).replace(/[\\/]$/u, '');
const AsyncFunction = (async () => {}).constructor;
const PROMPT = 'FORBIDDEN_PROMPT_BODY_SENTINEL';
const MESSAGE = 'FORBIDDEN_MESSAGE_BODY_SENTINEL';

function documentedRecipe() {
  const doc = readFileSync(new URL('docs/mcp-tool-safety.md', root), 'utf8').replace(/\r\n/gu, '\n');
  const section = doc.split('\n## Code mode before the first large call\n')[1]?.split('\n## ')[0];
  const recipe = section && /```javascript\n([\s\S]*?)\n```/u.exec(section)?.[1];
  assert.ok(recipe, 'docs/mcp-tool-safety.md keeps its code-mode recipe');
  return recipe.replaceAll('/absolute/Agents-Core', JSON.stringify(rootPath).slice(1, -1));
}

/** The documented loader alone: everything up to compiling the adapter, then hand it back. */
function documentedLoader() {
  const lines = documentedRecipe().split('\n');
  const compiled = lines.findIndex(line => line.includes('new Function(loaded.output)'));
  assert.ok(compiled > 0, 'the recipe compiles the adapter from the shell output');
  return lines.slice(0, compiled + 1).join('\n') + '\nreturn guard;';
}

/** FastMCP's result for a str-returning Agents-Core tool: one text block plus the legacy
 * structuredContent.result wrapper around the same JSON string. */
function agentsCoreResult(json) {
  return {content: [{type: 'text', text: json}], structuredContent: {result: json}, isError: false};
}

function codeModeIsolate(toolResults, {truncateShell = false} = {}) {
  const stored = new Map();
  const emitted = [];
  const calls = [];
  const tools = {
    async exec_command({cmd, max_output_tokens: limit}) {
      calls.push('exec_command');
      const path = /^cat '(.*)'$/su.exec(cmd)?.[1];
      if (!path) return {exit_code: 2, output: 'unsupported command'};
      const output = readFileSync(path, 'utf8');
      // Conservative stand-in for the host's token cap: one token per character.
      if (truncateShell || output.length > limit) {
        return {exit_code: 0, output: output.slice(0, 200) + '\n…4000 tokens truncated…'};
      }
      return {exit_code: 0, output};
    },
  };
  for (const [name, result] of Object.entries(toolResults)) {
    tools[`mcp__Agents_Core__${name}`] = async () => { calls.push(name); return result; };
  }
  const store = (key, value) => { stored.set(key, value); };
  const text = value => { emitted.push(value); };
  const run = source => new AsyncFunction('tools', 'store', 'text', source)(tools, store, text);
  return {stored, emitted, calls, store, text, run};
}

const flowCatalogue = JSON.stringify({status: 'success', flows: [
  {id: 'pr-review', title: 'Review and merge a pull request or merge request', source_path: '/absolute/Agents-Core/flows/pr-review.md',
    revision: 'r1', source: 'builtin', qualified_id: 'builtin:pr-review'},
  {id: 'repo:notes', title: `Notes: ${PROMPT}`, source_path: '/absolute/target/notes.md', revision: 'r2', source: 'repo'},
], repo: {status: 'available', key: 'example', origin: 'https://github.com/example/target', path: '/absolute/target'},
issues: [{id: 'user:draft', error: `flow_invalid: ${MESSAGE}`}]});

test('documented code-mode recipe: flow discovery emits only the bounded envelope and retains the whole result', async () => {
  const raw = agentsCoreResult(flowCatalogue);
  const isolate = codeModeIsolate({list_flows: raw});
  await isolate.run(documentedRecipe());
  assert.deepEqual(isolate.calls, ['exec_command', 'list_flows']);
  assert.equal(isolate.emitted.length, 1);
  assert.deepEqual(JSON.parse(isolate.emitted[0]),
    {sourceRef: 'store:flowCatalogueSource', metadata: {status: 'success', count: 2}});
  assert.ok(isolate.emitted[0].length <= 8000);
  assert.ok(!/FORBIDDEN/u.test(isolate.emitted[0]));
  assert.equal(isolate.stored.get('flowCatalogueSource'), raw);
});

test('documented code-mode recipe: a malformed result refuses with its retained source and no body', async () => {
  const cut = flowCatalogue.slice(0, flowCatalogue.indexOf(PROMPT) + PROMPT.length);
  const raw = {content: [{type: 'text', text: cut}], structuredContent: {result: cut}, isError: false};
  const isolate = codeModeIsolate({list_flows: raw});
  await assert.rejects(isolate.run(documentedRecipe()), error =>
    error.name === 'MetadataGuardError' && error.code === 'invalid_or_incomplete_json' &&
    error.sourceRef === 'store:flowCatalogueSource' &&
    !/FORBIDDEN/u.test(`${error} ${error.stack} ${JSON.stringify(error)}`));
  assert.equal(isolate.emitted.length, 0);
  assert.equal(isolate.stored.get('flowCatalogueSource'), raw);
});

test('documented code-mode recipe: a truncated adapter load stops before the first tool call', async () => {
  const isolate = codeModeIsolate({list_flows: agentsCoreResult(flowCatalogue)}, {truncateShell: true});
  await assert.rejects(isolate.run(documentedRecipe()), /Trusted metadata adapter unavailable/u);
  assert.deepEqual(isolate.calls, ['exec_command']);
  assert.equal(isolate.emitted.length, 0);
  assert.equal(isolate.stored.size, 0);
});

test('documented loader: run_flow bodies stay retained; success is bounded and an oversized field refuses', async () => {
  const bundle = {status: 'needs_execution',
    flow: {id: 'pr-review', title: 'Review', source_path: '/absolute/Agents-Core/flows/pr-review.md', revision: 'r1', source: 'builtin'},
    repo_path: '/absolute/target', workspace_id: 'w1', request: `${PROMPT} review PR 475`,
    content: `# Review\n${MESSAGE}\n` + 'step\n'.repeat(4000), instruction: `Execute the supplied flow. ${MESSAGE}`,
    persona_activation: {status: 'SUCCESS', persona_block: MESSAGE, rules_block: MESSAGE, footer: 'Agent: code_reviewer'}};
  const raw = agentsCoreResult(JSON.stringify(bundle));
  const isolate = codeModeIsolate({run_flow: raw});
  const guard = await isolate.run(documentedLoader());
  const options = fields => ({mcp: true, fields, emit: isolate.text,
    retain: value => { isolate.store('runFlowSource', value); return 'store:runFlowSource'; }});

  const receipt = await guard.guardMetadata(raw, options({status: {path: ['status']},
    flow: {path: ['flow', 'id']}, revision: {path: ['flow', 'revision']},
    activation: {path: ['persona_activation', 'status']}}));
  assert.deepEqual(JSON.parse(isolate.emitted[0]), {sourceRef: 'store:runFlowSource',
    metadata: {status: 'needs_execution', flow: 'pr-review', revision: 'r1', activation: 'SUCCESS'}});
  assert.equal(receipt.characters, isolate.emitted[0].length);
  assert.ok(receipt.characters <= 8000 && !/FORBIDDEN/u.test(isolate.emitted[0]));

  await assert.rejects(guard.guardMetadata(raw, options({content: {path: ['content']}})), error =>
    error.code === 'output_budget_exceeded' && error.sourceRef === 'store:runFlowSource' &&
    !/FORBIDDEN/u.test(`${error} ${error.stack}`));
  assert.equal(isolate.emitted.length, 1);
  assert.equal(isolate.stored.get('runFlowSource'), raw);
});
