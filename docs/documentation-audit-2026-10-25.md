# Documentation audit — 2026-10-25

This records a bounded source-based refresh of Agents-Core at
`f154a39687f315ef24e51e7bc39b79bab87163b6` (`origin/dev` when fetched for this
run, 2026-10-25), on branch `claude/docs-refresh-release`, before the `dev` →
`main` promotion. The release baseline is `origin/main`
`c2a7b929afe33ca846f3f94447f600f66bdfc0dc`, tagged `v26.10.16.2041`. It is
evidence for this revision, not a guarantee that later source or installed
clients match it. Repeat the [documentation refresh](../flows/documentation-refresh.md)
for a later revision; the [documentation map](README.md) lists current reader
entry points.

## Method and scope

The previous refresh, [#451](https://github.com/IEZhu/Agents/pull/451), merged
into `dev` on 2026-10-24 and is recorded in the
[audit of 2026-10-18](documentation-audit-2026-10-18.md). This run covered what
reached `dev` after that refresh's last documentation commit: #475, #481, #487,
#490, #492, #494, #495, #502, #509, #514, #517, #519, #521 and #522. It also
checked the documentation map, the discovery file, the workflow catalog, the
language policy and the release records against the current tree. Older guidance
outside these changes was not re-audited line by line.

Five prepared read-only `tech_writer` audits covered disjoint areas: the persona
protocol and generated instructions; delegated workers; repository description
and thread close; runtime composition, dependencies and release gates; and the
map, catalogs, language and release records. Each returned result passed
validation against its original contract before use; two areas needed a second
attempt after their first result failed the text format, and the second attempt
re-checked every finding it kept. The integrator spot-checked the load-bearing
source claims of each report before editing, and a separate prepared worker
applied the delegation area's exact replacements, whose diff the integrator
reviewed. Structural validation alone was not treated as evidence of correctness.

CodeGraph indexes the repository; current worktree files were the evidence. The
live installation was only read; all edits stayed in a linked worktree created
with `--no-track` from fetched `origin/dev`.

Dispositions below mean **corrected** (material drift fixed), **added** (a
missing reference supplied), **verified unchanged** (the bounded check found no
required edit), **historical** (a dated record labelled rather than rewritten) or
**deferred** (outside documentation, with the reason under Limits).

## Coverage and corrections

| Reader task / surface | Maintained reference | Source and test evidence inspected | Disposition and result |
|---|---|---|---|
| Activate, restore and keep a persona; instruction pointers and drift warnings (#514, #517, #521) | [Routing](routing_flow.md), [README](../README.md#persona-continuity), [AGENTS.md](../AGENTS.md), the `CLAUDE.md` repository notes, [session playbook](session-playbook.md), [test guide](../tests/README.md) | `src/client_instructions.py`, `src/engine/protocol/`, `src/engine/protocol_text.py`, `src/engine/persona.py`, `src/server.py`, `src/result_size.py`, `tests/test_instruction_pointers.py`, `tests/test_log_interaction_contract.py`, `tests/test_flow_result_size.py` | Corrected: the Codex initialization pointer's conditions (registered checkouts, linked worktrees, the assumed 32 KiB limit), the drift-warning files per client and in a flow's `persona_activation`, which texts state the cooperation fallback, the 2,048 and 4,096 character windows, the `pointer` section's ownership, the restart warning when the daemon is older than the refreshed files. Verified unchanged: [agent guide](agent-guide.md) and [tool index](mcp-tools.md) for this area |
| Prepare a worker and validate its result (#509) | [Worker contract](subagent-contract.md), [model tiers](subagent-model-tiers.md), [shared artifacts](shared-artifacts.md), [workflow catalog](../flows/README.md#delegated-workers), [agent guide](agent-guide.md), [tool index](mcp-tools.md), [routing](routing_flow.md#delegated-workers), [autodev workers](autodev-workers.md), worker rules and READMEs | `src/engine/subagent.py`, `src/engine/subagent_text.py`, `src/engine/subagent_exchange.py`, `src/engine/subagent_validation.py`, `src/schemas/subagent.py`, `src/server.py`, `src/schedules/workers.py`, `tests/test_subagent_exchange.py`, `tests/test_subagent_tiers_mcp.py` | Corrected: the default file receipt, `launch` text, `contract_revision` and `result_ref` replace the inline-only description; new arguments `delivery` and `result_format`; receipt fields, retention, result-file limits and error codes; the plain-text result format in worker rules and flows; `get_subagent_context` is not read-only |
| Describe a repository; log a turn (#519, #487) | [Memory specification](memory-subsystem-spec.md), [README](../README.md#-repository-memory), [tool index](mcp-tools.md), [agent guide](agent-guide.md), [routing](routing_flow.md), [cloud runs](cloud-runs.md) | `src/memory/describer.py`, `src/memory/config.py`, `src/result_size.py`, `src/server.py`, `tests/test_describer.py`, `tests/test_server_describe.py` | Corrected: the prompt copy uses the shipped placeholders and read-only wording, the bundle's shown units and caps, the first-work rule, the managed-section refresh condition, the redaction contract of `log_interaction`, and that `write_repo_summary` owns the hash and locks |
| Close a thread (#475, #487) | [Thread-close flow](../flows/thread-close.md), [MCP tool safety](mcp-tool-safety.md) | `scripts/dev/thread_inventory.py`, `scripts/dev/mcp_metadata_guard.mjs`, `tests/test_thread_inventory.py`, `tests/mcp_metadata_guard.test.mjs` | Corrected: the conditional closing log, the helper's summary keys, the write-time visibility recheck, the native-thread paragraph's section |
| Personal runtime, log worker, dependency gate, Claude desktop stop status (#490, #494, #495, #492, #502, #481) | [Runtime boundary](personal-runtime-boundary.md), [README](../README.md), [test guide](../tests/README.md), [quality checks](quality-checks.md), [thread status](thread-status.md), `env.example`, [routing](routing_flow.md#startup-and-readiness) | `src/personal_runtime.py`, `src/stdio_runtime.py`, `src/server.py`, `src/engine/onnx_embedding.py`, `.github/workflows/`, `tests/test_personal_runtime.py`, `tests/test_runtime_composition.py`, `tests/test_log_worker_startup.py`, `tests/test_thread_status_interrupt.py`, `tests/test_uninstall_thread_status.py` | Corrected: test-guide rows and workflow summaries, the source tree, when the vulnerability gate blocks a merge and when its schedule runs, the removed `fastembed` package, readiness started by `src/stdio_runtime.py`, current-behavior wording. `filelock` (#502) changed only `uv.lock`; no document names it |
| Find documents and flows; language policy | [Documentation map](README.md), `llms.txt`, [workflow catalog](../flows/README.md), [agent catalog](../agents/README.md) | `git ls-files '*.md'`, `src/flows.py`, flow frontmatter, `agents/*/system_prompt.mdc` | Corrected: links to six unreached files, the agent count, the workflow persona list, the release-row sources, the discovery file's tool index and result format. Added English glosses to four Russian examples. No other non-English narrative found |
| Prepare the promotion | [Release procedure](releases.md), this record, the [audit of 2026-10-18](documentation-audit-2026-10-18.md) | `scripts/bump_plugin_version.py`, `scripts/release_version.py`, `.github/workflows/release-version.yml`, `tests/test_release_version.py` | Verified unchanged apart from a dated Glama observation; some long lines remain unwrapped. The 2026-10-18 record is labelled historical, its unserved commit link is plain text, and its pre-promotion list now points here |

## Before the promotion

The [2026-10-18 record](documentation-audit-2026-10-18.md) listed five steps
before this promotion. Their status on 2026-10-25:

1. Done: the documentation and version PR
   [#451](https://github.com/IEZhu/Agents/pull/451) merged into `dev` on
   2026-10-24.
2. Done by this refresh: `dev` was re-fetched and the README, documentation map,
   daemon and runtime guides were reconciled with the integrated source above.
   `VERSION` stays the dynamic package version source; this refresh changes no
   lock file.
3. Partly re-checked: the thread-status guide was compared with the Claude
   desktop stop status of #481. The proxy CLI/help and sync allowlist were not
   re-audited in this run.
4. Partly done: the documentation checks below ran on this branch. The
   release-version and affected status, proxy and sync checks belong to the
   frozen promotion head.
5. The version was validated against fetched `origin/main` (below); the release
   procedure, the owner's decision and the tag and release checks remain.

The promotion covers 39 pull requests merged into `dev` since `v26.10.16.2041`
(#441 to #522), 192 non-merge commits that only `dev` has and none that only
`main` has. On 2026-10-25 no other pull request was open. Their descriptions name
these issues with closing keywords, all already closed: #431, #432, #433, #434,
#476, #508 and #520.

`VERSION` is `26.10.21.0011`. `python3 scripts/bump_plugin_version.py --check
--base origin/main` passes against the baseline `26.10.16.2041`, which `main`
derives from its merge time because it has no `VERSION` file; running the
helper again keeps the newer value, so the release is expected to be tagged
`v26.10.21.0011`.

Still owed at the frozen promotion head: the release checks of
[step 3](releases.md#prepare-a-promotion), the promotion PR's `Release version`
and `Vulnerability scan` checks, current-head CI, the bot reviews and the
owner's decision. After the merge: the tag and GitHub Release at the exact merge
commit, the release run's own scan, the Glama build and version, and the raw
`main` URLs of `llms.txt`.

## Executed checks

- `scripts/check_docs.py` from the worktree: no navigation or tool-index errors
  (163 tracked Markdown files at the base, rerun at the final head).
- `python -m src.engine.protocol_text --check` at the base: no stale generated file.
- `scripts/bump_plugin_version.py --check --base origin/main`: passed (above).
- Focused tests for the edited documents, run from the worktree with its `src`:
  the protocol-text, instruction-pointer, logging-contract, flow,
  documentation-checker, tool-safety and thread-inventory tests (432 passed);
  the configuration, daemon-control, describer, installer, model-migration and
  vulnerability-gate tests that read the edited files (341 passed, 2 skipped);
  and the worker, tier, artifact, rule, bundle and cooperation-instruction tests
  after the worker-rule wording changed (427 passed).
- Not run: the full regular suite, external URLs and Glama. Claims about Claude
  Code and Codex limits were checked against source comments, the Claude Code
  documentation where it is cited and PR evidence, not against a live client.

## Limits

- Deferred, outside documentation: the protocol source tells a client to restore
  only when the full instruction is gone, while the bundle pointer also restores
  on a different `instruction_revision` (`src/engine/protocol/apply.md`);
  changing it changes the template revision. The worker result template allows
  100 list items where a generic result's `items` holds 50, and two
  delegation messages still describe the pre-#509 exchange
  (`src/engine/subagent_text.py`, `src/engine/subagent_exchange.py`,
  `src/server.py`, `src/engine/subagent_ledger.py`).
- Owner decision: the thread-close tool catalogue names the example upstreams
  that [the proxy guide](mcp-proxy.md) documents publicly.
- Autodev still pins and targets `main`; the broker opens its PRs against it.
- Deterministic checks and fixtures are not live client acceptance.
