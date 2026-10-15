# Agent cooperation

Status: **implemented in source, disabled by default; not installed by this task**. The implementation was cut from `origin/dev` at `d7e34eb10cb2ab9f593ebf782a4538f677484493`; all 19 original source fingerprints matched before editing. See [validation results](validation-results.md) for actual checks and remaining rollout limits.

The optional local coordinator lets workers of one parent and independent sessions discover overlapping work, negotiate a bounded agreement, order artifact dependencies, and exchange messages through a durable MCP mailbox. Different AI clients and model providers can use the same protocol when they connect to the same enabled installed daemon, directly over HTTP or through its existing stdio bridge. It serves one trusted local OS user, one installation and one machine. Linked worktrees share their Git common-directory domain; independent clones and submodules do not.

The service reserves metadata and stores addressed messages for authenticated recipients to poll. It does not lock files against arbitrary shell writes, launch or wake workers, call model providers, run tests, apply patches, merge, or expand a participant's permissions. Native host messaging is not required for the mailbox. Independent sessions should continue using separate worktrees. Parents retain and validate the original `agents-core/subagent/1` contracts; persona protocol 2 and existing worker result shapes are unchanged.

## Runtime entry points

| Interface | Purpose |
|---|---|
| `cooperation_register` | Bind a fresh task attempt, verified physical checkout, bounded scope and optional parent; return a private capability once |
| `cooperation_read` | Read a consistent repository snapshot, queue, ownership, agreements and event cursor |
| `cooperation_apply` | Commit one authenticated typed action with revision/generation checks and an idempotency receipt |
| `cooperation_send` | Persist an authenticated message addressed to a participant in the same repository |
| `cooperation_receive` | Poll only the authenticated participant's inbox, without consuming messages |
| `cooperation_ack` | Record receipt of selected inbox messages; does not acknowledge agreement terms or accept artifacts |
| `agents-core://cooperation/schema` | Read-only MCP resource containing the exact closed request, action, event and response JSON Schemas |

Every tool accepts `request` (an object or raw JSON string) and optional `workspace`. Read the schema resource before constructing actions; [schemas.json](schemas.json) is its reproducible source export. Raw JSON rejects duplicate keys, nonfinite values and unknown fields. Inputs/events are limited to 32 KiB, text fields to 4,000 characters, scope/dependency/evidence lists to 50 and agreements to eight parties.

The typical sequence is registration, intent/discovery, message exchange, proposal, explicit agreement acknowledgement by every party, complete-set claim acquisition, checkpoint/artifact exchange, consumer acceptance, and release. Each agreement acknowledgement names the exact proposal revision and terms digest. A mailbox acknowledgement confirms delivery only. A parent relay records the actual parent actor and child subject; it cannot impersonate an independent root. A scope change requires a newly prepared worker attempt.

Use `communication_mode="mcp"` (the default) for direct mailbox participation; `native`, `parent_relay` and `checkpoint` remain available. Send a bounded request, let the recipient poll and acknowledge delivery, then reply with `reply_to` naming that message. Neither a received request nor an acknowledgement grants permission or transfers claims. See the [mailbox workflow](runtime.md#mailbox-workflow) for credentials, retries, cursors and limits. Provider independence describes the shared transport and schema; no cross-provider model trial or live deployment was performed.

See [runtime operations](runtime.md) for configuration, access, priority, retention and recovery. The runnable examples are the isolated [MCP tests](../../tests/test_cooperation_mcp.py), [mailbox tests](../../tests/test_cooperation_mailbox.py) and [service tests](../../tests/test_cooperation_service.py). The historical [examples.json](examples.json) remains a design artifact, not a current request fixture.

## Ownership and recovery

Claims are all-or-none per bounded work unit. Shared files have one writer; physical paths include the actual checkout identity, while integration targets and shared Git state include repository identity. Logical overlap is visible without forbidding independent private edits. Existing heavy-execution leases remain authoritative; the default service refuses heavy admission without a host adapter.

Silence, an expired proposal and a missed heartbeat do not transfer ownership. Stale participants and claims are quarantined. Coordinator restart advances its persistent epoch once. Explicit recovery requires a valid capability (or registered parent relay), execution-stop/release evidence, inspected checkpoint and exact held claim revisions. A fresh generation acquires claims again. Handoff additionally requires acceptance of an existing immutable release-ready checkpoint, bound to its owner and digest.

Loss of an independent root's capability has no online reset. Only explicitly authorized installation-owner offline cleanup, while the service and execution are stopped, can cancel that participant and release the enumerated claims. It never edits the checkout or grants replacement ownership.

## Documents and source

1. [Runtime operations](runtime.md): implemented behavior and host limitations.
2. [Validation results](validation-results.md): commands, results and deterministic scenario coverage.
3. [Protocol](protocol.md): normative semantic contract.
4. [Evaluation](evaluation.md): acceptance scenarios and authored model fixtures.
5. [Research](research.md), [implementation checklist](implementation.md), [draft instruction history](draft-instructions.md) and [source manifest](source-manifest.json): retained design provenance.

Implementation lives in [src/cooperation](../../src/cooperation), with thin MCP wrappers in [src/server.py](../../src/server.py), daemon lifecycle/access in [src/daemon](../../src/daemon), and concise [conversation](../../rules/rule-agent-cooperation.mdc) and [worker](../../rules/subagents/rule-worker-cooperation.mdc) instructions. Existing active personas pick up changed conversation rules only through a new activation or explicit refresh.
