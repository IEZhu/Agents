# Work map: a parent's top-down view of verified delegated work

**Status:** proposal of 2026-11-25, implemented in one pull request: the store, the views, the tools `workmap_update`, `workmap_view` and `workmap_submit` (see [MCP tools](mcp-tools.md)), the binding of a package to a node, the worker's own submission, verifier verdicts, the [`orchestrate` flow](../flows/orchestrate.md) and the measurement script `evals/scripts/workmap_context.py`. The design below states the behaviour as built; the [Work-map binding](subagent-contract.md#work-map-binding) section of the subagent contract is its reference. Its one load-bearing assumption, that a worker can reach the server's tools itself, was checked in Claude Code on that date ([spike](#spike-of-2026-11-25)); Codex and Cursor workers are unchecked. Builds on
the [task-scoped subagent contract](subagent-contract.md), [shared artifacts](shared-artifacts.md)
and [validated claims](validated-claims.md).

A parent session that splits work into stages should hold a map of that work, not the
work itself: which stages exist, what each depends on, which results are in and how well
each was checked. Workers do the stages; their full results, format corrections and
evidence stay between the worker, the server and, where the risk warrants one, an
independent verifier. The parent's context then carries a top-down view that grows by
a line per stage instead of by a full result per stage.

## Problem

Four things put intermediate work into the parent's context today, or keep the state of
the work nowhere else.

1. **The hand-back is the whole result.** The launch text tells the worker to "reply with
   only the result it specifies" (`launch_text` in
   [src/engine/subagent_exchange.py](../src/engine/subagent_exchange.py)), and the host
   delivers that reply to the parent; Claude Code shows it as the agent's hand-back
   message. In the spike below, the two hand-backs of one
   trivial worker were about 2,700 and 3,400 characters, and both stay in the parent's context
   for the rest of the session. A review or research result with evidence is larger.
2. **The correction loop runs through the parent.** On `valid: false` the parent sends the
   diagnostics back and receives a second full result. The spike needed one such round.
3. **The parent must assess the evidence itself.** `validate_subagent_result` is
   structural, and the protocol then asks the parent to check evidence and acceptance
   criteria independently, which means reading them.
4. **The state of the work lives in the parent's conversation.** "Preparation and
   validation hold no state; the parent's working set is the state"
   ([delegation reliability plan](delegation-reliability-plan.md)). Compaction or a new
   session loses which stages are done, what depends on what and how sure each result is.

What already works is the other half: validation by reference (#453) means the parent
never retypes a result, and `validate_subagent_result` answers with `valid`, `errors` and
`result_sha256` only.

## Goals and non-goals

Goals:

- The parent holds a map: per stage a title, a summary of at most 300 characters, a
  status, a verification level and references. Nothing else of a stage's work enters
  the parent's context unless it asks for it by name.
- A worker submits its result to the server and corrects its own format errors; its
  hand-back to the parent is one line.
- Verification is proportional to risk: every result is structurally valid and carries
  the worker's own checks (V2); a stage marked critical, or prepared with
  `complexity=high`, also gets an independent verifier (V3).
- A stage's input from an earlier stage travels from server to worker as a pinned
  artifact, never through the parent.
- The map is durable: it survives compaction and session restarts, and another session in
  the same workspace can read it.
- The same procedure works in Claude Code and Codex, and falls back to today's exchange
  where a worker cannot reach the server.

Non-goals:

- Launching workers. The host still owns execution, models and limits; no tool spawns.
- Semantic verification by the server. Validation stays structural; verification is a
  verifier worker's job.
- Coordination across machines (#690) and resource scheduling (#723).
- A graphical view. A mind-map tab in the daemon's web UI is a later step.
- Replacing validated claims. The map is about one task's stages; a fact that outlives
  the task still goes to `claim_record`.

## Spike of 2026-11-25

One prepared worker (task `workmap-spike-worker-mcp`, `complexity=low`, launched with the
receipt's `Agent` arguments on `haiku`) was asked to call two Agents-Core tools itself.

- **A Claude Code worker reaches the server's tools.** It loaded `list_flows` and
  `validate_subagent_result` with `ToolSearch` and called them: one `list_flows` call
  (43 flows returned) and two `validate_subagent_result` calls, as its transcript shows.
- **The package does not give the worker its full contract revision.** It shows the
  12-character `contract` id, so the worker's first self-validation failed with
  `invalid_revision`. Given the 64-character revision, it returned `valid: true`.
- **Self-validation alone proves nothing about what the parent receives.** The worker
  validated a draft and then replied with a different text: the parent's own validation
  by `result_ref` produced a different `result_sha256`. Hence the rule below that the
  submitted bytes are the result and the reply is only a receipt.
- **The hand-back's own validation failed once.** The first reply said `completed` with
  remaining work (`completed_incomplete`); the corrected reply passed. Under this design
  that round happens between the worker and the server.
- Not checked: Codex `spawn_agent` workers, Cursor `Task` workers and the autodev runner's
  sandboxed CLI workers.

## Design

### Data model

One map per workspace and `task_id`. It holds nodes and edges.

| Node field | Content |
|---|---|
| `id` | Parent-assigned slug, 1–40 characters of `a-z`, `0-9` and `-`, unique in the map |
| `kind` | `goal`, `stage`, `decision` or `question` |
| `title` | At most 120 characters |
| `summary` | At most 300 characters; the parent's intent until a result arrives, then the result's own summary cut to 300 |
| `risk` | `routine`, `load_bearing` (default) or `critical`, the importance scale of validated claims |
| `status` | See the table below |
| `level` | `V0` to `V4`, the thread-close and claims scale: V0 claimed, V1 read, V2 executed, V3 independently reviewed, V4 accepted by the owner |
| `refs` | `contract_revision` and `attempt_id` of the current attempt, the `inputs` pinned for it as `{node: result_sha256}`, and the recorded `result_sha256` |
| `checks` | Counts only: passed, failed, not run; plus the count of limitations and of findings by severity |
| `handback`, `result_status`, `question`, `escalation` | Whether the result arrived as the worker's one line or in full, the worker's own status, its question cut to 300 characters and the escalation validation named |

An edge is `{from, to, type}` with `type` one of `depends_on`, `verifies`, `supersedes`
and `contradicts`. A `depends_on` cycle is refused, and so is a `depends_on` edge onto a
goal, which never settles.

| Status | Set by | Meaning |
|---|---|---|
| `planned` | parent | Defined, not yet prepared |
| `prepared` | server, on a preparation bound to the node | A package exists; the server does not know whether a worker runs |
| `validated` | server, on a valid submission with status `completed` and no failed check | Level V2 when at least one check passed, V1 when none did; V2 is the worker's own report, since the server reruns no check |
| `needs_attention` | server, on a valid submission with status `partial`, `failed` or `input_required` or with a failed check, or on the fifth invalid submission for the node's current, still `prepared` attempt | The parent decides: new attempt, re-plan or ask the user |
| `verified` | server, as the verdict of a verifier's `validated` submission without a `critical` or `high` finding | Level V3 |
| `disputed` | server, as the verdict of a verifier's `validated` submission with a `critical` or `high` finding | A finding is not a refutation; the parent decides |
| `accepted` | parent, only on a `validated`, `verified` or already `accepted` node (`invalid_transition` otherwise) | Level stays; V4 only when the user accepted it, and accepting an `accepted` node again with V4 is how a later acceptance by the user is recorded |
| `refuted`, `superseded`, `blocked` | parent | With a reason of at most 300 characters |

Server-owned statuses cannot be written by `workmap_update`: a parent cannot mark
unchecked work as validated. Three states are derived when the view is rendered and never
stored. A node is **settled**, so that a dependant may build on it, when it is `validated`,
`verified` or `accepted` at the level it needs (V3 for a `critical` node or one ever
prepared with `complexity=high`, otherwise V2); acceptance settles a `routine` or
`load_bearing` node at any level, while an accepted `critical` node still needs V3 or V4.
An accepted node takes no verdict, so `workmap_update` refuses to accept a `critical` node
below V3 unless the acceptance carries the user's V4 (`invalid_transition`).
A planned node other than a goal is **ready** when every `depends_on` target is settled and every node it
`verifies` is `validated`, `verified`, `accepted` or `disputed` and holds a result, the
inputs a verifier's package could take. A node is **stale** when anything it depends on is
`refuted`, `superseded` or `disputed`. The view's `needs attention:` list names the
`needs_attention` and `disputed` nodes and every `validated`, `verified` or `accepted`
node below the level it needs (an `accepted` `critical` node below V3 without V4 can
appear only on a map written before that acceptance was refused), which readies no
dependant and does not change on its own. The parent has three ways out: a verifier for a
`validated`, `verified` or `disputed` node, acceptance where acceptance settles the node (a
`routine` or `load_bearing` one, or a `critical` one with the user's V4), or a new attempt. A `validated` node is not preparable, so a new
attempt starts by setting it back with `workmap_update` to `planned`, which the map takes
from `validated`; the node keeps its old level and result until the new package is
prepared.

### Storage

`<workspace>/data/workmap/<key>.jsonl`, an append-only event log, with the exact text of
each submitted result beside it in `<key>.results.jsonl`. The key is the first 16
hexadecimal characters of the SHA-256 of the task ID, so the dots and colons a task ID may
hold never reach a file name, and the first event names the task. It follows the validated-claims store in everything that matters for safety: anchored
opens that refuse links, private permissions, an exclusive lock per write, a torn last
line never glued to the next event, a size cap and refusal of a `workspace` outside the
registered root (#576). Those helpers live in `src/safe_store.py`, which the claims store
and the work-map store share. The events of one change are written together and each
carries the change's last revision, so a torn write applies none of them.

The map keeps its own copy of each result because the exchange directory prunes packages
after seven days, after which `contract_revision` no longer resolves. Every write keeps a
`.gitignore` holding `*` in `data/workmap/`, so a repository that does not ignore `data/`
never stages a map or a worker's result; this repository ignores `data/` anyway. Nothing
under it is part of the user-library sync, so a map of a work repository stays on the
machine that made it.

### Tools

Two new tools and two changed ones.

**`workmap_update(task_id, ops, expected_revision?, workspace?)`** applies up to 50
operations atomically: `add_node`, `update_node` (title, summary, risk, a parent-owned
status with its reason), `add_edge`, `remove_edge`, `close`. It returns the new
`revision` and the changed node ids, not the map. The first call creates the map; with
`expected_revision=0` it is refused with `revision_conflict` when a map of that `task_id`
already exists, and any other `expected_revision` refuses a stale write the same way. A
server-owned status is refused with `status_reserved`, `accepted` on a node that is not
`validated`, `verified` or `accepted` with `invalid_transition`, a `depends_on` cycle with `cycle`, a
`depends_on` edge onto a goal with `invalid_request`, and any write to a closed map with
`map_closed`. The parent sets level `V4` only together with `accepted`.

**`workmap_view(task_id?, node?, since?, detail?, budget?, workspace?)`** renders text,
never the raw event log.

- Without `task_id`: the maps of the workspace, the most recently changed first, one line
  each, `<task_id> · revision <n> · <first goal title> · <count per status>` with
  `· closed` after the revision of a closed map, as many as fit 3,000 characters, then
  `… <n> more maps: name a task_id to read one`; `total` counts them all.
- With `task_id`: the top-down view within `budget` characters (3,000 by default, 500 to
  8,000): a header `map <task_id> · revision <n>`, then one line per node in dependency
  order, `[<status> <level>] <id>: <title> (after <ids>) (verifies <ids>) — <summary>`
  with `stale` after the level of a stale node, then the `ready:` and `needs attention:`
  lists, each naming at most ten nodes and counting the rest (`and <n> more`). When the
  budget runs short the summaries go first, then the last nodes, counted in
  `… <n> more nodes: read them with since or node`.
- With `since`, a revision: only the nodes changed after it, under a second header line
  `changed after revision <since>: <k> of <n> nodes`. The parent re-reads the map this
  way after each hand-back instead of paying for the whole view again.
- With `node`: that node's record within 2,000 characters, opening with
  `node <id> (<kind>, risk <risk>, needs <V2 or V3>) [<status> <level>]`, then its title,
  summary, reason, relations, references (`contract_revision`, `attempt_id`, the pinned
  `inputs`, `result_sha256`), check, limitation and finding counts, `hand-back: line` or
  `full`, the worker's `result status`, its `question` and the escalation validation
  named, as `escalation: complexity <c>, reason <r>, tier <t>`, each cut to 300
  characters, and `invalid submissions: <n> (codes <codes>)` in any status once the node's
  current contract has one; the codes are those of the last invalid submission, not of
  all of them.
- With `node` and `detail="result"`: the stored result itself. This is the explicit escape
  hatch, the one call that brings a stage's work into the parent's context.

**`get_subagent_context`** gains two optional arguments, both requiring `task_id` to name
the map, and a bound package uses `result_format` text.

- `workmap_node`: the node this package serves. The map must exist and be open
  (`map_closed`), and the node must exist and be a `planned`, `prepared`,
  `needs_attention` or `disputed` node other than a goal (`node_not_preparable`); a node
  that `verifies` others is prepared only with `result_type` `review` (`invalid_request`).
  Preparing a node again is a new attempt and needs a new `attempt_id`: an unchanged
  request reproduces a contract that already has a result, or reached five invalid
  submissions, and is refused with `attempt_reused`. The binding `{node, root}` goes into
  the contract, so it is fingerprinted; `root` is the workspace the server resolved for
  the session, never a caller's value. The node becomes `prepared`, keeping the highest
  complexity it was ever prepared with (a new attempt after an escalation passes the
  escalation's complexity, which the node record shows), and a bound package states the full
  `contract_revision` and the submission instruction below. A map that cannot be read or
  written answers `workmap_unavailable`. A bound package needs its stored contract, so
  when the exchange directory cannot store it the request is refused with
  `exchange_unwritable` for either delivery, before the node is marked.
- `workmap_inputs`: up to 10 distinct node ids whose stored results the worker needs. The
  server publishes each as an artifact under a manifest bound into the contract, exactly as
  a caller-supplied `artifact_manifest` is, so a verifier or a dependent stage reads its
  input from the server; it is not combined with `artifact_manifest`. Each input must be
  `validated`, `verified`, `accepted` or `disputed` and hold a stored result
  (`input_not_ready` otherwise). The node's `prepared` event records the pinned inputs as
  `{node: result_sha256}`. The request's fields, the node and its inputs are checked
  first, before anything is published; the agent profile, the request's size and the
  context budget are checked after the inputs are published, so a request refused there
  can leave published input artifacts behind. `attempt_reused` needs the contract's
  revision, which exists only once the package is assembled, so it is found after that and
  before the node is marked.

**`workmap_submit(contract_revision, result | result_ref)`** validates exactly as
`validate_subagent_result` does, which keeps its arguments, its structural checks and its
read-only annotation and records nothing. When the contract `workmap_submit` loaded by
`contract_revision` is bound to a node, a valid result is also stored and recorded on the
node, and the response adds the receipt
`workmap: {task_id, node, status, level, revision, duplicate, refused}`: the node's status
and level, the map's revision, `duplicate` true when this contract's
identical result (the same `result_sha256`) was already recorded and is accepted again
without a new event, and `refused`, present only when the map did not take a valid
result. A refusal keeps `valid: true`, since the result is structurally valid, and the
receipt then reports the node as the map holds it, when the map could be read. The
refusal codes are `already_submitted` (a different result is already recorded for this
contract), `stale_contract` (the node was prepared again since), `node_not_open` (the map
is closed, or the node is neither `prepared` nor in `needs_attention` from this
contract's own invalid submissions) and `workmap_unavailable` (the map could not be read
or written), and the store's own `unknown_map`, `unknown_node`, `key_collision`,
`store_full` and `unsafe_store`. The first three are final for the contract; after any
other the worker submits once more and then hands back its full result. `workmap_submit`
takes no inline `contract`: a caller can fingerprint a contract it wrote itself, so only a
contract the server stored at preparation may name the workspace to write to. Who calls it does not matter: the worker
(preferred) or the parent by `result_ref` (fallback). The first valid submission for a
contract is final, so a worker cannot change a result after the parent has read its node.
A new attempt is a new package with a new `attempt_id`, as today. A bound contract's
submissions are recorded on its node: a valid result once, and an invalid one counted
toward the limit while the node still takes it. An invalid submission is counted against
its contract with its diagnostic codes, not their text, and the fifth sends the node to
`needs_attention` while that contract's attempt is still `prepared`. Only a submission the
node could still take is counted: for a contract that has its result, one that was
prepared again, or a node that is settled, set aside or closed, an invalid one is refused
(`already_submitted`, `stale_contract` or `node_not_open`) and writes nothing. The node
record shows the codes of the last invalid submission. Only a review result, one that
carries findings (counted by severity, none when it found none), gives a verdict, so a
generic result of a node that gained a `verifies` edge after its preparation gives none.
With the result the node keeps the worker's own status, its question cut to 300
characters and the escalation validation named.

### Worker submission

A bound package replaces the last sentence of the launch text with "Submit the result and
reply exactly as it says." and adds one paragraph to the package's result format:

> This result is recorded on node `<node>` of the work map of task `<task_id>`. Submit
> only your finished result: the first valid submission is final. Submit it with the
> Agents-Core tool `workmap_submit(contract_revision="<64 hex>", result=<your complete
> result text>)`. If it answers `valid: false`, correct the result as its errors say and
> submit again. When it answers `valid: true` without `workmap.refused`, reply with
> exactly one line: `submitted <task_id>/<node> <the first 12 characters of
> result_sha256> <status>`. If `workmap.refused` is `already_submitted`, `stale_contract`
> or `node_not_open`, do not resubmit; reply with one line: `not recorded
> <task_id>/<node> <that code>`. If it is `workmap_unavailable`, `store_full`,
> `unsafe_store`, `unknown_map`, `key_collision` or any other code, submit the same result
> once more; if it is refused again, reply with the full result, so the parent can
> validate it by `result_ref`. If the call cannot reach the server, wait about a minute
> and try once more; if it still fails, reply with the full result. If the tool is not
> available to you, reply with the full result instead.

The reply line carries no work, and the parent neither validates nor submits it: either
would only answer `valid: false`, and a submission would count as an invalid one while
the node still takes it. The parent calls `workmap_view(task_id, since=<last revision>)` or reads the node instead. This changes one protocol duty for bound packages
only, as the description of `validate_subagent_result` states: the server's record of a
valid submission against the contract the parent prepared takes the place of the parent's
own `validate_subagent_result` call. The parent trusts the node, never the reply line, so
a worker that claims a submission it did not make leaves its node `prepared`. Where a
worker cannot reach the server (a sandboxed runner, a host without MCP in its workers),
the fallback is today's exchange: the full result arrives, the parent reads the node and
validates the result by `result_ref`, and only while the node is still `prepared` submits
it with `workmap_submit` by the same `result_ref`, which records it. The parent's context then holds that result, which the node marks
as `hand-back: full`.

### Verification by risk

- Every node gets structural validation and the worker's own checks: V2 when at least
  one passed and none failed, V1 when none passed; a failed check sends the node to
  `needs_attention`. V2 is worker-reported: the server reruns no check, so a V2 node
  rests on the worker's own report of what it ran.
- A node whose `risk` is `critical`, or whose package was ever prepared with
  `complexity=high`, is settled for its dependants only at V3 (`needs V3` in its record).
  A `validated` node below the level it needs, V1 or V2 where V3 is needed, appears under
  `needs attention:` until the parent acts. It accepts the node, which settles a `routine`
  or `load_bearing` one only: a `critical` node needs a verifier (V3) first, or is
  accepted with the user's V4, and accepting it below V3 otherwise is refused. Or it
  verifies the node, or sets a `validated` one back to `planned` and prepares a new
  attempt, which starts again at V0.
- The parent adds a verifier node with a `verifies` edge to the target and prepares it as
  a `review` package, the only result type a node that `verifies` others accepts, bound to
  the verifier node with `workmap_inputs=[<target>]`; the prepared event pins the target's
  `result_sha256`. The verifier is ready once each target is `validated`, `verified`,
  `accepted` or `disputed` and holds a result. A verdict comes only from a review result
  the server marks `validated`, and applies only to a target that is still `validated`, `verified` or
  `disputed` and holds the result the verifier was given: without a `critical` or `high`
  finding the target becomes `verified` at V3, with one `disputed`. A partial, failed or
  `input_required` verifier result, or one with a failed check, gives no verdict, and
  neither does a target that was accepted or prepared again since; the verifier's own
  node still records its result.
- A `disputed` target is the parent's decision: a new attempt (with the escalation's
  complexity, shown in the node record when validation returned one), a second verifier, a re-plan or a question to the user. The
  parent reads the finding's node record, not the whole result, unless it chooses
  `detail="result"`.
- This does not change when the parent may add a verifier: the description of
  `get_subagent_context` still limits a re-checking worker to a check the user asked for
  or a risk that warrants one, and `risk` is where the parent states that risk.

### The parent's loop

A built-in flow, [`flows/orchestrate.md`](../flows/orchestrate.md), states the procedure.
One protocol sentence in the description of `get_subagent_context` points to it, and the
description of `validate_subagent_result` states the exception for bound packages:

1. Write the map with `expected_revision=0` and a `task_id` no other map of the workspace
   uses (a date or a random suffix): a goal node, stage nodes with their risk,
   `depends_on` edges.
2. Read `workmap_view(task_id)` for the `ready:` list. For each ready stage, prepare a
   bound package with its `workmap_inputs`, launch the worker in the background with the
   receipt's arguments, and keep working.
3. On each one-line hand-back, read the map with `since`; validate a full hand-back by
   `result_ref`, and record it with `workmap_submit` only while its node is still `prepared`.
4. For a node under `needs attention:`, a `not recorded` hand-back or a node left
   `prepared`, read its node record (result status, question, escalation, and the codes of
   the last invalid submission) and decide: a new attempt with the escalation's
   complexity, a verifier, acceptance where it settles the node, a re-plan or the
   worker's question to the user. The store's own refusal codes (`unknown_map`,
   `unknown_node`, `key_collision`, `store_full`, `unsafe_store`) can arrive here too: the
   parent retries `workmap_unavailable` once and reports the others.
5. For a node that needs V3, add and launch its verifier.
6. Accept the `validated`, `verified` or already `accepted` nodes that meet the task's
   need (a `critical` node still needs V3 or the user's V4); record a fact
   that outlives the task with `claim_record`, citing the node's `contract_revision` and
   `result_sha256`.
7. Report to the user from the view, and close the map.

By default the parent does not read results or evidence; `detail="result"` is a decision
it states.

### Rollback

A contract bound to a node carries a `workmap` field that the previous version's strict
contract model does not know, so after a rollback to it `validate_subagent_result`
answers `valid: false` for every contract this version bound, whoever submits. Unbound
contracts carry no such field and are unaffected. Settle or abandon the bound attempts
before rolling back; the maps under `data/workmap/` stay on disk and are read again once
this version returns.

## Cost

- **Tool list.** Every tool's description is loaded into every session. Reviewers
  measured the two existing worker tools at about 11,300 of 43,500 characters of that list
  (#329). The two new descriptions are held to about 1,500 characters each and measured
  with the context-size audit before merging.
- **Views.** A full view is up to 3,000 characters; reading it after every hand-back
  would cost what the results did. `since` keeps a routine re-read to the changed nodes.
- **Workers.** A worker now makes at least one more tool call. Its correction rounds move
  from the parent's context, where every later request pays for them again, to its own.

## Evaluation

- **Store and transitions.** Unit tests in the style of `tests/test_claims.py`: append,
  torn last line, refused links, size cap, concurrent writers, reserved statuses, cycle
  refusal, `revision_conflict`, `already_submitted`, the submission limit.
- **View.** Budget and truncation order, dependency order, derived `ready` and `stale`,
  `since` deltas.
- **Binding.** Preparation with `workmap_node` and `workmap_inputs`; a valid submission by
  a fake worker moves the node and makes its dependant ready; the verifier path to
  `verified` and to `disputed`; the `result_ref` fallback.
- **Measurement.** On a fixed task of four to six stages, the characters that enter the
  parent's context per stage (hand-backs plus tool results), with and without the map,
  over several runs; and how many correction rounds the workers absorbed. Report medians
  with the run count, since a single run of a model-driven task is noisy.
- **Real clients.** A worker submission end to end in Claude Code (the spike covered tool
  access) and in Codex, which only the owner can run.

## Order of work

Each step is useful on its own; all six land together in one pull request against `dev`.

1. **The package states the full `contract_revision`.** Small and independent: any worker
   can then validate its own draft, map or not.
2. **The store, `workmap_update` and `workmap_view`**, with `since`.
3. **Binding and submission:** `workmap_node`, `workmap_inputs`, recording on the node in
   `workmap_submit`, `already_submitted`, the launch text of a bound package.
4. **Verification by risk:** the verifier mapping and readiness levels.
5. **The `orchestrate` flow**, the protocol pointer and the reference documentation
   (`docs/mcp-tools.md`, `docs/subagent-contract.md`).
6. **The measurement.**

Relation to open work: the map does not wait for `agents-core/subagent/2` (#324 to #327)
and needs no change when it lands; the output budgets of #504 apply to submitted results
unchanged; #453's `result_ref` stays the fallback; #329's integration report is not needed
for this; #723's resource needs would become a node field later.

## Open questions

- Should `accepted` at V4 require the user's own words in the conversation, or is the
  parent's statement enough? The claims store's V4 means "the owner accepted it and it
  held".
- Retention: a closed map stays under the size cap until deleted by hand, or is pruned
  after a period like the exchange directory?
- Whether `workmap_view` should also serve the daemon's web UI as the source of a
  mind-map tab, which would make the graphical view a rendering of the same data.
