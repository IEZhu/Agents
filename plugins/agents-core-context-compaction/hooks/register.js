// Keep mods API calls in this file: Claude Code statically checks their use.
const DEFER_MS = 50;
const SAFE_AGENT_STATUSES = new Set(['completed', 'failed', 'killed', 'idle']);
const DEFAULTS = {
  schema_version: 1, enabled: false, min_context_tokens: 100000,
  min_turns: 5, cooldown_seconds: 900, cursor_bindings: [],
};
const MAX_CONFIG_BYTES = 65536;

export function isAbsoluteLocalPath(value) {
  return typeof value === 'string' && !/[\0\r\n]/.test(value) &&
    !/^[\\/]{2}/.test(value) &&
    (value.startsWith('/') || /^[A-Za-z]:[\\/]/.test(value));
}

export function parseConfig(text) {
  try {
    if (typeof text !== 'string' || text.length > MAX_CONFIG_BYTES ||
        new TextEncoder().encode(text).length > MAX_CONFIG_BYTES) return null;
    const document = JSON.parse(text);
    if (!document || typeof document !== 'object' || Array.isArray(document) ||
        Object.keys(document).some(key => !Object.hasOwn(DEFAULTS, key))) return null;
    const config = { ...DEFAULTS, ...document };
    if (config.schema_version !== 1 || config.enabled !== true ||
        !Number.isSafeInteger(config.min_context_tokens) || config.min_context_tokens < 10000 || config.min_context_tokens > 10000000 ||
        !Number.isSafeInteger(config.min_turns) || config.min_turns < 1 || config.min_turns > 1000 ||
        !Number.isSafeInteger(config.cooldown_seconds) || config.cooldown_seconds < 0 || config.cooldown_seconds > 86400 ||
        !Array.isArray(config.cursor_bindings) || config.cursor_bindings.length > 100) return null;
    const conversations = new Set();
    const labels = new Set();
    for (const binding of config.cursor_bindings) {
      if (!binding || typeof binding !== 'object' || Array.isArray(binding) ||
          Object.keys(binding).length !== 3 ||
          !['conversation_id', 'window_title', 'chat_title'].every(key => Object.hasOwn(binding, key)) ||
          Object.values(binding).some(value => typeof value !== 'string' || !value.trim() ||
            Array.from(value).length > 512 || /[\x00-\x1f]/.test(value))) return null;
      const label = JSON.stringify([binding.window_title, binding.chat_title]);
      if (conversations.has(binding.conversation_id) || labels.has(label)) return null;
      conversations.add(binding.conversation_id);
      labels.add(label);
    }
    return config;
  } catch {
    return null;
  }
}

export async function readConfig($, options = {}) {
  try {
    const override = await $.env.get('AGENTS_CONTEXT_COMPACTION_CONFIG');
    let path;
    if (override !== undefined) path = override;
    else if (options.configPath !== undefined && options.configPath !== '') path = options.configPath;
    else {
      const homePath = await $.env.get('HOME') || await $.env.get('USERPROFILE');
      if (!isAbsoluteLocalPath(homePath)) return null;
      path = homePath.replace(/[\\/]+$/, '') + '/.config/agents-core/context-compaction.json';
    }
    if (!isAbsoluteLocalPath(path)) return null;
    let ancestor = path.replace(/\\/g, '/').replace(/\/$/, '');
    let target = true;
    while (ancestor) {
      const stat = await $.fs.stat(ancestor);
      if (stat.isLink || (target && (stat.kind !== 'file' || stat.size > MAX_CONFIG_BYTES))) return null;
      target = false;
      const cut = ancestor.lastIndexOf('/');
      if (cut < 0 || ancestor === '/' || /^[A-Za-z]:\/$/.test(ancestor)) break;
      ancestor = cut === 0 ? '/' : cut === 2 && /^[A-Za-z]:/.test(ancestor)
        ? ancestor.slice(0, 3) : ancestor.slice(0, cut);
    }
    return parseConfig(await $.fs.read(path));
  } catch {
    // Missing, unreadable, invalid or partially replaced config is disabled.
    return null;
  }
}

export function createCompactionState() {
  return {
    sessionToken: {},
    generation: 0,
    activeTurnId: null,
    activeAgentTurns: new Map(),
    seenTurns: new Set(),
    completedTurns: 0,
    lastAttemptAt: null,
    pending: null,
    inFlight: null,
    compactionDepth: 0,
    lastOutcome: null,
  };
}

function cancelPending(state) {
  const pending = state.pending;
  state.pending = null;
  if (pending?.timer) pending.timer.cancel();
}

function isCurrent(state, ticket) {
  return state.generation === ticket.generation && state.activeTurnId === null &&
    state.compactionDepth === 0 && state.activeAgentTurns.size === 0;
}

function meetsPolicy(state, config, tokens, now) {
  return config !== null && Number.isSafeInteger(tokens) && tokens >= config.min_context_tokens &&
    state.completedTurns >= config.min_turns && Number.isFinite(now) &&
    (state.lastAttemptAt === null || now - state.lastAttemptAt >= config.cooldown_seconds * 1000);
}

function agentsAreIdle(agents) {
  // Unknown task statuses fail closed, including pending/running/waiting.
  return Array.isArray(agents) && agents.every(agent =>
    agent && SAFE_AGENT_STATUSES.has(agent.status));
}

export async function runDeferred($, state, options, ticket) {
  if (state.pending !== ticket) return;
  state.pending = null;
  if (!isCurrent(state, ticket) || state.inFlight) return;
  state.inFlight = ticket;
  try {
    let config = await readConfig($, options);
    if (!config || !isCurrent(state, ticket)) return;
    const usage = await $.session.usage();
    if (!isCurrent(state, ticket)) return;
    const agents = await $.agent.list();
    if (!agentsAreIdle(agents) || !isCurrent(state, ticket)) return;
    const now = await $.clock.now();
    // A user may opt out while the usage/agent checks are in progress.
    config = await readConfig($, options);
    if (!isCurrent(state, ticket) || !meetsPolicy(state, config, usage?.context?.tokens, now)) return;

    const surfaces = await $.session.surfaces();
    if (!isCurrent(state, ticket) || !Array.isArray(surfaces)) return;
    // Desktop's embedded host can lack the terminal compaction implementation.
    // Select its documented native command up front, never retry a refusal or
    // an arbitrary API failure through a second route.
    const desktop = surfaces.includes('desktop') && !surfaces.includes('terminal');
    if (desktop) {
      const commands = await $.command.list();
      if (!isCurrent(state, ticket) || !commands.some(command =>
        command.name === 'compact' && command.source === 'builtin')) return;
    }
    config = await readConfig($, options);
    if (!isCurrent(state, ticket) || !meetsPolicy(state, config, usage?.context?.tokens, now)) return;

    // Consume the attempt before dispatch. Refusal/error cannot create a retry
    // timer or immediately reuse the same accumulated turns.
    state.lastAttemptAt = now;
    state.completedTurns = 0;
    ticket.dispatched = true;
    try {
      if (desktop) {
        await $.command.run({ command: 'compact' });
        // CommandRunResult does not certify whether the transcript compacted.
        state.lastOutcome = 'command_dispatched';
        return;
      }
      const result = await $.session.compact();
      if (typeof result?.skip === 'string') {
        state.lastOutcome = 'refused';
        $.ui.log('Automatic compaction was skipped by an existing compaction hook.');
      } else if (Array.isArray(result?.messages)) {
        state.lastOutcome = 'compacted';
      } else {
        state.lastOutcome = 'error';
        $.ui.log('Automatic compaction returned no confirmed result; no retry was scheduled.');
      }
    } catch {
      state.lastOutcome = 'error';
      $.ui.log('Automatic compaction failed; no retry was scheduled.');
    }
  } catch {
    // Config and runtime errors never affect the answer or submit another turn.
    state.lastOutcome = 'error';
  } finally {
    if (state.inFlight === ticket) state.inFlight = null;
  }
}

export async function afterTurn($, event, next, state, options) {
  const sessionToken = state.sessionToken;
  if (event.agentId !== undefined) {
    try { return await next(event); }
    finally {
      if (state.sessionToken === sessionToken && state.activeAgentTurns.get(event.agentId) === event.turnId) state.activeAgentTurns.delete(event.agentId);
    }
  }
  const validId = typeof event.turnId === 'string' && event.turnId.length > 0;
  const duplicate = !validId || state.seenTurns.has(event.turnId);
  if (validId) state.seenTurns.add(event.turnId);
  const generation = state.generation;
  let result;
  try {
    result = await next(event);
  } finally {
    if (state.sessionToken === sessionToken && state.activeTurnId === event.turnId) state.activeTurnId = null;
  }
  if (duplicate || event.reason !== 'answer' || event.isAborted !== false ||
      state.generation !== generation || state.activeTurnId !== null ||
      state.inFlight || state.compactionDepth > 0 || state.activeAgentTurns.size > 0) return result;
  state.completedTurns += 1;
  try {
    const config = await readConfig($, options);
    if (!config || state.completedTurns < config.min_turns || state.pending ||
        state.inFlight || state.activeTurnId !== null || state.generation !== generation ||
        state.compactionDepth > 0) return result;
    const now = await $.clock.now();
    if (!Number.isFinite(now) || state.generation !== generation || state.activeTurnId !== null || state.pending || state.inFlight ||
        (state.lastAttemptAt !== null && now - state.lastAttemptAt < config.cooldown_seconds * 1000)) return result;
    const ticket = { generation, timer: null };
    state.pending = ticket;
    ticket.timer = $.clock.after(DEFER_MS, () => runDeferred($, state, options, ticket));
  } catch {
    cancelPending(state);
  }
  return result;
}

export async function observeCompaction($, event, next, state) {
  if (event.agentId !== undefined) return next(event);
  cancelPending(state);
  if (!state.inFlight) state.generation += 1;
  const sessionToken = state.sessionToken;
  state.compactionDepth += 1;
  try {
    const result = await next(event);
    if (state.sessionToken === sessionToken && event.trigger !== 'precompute' && Array.isArray(result?.messages)) {
      state.completedTurns = 0;
      try {
        const now = await $.clock.now();
        if (state.sessionToken === sessionToken && Number.isFinite(now)) state.lastAttemptAt = now;
      } catch {
        // Optional cooldown bookkeeping must not fail a successful native compact.
      }
    }
    return result;
  } finally {
    if (state.sessionToken === sessionToken) state.compactionDepth -= 1;
  }
}

export function register(on, options = {}) {
  const state = createCompactionState();
  on('turn.start', async ($, event, next) => {
    cancelPending(state);
    state.generation += 1;
    state.activeTurnId = event.turnId;
    return next(event);
  });
  on('turn.step', async function* ($, event, next) {
    if (event.agentId !== undefined && state.compactionDepth === 0 && !state.inFlight?.dispatched) {
      cancelPending(state);
      state.generation += 1;
      state.activeAgentTurns.set(event.agentId, event.turnId);
    }
    return yield* next(event);
  });
  on('agent.spawn', async ($, event, next) => {
    cancelPending(state);
    state.generation += 1;
    return next(event);
  });
  on('turn.complete', async ($, event, next) => afterTurn($, event, next, state, options));
  on('session.compact', async ($, event, next) => observeCompaction($, event, next, state));
  on('session.end', async ($, event, next) => {
    cancelPending(state);
    const generation = state.generation + 1;
    Object.assign(state, createCompactionState(), { generation });
    return next(event);
  });
}
