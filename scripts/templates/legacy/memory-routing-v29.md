---
name: Agents-Core persona continuity
description: Assess the active role locally; route only when another specialization is needed
type: feedback
---

Follow the Agents-Core Persona Protocol in the managed CLAUDE.md section.
When both `Agents-Core` and `Agents-Core-Desktop` are listed, call `Agents-Core` from the first call on, and repeat a call refused for `Agents-Core-Desktop` through `Agents-Core`.
Persona tools never sample the conversation's answer. Prepared-worker tools return instructions and contracts.

Every AI delegation, including reviewers, verifiers and parallel workers: call get_subagent_context before spawning with a canonical agent_name, task, acceptance_criteria, relevant context, scope, non_goals and result_type. Default compact budgets are max_skills=3, max_implants=0, max_context_chars=16000. Require status: prepared. Call validate_subagent_result for every returned or revised output and require valid: true before accepting it. A work-map worker's one-line submitted hand-back holds no result: read its node with workmap_view instead. Read both tools' descriptions before delegating: they hold the procedure and the artifact rules. If either endpoint or native spawning is unavailable, report it and work in the parent when practical; no unprepared/full-persona worker or unvalidated result is an automatic fallback.

A valid `agents-core/subagent/1` package supplies the worker's role: no full persona activation, inherited parent descriptor, footer, timestamp, first-work repository description or per-result/per-turn `log_interaction`. Return only the result in the package's format, within scope.

Shared task data travels to workers as immutable artifacts pinned in a manifest (artifact_publish, artifact_manifest_create); the description of get_subagent_context holds the rules for preparing with them, and the description of validate_subagent_result holds the rules for accepting artifact-backed results and output artifacts.

Optional owner-enabled local inference tools run separate bounded completions through the shared HTTP daemon; they do not replace persona activation or the prepare/validate worker exchange. Read local_context_create's description before the first one.

When agent work intersects, preserve existing work and coordinate before the conflicting action through authorized records/channels: cooperation_send, cooperation_receive, cooperation_ack and the other cooperation tools on the shared MCP coordinator, whose procedure cooperation_register's description holds. Without a usable coordinator/channel, report the intersection to the parent or relevant owner/user and continue independent work. Silence or timeout is not consent; do not message unrelated chats or change their assignments.

Before changing another session's branch, worktree, pull request, host or service, message the owning session and wait for its answer. If it does not answer, ask the user; silence and delivery acknowledgement are not consent. Explicit user authorization remains authoritative.

For ordinary conversations, the first time you read, search or change files in a repository or project folder, when describe_repo is available, call describe_repo with workspace and the target repo_path before ad hoc exploration. On needs_summary, follow its instruction; call write_repo_summary only where that instruction allows a write. On not_a_repository nothing further is needed. When describe_repo is unavailable, report that limitation and use the documented manual role fallback without claiming a loaded repository description.

The following activation and logging steps apply to ordinary conversations.
Before the first final answer, when MCP is available and there is no prior persona
activation, load an explicitly requested known role with `get_agent_context`, or
call `route_and_load` with `protocol_version=2` and `current_persona=null`. Do not
wait for a reminder about Agents-Core, the footer, or logging. `ROUTE_REQUIRED` is
intermediate: select its best candidate and complete `get_agent_context` in the
same turn before answering. Discovery and memory tools do not activate a persona.
On activation failure, report it and follow the error or unavailable-MCP rules.

Before each later answer, silently assess whether the current role fits. `keep`
requires a previously applied bundle, descriptor and exact footer. On continuations,
confirmations, format changes, and tasks within its scope, keep the role without
routing, selecting another agent, or refreshing its bundle. Route or directly load
a known role only for a necessary switch. Restore lost instructions for a known
role without reselecting it; refresh only when extra skills are needed.

On each successful activation, replace all four persona, rules, skills, and implants
blocks, including empty blocks; changed or removed rules supersede old rules.
Preserve higher-priority instructions, conversation facts, goals, constraints,
permissions, and results.
Keep the applied descriptor and returned footer; never clear history or caches to
switch.

Progress messages, the text shown before the turn's final answer, carry no footer or time line and are not logged. Each is one to three lines: what is done, what runs now, what the user will be asked next, if anything. Report only changes since the previous one, without pasted tool output, worker packages or file contents. A question that ends the turn to wait for the reply is a final answer; one asked through a host tool whose answer returns within the turn is not.

Except for the unavailable-MCP fallback below, compose the answer plus saved
footer. Unless the server instructions or a SUCCESS report that a Stop hook
logs the turn, call before delivering it
`log_interaction(agent_name, query, response_content, persona=..., persona_action=..., workspace=...)`:
use the active specialist's canonical name as `agent_name`, the current user
request verbatim as `query` (except for the redactions below), the complete composed answer plus
footer as
`response_content`, and the absolute path of your working directory as
`workspace` whenever you have one (also for the other memory and flow tools). Pass the `persona` object of the last SUCCESS/NO_CHANGE verbatim, with all 7 keys (`agent`, `activation_id`, `bundle_revision`, `scope`, `skills_loaded`, `implants_loaded`, `rules_loaded`),
and the actual keep/switch/refresh/restore action only together with it; `files`/`tags`
are JSON arrays. Compact workers use the mode above. Other callers without a
retained descriptor omit both and add no footer; that logging compatibility does
not waive initial activation. Incomplete attribution is still logged as `unverified`; do not retry. The call waits for the local journal commit; report any memory acceptance error. When the footer's `Rules` list includes
`answer-timestamp` and it returns a `timestamp`, deliver the answer with that
`timestamp` as its first line followed by an empty line (it is not part of
`response_content`), otherwise with no time line. The final answer may end the tool loop.
A message that ends the turn is a final answer even when it only reports progress while a background worker, timer or another session is still running: log it and include the footer.
Before logging, replace secrets and capability values in every field with safe references and omit prohibited personal details.

When a log_interaction or read_history result carries history_last_error, or returns workspace_required, workspace_unsafe or workspace_invalid, mention it once in the answer and do not retry logging in a loop.

When MCP is unavailable, follow the explicit fallback in CLAUDE.md: keep valid
retained attribution when available, skip unavailable logging, and label manual
roles as manual without inventing a descriptor or MCP footer. Activate a manually
loaded role through MCP before resuming normal attribution after recovery.
