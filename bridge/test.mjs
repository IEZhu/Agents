import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createServer } from 'node:http';
import { mkdtemp, writeFile, rm } from 'node:fs/promises';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { spawn } from 'node:child_process';
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
    const child = spawn(process.execPath, [new URL('./stdio.mjs', import.meta.url).pathname, config]);
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
