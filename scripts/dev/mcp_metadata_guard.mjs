/** Executable boundary for metadata inspection in a JavaScript tool runtime. */
export const DEFAULT_MAX_CHARACTERS = 8000;

/** The message names the fixed code and, once a usable reference exists, that validated reference.
 * A refusal before then has sourceRef null, whether or not `retain` ran. Neither ever
 * includes source content or a sensitive reference.
 */
export class MetadataGuardError extends Error {
  constructor(code, sourceRef = null) {
    super(sourceRef === null
      ? `Metadata inspection refused: ${code}. No usable source reference is available; retain the source under a valid reference and read it in bounded ranges.`
      : `Metadata inspection refused: ${code}. Source retained as ${sourceRef}; read it in bounded ranges.`);
    this.name = 'MetadataGuardError';
    this.code = code;
    this.sourceRef = sourceRef;
  }
}

function object(value) {
  if (value === null || typeof value !== 'object' || Array.isArray(value) ||
      ![Object.prototype, null].includes(Object.getPrototypeOf(value))) {
    throw new MetadataGuardError('expected_object');
  }
  return value;
}

function own(value, key) {
  const descriptor = Object.getOwnPropertyDescriptor(object(value), key);
  if (descriptor && !Object.hasOwn(descriptor, 'value')) {
    throw new MetadataGuardError('accessor_not_allowed');
  }
  return descriptor?.value;
}

/** Parse once; never expose JSON.parse's input-bearing error message. */
export function decodeObject(raw) {
  let value = raw;
  if (typeof value === 'string') {
    if (!value.trim()) throw new MetadataGuardError('empty_input');
    if (/(?:…|\.\.\.)\s*\d+\s+(?:tokens|characters|chars)\s+truncated\s*(?:…|\.\.\.)/iu.test(value)) {
      throw new MetadataGuardError('client_truncated');
    }
    try { value = JSON.parse(value); }
    catch { throw new MetadataGuardError('invalid_or_incomplete_json'); }
  }
  return object(value);
}

/** MCP CallToolResult: structured data wins; text must be one complete JSON block.
 * Agents-Core's legacy structuredContent.result JSON string is unwrapped once.
 * A direct payload must use decodeObject, so its `content`/`result` keys stay data.
 */
export function decodeMcpObject(raw) {
  const envelope = decodeObject(raw);
  const isError = own(envelope, 'isError');
  if (isError !== undefined && typeof isError !== 'boolean') throw new MetadataGuardError('invalid_mcp_error_state');
  if (isError === true) throw new MetadataGuardError('tool_error');
  const structured = own(envelope, 'structuredContent');
  if (structured !== undefined) {
    const value = decodeObject(structured);
    if (Object.keys(value).length === 1 && typeof own(value, 'result') === 'string') {
      return decodeObject(own(value, 'result'));
    }
    return value;
  }
  const blocks = own(envelope, 'content');
  if (!Array.isArray(blocks) || blocks.length !== 1 || own(blocks[0], 'type') !== 'text') {
    throw new MetadataGuardError('ambiguous_mcp_content');
  }
  return decodeObject(own(blocks[0], 'text'));
}

/** A declarative allowlist: each output name maps to {path: [...], count?: true}.
 * Values are scalars only; collections can leave the boundary only as counts.
 */
export function projectMetadata(value, fields) {
  object(value);
  const selected = Object.create(null);
  for (const name of Object.keys(object(fields))) {
    const spec = own(fields, name);
    object(spec);
    const path = own(spec, 'path');
    if (!Array.isArray(path) || path.length === 0 || path.length > 16 ||
        path.some(key => typeof key !== 'string' || !key ||
          ['__proto__', 'constructor', 'prototype'].includes(key) ||
          /(?:token|password|secret|capability|authorization|credential)/iu.test(key))) {
      throw new MetadataGuardError('invalid_field_path');
    }
    let field = value;
    for (const key of path) field = field === undefined ? undefined : own(field, key);
    if (own(spec, 'count') === true) {
      if (!Array.isArray(field)) throw new MetadataGuardError('expected_array_for_count');
      field = field.length;
    }
    if (field !== null && !['string', 'boolean', 'number'].includes(typeof field)) {
      throw new MetadataGuardError('expected_scalar_field');
    }
    if (typeof field === 'number' && !Number.isFinite(field)) {
      throw new MetadataGuardError('non_finite_field');
    }
    selected[name] = field;
  }
  return selected;
}

/** Best-effort context redaction, not a detector for unknown opaque secrets.
 * Supply known sensitive values; keep sensitive sources only in memory.
 */
export function redactCredentials(text, secrets = []) {
  if (typeof text !== 'string' || !Array.isArray(secrets) ||
      secrets.some(secret => typeof secret !== 'string' || secret.length === 0)) {
    throw new MetadataGuardError('invalid_redaction_options');
  }
  // Suppress the whole selected string: quoted values and authentication
  // schemes do not have a safe first-whitespace boundary.
  if (/(?:\b(?:Bearer|Basic)\s+|(?:token|password|secret|capability|authorization|credential)[\w-]*["']?\s*[:=])/iu.test(text)) {
    return '[redacted]';
  }
  // Match against the original text: replacing one value first can hide a
  // nested or partially overlapping value and leave its suffix visible.
  const spans = [];
  for (const secret of secrets) {
    for (let start = text.indexOf(secret); start !== -1; start = text.indexOf(secret, start + 1)) {
      spans.push([start, start + secret.length]);
    }
  }
  spans.sort((a, b) => a[0] - b[0] || b[1] - a[1]);
  let redacted = '', cursor = 0;
  for (let index = 0; index < spans.length; index++) {
    const start = spans[index][0];
    let end = spans[index][1];
    while (index + 1 < spans.length && spans[index + 1][0] <= end) {
      end = Math.max(end, spans[++index][1]);
    }
    redacted += text.slice(cursor, start) + '[redacted]';
    cursor = end;
  }
  return redacted + text.slice(cursor);
}

/** Metadata only: never copies message text, previews, reasoning or tool input.
 * Even agent messages expose only a closed type/phase pair, not their body.
 */
export function summarizeThread(value) {
  const thread = object(own(value, 'thread'));
  const turns = Object.hasOwn(value, 'turns') ? own(value, 'turns') : own(thread, 'turns');
  if (!Array.isArray(turns)) throw new MetadataGuardError('expected_thread_turns');
  const items = [];
  let omittedItems = 0;
  for (const turn of turns) {
    const sourceItems = own(turn, 'items');
    if (!Array.isArray(sourceItems)) throw new MetadataGuardError('expected_turn_items');
    for (const item of sourceItems) {
      if (own(item, 'type') !== 'agentMessage') { omittedItems++; continue; }
      const phase = own(item, 'phase');
      items.push({type: 'agentMessage', phase: ['commentary', 'final'].includes(phase) ? phase : 'unknown'});
    }
  }
  const id = own(thread, 'id');
  return {threadId: typeof id === 'string' && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/u.test(id) ? id : null,
    turnCount: turns.length, items, omittedItems};
}

/** Retain before parsing, project before serializing, check before emitting.
 * `retain` must save the complete source in the authorized evidence store (or
 * an in-memory store for sensitive metadata) and return an opaque reference.
 */
async function guardedEmission(raw, {retain, emit, mcp = false, secrets = [],
    maxCharacters = DEFAULT_MAX_CHARACTERS}, select) {
  // Check every option before `retain` runs; a snapshot keeps the checked secrets fixed.
  const known = Array.isArray(secrets) ? [...secrets] : null;
  if (!Number.isSafeInteger(maxCharacters) || maxCharacters < 1 || maxCharacters > DEFAULT_MAX_CHARACTERS ||
      typeof retain !== 'function' || typeof emit !== 'function' ||
      known === null || known.some(secret => typeof secret !== 'string' || secret.length === 0)) {
    throw new MetadataGuardError('invalid_guard_options');
  }
  let sourceRef;
  try { sourceRef = await retain(raw); }
  catch { throw new MetadataGuardError('source_retention_failed'); }
  if (typeof sourceRef !== 'string' || !/^[A-Za-z0-9][A-Za-z0-9_./:#-]{0,255}$/u.test(sourceRef)) {
    throw new MetadataGuardError('invalid_source_reference');
  }
  if (redactCredentials(sourceRef, known) !== sourceRef) throw new MetadataGuardError('sensitive_source_reference');
  let text;
  try {
    const value = mcp ? decodeMcpObject(raw) : decodeObject(raw);
    const metadata = select(value);
    // Redact selected strings before serialization, including strings in a safe
    // adapter's nested projection. Raw data never reaches this serialization.
    text = JSON.stringify({sourceRef, metadata}, (_key, field) =>
      typeof field === 'string' ? redactCredentials(field, known) : field);
    if (text.length > maxCharacters) throw new MetadataGuardError('output_budget_exceeded');
  } catch (error) {
    throw new MetadataGuardError(error instanceof MetadataGuardError ? error.code : 'invalid_metadata', sourceRef);
  }
  try { await emit(text); }
  catch { throw new MetadataGuardError('emission_failed', sourceRef); }
  return {sourceRef, characters: text.length};
}

export function guardMetadata(raw, options) {
  return guardedEmission(raw, options, value => projectMetadata(value, options.fields));
}

export function guardThreadMetadata(raw, options) {
  return guardedEmission(raw, options, summarizeThread);
}
