# Proposed MCP contract

All names and schemas below are new API requirements. None is a claim that these tools exist on the base branch. Public result schema version: `agents-core/local-inference/1`.

## Common rules

Every tool receives MCP Context internally and resolves the existing HTTP ClientContext. Caller JSON cannot select an engine URL, arbitrary filesystem root, model path, engine flags, Redis credentials or snapshot filename. Status may run without a registered workspace and returns only global enabled/readiness/capability fields; all other operations require a valid workspace.

Use strict Pydantic validation with forbidden extra fields, finite numbers and bounded strings/arrays. A malformed JSON/type request is an MCP validation error. A well-formed operational refusal returns the error envelope below. Do not print stack traces or secrets to clients.

Success envelope:

```json
{
  "schema": "agents-core/local-inference/1",
  "status": "ok",
  "data": {}
}
```

Operational error:

```json
{
  "schema": "agents-core/local-inference/1",
  "status": "error",
  "error": {
    "code": "budget_exhausted",
    "message": "The context has no model requests remaining.",
    "retryable": false,
    "generation_dispatched": false
  }
}
```

Errors include `disabled`, `configuration_invalid`, `transport_unsupported`, `workspace_required`, `workspace_invalid`, `context_unavailable` (unknown, expired, released or wrong-workspace), `permission_denied`, `source_changed`, `input_too_large`, `context_overflow`, `budget_exhausted`, `request_conflict`, `job_limit_reached`, `busy`, `deadline_exceeded`, `cancelled`, `backend_unavailable`, `backend_protocol_error` and `unsupported`. Missing/incorrect capabilities reveal no context contents. `retryable` is advice for a later new request, never permission for automatic replay of a dispatched call.

## Tools

### local_inference_status

Arguments: none.

Return enabled, backend state (`disabled/not_started/starting/ready/restarting/unavailable/stopping`), configured model label, stable profile hash if already known, capabilities (`unknown/supported/unsupported` separately for resident and snapshot), current active/queued count and configured hard limits. Do not return host paths, prompt contents, context lists, token secrets or Redis URL. This tool performs no model start or network call.

Annotation: read-only, idempotent, closed-world.

The limits include configured `context_tokens`, validated `context_headroom` and `effective_context_tokens`; the latter two are null until the backend profile is resolved. The pinned native build reserves one position because reaching its exact capacity sets `truncated=true`.

### local_context_create

Required: `common_system: str`, `shared_context: str`.

Optional:
- `source_paths: list[str] = []`, at most 32 regular UTF-8 files relative to an owner-authorized source root.
- `source_root_id: str | null = null`; required when source_paths is nonempty, must name an owner-configured root bound to this HTTP workspace. No raw path override.
- `ttl_seconds: int = 900`, range 30..3600, further limited by owner configuration.
- `max_requests: int = 10`, range 1..100.
- `max_total_output_tokens: int = 4096`, range 1..65536.

Maximum source file is 128 KiB; total UTF-8 bytes of instructions, shared text and captured source data is 256 KiB by default. All limits are capped by owner configuration. Empty common_system is allowed; shared_context plus captured files must contain data. Reject raw surrogates with a structured input error.

Return context_id, owner_token, owner access_token, expires_at (UTC), requested limits and captured canonical source root and path/hash list. The selected root must be explicit because repository memory can normalize a worktree to its main checkout. Owner and access tokens have different purposes. The owner access grant receives the same initial budgets and expiry as its context and cannot create child grants without the separate owner token. Creation is CPU/filesystem work only; token/context-size validation may wait until the configured model starts on first generate. Context state after creation is `registered`, not `warm`.

A model capability is a bearer secret. Return tokens once and keep them out of global logs. Do not put them inside the model prompt. A caller that loses one must create a replacement context/grant.

Annotation: not read-only; not destructive; not idempotent.

### local_context_grant

Required: context_id, owner_token, `label: str` (1..80 characters), `max_requests: int` (1..100), `max_total_output_tokens: int` (1..65536).

Optional: ttl_seconds, default remaining context lifetime, cannot exceed it.

Return grant_id, access_token and effective limits. Grant caps cannot exceed current context limits, but the sum of grant caps need not be reserved; shared remaining context budget is authoritative at dispatch. No child may regrant. The label is diagnostic, not identity.

Annotation: not read-only; not destructive; not idempotent.

### local_context_revoke

Required: context_id, owner_token, grant_id.

Revoke that grant and reject its queued jobs at dispatch. For an active job, set cancellation, fence/terminate the owned engine as specified by the architecture, and immediately suppress publication under that grant. Return revocation success with cleanup_state (`done` or `cancelling`); engine fencing may continue asynchronously. Never admit another generation until fencing completes. Do not reclaim consumed call credits. Repeating a revoke of a known already revoked grant is successful. Reject attempts to revoke the owner access grant; release the context instead.

Annotation: not read-only; destructive; idempotent.

### local_generate (submit)

Required: context_id, access_token, request_id (canonical UUID), task (nonempty UTF-8 text).

Optional:
- max_output_tokens: default 512, range 1..2048 and owner/grant/context ceilings.
- timeout_seconds: default 120, range 1..600 and owner ceiling.
- cache_mode: `auto` (default) or `off`.
- temperature: default 0.0, finite 0..2.
- seed: default 42, integer 0..2147483647.

Do not expose arbitrary engine arguments. A version 1 submission schedules one textual completion and returns promptly without waiting for generation. It cannot spawn agents, execute returned tools or apply code. Raw prepared worker_context may be included unchanged in task text by the orchestrating client; the service does not replace the existing prepare/validate protocol or declare a worker result valid.

`cache_mode=off` bypasses Redis lookup/store and dispatches with native reuse disabled. It invalidates the broker's preceding resident reuse assumption. The native engine may still hold bytes internally after a call; the next auto call must be classified according to the service state, never pretend it was warmed by an off control.

Submission returns job_id, request_id, context_id, state (`queued/running/succeeded/failed/cancelling/cancelled`), deadline_at and deduplicated. The deadline is capped by context and submitting-grant expiry. No output is returned by submission, even if an existing job has finished. Duplicate request_id and identical arguments from the same submitting access grant returns the same job_id. A different grant cannot take ownership of an existing request ID; return permission_denied without exposing its job. Canonical request hashing excludes secret token bytes and includes the submitting grant ID and effective generation arguments. Every accepted ID counts toward max_context_jobs, even if cancelled before dispatch; exhausted admission returns job_limit_reached. The caller obtains the terminal payload through local_generation_result.

Terminal generation result data includes:
- request_id and context_id;
- output_text, finish_reason (`stop/length`) and model label;
- input_tokens, evaluated_input_tokens, reused_input_tokens, output_tokens;
- cache: requested mode, actual state, stable profile hash, epoch, compatibility status;
- timings_ms: total, queue, startup, render_tokenize, source_check, snapshot_lookup, snapshot_load, snapshot_restore, model, first_token, snapshot_save, snapshot_store;
- remaining context/grant budgets;
- optional nonfatal cache_warning code.

All durations are nonnegative monotonic measurements. The total is measured from accepted job submission through terminal job completion, including queue and awaited snapshot work. Polling intervals and time before the client retrieves a completed result are measured separately by the benchmark as client-observed end-to-end latency. Component times must define whether they overlap; version 1 uses sequential spans, and first_token is an event offset within total, not an additive span. `first_token` is null when no output token was observed. Keep all timing fields present, zero when inapplicable.

Example request (tokens are illustrative placeholders, not working credentials):

```json
{
  "context_id": "ctx_example",
  "access_token": "example-only",
  "request_id": "c02e5e30-f039-49d7-8045-38f6e94cefbb",
  "task": "Review the captured parser for Unicode error handling. Return JSON findings.",
  "max_output_tokens": 512,
  "timeout_seconds": 120,
  "cache_mode": "auto",
  "temperature": 0,
  "seed": 42
}
```

Response invariant: `reused_input_tokens + evaluated_input_tokens == input_tokens`; no silent truncation; native reuse does not exceed verified longest common prefix. Missing/inconsistent counters are `backend_protocol_error`, not zero and not an invented hit. Partial/invalid responses are not returned as successful completions. A length finish remains a successful bounded completion, but any downstream JSON validation may reject it.

Annotation: not read-only, not idempotent in general, not destructive to repository files; duplicate request_id semantics still prevent repeated dispatch.

### local_generation_result

Required: context_id, capability_token, job_id. Optional: wait_seconds, default 0, integer 0..20.

Only the access capability that submitted a job or its context owner capability may inspect it. It must be bound to the same workspace and context. Return state and a result field: null until terminal, the generation success/error envelope afterward. A failed/cancelled state includes its recorded error and consumed-budget information. Repeated polling returns the same retained terminal data. A revoked capability cannot read further output. Poll waits only on the job event; it never reruns generation or extends the deadline. An unknown job is context_unavailable without disclosing another context's existence.

Annotation: read-only, idempotent, closed-world. Polling changes no job state.

### local_generation_cancel

Required: context_id, capability_token, job_id. Same authorization as result.

Set the job cancellation flag and return state (`cancelled` for queued, `cancelling` while fencing active work, or the unchanged terminal state). Repeating cancellation is safe. Active cancellation becomes terminal only after the engine is fenced/reaped; a late output is discarded. No model retry follows. Poll result to observe completion. No refund after dispatch; queued cancellation consumes no request/output credit.

Annotation: not read-only, destructive, idempotent.

### local_context_release

Required: context_id, owner_token.

Invalidate all grants, cancel queued/active requests according to the same fencing rules, erase matching resident state when safe, forget canonical text and request results, and request deletion of this context's Redis manifest/chunks. Return cleanup_state (`done` or `cancelling`) immediately after invalidation; asynchronous engine fencing must finish before the next dispatch. Release never flushes Redis globally. A valid repeated release is successful during a bounded tombstone retention interval (same configured maximum context TTL, additionally bounded by max_release_tombstones); after broker restart or tombstone expiry use `context_unavailable`.

Annotation: not read-only; destructive; idempotent.

## Minimal parent and children interaction

1. Parent creates a context containing shared project instructions and captured source files.
2. Parent receives a prepared worker package using the existing Agents-Core tool.
3. Parent creates a grant for the implementation child and passes that capability only to the trusted host/client, outside worker_context text.
4. The client submits local_generate with the unchanged worker_context plus task material, then polls local_generation_result (wait_seconds up to 20).
5. Parent validates the raw JSON envelope with the existing validate_subagent_result tool and retained original contract.
6. A reviewer receives a different prepared package and grant; its call may reuse the shared prefix.
7. Parent validates reviewer output and applies its own checks before any code adoption.
8. Parent releases the context when done.

The service accelerates model calls. It does not authenticate the factual claims inside a model answer or replace code tests.

## Proposed Python entry point

Both MCP wrappers and benchmarks call the same LocalInferenceService methods with an explicit validated ClientContext. This is the SDK baseline; avoid a second cache implementation. A benchmark can select `cache_mode=off/auto` and optional Redis without changing prompts, policies, task budget or checker.
