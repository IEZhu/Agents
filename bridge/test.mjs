import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, writeFile, rm, realpath } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';
import { once } from 'node:events';

test('stdio forwards concurrent IDs and notifications, suppresses callbacks, never retries', async () => {
  const seen = [];
  const server = createServer(async (request, response) => {
    let body = '';
    for await (const chunk of request) body += chunk;
    const message = JSON.parse(body); seen.push(message);
    assert.equal(request.headers.authorization, 'Bearer test');
    if (message.method === 'fail') { response.writeHead(503); response.end(); return; }
    if (!Object.hasOwn(message, 'id')) { response.writeHead(202); response.end(); return; }
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end(JSON.stringify({ jsonrpc: '2.0', id: message.id, result: { capabilities: {}, protocolVersion: '2025-11-25' } }));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  const dir = await mkdtemp(join(tmpdir(), 'agents-bridge-'));
  try {
    const config = join(dir, 'config.json');
    await writeFile(config, JSON.stringify({ url: `http://127.0.0.1:${server.address().port}/mcp`, headers: { Authorization: 'Bearer test' } }), { mode: 0o600 });
    const child = spawn(process.execPath, [fileURLToPath(new URL('./stdio.mjs', import.meta.url)), config]);
    let output = '', errors = '';
    child.stdout.on('data', data => output += data);
    child.stderr.on('data', data => errors += data);
    for (const message of [
      { id: 'init', method: 'initialize', params: { capabilities: { sampling: {}, roots: {} } } },
      { method: 'notifications/initialized' }, { id: 0, method: 'tools/list' }, { id: 2, method: 'fail' },
    ]) child.stdin.write(JSON.stringify({ jsonrpc: '2.0', ...message }) + '\n');
    child.stdin.end();
    const [code] = await once(child, 'exit');
    assert.equal(code, 0);
    const responses = output.trim().split('\n').map(JSON.parse);
    assert.equal(responses.length, 3);
    assert.deepEqual(seen.find(m => m.method === 'initialize').params.capabilities, {});
    assert.equal(seen.filter(m => m.method === 'fail').length, 1);
    assert.equal(responses.find(r => r.id === 2).error.code, -32000);
    assert.ok(!errors.includes('Bearer'));
  } finally {
    await new Promise(resolve => server.close(resolve));
    await rm(dir, { recursive: true, force: true });
  }
});

// A fake daemon: /workspaces answers from `answer`, /mcp records each request's workspace header.
async function fakeDaemon(answer) {
  const registrations = [], calls = [];
  const server = createServer(async (request, response) => {
    let body = '';
    for await (const chunk of request) body += chunk;
    const message = JSON.parse(body);
    assert.equal(request.headers.authorization, 'Bearer test');
    if (request.url === '/workspaces') {
      registrations.push(message);
      const [status, reply] = answer(message, registrations.length);
      response.writeHead(status, { 'content-type': 'application/json' });
      response.end(JSON.stringify(reply));
      return;
    }
    calls.push({ message, workspace: request.headers['x-agents-workspace'] });
    if (!Object.hasOwn(message, 'id')) { response.writeHead(202); response.end(); return; }
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end(JSON.stringify({ jsonrpc: '2.0', id: message.id, result: {} }));
  });
  server.listen(0, '127.0.0.1'); await once(server, 'listening');
  return { server, registrations, calls, url: `http://127.0.0.1:${server.address().port}/mcp` };
}

// A client that declares roots, answers the bridge's roots/list and sends `messages` in order.
async function session(daemon, settings, messages, env = {}, cwd = undefined) {
  const dir = await mkdtemp(join(tmpdir(), 'agents-bridge-'));
  try {
    const config = join(dir, 'config.json');
    await writeFile(config, JSON.stringify({ url: daemon.url, headers: { Authorization: 'Bearer test' }, ...settings }), { mode: 0o600 });
    const environment = { ...process.env, ...env };
    for (const [key, value] of Object.entries(environment)) if (value === undefined) delete environment[key];
    const child = spawn(process.execPath, [fileURLToPath(new URL('./stdio.mjs', import.meta.url)), config],
      { env: environment, cwd });
    const responses = new Map();
    let rootsAsked = 0, buffer = '', waiting;
    child.stdout.on('data', data => {
      buffer += data;
      for (let end; (end = buffer.indexOf('\n')) >= 0; buffer = buffer.slice(end + 1)) {
        const message = JSON.parse(buffer.slice(0, end));
        if (message.method === 'roots/list') {
          rootsAsked++;
          child.stdin.write(JSON.stringify({ jsonrpc: '2.0', id: message.id, result: { roots: [{ uri: 'file:///roots/a' }] } }) + '\n');
        } else {
          responses.set(message.id, message);
          waiting?.();
        }
      }
    });
    const write = message => child.stdin.write(JSON.stringify({ jsonrpc: '2.0', ...message }) + '\n');
    const answered = id => new Promise(resolve => { waiting = () => responses.has(id) && resolve(); waiting(); });
    write({ id: 'init', method: 'initialize', params: { capabilities: { roots: { listChanged: true } } } });
    await answered('init');
    for (const message of messages) {
      write(message);
      await answered(message.id);
    }
    child.stdin.end();
    const [code] = await once(child, 'exit');
    assert.equal(code, 0);
    const header = id => daemon.calls.find(call => call.message.id === id)?.workspace;
    return { responses, rootsAsked, header };
  } finally {
    await new Promise(resolve => daemon.server.close(resolve));
    await rm(dir, { recursive: true, force: true });
  }
}

const call = (id, workspace) => ({ id, method: 'tools/call', params: { name: 'log_interaction', arguments: { workspace } } });

test('auto workspace: the session directory names every request, retried while the daemon is not up', async () => {
  const daemon = await fakeDaemon((message, count) => {
    if (count === 1) return [503, {}]; // the daemon is still starting
    if (message.path === '/outside') return [400, { error: 'workspace_invalid', message: 'outside the roots' }];
    return [200, { workspace_id: message.roots ? 'named-id' : 'session-id', root: message.path }];
  });
  const { responses, rootsAsked, header } = await session(daemon, { workspace: 'auto' },
    [{ id: 'list', method: 'tools/list' }, call('named', '/named'), call('outside', '/outside')],
    { CLAUDE_PROJECT_DIR: '/session' });
  const named = { path: '/session', origin: 'CLAUDE_PROJECT_DIR' };
  assert.deepEqual(daemon.registrations, [named, named,
    { path: '/named', roots: ['file:///roots/a'] }, { path: '/outside', roots: ['file:///roots/a'] }]);
  assert.equal(rootsAsked, 2);
  assert.equal(header('init'), undefined);
  assert.equal(header('list'), 'session-id');
  assert.equal(header('named'), 'named-id');
  // A per-session bridge falls back to its own project when a named workspace is refused.
  assert.equal(header('outside'), 'session-id');
  assert.ok(!responses.get('outside').error);
  assert.deepEqual(daemon.calls.find(c => c.message.id === 'init').message.params.capabilities, {});
});

test('a shared bridge refuses a call whose workspace lies outside the roots', async () => {
  const daemon = await fakeDaemon(message => message.path === '/outside'
    ? [400, { error: 'workspace_invalid', message: 'outside the roots' }]
    : [200, { workspace_id: 'named-id', root: message.path }]);
  const { responses, header } = await session(daemon, {}, [call('named', '/named'), call('outside', '/outside')]);
  assert.deepEqual(daemon.registrations.map(r => r.path), ['/named', '/outside']);
  assert.equal(header('named'), 'named-id');
  assert.ok(!daemon.calls.some(c => c.message.id === 'outside'));
  assert.equal(responses.get('outside').error.code, -32602);
  assert.match(responses.get('outside').error.message, /workspace_invalid: outside the roots/);
});

test('auto workspace without CLAUDE_PROJECT_DIR reports its cwd as a launch directory', async () => {
  const daemon = await fakeDaemon(message => [200, { workspace_id: 'cwd-id', root: message.path }]);
  const cwd = await mkdtemp(join(tmpdir(), 'agents-bridge-cwd-'));
  try {
    const { header } = await session(daemon, { workspace: 'auto' }, [{ id: 'list', method: 'tools/list' }],
      { CLAUDE_PROJECT_DIR: undefined }, cwd);
    // No origin: the daemon then requires a .git or CLAUDE.md, as it does for a stdio server's cwd.
    assert.equal(daemon.registrations.length, 1);
    assert.deepEqual(Object.keys(daemon.registrations[0]), ['path']);
    assert.equal(await realpath(daemon.registrations[0].path), await realpath(cwd));
    assert.equal(header('list'), 'cwd-id');
  } finally {
    await rm(cwd, { recursive: true, force: true });
  }
});
