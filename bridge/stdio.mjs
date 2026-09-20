// Zero-dependency bridge. One HTTP attempt per JSON-RPC message; never replay writes.
import { readFileSync, statSync } from 'node:fs';
import { createInterface } from 'node:readline';
import { once } from 'node:events';

const path = process.argv[2];
if (!path || (statSync(path).mode & 0o077)) throw new Error('Private config (0600) required');
const config = JSON.parse(readFileSync(path, 'utf8'));
const endpoint = new URL(config.url);
if (endpoint.protocol !== 'http:' || endpoint.hostname !== '127.0.0.1' || endpoint.pathname !== '/mcp') {
  throw new Error('Only the local Agents-Core endpoint is supported');
}
const pending = new Set();
let protocolVersion = '2025-11-25';
let output = Promise.resolve();
function send(value) {
  output = output.then(async () => {
    if (!process.stdout.write(JSON.stringify(value) + '\n')) await once(process.stdout, 'drain');
  });
  return output;
}
async function forward(message) {
  const hasId = Object.hasOwn(message, 'id');
  try {
    if (message.method === 'initialize') {
      // The upstream stateless transport cannot make server-to-client requests.
      message.params = { ...message.params, capabilities: {} };
      protocolVersion = message.params.protocolVersion || protocolVersion;
    }
    const response = await fetch(endpoint, {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(120_000),
      headers: { ...config.headers, 'Content-Type': 'application/json',
        Accept: 'application/json, text/event-stream', 'MCP-Protocol-Version': protocolVersion },
      body: JSON.stringify(message),
    });
    if (!response.ok) throw new Error(`Daemon HTTP ${response.status}`);
    if (response.status === 202 || response.status === 204) return;
    if (!(response.headers.get('content-type') || '').includes('application/json')) {
      throw new Error('Expected stateless JSON response');
    }
    const result = await response.json();
    if (hasId) {
      if (result.id !== message.id) throw new Error('Mismatched response ID');
      if (message.method === 'initialize' && result.result?.protocolVersion) protocolVersion = result.result.protocolVersion;
      await send(result);
    }
  } catch (error) {
    if (hasId) await send({ jsonrpc: '2.0', id: message.id, error: {
      code: -32000, message: 'Agents-Core unavailable or response uncertain; no automatic retry. Read state before repeating a write.',
    } });
    process.stderr.write('Agents-Core bridge: upstream request failed; not replayed.\n');
  }
}
const input = createInterface({ input: process.stdin, crlfDelay: Infinity });
for await (const line of input) {
  if (!line.trim()) continue;
  let message;
  try {
    message = JSON.parse(line);
    if (!message || Array.isArray(message) || message.jsonrpc !== '2.0' || typeof message.method !== 'string') throw new Error();
  } catch {
    await send({ jsonrpc: '2.0', id: null, error: { code: -32700, message: 'Invalid JSON-RPC message' } });
    continue;
  }
  if (pending.size >= 32) {
    if (Object.hasOwn(message, 'id')) await send({ jsonrpc: '2.0', id: message.id, error: { code: -32000, message: 'busy' } });
    continue;
  }
  const task = forward(message);
  pending.add(task);
  task.finally(() => pending.delete(task));
}
await Promise.allSettled(pending);
await output;
