---
persona:
  agent: software_engineer
---

# Implement shared local inference and KV caching

## Goal and result

Implement the optional local generation service specified in [the handoff](../docs/shared-kv-mcp/START_HERE.md). Produce reviewable code, focused tests, updated documentation and an honest capability/evaluation report. The shared resident cache works through a dedicated local model engine; optional Redis provides bounded snapshot storage.

This is an Agents-Core-specific implementation flow. It is not a generic flow for adding inference to any repository. The handoff files are part of this same checkout. Reading or preparing this flow does not execute it; run it only when the user requests implementation.

## Inputs and defaults

- Target repository: the caller's authorized Agents-Core worktree. For new changes, create a linked worktree from current `origin/dev` under the target repository's instructions.
- Branch: the caller's own implementation branch, normally `codex/<slug>`; preserve the historical shared-KV and research worktrees. The paths and branch recorded in the handoff are provenance, not defaults.
- Owner-selected existing local GGUF and llama.cpp binary for optional live checks.
- Implementation order: deterministic core, owned engine, resident cache, MCP job API, optional Redis, live checks.
- Generation and Redis remain disabled by default.
- No automatic deployment, downloads, cloud-model calls, pushes, PR creation or worktree removal.

If invoked through MCP in another installation, distinguish the flow source from the target repository. Resolve relative specification links against this flow's source checkout; apply edits/tests only to the user's authorized target. Verify the target contains the same handoff or explicitly transfer/reconcile it before executing. Do not write in the installation just because that is where run_flow found the instructions.

## Steps

1. Read the target AGENTS.md and CLAUDE.md and honor the shared persona/delegation protocol.
2. Verify the target path/branch and existing changes. Never modify the live installation at `/Users/alexey.zhuchkov/Documents/Agents`. Preserve unrelated changes.
3. Read the handoff, its [reconciliation report](../docs/shared-kv-mcp/reconciliation-report.md) and the files in its reading order. Compare the requested behavior with the current source and tests before selecting gaps. Use the API/architecture as the contract and [implementation-plan.md](../docs/shared-kv-mcp/implementation-plan.md) as the ordered checklist; historical reports do not prove current runtime state.
4. Implement only the requested unfinished milestones, starting each new behavior with a fake backend. Keep completed current behavior and its later fixes; do not recreate it or copy the old worktree wholesale. Run focused tests before moving on. Do not skip cancellation, workspace-root or native-counter checks to reach a live demo.
5. If the selected engine cannot restore compatible snapshots, implement/report the unsupported capability and keep cold/resident operation usable. Do not claim Redis acceleration without live evidence.
6. Update README, configuration and daemon/test documentation when corresponding behavior exists. Reconcile any necessary managed-protocol wording through the source template and required restatements.
7. Run appropriate checks from [validation.md](../docs/shared-kv-mcp/validation.md). Run only one heavy process at a time.
8. Obtain an independent code review through the mandatory prepared-worker exchange when available. Validate its exact result, assess the evidence, fix actionable defects and rerun affected checks. If independent review is unavailable, report that limitation instead of claiming it passed.
9. Leave a reviewable diff in the worktree. If the user separately authorizes publication, follow [pr-review.md](pr-review.md) and target dev; do not merge by default.

## Validation and done criteria

- The implemented subset is explicit; all acceptance rows applying to it pass.
- Existing Agents-Core behavior remains intact when local inference is disabled or unavailable.
- Shared requests use one owned serialized engine and measured cache counters.
- Long generation uses submit/result/cancel; duplicate submissions cannot duplicate generation.
- Worktree source identity is explicit and capability/budget checks cannot be bypassed by actor labels.
- Redis is optional, chunked, bounded and recoverable; any unsupported live capability is reported.
- No live-installation changes or unrelated process termination occurred.
- The final report lists changed files, actual checks, observed live results, unsupported/unperformed checks and remaining work. It makes no performance or quality claim beyond the evidence.
