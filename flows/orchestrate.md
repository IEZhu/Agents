# Orchestrate staged work on a work map

Use this flow when a task splits into stages that workers can do, and the parent should
keep a top-down view instead of the workers' results. The parent holds the map; the work
stays with the workers, the server and, where the risk warrants one, a verifier.

## Inputs

- The user's task and its boundaries.
- A `task_id` for the map that no other map in the workspace uses, such as a name with the
  date or a random suffix, reused by every package of the task.

## Procedure

1. **Write the map.** One `workmap_update` call with `expected_revision=0`, which creates
   the map and is refused with `revision_conflict` when a map with that `task_id` already
   exists: then choose another `task_id`. The call holds a `goal` node, one `stage` node
   per part that genuinely needs its own worker, a `risk` for each (`critical` where a
   wrong result would be costly or hard to notice), and `depends_on` edges between stages.
   Decide the fan-out as the description of `get_subagent_context` says.
2. **Launch the ready stages.** Read `workmap_view(task_id)` for its `ready:` list. For
   each node in it, call `get_subagent_context` with `task_id`, `workmap_node` and, when
   the stage builds on earlier ones, `workmap_inputs`; rate `complexity`. Launch the worker
   in the background with the receipt's `launch` text and `execution.spawn` arguments, and
   keep working.
3. **Read the node, not the result.** A bound worker submits its result with
   `workmap_submit` and hands back one line: `submitted <task>/<node> <sha> <status>`, or
   `not recorded <task>/<node> <code>` when the map refused a valid result for good
   (step 4). Neither validate nor submit that line: it carries no result, so it only
   answers `valid: false`, and `workmap_submit` would count it as an invalid submission
   while the node still takes one. Call `workmap_view(task_id, since=<the last revision
   you read>)` and trust the node, never the line. A worker that could not reach the
   server, or whose submission the map refused twice with a code that is not final
   (step 4), hands back its full result instead: read its node first, and validate the
   result by `result_ref`; while the node is still `prepared`, `workmap_submit` with the
   same `result_ref` validates it the same way and records it with `hand-back: full`. A
   node that has moved on already holds what the worker submitted.
   `validate_subagent_result` records nothing.
4. **Handle what needs you.** The view's `needs attention:` list names nodes in
   `needs_attention` or `disputed`, and every `validated`, `verified` or `accepted` node
   below the level its dependants need (V1, or V2 where the node needs V3). Handle as well a node that stayed
   `prepared` after its worker finished and every `not recorded` hand-back. Read
   `workmap_view(task_id, node=<id>)`: besides the counts, the record shows the worker's
   `result status`, its `question`, the escalation as
   `escalation: complexity <c>, reason <r>, tier <t>`, and
   `invalid submissions: N (codes ...)`, the codes of the last invalid submission only.
   Then decide: a new attempt (a new `attempt_id`, passing the escalation's complexity
   as `complexity` when there is one), acceptance of a below-level node whose result
   suffices (step 6), a verifier (step 5), a re-plan with `workmap_update`, or the
   worker's question to the user.
   - **A below-level `validated` node is not preparable.** Its way to a new attempt is
     to set it back with `workmap_update` (`status="planned"`, which the map takes from
     `validated`), then prepare it again; it keeps its old level and result until then.
     Or verify it (step 5), or accept it when it suffices (step 6; a `routine` or
     `load_bearing` node only).
   - **`not recorded`.** The result was valid, but the map will never take it for that
     contract: `already_submitted` (a different result is already recorded for it),
     `stale_contract` (the node was prepared again since) or `node_not_open` (the map is
     closed or the node was settled or set aside). The worker does not resubmit, and a
     new result needs a new attempt.
   - **A full result after a refusal.** Any other refusal, `workmap_unavailable` (the map
     could not be read or written) or the store's own `store_full`, `unsafe_store`,
     `unknown_map`, `key_collision` or `unknown_node`, makes the worker submit once more
     and, refused again, hand back its full result, so the result is not lost: validate it
     by `result_ref` (step 3). After `workmap_unavailable`, submit it once with
     `workmap_submit` by that `result_ref`. Report the others to the user: a wrong
     `task_id` or node, a full store and an unsafe directory are not the parent's to
     repair. `get_subagent_context` answers the same codes as errors; retry its
     `workmap_unavailable` once.
   - **Still `prepared`.** The worker submitted nothing the map took. A worker whose call
     cannot reach the server waits about a minute, tries once more and then hands back its
     full result (step 3); one without the tool hands it back at once. A `submitted` line
     that the node does not confirm is not a submission.
   - **A new attempt needs a new `attempt_id`.** An unchanged request reproduces a
     contract that already has a result or reached five invalid submissions, and is
     refused with `attempt_reused`.
5. **Verify by risk.** A node needs V3 (`needs V3` in its record) when it is `critical` or
   was ever prepared with `complexity=high`. Add a verifier node with a `verifies` edge to
   it; the verifier is ready once each target is `validated`, `verified`, `accepted` or
   `disputed` and holds a result. Prepare it as a review package,
   `result_type="review"` (any other result type is refused with `invalid_request`), bound
   to the verifier with `workmap_inputs=[<target>]`, which pins the result it checks. Only
   a review result the server marks `validated` (completed, no failed check) gives a
   verdict, and only to a target that is still `validated`, `verified` or `disputed` and
   holds the result the verifier was given: without a `critical` or `high` finding the
   target becomes `verified` at V3; with one it becomes `disputed`, which is your decision,
   not a refutation. A partial, failed or input_required verifier result, a failed check,
   or a target re-attempted or accepted since gives no verdict; the verifier's own node
   still records its result.
6. **Accept.** Mark nodes `accepted` with `workmap_update` when they meet the task's need;
   only a `validated`, `verified` or already `accepted` node can be accepted
   (`invalid_transition` otherwise), and accepting an `accepted` node again with level
   `V4` is how a later acceptance by the user is recorded. Acceptance below the level a
   node needs is for a `routine` or `load_bearing` node only: a `critical` one is verified
   first (V3), or accepted with level `V4` when the user accepted it, and accepting it below
   V3 otherwise is refused, because an accepted node takes no verdict. Use level `V4` only
   when the user accepted the node.
   V2 is the worker's own report of a passed check, which the server does not rerun.
   Record a fact that outlives the task with `claim_record`, citing the node's
   `contract_revision` and `result_sha256`.
7. **Report and close.** Report to the user from the view, then `close` the map.

## Rules

- Read a stored result (`workmap_view` with `detail="result"`) only for a reason you state
  to the user; it is the one call that brings a stage's work into your context.
- Report how many workers ran and what each was for.
- Never set a server-owned status; `workmap_update` refuses it.
