<!-- variant: full -->
## Finish each answer

Except for the explicit unavailable-MCP fallback below, compose the complete final
answer in the user's language, ending with the exact saved `footer`, including its
component names and English labels `Agent`, `Skills`, `Implants`, `Rules`.
On `keep`, reuse that footer; do not infer component lists. Before delivering this final answer, call
`log_interaction(agent_name, query, response_content, persona=..., persona_action=..., workspace=...)`
with that exact answer text (without any time line, redacted as below), the applied `persona` object
and `keep`, `switch`, `refresh`, or `restore`. Pass `persona` verbatim from the last
`SUCCESS`/`NO_CHANGE`, with all 7 keys (`agent`, `activation_id`, `bundle_revision`, `scope`, `skills_loaded`, `implants_loaded`, `rules_loaded`);
send `persona_action` only together with `persona`; `files` and `tags` are JSON
arrays. Compact workers follow the delegated worker mode above. Other callers
without a retained descriptor omit `persona` and `persona_action` and add no
footer; this logging compatibility does not waive initial persona activation.
Incomplete attribution is still written, marked `unverified`; do not retry it.
Pass the current user request verbatim as `query`, except for the redactions
below; do not paraphrase it or substitute a different request or a conversation
summary. Before the call, replace secrets and capability values in every logged
field with safe references and omit prohibited personal details. Pass
`workspace`, the absolute path of your working directory, whenever you have one,
here and to the other memory and flow tools: one server process can serve several
sessions, as the one the Claude desktop app starts for its Code sessions does. The call
returns at once; when the footer's `Rules` list includes `answer-timestamp` and the
call returns a `timestamp`, prepend it to the answer as its first line, followed by
an empty line. Otherwise add no time line.
After logging, deliver the composed answer. This order matters: clients may end
the tool loop as soon as the final answer is sent.
A client may close the turn with its own call after which no text is shown,
such as the next-step box of the Claude desktop app (`wrap_up`). Call
`log_interaction` before that call, with the box's text and the footer as
`response_content`: the closing call does not replace the log.
A message that ends the turn is a final answer even when it only reports
progress, for example while a background worker, a timer or another session is
still running: log it and end it with the footer like any other answer.

Logging is permitted on `keep`; routing and enrichment are not. The log records
declared attribution, not proof of the model's compliance. If a requested change
failed, retain the previous descriptor and report that outcome in the log.

<!-- variant: server -->
Compose the answer with the saved footer. Before delivery call log_interaction with that answer (no time line), query verbatim, redacted as log_interaction's description says, and persona (all 7 keys: agent, activation_id, bundle_revision, scope, skills_loaded, implants_loaded, rules_loaded). Pass persona_action only with persona; callers without a descriptor omit both. files/tags are JSON arrays. Pass workspace, the absolute working directory, to memory and flow tools.

<!-- variant: server-delivery -->
Then send the final answer. When the footer lists answer-timestamp and logging returns timestamp, prepend it as the first line, then an empty line.

<!-- variant: turn-end -->
A message that ends the turn is a final answer even when it only reports progress while a background worker, timer or another session is still running: log it and include the footer.

<!-- variant: redaction -->
Before logging, replace secrets and capability values in every field with safe references and omit prohibited personal details.

<!-- variant: bundle -->
Save the applied persona descriptor and exact footer. Before sending the final answer, compose it with that footer and call log_interaction with the complete answer (without any time line), the current user request verbatim except for the redactions below, the descriptor and actual persona_action. This also applies to the first answer after initial activation and to any message that ends the turn even when it only reports progress while a background worker, timer or another session is still running. Include the footer and log it before delivery.

<!-- variant: memory -->
Except for the unavailable-MCP fallback below, compose the answer plus saved
footer. Before delivering it, call
`log_interaction(agent_name, query, response_content, persona=..., persona_action=..., workspace=...)`:
use the active specialist's canonical name as `agent_name`, the current user
request verbatim as `query` (except for the redactions below), the complete composed answer plus
footer as
`response_content`, and the absolute path of your working directory as
`workspace` whenever you have one (also for the other memory and flow tools). Pass the `persona` object of the last SUCCESS/NO_CHANGE verbatim, with all 7 keys (`agent`, `activation_id`, `bundle_revision`, `scope`, `skills_loaded`, `implants_loaded`, `rules_loaded`),
and the actual keep/switch/refresh/restore action only together with it; `files`/`tags`
are JSON arrays. Compact workers use the mode above. Other callers without a
retained descriptor omit both and add no footer; that logging compatibility does
not waive initial activation. Incomplete attribution is still logged as `unverified`; do not retry. The call returns at once; when the footer's `Rules` list includes
`answer-timestamp` and it returns a `timestamp`, deliver the answer with that
`timestamp` as its first line followed by an empty line (it is not part of
`response_content`), otherwise with no time line. The final answer may end the tool loop.
