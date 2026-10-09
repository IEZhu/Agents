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
// The daemon registers the project a session names here and answers its X-Agents-Workspace (#253).
const registration = new URL('/workspaces', endpoint);
const pending = new Set();
const answers = new Map(); // this bridge's own requests to its client, by id
const named = new Map(); // a tool call's workspace -> its UUID; cleared when the client's roots change
let protocolVersion = '2025-11-25';
let clientRoots = false;
let session; // the session's workspace UUID (workspace "auto"), or null
let requests = 0;
let output = Promise.resolve();
function send(value) {
  output = output.then(async () => {
    if (!process.stdout.write(JSON.stringify(value) + '\n')) await once(process.stdout, 'drain');
  });
  return output;
}
async function register(directory, roots) {
  const response = await fetch(registration, {
    method: 'POST', redirect: 'error', signal: AbortSignal.timeout(30_000),
    headers: { Authorization: config.headers.Authorization, 'Content-Type': 'application/json' },
    body: JSON.stringify(roots ? { path: directory, roots } : { path: directory }),
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok || typeof body.workspace_id !== 'string') {
    const error = new Error(body.message ? `${body.error}: ${body.message}` : `Daemon HTTP ${response.status}`);
    error.refused = response.status === 400; // the daemon's checks said no; anything else may pass later
    throw error;
  }
  return body.workspace_id;
}
// Workspace "auto": the client starts this bridge once per session, in the session's directory,
// and Claude Code also names it in CLAUDE_PROJECT_DIR; that is what a stdio server trusts too.
function sessionWorkspace() {
  if (config.workspace !== 'auto') return Promise.resolve(null);
  session ??= register(process.env.CLAUDE_PROJECT_DIR || process.cwd()).catch(error => {
    process.stderr.write(`Agents-Core bridge: no workspace for this session (${error.message}).\n`);
    if (!error.refused) session = undefined; // the daemon may still be starting: ask again next time
    return null;
  });
  return session;
}
function ask(method) {
  const id = `agents-core-bridge-${requests++}`;
  const answer = new Promise((resolve, reject) => {
    const timer = setTimeout(() => { answers.delete(id); reject(new Error(`${method} timed out`)); }, 5_000);
    answers.set(id, { resolve, reject, timer });
  });
  send({ jsonrpc: '2.0', id, method });
  return answer;
}
// One bridge can serve several sessions (the Claude desktop app): a tool call may name its
// workspace, which counts only inside the client's MCP roots; the daemon checks that.
async function workspaceFor(message) {
  const workspace = message.method === 'tools/call' ? message.params?.arguments?.workspace : undefined;
  if (typeof workspace !== 'string' || !workspace.trim() || !clientRoots) return sessionWorkspace();
  if (!named.has(workspace)) {
    const identity = ask('roots/list').then(answer => register(workspace, (answer?.roots ?? []).map(root => root.uri)));
    named.set(workspace, identity);
    identity.catch(() => named.delete(workspace)); // a refusal is checked again on the next call
  }
  try {
    return await named.get(workspace);
  } catch (error) {
    // A bridge started for one session still knows that session's project; only a bridge
    // shared by several sessions has nothing better and refuses the call.
    const own = await sessionWorkspace();
    if (!own) throw error;
    process.stderr.write(`Agents-Core bridge: workspace refused (${error.message}); using this session's project.\n`);
    return own;
  }
}
async function forward(message) {
  const hasId = Object.hasOwn(message, 'id');
  if (message.method === 'initialize') clientRoots = Boolean(message.params?.capabilities?.roots);
  if (message.method === 'notifications/roots/list_changed') named.clear();
  let workspace;
  try {
    workspace = await workspaceFor(message);
  } catch (error) {
    if (hasId) await send({ jsonrpc: '2.0', id: message.id, error: { code: -32602, message: `Agents-Core refused the workspace: ${error.message}` } });
    return;
  }
  try {
    if (message.method === 'initialize') {
      // The upstream stateless transport cannot make server-to-client requests.
      message.params = { ...message.params, capabilities: {} };
      protocolVersion = message.params.protocolVersion || protocolVersion;
    }
    const response = await fetch(endpoint, {
      method: 'POST', redirect: 'error', signal: AbortSignal.timeout(120_000),
      headers: { ...config.headers, ...(workspace && { 'X-Agents-Workspace': workspace }), 'Content-Type': 'application/json',
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
  } catch {
    message = undefined;
  }
  // The client's answer to one of this bridge's own requests.
  if (message?.jsonrpc === '2.0' && !Object.hasOwn(message, 'method') && answers.has(message.id)) {
    const { resolve, reject, timer } = answers.get(message.id);
    answers.delete(message.id);
    clearTimeout(timer);
    if (message.error) reject(new Error(message.error.message || 'client error'));
    else resolve(message.result);
    continue;
  }
  if (!message || Array.isArray(message) || message.jsonrpc !== '2.0' || typeof message.method !== 'string') {
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
