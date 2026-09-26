**Short answer: you're mostly right.** Transient tool errors should be retried where they happen: inside the SQL tool's execution code in the sub-agent. Your teammate still has a point, though. The orchestrator needs a coarse fallback of its own, and it can only do that job if sub-agents report failures in a structured way.

## Why tool-level retry is the main fix

| Concern | Retry in sub-agent tool layer | Retry in orchestrator |
|---|---|---|
| Can tell transient from permanent errors | Yes. It sees the exception type (timeout, deadlock, connection reset) | No. It only sees final text, so it would be guessing from prose |
| Cost of a retry | One SQL query | A whole sub-agent run, or the whole fan-out, plus the ~2 min of LLM calls |
| Latency | Seconds of backoff | Minutes |
| Blast radius | One tool call | Every sub-agent whose work gets thrown away |

Retrying from the orchestrator means spending a lot of work to recover from an error it can't even see. Handling the error at the layer that has the error information is the right design.

## How to do it

**1. Wrap the SQL tool's executor in a deterministic retry.** Do it in code, not through the LLM.
- Retry only the transient classes: timeouts, connection resets, deadlock and serialization failures. Don't retry syntax errors, permission errors, or missing tables.
- Use exponential backoff with jitter, about 3 attempts, and cap the total time.
- **Idempotency:** retrying `SELECT` is safe. Retrying writes is safe only if they're idempotent or run in a transaction that rolled back. If the SQL agent can write, gate retries on statement type.

**2. When retries run out, return the error to the model instead of raising it.** Send it back as a tool result with an error flag (`is_error: true` on the `tool_result` block in the Anthropic Messages API, as I recall; confirm in the docs). The sub-agent can then change its query, try something else, or report the failure cleanly. From your description, the whole run dies because an uncaught exception from the tool propagates up through the sub-agent and the orchestrator. That is the real bug. Retries only make it rarer.

**3. Give sub-agents a structured return value, not just free text.** For example:
```json
{"status": "ok | failed", "error_kind": "transient | permanent | null", "result": "..."}
```
With that, the orchestrator can make a real decision. It can retry one sub-agent on a `transient` failure, degrade gracefully on a `permanent` one, or continue with partial results.

**4. Keep completed work (checkpoint).** Store each sub-agent's output as soon as it finishes. If the orchestrator retries, it re-runs only the failed sub-agent, not all four. This is what actually stops you losing the ~2 min.

**5. Keep the LLM API retry layer separate.** The SDK's built-in retry for 429/5xx on the model calls themselves (`max_retries` on the client, recalled, not verified) covers a different failure mode. Don't count it as your DB retry.

## Resolution for the debate

- **Tool layer:** handles transient errors with fast, cheap, typed retries. This is your position.
- **Sub-agent:** turns exhausted retries into a structured failure instead of crashing.
- **Orchestrator:** retries or degrades per sub-agent based on that structured status, using checkpointed results. This is your teammate's position, but it only works once steps 2–4 exist.

**Check before you build this:** confirm the crash really is an unhandled exception propagating up the stack. Look at the traceback of a failed run and see whether the DB timeout exception reaches the orchestrator's top-level frame. If the run fails some other way (say, the orchestrator treats "I couldn't query the DB" text as fatal), step 3 matters more than step 1.
