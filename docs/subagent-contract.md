# Task-scoped subagent context and exchange contract

Status: implemented; mandatory delegation guidance updated 2026-10-11.
Tracking: [Issue #291](https://github.com/IEZhu/Agents/issues/291). This reference describes
the implemented worker API; the ordinary persona protocol remains version 2.

## Problem and goals

Agents-Core currently assembles a persona for an entire conversation. Delegated
workers need a smaller package for one assignment, explicit boundaries, and a
result their parent can validate without interpreting a free-form report.

Provide a dedicated context tool, authored compact versions of all shipped
personas, a task-specific component policy, worker-only rules, and a versioned
exchange contract with an actual validation tool. Preparing a worker must not
switch the parent's persona or update the session routing cache. Every artifact,
instruction, test description, issue and implementation document is English.

## Scope and architecture

The two worker MCP tools are:

- `get_subagent_context`: prepare instructions and an immutable task contract.
- `validate_subagent_result`: validate a result against the original contract
  retained by the parent.

These tools do not launch models, manage execution sessions, follow arbitrary
task references or artifact URLs, install instructions or restart the daemon.
When `artifact_manifest` is supplied, the MCP preparation endpoint verifies its
active workspace binding and all selected stored bytes before preparing the
package. It does not inline those bytes; the native worker reads them through
`artifact_read`. Without a manifest, preparation does not resolve artifact storage.
The pure builder and result validator perform no artifact I/O. See
[shared artifacts](shared-artifacts.md) for publication, required reads and the
parent's separate integrity check before acceptance.
The host supplies its native spawning, cancellation, permissions and execution
budget controls. The contract is inspired by A2A's task/message/artifact model;
this release does not implement an A2A network transport or claim wire compatibility.

Use a separate task-scoped builder and schemas. Reuse low-level fresh source
loading, component retrieval and hashing where appropriate; preserve session
persona loading, the seven-key `PersonaDescriptor`, and its `NO_CHANGE` behavior.
Use a separate worker descriptor/envelope instead of adding fields to that model.

## Preparation request

The public tool accepts these named arguments, with simple documented MCP input
types and internal strict validation. Arrays contain strings, not comma-separated
text. Preserve the repository's client-compatible schema conventions.

| Argument | Contract |
|---|---|
| `agent_name` | Required canonical existing persona name |
| `task` | Required nonempty assignment |
| `acceptance_criteria` | Required nonempty list of concrete completion criteria |
| `task_id` | Optional caller ID; generate one when omitted |
| `attempt_id` | Nonempty string, default `1`; caller changes it for a new attempt |
| `context` | Relevant facts and reference strings, default empty; never the whole parent transcript by default |
| `scope` | Explicit area/actions to work within, default empty |
| `non_goals` | Explicit excluded work, default empty |
| `result_type` | `generic`, `review`, `implementation`, or `research`; default `generic` |
| `artifact_manifest` | Optional complete active manifest (`data` from `artifact_manifest_create` or `artifact_manifest_get`); pins exact inputs and adds required read instructions. Requires a valid workspace; omit `task_id` to inherit the manifest task or supply the same ID. |
| `max_skills` | Default 3, range 0..5 |
| `max_implants` | Default 0, range 0..2 |
| `max_context_chars` | Default 16000, range 2000..64000; complete worker-context character budget |
| `complexity` | Optional `low`, `standard` or `high`; returns `execution` with the worker's model tier ([model tiers](subagent-model-tiers.md)) |
| `client` | Optional `claude`, `codex` or `cursor`, the parent's host; pass it with `complexity`, otherwise the server falls back to the `X-Agents-Client` header or, over stdio, MCP `clientInfo` |

Empty required text, unknown roles or types, invalid bounds and oversized input
return a structured error, never a partial usable bundle. Bound individual input
strings, list lengths and total request size; document the concrete bounds in the
API reference. Do not coerce booleans into integer budgets or objects into text.

## Prepared package

A successful response has `protocol: agents-core/subagent/1` and
`status: prepared`, plus:

- `task_id`, `attempt_id`, and a fresh `bundle_id` for tracing.
- A worker descriptor with canonical agent identity, component manifest and
  `bundle_revision` (SHA-256 of the delivered reusable instructions and schemas).
- The original task `contract`, including task text, context, scope, non-goals,
  acceptance criteria with stable generated IDs, result type and budgets.
  When supplied, it also retains the complete `artifact_manifest`; model-tier
  fields are retained as `execution` when `complexity` is supplied.
- `contract_revision`: SHA-256 of canonical JSON for the contract, including
  its protocol/version and task/attempt IDs. This is a consistency fingerprint,
  not authentication or a signature.
- One ready-to-forward `worker_context` containing each effective instruction
  block, assignment and result schema once. Avoid returning another full copy
  of each prompt block alongside this string.
- `result_schema`: machine-readable JSON Schema for the selected result type.
- Actual character count of the complete `worker_context`, selected/omitted
  component IDs and selection reasons. Do not call character estimates exact
  model tokens. Compact component counts are defaults to evaluate, not quality
  guarantees.

The model-facing context must clearly name the worker role, task/attempt IDs,
bundle revision and contract revision, the complete result shape, and the rules
for missing input, incomplete work and finishing. It directs the worker to use
this package without activating a full conversation persona. Parent descriptors
visible in inherited history are references, not the worker's activation.

Every AI delegation, including reviewers, verifiers and parallel workers, requires `get_subagent_context` and `status: prepared` before native spawning. The parent launches a fresh worker without inherited history when supported (Codex: `fork_turns="none"`) with the receipt's `launch` text and only necessary task materials (with inline delivery: the unchanged `worker_context`). It keeps the receipt's `contract_revision` (inline: the original contract) and its descriptor, and validates every returned or revised output unchanged against that contract, by reference where a file holds it ([compact exchange](#compact-exchange)). A worker must not replace the authoritative contract. `valid: true` is required before acceptance or integration, followed by independent evidence and acceptance-criteria checks.

## Compact exchange

The exchange is built so that the parent's context carries as little of it as possible
and the model never retypes it: JSON in a parent's context is paid again on every later
request, and a parent that copies a package or result into a tool call pays output tokens
for it and can alter it (#453).

**File delivery (default).** `get_subagent_context` writes `worker_context` to
`data/subagent-exchange/<contract_revision>.md` in the installation and the contract to
`<contract_revision>.contract.json` beside it (0700 directory, 0600 files, removed after
seven days; they hold task text and stay on this machine). It returns a receipt of under
2,000 characters instead of the package: `task_id`, `attempt_id`, `contract_revision`,
`context_path`, `context_chars`, `context_sha256`, `launch`, `descriptor` and, with
`complexity`, `execution`. The parent starts the worker with `launch` as its prompt (the
worker reads the package file itself), keeps `contract_revision`, and never holds the
package or the contract. `delivery="inline"` returns the full package as before, for a
worker that cannot read the server's files (another machine); the parent then retains
`contract` and forwards `worker_context` unchanged. The tool is therefore no longer
read-only: it writes these private files.

**Plain-text format (default).** With `result_format="text"` the package carries the
assignment as plain text (task, acceptance criteria with their IDs, context, scope,
non-goals, pinned artifacts, the first 12 characters of the contract revision) and a
filled-in result template instead of the JSON contract and JSON Schema. On the reference
fixture the package shrinks from 4,304 to 1,985 characters. The worker answers:

```text
task: review-42
attempt: 1
contract: 3f2a9c01b7de
status: completed

## summary
Checked the parser diff and its two callers.

## findings
- high src/parser.py:88 — An empty token list now raises IndexError.[^e1]
  fix: Return early when the list is empty.

## checks
- passed: Ran the parser tests.[^e2]

## evidence
[^e1]: src/parser.py:88 — tokens[0] without a length check
[^e2]: pytest tests/test_parser.py -q — 31 passed
```

The header lines are `task`, `attempt`, `contract` (at least 12 hex characters of the
revision), `status` and, for `input_required`, `question`. Sections are `summary`, the
payload (`items`, `conclusions`, `findings` or `changes`), `checks`, `evidence`,
`artifacts`, `limitations`, `remaining_work` and `question`; empty ones are omitted. A
list item starts with `- `, continuation lines are indented, two-part items separate
their parts with ` — `, a finding's recommendation follows on a `fix:` line, and `[^id]`
cites an evidence entry. `src/engine/subagent_text.py` parses this into the same closed
result model, filling the protocol, the full contract revision and the bundle revision
from the contract, so every structural rule below applies unchanged; a text result may
sit in one code fence. `result_format="json"` keeps the JSON contract, schema and
envelope for structured-output runners: the autodev launcher passes `result_schema` to
the CLI's `--json-schema`. The validator accepts both forms for any contract.

**Validation by reference.** `validate_subagent_result` takes the contract as
`contract_revision` (the stored copy, whose fingerprint is rechecked) or `contract`, and
the output as `result_ref` or `result`, exactly one of each. `result_ref` is an absolute
path the server reads: a result file, or the final assistant message of a Claude Code
agent transcript (`agent-<id>.jsonl` in a Workflow's transcript directory, a background
agent's `.output` file, including a `SubagentHandback`) or of a Codex `rollout-*.jsonl`.
A result file must lie under a temporary directory or the exchange directory; a transcript
(`.jsonl`, `.output`) may also lie in a `~/.claude*` or `~/.codex` home (`CLAUDE_CONFIG_DIR`,
`CODEX_HOME`), whose settings and credentials stay unreadable. Transcripts are read up to
64 MiB. These are files the parent could read itself; the server only parses them and never
echoes their content. A stored contract must carry the revision it is filed under. The response adds `result_sha256`, the SHA-256 of the exact
text (or canonical JSON of a result object) it checked.

## Model tiers

With `complexity`, preparation resolves a model tier for the launching client and
records it in the contract as `execution`, covered by `contract_revision`. The
package's `execution.spawn` holds the arguments for the host's launching tool
(`Agent`, `spawn_agent` or `Task`), and validation of such a contract returns
`escalation`, the next tier, when a result fell short. Without `complexity` the
package, contract and validation response keep the shape described here. Tiers,
floors, model resolution and the opt-in ledger are specified in
[Model tiers for delegated workers](subagent-model-tiers.md).

## Authored compact personas and component selection

Ship `agents/<name>/subagent.mdc` for every existing persona with a matching
`agent` identity in YAML frontmatter, ordered `skills` and `implants` candidate
IDs, and an authored English body of at most 250 words. The body retains the
specialization's operational judgment and critical domain boundaries, without
dialogue ceremony, compulsory broad workflows or user-facing response templates.
Do not create compact roles by truncating or generating summaries at request time.

Candidate skills and implants must be declared by the corresponding full persona.
Use the assignment to select relevant candidates, within the requested caps,
with deterministic tie-breaking. Respect installation component switches. Do not
reuse exact `ComponentSelection` as an authorization policy: that API deliberately
bypasses the ordinary allowlist and toggles for flow selections.

Skills use their authored `compiled` instructions; a title/description is not a
substitute. Missing required compact text is an explicit preparation error.
Default zero implants. For explicitly enabled implants, use fresh complete source
instructions within the package budget; never substitute a vague `one_liner` for
the operational method. The package is compact primarily by choosing fewer
components. Optional components may be omitted whole, lowest priority first,
with the omission recorded; never truncate any component mid-instruction.

Missing/invalid compact profiles fail explicitly rather than silently loading a
full persona. The mandatory role, common rules, worker rules, assignment and
result contract must fit the budget together. If they do not fit, return a
`context_budget_exceeded` error; never silently remove mandatory safeguards.

## Worker rules

Keep the installation-enabled non-presentation universal rules (including honesty
and uncertainty). Omit `answer-timestamp`, `language-match` and `thread-status`: they
govern conversation presentation and chat titles, rather than the task worker.
Language requirements can be stated in the assignment. The output is the data
contract, not a chat answer with a footer.
Universal `agent-cooperation` is also omitted, with selection reason
`worker_equivalent`; mandatory `worker-cooperation` supplies its scoped equivalent.
All five base worker rules remain mandatory when conversation rules are disabled.
An artifact-backed assignment also requires `worker-artifacts`, regardless of
component switches. Its manifest and complete read instructions count toward the
same context budget; preparation fails if they cannot fit. It tells the worker
to follow `next_offset` through `eof` for every required entry and return
`input_required` or `partial` if the worker cannot read input required for completion. See
[the artifact workflow](shared-artifacts.md#native-worker-workflow).

Add a dedicated `rules/subagents/` collection, loaded only by the worker builder;
do not add unsupported applicability metadata to existing universal rules.
The short worker rule set must cover:

1. Stay within the assignment and stated scope. Report relevant adjacent issues
   briefly without doing unrequested work.
2. Honor the requested operation, e.g. review/analysis does not authorize edits.
3. Keep the assigned specialization and component package. Do not spawn further
   workers or broaden the task unless the assignment explicitly allows it.
4. Return `input_required` with the concrete missing dependency when critical
   information is unavailable. Distinguish harmless assumptions from blockers.
5. Stop when completion criteria are met; return usable partial work and what
   remains if the allocated effort is exhausted. A prompt is not a runtime meter.
6. Return the exact result envelope with concise evidence and limitations; omit
   greetings, user-facing narration, timestamps, footers and the search transcript.
7. Never present suggested checks as executed. Preserve failed/not-run checks,
   uncertainty and incomplete work even when the result must be short.
8. Preserve intersecting work and report it to the parent through an authorized
   channel. With a usable coordinator, conflicting edits require a committed
   agreement and current claims. A sidecar never changes the prepared contract:
   changed assignments need a newly prepared attempt. Without an intermediate
   channel, use the existing partial/input_required envelope; add no result fields.

The optional [cooperation service](agent-cooperation/README.md) passes its
`agents-core/cooperation/1` sidecar as task material alongside unchanged
`worker_context`. The parent keeps the original contract and validates every
returned or revised result before integration. Neither the sidecar nor a peer
message grants permission or replaces that validation.

The managed routing template, its checked-in CLAUDE.md copy, memory reminder,
MCP initialization and APPLY_INSTRUCTION must explicitly recognize this worker
mode before the first-answer/full-persona rule. A valid worker package supplies
its role. The worker returns the data envelope without a second full activation
or per-answer conversation footer/log call; parent interaction logging remains
under the existing v2 contract. If preparation fails, either worker endpoint is unavailable, or the host cannot spawn a worker, report the limitation and do the work in the parent when practical. Do not launch an unprepared/full-persona worker or accept unvalidated output. Ordinary conversation fallback does not waive this requirement; explicitly authorized nested delegation follows the same exchange.

## Worker result

All result types share a closed envelope (unknown keys are rejected):

```json
{
  "protocol": "agents-core/subagent/1",
  "task_id": "review-42",
  "attempt_id": "1",
  "bundle_revision": "<64 lowercase hex characters>",
  "contract_revision": "<64 lowercase hex characters>",
  "status": "completed",
  "result": {"summary": "No actionable findings in the requested diff.", "findings": []},
  "evidence": [],
  "artifacts": [],
  "checks": [],
  "limitations": [],
  "remaining_work": [],
  "question": null
}
```

The sample demonstrates shape only; an empty evidence list is not proof of a
successful review. Concrete evidence items have stable IDs and nonempty reference
and description fields. Artifacts identify a path/URI and purpose. Checks identify
what was checked, `passed`/`failed`/`not_run`, and evidence references where present.
These are worker-reported assertions. Do not dereference them during validation.

Typed payloads:

- `generic`: nonempty `summary` and `items` (strings).
- `review`: nonempty `summary` and `findings` (severity, location, explanation,
  recommendation and evidence IDs). Severity is a fixed documented enum.
- `implementation`: nonempty `summary` and `changes` (path and description).
- `research`: nonempty `summary` and `conclusions` (claim and evidence IDs).

Use the same typed payload even for partial/error statuses; it can describe the
work completed or the failure. `completed` requires no `remaining_work` or
`question`; `input_required` requires a nonempty question; `partial` requires
nonempty remaining work; `failed` requires a nonempty limitation explaining the
failure. Unknown/dangling evidence IDs are rejected. A valid completed envelope
does not establish the truth of the completion claim.

## Result validation

`validate_subagent_result` accepts the contract as the receipt's `contract_revision` or the
original contract object from an inline package (including the expected bundle revision),
and the output as `result_ref`, the text result, the parsed result object or a raw JSON
string ([compact exchange](#compact-exchange)). Raw JSON must be exactly one JSON object;
markdown fences, duplicate keys, non-finite numbers, trailing prose and oversized payloads
are errors. A text result follows the text format strictly, apart from one optional
surrounding code fence. No silent repair or coercion into a valid result.

Validate protocol version, contract fingerprint, task and attempt identity,
expected bundle revision, the closed envelope, selected typed payload, status
invariants and evidence references. Bound nesting, arrays, strings, total payload
size and diagnostic output. No caller-provided executable schema or remote `$ref`
resolution in v1: JSON Schemas come only from the fixed built-in result models.

Return `valid: true/false`, a bounded list of errors with field paths and error
codes, and `verification: structural_only`. Validation is stateless and does not
write project memory or mutate persona state. It does not prove claims, rerun
tests, open artifacts, establish authorization or mark the issue complete. The
parent assesses evidence and acceptance criteria before integrating the work.

The MCP preparation tool's `outputSchema` describes the server-produced package.
It is distinct from `result_schema`, which describes the later worker output;
test both through the actual MCP registration/call layer.

## API limits and examples

Text values are nonblank and at most 4,000 characters. Assignment lists contain
at most 50 strings; acceptance criteria require at least one. Canonical agent
names use lowercase letters, digits and underscores, start with a letter or
digit, and are at most 80 characters. Task, attempt and evidence IDs are at most
128 characters: start with an ASCII letter or digit, then use letters, digits,
underscore, dot, colon or hyphen. Hashes are 64 lowercase hexadecimal characters.
The preparation request is limited to 48,000 canonical JSON characters, measured
on the original arguments before MCP parsing, including unknown fields. Both
worker tools reject unknown top-level arguments with bounded structured errors;
misspelled fields are never silently discarded. This check does not change other
tools' argument handling.

Results are limited to 128,000 raw/canonical JSON characters, 12 nesting levels,
10,000 JSON nodes and 100 keys per object. Result arrays hold at most 100 items;
the shared text lists (`items`, `limitations`, `remaining_work`) hold at most 50.
Object keys are at most 128 characters. Validation returns at most 20 diagnostics,
with paths up to 256 characters, codes up to 80 and messages up to 240.
The retained contract is limited to 64,000 canonical JSON characters.
Raw JSON strings are supported for `result`; `contract` must be an object.
These limits count Unicode characters, not tokens or UTF-8 bytes.

For a focused review, call `get_subagent_context` with these arguments. This
example uses Codex; set `client` to the actual launching host.

```json
{
  "agent_name": "code_reviewer",
  "task_id": "review-42",
  "attempt_id": "1",
  "task": "Review the supplied parser diff for correctness regressions.",
  "acceptance_criteria": [
    "Inspect changed parsing paths and their callers.",
    "Report actionable findings with evidence or explain the coverage limits."
  ],
  "scope": ["Read-only review of the supplied diff and relevant callers."],
  "non_goals": ["Do not edit files or undertake unrelated refactoring."],
  "context": ["The parent will attach the diff and relevant source files."],
  "result_type": "review",
  "complexity": "high",
  "client": "codex"
}
```

Keep the receipt's `contract_revision` and start a fresh native worker with its `launch`
text and the actual diff/source materials (inline delivery: retain the returned `contract`
verbatim and send `worker_context`). The MCP response's context count
covers `worker_context`; additional host instructions, attachments, tool results
and generated output are outside that count. Copy only the necessary context.

A text result names `task`, `attempt` and the contract revision prefix; a JSON envelope
copies `protocol`, `task_id`, `attempt_id`, `bundle_revision` and `contract_revision` from
the assignment contract. Review
findings use `severity` (`critical`, `high`, `medium`, `low`, `info`), `location`,
`explanation`, `recommendation` and `evidence_ids`. Evidence items use `id`,
`reference` and `description`; checks use `name`, `status` and `evidence_ids`;
artifacts use `path` and `purpose`. Findings and conclusions, as well as checks,
must reference supplied evidence IDs. An empty list is allowed and does not
establish the quality or completeness of the work.

Call `validate_subagent_result` with `contract_revision` and `result_ref` set to the
worker's transcript or result file (or `result` set to its unmodified output; `contract`
for an inline package). The following is the success shape, not a claim that the worker's
conclusions are true:

```json
{"valid": true, "errors": [], "verification": "structural_only", "result_sha256": "<64 hex>"}
```

On `valid: false`, inspect `errors[].path`, `code` and `message`, return concrete diagnostics for correction, and validate the revised output again. A new execution attempt requires a new package with a different `attempt_id` and its matching receipt or retained contract. Never rewrite identities or the parent contract to make an invalid result pass. A missing, failed or unavailable validation is not success. Require `valid: true`, then independently verify evidence and acceptance criteria before accepting, integrating, citing as verified, or marking the delegated work complete. Validation opens no path or URI except `result_ref`, and never one named inside a result.

Component selection uses lexical overlap between task/criteria/scope/context
and each eligible component's ID, description and keywords, with profile order
breaking ties. This deterministic ranking needs no embedding model or router
cache; it is not a semantic relevance guarantee. Only authored profile candidates
belonging to the full role are eligible. Selected skills deliver their `compiled`
text; selected implants deliver complete source instructions.

Preparation errors have `status: error` and bounded `errors`, with no usable
partial bundle. Examples include `unknown_agent`, `compact_profile_missing`,
`invalid_profile`, `component_not_allowed`, `missing_component_text`,
`worker_rules_missing`, `context_budget_exceeded` and `invalid_source`, plus
strict field-validation diagnostics. Invalid JSON or result identity failures
instead return `valid: false` from the validation tool.
Artifact-backed preparation can also fail with `worker_artifact_rule_missing`,
workspace errors, `artifact_unavailable` or `integrity_error`; it returns no
usable partial package. A later release or expiry does not make an otherwise
matching result envelope structurally invalid, so the parent's separate
`artifact_manifest_verify` remains required.

See the [recorded validation and size comparison](subagent-validation-results.md)
for measured coverage and limits. Compact profiles do not guarantee a smaller
complete package for every role; model quality and total token cost need a
separate evaluation.

## Acceptance criteria

- Both new tools are discoverable with useful parameter types/descriptions and
  structured output; errors do not expose a partial prepared package.
- Every shipped persona has a valid authored compact profile. Declared component
  references exist, belong to that role, and the 250-word limit is checked.
- Default worker packages contain no implants and at most three relevant skills;
  explicit caps, disabled components and whole-package budgets are honored.
- Preparation is isolated from parent activation, v2 caches and shared runtime state.
- Revisions change when applicable profile, component, rule, schema or contract
  content changes; replayed results for another task/attempt/contract are rejected.
- Positive and negative tests cover every payload and status, malformed JSON,
  extra fields, wrong identifiers, duplicate keys, evidence references, bounds,
  budget errors and disabled/missing components through pure and MCP APIs.
- Worker instruction distribution agrees across templates, server instructions
  and activation guidance; previous routing reminders remain migratable.
- README/API/agent/rules/test documentation and the documentation map are updated.
- Record measured package sizes versus ordinary persona bundles for representative
  roles. Do not claim lower total token use or equal task quality without a model
  evaluation; define a follow-up evaluation measuring quality, scope adherence,
  schema validity and total tokens including retries.

## References

- [A2A task, message and artifact model](https://a2a-protocol.org/latest/specification/)
- [MCP tool structured content and output schemas](https://modelcontextprotocol.io/specification/2025-11-25/server/tools)
- [Anthropic: multi-agent delegation](https://www.anthropic.com/engineering/multi-agent-research-system)
- [Anthropic: effective context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
