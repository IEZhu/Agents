# Model tiers for delegated workers

Status: implemented 2026-10-17. Extends the
[task-scoped subagent contract](subagent-contract.md).

## Problem and goal

A prepared worker runs on whatever model its host chooses, which is the parent's
model unless the parent asks otherwise. Tiering lets the parent request a smaller
model for mechanical or well-specified assignments. The goal is fewer flagship
tokens per delegated task with a comparable accepted result; this implementation
does not establish equivalent model quality or measured token savings.

The parent rates each assignment, Agents-Core resolves a model tier to the latest
model of that tier for the calling client, the host launches the worker on that
model, and the existing structural validation decides whether the attempt climbs
to the next tier.

Scope: interactive delegation from Claude Code, Codex and Cursor through
`get_subagent_context` and `validate_subagent_result`. Autodev schedules keep their
own per-step `client`/`model`/`effort` settings and are out of scope.

## Host facts this design rests on

Checked on 2026-10-17 against the installed clients; re-check when a client changes
its subagent tool.

| Client | Launch tool and model field | Model catalog the server can read |
|---|---|---|
| Claude Code 2.1.302 | `Agent(model=...)`: aliases `haiku`, `sonnet`, `opus`, `fable` | none needed: the aliases always name the latest model of each class |
| Codex 0.163.0 | `spawn_agent(model=..., reasoning_effort=..., fork_turns="none")` | `$CODEX_HOME/models_cache.json` (`models[].slug`, `visibility`) |
| Cursor 3.24 | Task tool `model` (optional); custom subagent frontmatter `model`, default `inherit` | `state.vscdb` key `src.vs.platform.reactivestorage.browser.reactiveStorageServiceImpl.persistentStorage.applicationUser`, field `availableDefaultModels2[].serverModelName` |

Codex's built-in `spawn_agent` description tells the model not to set `model`
unless the user explicitly asks for a different model. The routing protocol line
added by this design is that explicit user instruction.

Not verified: whether Cursor's Task tool accepts `model: "default"` (Auto). If it
does not, the override in [Configuration](#configuration) replaces it.

## Tiers and complexity

Three tiers, ordered: `light` < `standard` < `inherit`. `inherit` sets no model
override, so the worker runs on the parent's model and effort.

The parent rates the assignment with `complexity`; the server maps it 1:1:

| `complexity` | Tier | Rubric (protocol text) |
|---|---|---|
| `low` | `light` | Mechanical, fully specified and checkable: collect facts from named files, rename, format, apply a given edit, write tests from a given spec, summarize given material. |
| `standard` | `standard` | Bounded implementation or research inside one module with clear acceptance criteria. |
| `high` | `inherit` | Ambiguous or cross-cutting work, design decisions, final reviews, or anything where a wrong result is costly or hard to verify. |

Omitting `complexity` keeps today's behavior: no tier, no model recommendation, no
contract change.

### Floors

The server may raise a tier and never lowers one. It scans the casefolded task,
context, scope and acceptance criteria. Non-goals are left out: they name work the
worker must not do, such as "never print secrets", which says nothing about the
risk of the work itself.

- `inherit` floor: security or vulnerability work, authentication, authorization,
  credentials or secrets, encryption, data migrations or schema changes, deleting
  data, production or deployment changes, infrastructure as code, concurrency or
  race conditions, architecture decisions. Russian stems of the same terms are
  included because parents sometimes write assignments in Russian.
- `standard` floor: more than 8 acceptance criteria, more than 8 scope items,
  more than 12000 characters of task and context, or an artifact manifest with more
  than 5 entries or more than 65536 bytes.

`floor_reasons` reports floors above the originally requested tier. A size floor
does not appear when `standard` or `inherit` was already requested. An unknown
client adds `client:unknown` only when it raises a requested `light` or `standard`
tier to `inherit`. Floors are conservative lexical checks, not a guarantee that
the selected model can complete the assignment.

## Model resolution

Per client and tier:

| Client | `light` | `standard` |
|---|---|---|
| `claude` | alias `haiku` | alias `sonnet` |
| `codex` | newest listed `gpt-<version>-luna`, effort `medium`; pinned `gpt-6-luna` | newest listed `gpt-<version>-sol`, effort `medium`; pinned `gpt-6.1-sol` |
| `cursor` | `default` (Auto, the first entry of Cursor's model list) | newest listed `composer-<version>`; pinned `composer-2.5` |

"Newest" compares the numeric version in the slug (`gpt-6.1-sol` > `gpt-6-sol` >
`gpt-5.6-sol`). Only entries Codex lists (`visibility: "list"`) qualify. Cursor's
Composer has no effort levels; its `fast` variant raises usage and is never chosen.

A catalog that is missing, unreadable, malformed or without a matching entry falls
back to the pinned model; `source` says which applied: `alias`, `catalog`,
`pinned`, `override`, `inherit` or `disabled`. Catalog reads are read-only, take
model names only and are cached for 10 minutes per path. Cursor's database is
opened with SQLite `mode=ro` and a short timeout.

### Client detection

The parent passes `client`, the host it runs in, as the protocol asks: a model
knows whether it runs in Claude Code, Codex or Cursor. Without the argument the
server falls back to the request: the shared daemon's `X-Agents-Client` header
(`claude-code`, `codex`, `cursor`), or over stdio the MCP `clientInfo.name`. A name
containing `codex` or `cursor` names that client; `claude-code` (or `claude`) names
Claude Code. The fallback misses two kinds of Claude Code session:

- Projects pinned before the header existed (#187). The service keeps the
  user-scope entries of Claude Code and Cursor, their project layers where it
  manages them, and the Codex header helper in their current form, and those send
  the header. A Claude Code project pin keeps the form `migrate --workspace` wrote
  (`src/daemon/registrations.py`), so an old pin sends only `Authorization` and
  `X-Agents-Workspace`, and in its project that pin takes precedence over the user
  entry. The upkeep also leaves Git-tracked files, entries the user adapted, and
  profiles that use the agents-core Claude Code plugin as they are.
- Sessions that a stdio server of a service installation hands to the bridge, such
  as agents-core plugin sessions. They reach the service through
  `bridges/stdio-auto.json`, which names the app `stdio`; that names no client.
  The service writes that file only where Node is available; a stdio server that
  serves standalone (no Node, or `AGENTS_STDIO_STANDALONE=1`) reads the client from
  `clientInfo`.

Over stateless HTTP, `clientInfo` arrives only with `initialize`, not with tool
calls. On 2026-10-17 one installation's Claude Code project pins sent no client
header while its Codex and Cursor registrations did. #447 tracks closing these
gaps. An unidentified client (also the
Claude desktop chat or Antigravity) gets tier `inherit` with `client: null`, no
`spawn` and the reason `client:unknown` in `floor_reasons`: no lighter model can be
launched for it, so the host keeps its model and nothing escalates;
`requested_tier` still reports the rating.

### Configuration

Environment variables, read on every call:

- `AGENTS_SUBAGENT_TIERING=0` disables resolution: every assignment gets tier
  `inherit` with `source: "disabled"`, so nothing escalates;
  `requested_tier` and `floor_reasons` still report.
- `AGENTS_SUBAGENT_MODEL_<CLIENT>_<TIER>` replaces a model, for example
  `AGENTS_SUBAGENT_MODEL_CURSOR_LIGHT=composer-2.5`.
- `AGENTS_SUBAGENT_EFFORT_<CLIENT>_<TIER>` replaces an effort; an empty value
  removes it.

Overrides must match the plain model-name form the CLIs take
(`src/flow_execution.py` `MODEL_NAME`); an invalid value is ignored with a logged
warning and the default applies.

## Contract and responses

`get_subagent_context` gains two optional arguments:

| Argument | Contract |
|---|---|
| `complexity` | `low`, `standard` or `high`; omitted = no tiering |
| `client` | `claude`, `codex` or `cursor`, the parent's host; pass it with `complexity`, otherwise the server falls back to the `X-Agents-Client` header or, over stdio, MCP `clientInfo` ([client detection](#client-detection)) |

With `complexity`, the contract gains `execution`, covered by
`contract_revision` like every other field:

```json
{"complexity": "low", "tier": "light", "client": "codex",
 "model": "gpt-6-luna", "reasoning_effort": "medium"}
```

The prepared package gains a top-level `execution` with the same fields plus
`requested_tier`, `floor_reasons`, `source` and `spawn`, the launch arguments
for the host:

| Client | `spawn` |
|---|---|
| `claude` | `{"tool": "Agent", "arguments": {"model": "haiku"}}` |
| `codex` | `{"tool": "spawn_agent", "arguments": {"model": "gpt-6-luna", "reasoning_effort": "medium", "fork_turns": "none"}}` |
| `cursor` | `{"tool": "Task", "arguments": {"model": "default"}}` |

At `inherit` the arguments carry no model or effort (Codex keeps
`fork_turns: "none"`); for an unknown client `spawn` is null.

### Escalation

For a contract with `execution`, `validate_subagent_result` may add `escalation`:

- absent when the result is acceptable structurally and needs no climb: `valid`,
  status `completed` or `input_required`, and no check `failed`; also always at
  tier `inherit`, which has nothing above it. Such a response has the same shape
  as one for a contract without `execution`.
- `{"complexity": ..., "tier": ..., "reason": ...}` naming the next tier when the
  result is `failed` or `partial`, when any check `failed`, or when `valid` is
  false. For `valid: false` the parent first returns the diagnostics for one
  correction, as today; the escalation applies when the corrected output is still
  invalid. The validator is stateless and cannot count corrections, so the
  protocol carries that rule. A retained contract that fails its own checks gets
  no escalation.

An escalated attempt is a new attempt: the parent prepares a new package with a
new `attempt_id` and the escalation's `complexity`, keeps that new contract, and
launches a fresh worker. Floors still apply. `escalation` never makes a result
acceptable; the parent's own review of evidence and acceptance criteria is
unchanged.

## Ledger

To measure whether light workers hold up, set `AGENTS_SUBAGENT_LEDGER=1`. The
server then appends one JSON line per prepared package with `execution` and per
validation of a contract with `execution` to `data/subagent-tiers/ledger.jsonl` in
the installation (directory mode 0700, file mode 0600, size-capped at 5 MiB with
one rotated copy). A line holds a timestamp, event, the task ID's SHA-256 prefix,
the attempt ID, client, complexity, tier, model, effort, source and floor reason
codes, plus `valid`, result status, failed check count and the escalation tier and
reason for validations. It never holds task text, context, paths, results or
workspace names. A write failure is logged and never fails the tool call.

The ledger is off by default, like the `AGENTS_DEBUG` logs: both worker tools keep
their read-only tool annotations, which clients such as Codex use to decide whether
a call needs approval.

`scripts/subagent_tiers_report.py` summarizes the ledger per client, tier and
model: prepared packages, validations, the share accepted without escalation, and
escalations by target tier. Token counts are not visible to the server; an A/B
evaluation that runs the same assignments per tier is separate work.

## Protocol text

The delegation section, `src/engine/protocol/delegation.md`, gains one step in
Delegated worker mode, and `python -m src.engine.protocol_text --write` renders it
into every surface: the template `scripts/templates/routing-protocol-core.md`, the
MCP initialization instructions, `APPLY_INSTRUCTION` and the memory reminder:

- rate the assignment `complexity` (the three-line rubric) when preparing;
- launch the worker with the package's `execution.spawn` arguments; for Codex this
  is the user's explicit request to set `model` and `reasoning_effort`;
- on a returned `escalation`, prepare a new attempt at that complexity.

The details live in the tool descriptions and the response, so sessions that never
delegate do not pay for them.

## Tests

- Resolution: newest-version selection, hidden Codex entries, missing and
  malformed catalogs, the Cursor database read, pinned fallbacks, overrides,
  invalid overrides, the disable switch, unknown clients.
- Floors: each `inherit` and `standard` trigger, no lowering, reasons reported.
- Preparation: no `complexity` keeps the package and contract byte-identical in
  shape to before; `execution` in the contract and the package; revision changes
  with the tier; client detection from the header, `clientInfo` and the argument.
- Validation: escalation for each trigger, absent when acceptable and at
  `inherit`, contracts without `execution` unchanged.
- Ledger: off by default, line content without task text, rotation, failure
  isolation; report aggregation.
- MCP schemas: the new arguments and response fields.

## Non-goals

- Autodev schedule steps and their client/model/effort configuration.
- Measuring tokens inside the server, or an automatic A/B evaluation.
- Native agent definitions per client (`.claude/agents`, Codex agent roles,
  `.cursor/agents`).
- Choosing models by the parent's own model family.
- Changing budgets (`max_skills`, `max_context_chars`) per tier.
