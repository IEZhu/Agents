# Shared local inference and KV cache implementation handoff

Status: historical implementation contract and worktree handoff. Later integration
adds [persistent shared artifacts](../shared-artifacts.md); this handoff and its
reports retain their original revisions and deployment checkpoints. Prepared on 2026-10-14; see the [implementation report](implementation-report.md) for original gates and the [Redis live report](redis-live-report.md) for historical real-server acceptance and benchmarking. On 2026-10-15 the owner separately authorized connecting the working service: resident reuse is enabled, Redis remains off in production, and fresh mode/fault tests passed. The [resident deployment report](resident-deployment-report.md) records deployment, the pinned worktree and paused updater, token-rotation preservation limits, recovery and fresh evidence. The diff remains uncommitted/unmerged; package defaults remain disabled.

## Start here

Build an optional local model service behind Agents-Core MCP. Several agents should reuse previously computed prompt state when they use the same compatible local model and shared context. Implement resident reuse first, then optional Redis snapshot storage. Preserve ordinary Agents-Core behavior when the feature is disabled.

The prepared checkout is:

- Worktree: `/Users/alexey.zhuchkov/.codex/worktrees/shared-kv-mcp/Agents`.
- Branch: `codex/shared-kv-mcp`.
- Base: `origin/dev`, recorded commit `34671459c6b0a445f0ad0812d7c1e4c8b3e22827`.
- Experimental reference: `2f4f2810738d890f0d49c177bf0ab3c4780a6c3e`, pushed as `origin/codex/agent-language-concept`.
- Live installation, never modify: `/Users/alexey.zhuchkov/Documents/Agents`.

Do not merge or cherry-pick the experimental branch wholesale. It contains a research language, multiple historical harnesses and large evidence sets. Implement the small production component described here. Read reference files with `git show <recorded-commit>:<path>` without switching branches. [Evidence and provenance](evidence.md) supplies the exact paths.

The original handoff authorized preparation only; implementation followed the user's request to execute [the implementation flow](../../flows/shared-kv-inference.md), and service connection followed separate explicit authorization. This document itself grants no further deployment, model-setting change, model-weight download, publication or messaging authorization. Local implementation and focused tests belong in this worktree. Keep the user's original research branch, worktree and chat available. A runtime worktree must remain present and its runtime source must not change for as long as a service is pinned to it.

## Reading order

1. [Repository instructions](../../AGENTS.md) and [persona protocol](../../CLAUDE.md).
2. This file, then [architecture and invariants](architecture.md).
3. [MCP contract](api-contract.md) and [owner configuration example](owner-config.example.json).
4. [Ordered implementation plan](implementation-plan.md).
5. [Redis snapshot design](redis.md).
6. [Tests and evaluation](validation.md).
7. [Evidence and source map](evidence.md).

Read the architecture and API contract before editing. Work one milestone at a time. Each milestone has a concrete stopping condition. Do not invent a different API or replace a failing cache test with a success flag.

## Fixed decisions

| Question | Decision for version 1 |
|---|---|
| Language/runtime | Python 3.11+, existing Agents-Core package |
| Production code directory | New `src/local_inference/`; do not reuse the daemon's embedding inference lane |
| Model engine | One dedicated, service-owned llama.cpp process |
| Model weights | Existing owner-selected local GGUF; no automatic downloads |
| Initial compatibility target | Dense Qwen3-8B profile that already showed reuse; all profiles remain capability-tested |
| MCP entry point | Existing authenticated shared HTTP daemon, opt-in tools |
| Direct stdio | New tools return `transport_unsupported`; clients may use the existing bridge to the shared daemon |
| Scheduling | One active generation; bounded FIFO queue; short submit/result/cancel MCP calls |
| Hot cache | Resident state owned by llama.cpp |
| Redis | Optional L2 for compatible opaque slot snapshots; disabled by default |
| Prompt transport | Server-rendered full text; token IDs are computed internally for validation |
| Agent execution | One bounded completion per call; no recursive agent runner or automatic tool execution |
| IR/compiler | No Accord runtime dependency and no new language/compiler |
| Isolation | One trusted local OS account; workspace partitioning plus bearer capabilities, not hostile multitenancy |
| Failure behavior | Cache miss may recompute; failed or ambiguous model dispatch is never automatically repeated |
| Success criterion | Correct isolation/accounting and observed native cache hits; speed benefit reported separately |

A normal cloud model's internal KV state is outside this service. A client can call our local model as an MCP tool, but that does not redirect its own cloud reasoning or change native host subagent transport.

## What the previous experiments establish

Dense 8B reuse avoided 48.0% of input evaluation and reduced summed model-call time by 25.4% for Accord and 30.6% for Python. The full interactive workflow did not get faster, and all eight repair attempts failed. The stronger 27B run solved 7/8 episodes but recorded zero KV hits. These are small recorded pilots, not promises for the new service.

The original adapter shares a single slot sequentially and assumes a trusted host. Its process-local object handles and actor strings cannot be exposed as an authenticated cross-process API. The new component must implement the boundary specified here.

## Working method for the implementation model

- Start with `git status --short --branch` and verify this checkout, not the live installation.
- Confirm imported Python modules resolve to this worktree. Use the test guide's environment isolation.
- Keep a short progress checklist in the PR description or your task notes. Mark a milestone complete only after its listed checks pass.
- Prefer small typed modules and focused pytest files. Add deterministic fake-engine tests before enabling real inference.
- Preserve failures in evaluation output. A zero-hit run is a valid negative measurement.
- For every AI reviewer/worker, follow the prepared-package/validation exchange in CLAUDE.md. Do not copy the parent conversation into workers.
- Do not weaken the acceptance criteria, remove cancellation tests, or infer cache hits from `cache_prompt=true`.
- When blocked by a missing local model or unavailable optional dependency, finish independent code and fake tests; report the precise remaining live check. Do not claim that live acceptance passed.
- Keep documentation in English. Explain completion to the user in their language.

## Copyable prompt for the next model

> Work in /Users/alexey.zhuchkov/.codex/worktrees/shared-kv-mcp/Agents on codex/shared-kv-mcp. Read AGENTS.md and CLAUDE.md, then docs/shared-kv-mcp/START_HERE.md. Execute flows/shared-kv-inference.md in order. The architecture, API, Redis protocol and acceptance matrix in that package are the implementation contract. Start with the disabled-by-default resident-cache service and deterministic fake-engine tests, then implement the optional chunked Redis snapshot backend. Preserve the live installation and the existing Accord research worktree. Do not claim real model acceleration without native counters and complete wall-clock measurements. Do not deploy, push, open a PR, remove worktrees or close the chat without separate user authorization. Finish with the implemented milestones, actual check results, and any unperformed live acceptance.

## Preparation checks already performed

These check the handoff, not the future inference service:

- `tests/test_flows.py`: 27 passed in 41.41 seconds with the existing interpreter, from this worktree.
- The new flow loads from this checkout and its declared software_engineer persona is recognized.
- Relative specification links, JSON examples/configuration, Markdown fences and whitespace were checked; all 19 source-manifest hashes matched their recorded Git objects.
- An independent source audit checked integration boundaries. A separate document reviewer identified five contract gaps; the revised review confirmed those were resolved and identified a nonblocking-open requirement, which was added with a FIFO acceptance case.
- No generative-model inference, Redis service, deployment or compiler implementation was run during preparation. The 30 acceptance rows describe work for the implementation model.
