#!/usr/bin/env node
import { readFileSync, statSync } from 'node:fs';
import { createHmac } from 'node:crypto';
import { homedir } from 'node:os';
import { join } from 'node:path';

function respond(overwrite) {
  // PreToolUse requires a decision. ask honors cached Always Allow grants;
  // allow would bypass the user's confirmation policy, even for another server.
  const response = { decision: 'ask' };
  if (overwrite) response.overwrite = overwrite;
  process.stdout.write(JSON.stringify(response) + '\n');
}

function findConfig(arg) {
  if (arg) return arg;
  if (process.env.AGENTS_BRIDGE_CONFIG) return process.env.AGENTS_BRIDGE_CONFIG;
  const mcpConfigPath = join(homedir(), '.gemini', 'config', 'mcp_config.json');
  try {
    const mcpConfig = JSON.parse(readFileSync(mcpConfigPath, 'utf8'));
    const bridgeConfig = mcpConfig.mcpServers?.['Agents-Core']?.args?.[1];
    if (bridgeConfig) return bridgeConfig;
  } catch {}
  return null;
}

function readToken(configPath) {
  if (!configPath) return null;
  try {
    if (process.platform !== 'win32' && (statSync(configPath).mode & 0o077)) return null;
    const config = JSON.parse(readFileSync(configPath, 'utf8'));
    const auth = config.headers?.Authorization || '';
    return auth.replace(/^Bearer\s+/i, '').trim() || null;
  } catch {
    return null;
  }
}

async function main() {
  const configPath = findConfig(process.argv[2]);
  const token = readToken(configPath);

  process.stdin.setEncoding('utf8');
  let raw = '';
  for await (const chunk of process.stdin) raw += chunk;
  if (!raw.trim()) {
    respond();
    return;
  }

  let input;
  try {
    input = JSON.parse(raw);
  } catch {
    respond();
    return;
  }

  const toolCall = input.toolCall;
  const workspacePaths = input.workspacePaths;
  const workspace = Array.isArray(workspacePaths) && workspacePaths[0] ? String(workspacePaths[0]).trim() : null;

  if (!token || !workspace || !toolCall) {
    respond();
    return;
  }

  const name = toolCall.name || '';
  const server = toolCall.args?.ServerName;
  const isLazyAgentsCore = name === 'call_mcp_tool' && (server === 'Agents-Core' || server === 'Agents_Core');
  const isEagerAgentsCore = name.startsWith('mcp_Agents-Core_') || name.startsWith('mcp_Agents_Core_');

  if (!isLazyAgentsCore && !isEagerAgentsCore) {
    respond();
    return;
  }

  const signature = createHmac('sha256', token).update('workspace:' + workspace).digest('hex');

  if (isLazyAgentsCore) {
    let args = toolCall.args?.Arguments;
    const isString = typeof args === 'string';
    if (isString) {
      try { args = JSON.parse(args); } catch { args = {}; }
    } else if (!args || typeof args !== 'object') {
      args = {};
    }
    const updatedArgs = { ...args, workspace, workspace_signature: signature };
    respond({ Arguments: isString ? JSON.stringify(updatedArgs) : updatedArgs });
  } else {
    const args = toolCall.args && typeof toolCall.args === 'object' ? toolCall.args : {};
    respond({ ...args, workspace, workspace_signature: signature });
  }
}

main().catch(() => {
  respond();
});
