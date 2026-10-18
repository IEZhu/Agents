import assert from 'node:assert/strict';
import {readFileSync} from 'node:fs';
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
}
