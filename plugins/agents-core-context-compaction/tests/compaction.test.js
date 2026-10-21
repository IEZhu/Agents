import test from 'node:test';
import assert from 'node:assert/strict';
import { parseConfig, readConfig, register } from '../hooks/register.js';

const ENABLED = {
  schema_version: 1, enabled: true, min_context_tokens: 10000,
  min_turns: 1, cooldown_seconds: 60, cursor_bindings: [],
};

function harness(config = ENABLED, options = {}) {
  const hooks = new Map();
  const h = {
    text: JSON.stringify(config), now: 1000000, tokens: 10000,
    agents: [], surfaces: ['terminal'], calls: [], timers: [], reads: [],
    environment: { HOME: '/home/test' }, statOverrides: new Map(),
    nativeResult: { messages: [] },
  };
  h.$ = {
    env: { get: async name => h.environment[name] },
    fs: {
      stat: async path => {
        if (h.text === undefined) throw new Error('ENOENT');
        return h.statOverrides.get(path) || {
          kind: path.endsWith('.json') ? 'file' : 'dir',
          size: h.text.length, isLink: false,
        };
      },
      read: async path => {
        h.reads.push(path);
        if (h.text === undefined) throw new Error('ENOENT');
        return h.text;
      },
    },
    clock: {
      now: async () => h.now,
      after: (ms, fn) => {
        const timer = { ms, fn, cancelled: false, cancel() { this.cancelled = true; } };
        h.timers.push(timer);
        return timer;
      },
    },
    session: {
      usage: async () => { h.calls.push('usage'); return { context: { tokens: h.tokens } }; },
      surfaces: async () => h.surfaces,
      compact: async () => { h.calls.push('compact'); return h.nativeResult; },
    },
    agent: { list: async () => h.agents },
    command: {
      list: async () => [{ name: 'compact', source: 'builtin' }],
      run: async args => { h.calls.push(args); return {}; },
    },
    ui: { log: message => h.calls.push({ log: message }) },
  };
  register((name, handler) => { hooks.set(name, handler); }, options);
  h.emit = async (name, event, next = async () => ({})) => hooks.get(name)(h.$, event, next);
  h.start = async id => h.emit('turn.start', { turnId: id, text: 'test' });
  h.complete = async (id, fields = {}, next = async () => ({ text: 'answer' })) =>
    h.emit('turn.complete', { turnId: id, reason: 'answer', isAborted: false, ...fields }, next);
  h.turn = async (id, fields = {}) => { await h.start(id); return h.complete(id, fields); };
  h.step = async (id, agentId) => {
    const next = async function* () { yield { kind: 'text', text: 'unchanged' }; return { answer: 'done' }; };
    const stream = hooks.get('turn.step')(h.$, { turnId: id, agentId }, next);
    const chunks = [];
    for await (const chunk of stream) chunks.push(chunk);
    return chunks;
  };
  h.flush = async () => {
    const timers = h.timers.splice(0);
    for (const timer of timers) if (!timer.cancelled) await timer.fn();
  };
  h.compactions = () => h.calls.filter(call => call === 'compact').length;
  h.commands = () => h.calls.filter(call => call?.command === 'compact').length;
  return h;
}

test('only literal enabled true enables a valid document, omitted fields use shared defaults', () => {
  for (const enabled of [undefined, false, 'true', 1, null, [], {}]) {
    assert.equal(parseConfig(JSON.stringify({ enabled })), null);
  }
  assert.deepEqual(parseConfig('{"enabled":true}'), {
    schema_version: 1, enabled: true, min_context_tokens: 100000,
    min_turns: 5, cooldown_seconds: 900, cursor_bindings: [],
  });
  for (const value of ['null', '[]', '{', 'true', '{"enabled":true,"unknown":1}']) {
    assert.equal(parseConfig(value), null);
  }
});

test('shared schema rejects invalid bounds, malformed and duplicate Cursor bindings, and oversized UTF-8', () => {
  const invalid = [
    { schema_version: true }, { schema_version: 2 },
    { min_context_tokens: 9999 }, { min_context_tokens: 10000001 },
    { min_context_tokens: '10000' }, { min_context_tokens: 10000.5 },
    { min_turns: 0 }, { min_turns: 1001 }, { min_turns: false },
    { cooldown_seconds: -1 }, { cooldown_seconds: 86401 },
    { cursor_bindings: {} }, { cursor_bindings: Array(101).fill({}) },
    { cursor_bindings: [{ conversation_id: 'a', window_title: 'w' }] },
    { cursor_bindings: [{ conversation_id: 'a', window_title: 'w', chat_title: '\n' }] },
    { cursor_bindings: [{ conversation_id: 'a', window_title: 'w', chat_title: 'x'.repeat(513) }] },
  ];
  const binding = { conversation_id: 'a', window_title: 'w', chat_title: 'c' };
  invalid.push({ cursor_bindings: [binding, { ...binding, chat_title: 'other' }] });
  invalid.push({ cursor_bindings: [binding, { ...binding, conversation_id: 'other' }] });
  for (const patch of invalid) assert.equal(parseConfig(JSON.stringify({ ...ENABLED, ...patch })), null);
  assert.equal(parseConfig(JSON.stringify({ ...ENABLED, cursor_bindings: [binding] })).enabled, true);
  assert.equal(parseConfig(' '.repeat(65537)), null);
  assert.equal(parseConfig(JSON.stringify({ enabled: true }) + '　'.repeat(22000)), null);
});

test('missing, invalid and disabled configuration never checks usage or calls compaction', async () => {
  for (const text of [undefined, '{', '{}', JSON.stringify({ enabled: false }), JSON.stringify({ enabled: 'true' })]) {
    const h = harness(); h.text = text;
    assert.deepEqual(await h.turn('one'), { text: 'answer' });
    await h.flush();
    assert.deepEqual(h.calls, []);
    assert.equal(h.timers.length, 0);
  }
});

test('config resolution honors explicit environment/config options and refuses relative/network paths and symlinks', async () => {
  const h = harness();
  await readConfig(h.$, {});
  assert.equal(h.reads.at(-1), '/home/test/.config/agents-core/context-compaction.json');
  await readConfig(h.$, { configPath: '/options.json' });
  assert.equal(h.reads.at(-1), '/options.json');
  h.environment.AGENTS_CONTEXT_COMPACTION_CONFIG = '/override.json';
  await readConfig(h.$, { configPath: '/options.json' });
  assert.equal(h.reads.at(-1), '/override.json');
  for (const path of ['relative.json', '', '//server/config.json', '\\\\server\\config.json', '/bad\nconfig.json']) {
    h.environment.AGENTS_CONTEXT_COMPACTION_CONFIG = path;
    const count = h.reads.length;
    assert.equal(await readConfig(h.$, {}), null);
    assert.equal(h.reads.length, count);
  }
  h.environment.AGENTS_CONTEXT_COMPACTION_CONFIG = '/safe/config.json';
  h.statOverrides.set('/safe', { kind: 'dir', size: 0, isLink: true });
  assert.equal(await readConfig(h.$, {}), null);
  h.statOverrides.clear();
  h.statOverrides.set('/safe/config.json', { kind: 'file', size: 65537, isLink: false });
  assert.equal(await readConfig(h.$, {}), null);
});

test('only successful main answers count; completion is forwarded before deferral', async () => {
  const h = harness({ ...ENABLED, min_turns: 2 });
  for (const reason of ['error', 'refusal', 'aborted', undefined]) await h.turn(reason || 'unknown', { reason });
  await h.turn('flagged', { isAborted: true });
  await h.complete('subagent', { agentId: 'agent' });
  await h.turn('success-1');
  await h.flush();
  assert.equal(h.compactions(), 0);
  await h.start('success-2');
  let forwarded = false;
  await h.complete('success-2', {}, async () => {
    assert.equal(h.timers.length, 0);
    forwarded = true;
    return { text: 'preserved' };
  });
  assert.equal(forwarded, true);
  assert.equal(h.compactions(), 0);
  assert.equal(h.timers.length, 1);
  assert.ok(h.timers[0].ms > 0);
  await h.flush();
  assert.equal(h.compactions(), 1);
});

test('duplicate completion and simultaneous callbacks cannot compact twice', async () => {
  const h = harness();
  await h.start('same');
  await Promise.all([h.complete('same'), h.complete('same')]);
  assert.equal(h.timers.length, 1);
  const timer = h.timers[0];
  await Promise.all([timer.fn(), timer.fn()]);
  assert.equal(h.compactions(), 1);
  await h.complete('same');
  await h.flush();
  assert.equal(h.compactions(), 1);
});

test('a new main turn cancels pending work and a turn starting during reads invalidates the callback', async () => {
  const h = harness();
  await h.turn('old');
  await h.start('next');
  await h.flush();
  assert.equal(h.compactions(), 0);
  await h.complete('next');
  h.$.session.usage = async () => {
    await h.start('raced');
    return { context: { tokens: h.tokens } };
  };
  await h.flush();
  assert.equal(h.compactions(), 0);
});

test('configuration is rechecked after deferral and immediately before dispatch', async () => {
  for (const during of ['defer', 'usage', 'surface']) {
    const h = harness();
    await h.turn('main');
    const disable = () => { h.text = JSON.stringify({ ...ENABLED, enabled: false }); };
    if (during === 'defer') disable();
    if (during === 'usage') h.$.session.usage = async () => { disable(); return { context: { tokens: h.tokens } }; };
    if (during === 'surface') h.$.session.surfaces = async () => { disable(); return ['terminal']; };
    await h.flush();
    assert.equal(h.compactions(), 0);
    assert.equal(h.commands(), 0);
    if (during === 'defer') assert.deepEqual(h.calls, []);
  }
});

test('token threshold is inclusive, missing and invalid usage fails closed', async () => {
  for (const tokens of [undefined, null, NaN, Infinity, -1, 9999, '10000']) {
    const h = harness(); h.tokens = tokens;
    await h.turn('main'); await h.flush();
    assert.equal(h.compactions(), 0);
  }
  const h = harness();
  await h.turn('main'); await h.flush();
  assert.equal(h.compactions(), 1);
});

test('active and unknown agent statuses refuse automatic work; list errors fail closed', async () => {
  for (const status of ['pending', 'running', 'waiting', 'unknown', undefined]) {
    const h = harness(); h.agents = [{ status }];
    await h.turn('main'); await h.flush();
    assert.equal(h.compactions(), 0);
  }
  const h = harness();
  h.$.agent.list = async () => { throw new Error('unavailable'); };
  await h.turn('main'); await h.flush();
  assert.equal(h.compactions(), 0);
});

test('subagent/fork steps pass through and block compaction until their own completion', async () => {
  const h = harness();
  assert.deepEqual(await h.step('fork-turn', 'fork'), [{ kind: 'text', text: 'unchanged' }]);
  await h.turn('main'); await h.flush();
  assert.equal(h.compactions(), 0);
  await h.complete('fork-turn', { agentId: 'fork' });
  await h.turn('after-fork'); await h.flush();
  assert.equal(h.compactions(), 1);
});

test('agent spawning after eligibility cancels the pending attempt', async () => {
  const h = harness();
  await h.turn('main');
  await h.emit('agent.spawn', { agent: 'helper' });
  await h.flush();
  assert.equal(h.compactions(), 0);
});

test('native refusal and exceptions never fall back, retry or bypass cooldown', async () => {
  for (const outcome of ['refused', 'error', 'malformed']) {
    const h = harness();
    if (outcome === 'refused') h.nativeResult = { skip: 'managed restriction' };
    if (outcome === 'malformed') h.nativeResult = {};
    if (outcome === 'error') h.$.session.compact = async () => { h.calls.push('compact'); throw new Error('permission denied'); };
    await h.turn('one'); await h.flush();
    assert.equal(h.compactions(), 1);
    assert.equal(h.commands(), 0);
    assert.equal(h.timers.length, 0);
    await h.turn('two'); await h.flush();
    assert.equal(h.compactions(), 1);
    h.now += 60000;
    await h.turn('three'); await h.flush();
    assert.equal(h.compactions(), 2);
    assert.ok(h.calls.some(call => call?.log?.includes(outcome === 'refused' ? 'skipped' : outcome === 'error' ? 'failed' : 'no confirmed')));
  }
});

test('compaction-generated completion cannot recurse', async () => {
  const h = harness({ ...ENABLED, cooldown_seconds: 0 });
  h.$.session.compact = async () => {
    h.calls.push('compact');
    await h.step('summary', 'summary-fork');
    await h.complete('summary', { agentId: 'summary-fork' });
    await h.complete('unexpected-main');
    return { messages: [] };
  };
  await h.turn('main'); await h.flush();
  assert.equal(h.compactions(), 1);
  assert.equal(h.timers.length, 0);
  await h.turn('another-main'); await h.flush();
  assert.equal(h.compactions(), 2);
});

test('Desktop selects the native builtin compact command without trying the terminal API', async () => {
  const h = harness(); h.surfaces = ['desktop'];
  await h.turn('main'); await h.flush();
  assert.equal(h.compactions(), 0);
  assert.equal(h.commands(), 1);
  assert.deepEqual(h.calls.find(call => call?.command), { command: 'compact' });
  assert.equal(h.calls.some(call => call?.log), false);
  await h.turn('next'); await h.flush();
  assert.equal(h.commands(), 1);
});

test('Desktop refuses a missing or nonbuiltin compact command and command errors do not retry', async () => {
  for (const commands of [[], [{ name: 'compact', source: 'plugin' }], [{ name: 'compact', source: 'user' }]]) {
    const h = harness(); h.surfaces = ['desktop']; h.$.command.list = async () => commands;
    await h.turn('main'); await h.flush();
    assert.equal(h.commands(), 0); assert.equal(h.compactions(), 0);
  }
  const h = harness(); h.surfaces = ['desktop'];
  h.$.command.run = async () => { h.calls.push({ command: 'compact' }); throw new Error('failed'); };
  await h.turn('one'); await h.flush();
  await h.turn('two'); await h.flush();
  assert.equal(h.commands(), 1); assert.equal(h.compactions(), 0);
});

test('external native compaction cancels pending work and resets accumulated turns', async () => {
  const h = harness({ ...ENABLED, min_turns: 2, cooldown_seconds: 0 });
  await h.turn('one'); await h.turn('two');
  await h.emit('session.compact', { trigger: 'manual' }, async () => ({ messages: [] }));
  await h.flush();
  assert.equal(h.compactions(), 0);
  await h.turn('three'); await h.flush();
  assert.equal(h.compactions(), 0);
  await h.turn('four'); await h.flush();
  assert.equal(h.compactions(), 1);
});

test('session end invalidates pending and in-progress eligibility reads', async () => {
  const h = harness();
  await h.turn('one');
  await h.emit('session.end', { reason: 'clear' });
  await h.flush();
  assert.equal(h.compactions(), 0);
  h.$.session.usage = async () => {
    await h.emit('session.end', { reason: 'resume' });
    return { context: { tokens: h.tokens } };
  };
  await h.turn('two'); await h.flush();
  assert.equal(h.compactions(), 0);
});

test('failed completion middleware does not schedule compaction', async () => {
  const h = harness(); await h.start('one');
  await assert.rejects(h.complete('one', {}, async () => { throw new Error('hook failed'); }), /hook failed/);
  await h.flush();
  assert.equal(h.compactions(), 0);
});

test('manual compaction survives optional clock failure while automatic compaction is disabled', async () => {
  const h = harness({ enabled: false });
  h.$.clock.now = async () => { throw new Error('clock unavailable'); };
  const result = { messages: [{ role: 'user', content: 'retained' }] };
  assert.equal(await h.emit('session.compact', { trigger: 'manual' }, async () => result), result);
  const failure = new Error('native compact failed');
  await assert.rejects(h.emit('session.compact', { trigger: 'manual' }, async () => { throw failure; }), failure);
});
