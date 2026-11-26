# Documentation audit — 2026-11-26

**Status:** recorded audit, 2026-11-26, taken before the `dev` to `main`
promotion. Its review covered `origin/dev` at `43a1d121`; a
[delta](#delta-for-756-and-762) then covered #756 and #762 at `3a8c624a`. It is
evidence for the revisions named below; repeat the
[documentation refresh](../../flows/documentation-refresh.md) for a later one. The
[documentation map](../README.md) lists the current reader entry points.

This records a bounded source-based refresh of Agents-Core at
`43a1d1219e8a7695c93eddab932cd34b5eb87643` (`origin/dev`, confirmed with
`git ls-remote` on 2026-11-26), on branch `agent/docs-refresh-20261126`, before
the `dev` → `main` promotion. The release baseline is `origin/main`
`19e7e28bf11c71b100c70dd54d767f10f44f3127`, tagged `v26.11.14.1328`. It is
evidence for this revision, not a guarantee that later source or installed
clients match it.

## Method and scope

`git log origin/main..origin/dev` lists 299 commits; `main` holds nine promotion
merges whose content `dev` already contains, so the scope is what `dev` merged
after the last promotion ([#670](https://github.com/IEZhu/Agents/pull/670),
2026-11-15): the 42 pull requests in the table below, #524 and 41 numbered from
#671 to #755. Each changed surface
was read in its maintained reference and checked against its source and tests in
both directions: registered tools and their docstrings in `src/server.py`,
configuration in `src/engine/config.py`, module constants, script options,
workflow steps and test files. Older guidance outside those changes was checked
only where a change touched it, plus the catalogs, counts, the documentation map
and the language policy across the tree.

A script compared every backticked identifier of the changed references with
the source, tests, flows and workflows; each name it did not find was read in
context and is either an external client fact the document dates (listed under
Limits) or notation. A link walk from the documentation map reached every
tracked Markdown file except the protocol section files, which the map links as
a directory, and the frozen legacy reminders, which the
[legacy README](../../scripts/templates/legacy/README.md) lists by name. No worker was delegated: the refresh ran in one session. CodeGraph
was not used; current worktree files were the evidence. The live installation
was only read; all edits stayed in a linked worktree created with `--no-track`
from `origin/dev`.

Dispositions: **corrected** (material drift fixed), **added** (a missing
reference supplied), **verified unchanged** (the bounded check found no required
edit), **historical** (a dated record labelled rather than rewritten), **out of
scope** and **unverified** (with the reason).

## Coverage and corrections

| Surface (pull requests) | Maintained reference | Source and test evidence | Disposition |
|---|---|---|---|
| Validated claims: `claim_record`, `claim_lookup`, `src/claims/`, `src/estimation/`, the warmup hint and the ledger's recent rate (#726, #729, #731) | [Validated claims](../validated-claims.md), [tool index](../mcp-tools.md), [agent guide](../agent-guide.md), [features](../features.md), [architecture](../architecture.md), [test guide](../../tests/README.md), [quality checks](../quality-checks.md), [routing](../routing_flow.md#startup-and-readiness), [model tiers](../subagent-model-tiers.md) | `src/server.py` (`claim_record`, `claim_lookup`, `CLAIM_LOOKUP_MAX`), `src/claims/model.py` (`FAMILIES`, `ROUTES`, the importance needs, `VOLATILITY`, `GATE`), `src/claims/store.py` (limits, `data/claims/claims.jsonl`), `src/engine/warmup_estimate.py`, `src/engine/readiness.py`, `src/engine/subagent_ledger.py` (`ACCEPTANCE_HALF_LIFE_DAYS`), `tests/test_claims.py`, `tests/test_estimation_*.py`, `tests/test_warmup_estimate.py`, `.github/workflows/shared-runtime.yml` | Verified unchanged: parameters, levels, priors, states, limits, errors and the hint. Corrected: the architecture components, the agent guide's persistence line, the test-guide rows and the Shared runtime descriptions in the test guide and quality checks |
| Shared native memory: `read_shared_memory` (#695, #727) | [Shared memory](../shared-memory.md), [repository memory](../memory.md), [tool index](../mcp-tools.md), [README](../../README.md), `CLAUDE.md` repository notes | `src/server.py` (`read_shared_memory`), `src/memory/shared.py` (`get_index` under `data/memory/`, `STORE_NAME`), `src/memory/native.py`, `src/daemon/memory_ui.py`, `tests/test_shared_memory.py` | Verified unchanged. Corrected: the architecture tree, the README's project-memory row, the agent guide's persistence line, a test-guide row. Unverified: the Claude Code and Codex storage facts the guide dates 2026-11-18 were not fetched again |
| Typed turn records and `read_history` by `tag` (#700, #703) | [Turn records](../turn-records.md), [tool index](../mcp-tools.md) | `src/memory/record.py`, `src/memory/history.py`, `tests/test_turn_record.py`, `.github/workflows/workspace-bindings.yml` | Verified unchanged. Added: the test-guide row and the Workspace bindings paragraph |
| Durable memory acceptance, MEM-01 (#524, #694, #703) | [Memory acceptance](../memory-acceptance.md), [repository memory](../memory.md) | `src/memory/history.py` (`fsync` calls), `src/daemon/history_ui.py`, `src/daemon/workspaces.py`, `tests/test_memory_acceptance.py`, `tests/test_history_artifact_safety.py` (`AGENTS_TEST_REQUIRE_SYMLINKS`), `.github/workflows/workspace-bindings.yml` | Verified unchanged. Added: the test-guide row and workflow paragraph |
| Headless profile and activation overhead: `AGENTS_CLIENT_PROFILE`, `AGENTS_TURN_LOG_HOOK`, `ROUTER_SHORTLIST_SIZE`, `ROUTER_COLD_RESOLVE_MARGIN`, `SINGLE_ENCODED_TOOLS`, the section pointer, the bundle trim, procedures moved into tool descriptions, the Stop-hook turn logger, agent catalog cards, `not_a_repository` (#737) | [Configuration](../configuration.md#environment-variables), [routing](../routing_flow.md), [repository memory](../memory.md#turn-logging-by-a-stop-hook), [AGENTS.md](../../AGENTS.md), `CLAUDE.md` repository notes, [legacy templates](../../scripts/templates/legacy/README.md), the evaluation entries of the [map](../README.md#evaluation-guides) | `src/engine/config.py` (defaults and the `_int_env`, `_float_env`, `_choice_env` fallbacks), `src/engine/rules.py` (`HEADLESS_OMITTED_RULES`), `src/engine/persona_bundle.py`, `src/server.py` (`SINGLE_ENCODED_TOOLS`), `src/result_size.py`, `src/reindex.py`, `src/memory/turn_log_hook.py`, `src/memory/describer.py`, `src/engine/protocol_text.py` (`TOOL_PARTS`), `tests/test_client_profile.py`, `tests/test_cold_routing.py`, `tests/test_single_encoded_results.py`, `tests/test_turn_log_hook.py`, `tests/test_reindex_agent_cards.py` | Verified unchanged: every default, range and fallback in the configuration table. Corrected: the architecture tree's `reindex.py` line, which omitted the agent catalog cards |
| Persona restore after a compaction (#743) | [Session readiness](../session-readiness.md#the-restored-bundle), [uninstall](../install.md#uninstall) | `scripts/session_readiness.py` (`RESTORE_PARTS = 12`, `PART_LIMIT = 9500`, `SCAN_BUDGET = 3.0`), `src/uninstall.py` (`READINESS_RESTORE_PARTS`), `tests/test_session_readiness.py` | Verified unchanged. Unverified: the guide's transcript replay counts and timings are a recorded measurement and were not re-run |
| Thread status: attention set last, desktop rename (#680, #744) | [Thread status](../thread-status.md), `rules/rule-thread-status.mdc` | `src/thread_status.py`, `scripts/thread_status.py`, `tests/test_rules.py` (thread-status wording), `.github/workflows/thread-status.yml` | Verified unchanged. Corrected: the test-guide row now names the rule-text step |
| Thread close: the gate ladder, slugs, the miss-rate report and the inventory helper (#699, #714, #716, #717, #728, #730, #749, #750, #755) | [Thread-close flow](../../flows/thread-close.md), [turn records](../turn-records.md#a-rules-recent-miss-rate), [test guide](../../tests/README.md) | `scripts/thread_close_miss_rate.py` (`--workspace`, `--json`, `HALF_LIFE_DAYS = 14.0`, the alias kinds), `scripts/dev/thread_inventory.py`, `tests/test_thread_close_miss_rate.py`, `tests/test_thread_inventory.py`, `.github/workflows/documentation-quality.yml` | Verified unchanged. Added: the miss-rate test in the test guide |
| PR review: reviewer allowance, shared window, cross-PR agreement, resolved threads (#706, #712, #720, #742) | [PR review flow](../../flows/pr-review.md), [cooperation runtime](../agent-cooperation/runtime.md), `docs/agent-cooperation/schemas.json` | `scripts/dev/pr_threads.py` (options and docstring), `src/cooperation/models.py`, `src/cooperation/quota.py`, `tests/test_pr_threads.py` | Verified unchanged |
| Skills and agents: code generation, data intake, clinical lab units, statistical geography, SEC Form 13F (#678, #735, #736, #741) | [Skills](../../skills/README.md), [agent catalog](../../agents/README.md) | `skills/*.mdc` against the skills README, agent frontmatter, `tests/test_data_skills.py`, `.github/workflows/content-checks.yml` | Verified unchanged: every skill file has a README row and its agents name it. Corrected: the agent catalog's total read 45 against 46 persona folders and 46 table rows |
| Flows: consistency audit, verifier accuracy, open-source tuning from an own defect (#673, #677, #732, #748) | [Workflow catalog](../../flows/README.md), [map](../README.md) | the frontmatter of all 22 built-in flows against the catalog's persona paragraphs, `tests/test_flows.py` | Verified unchanged |
| Verifier accuracy measurement (#729, #732, #748) | [Measurement design](../verifier-accuracy-measurement.md), [pilot](verifier-accuracy-pilot.md), [stage 1](verifier-accuracy-stage1.md), [flow](../../flows/verifier-accuracy.md) | `evals/verifier_accuracy/`, `evals/scripts/verifier_accuracy.py`, `tests/test_verifier_accuracy.py` | Historical: recorded results with status lines, not re-run. Verified unchanged: the flow's commands and the map entries |
| Worker result rule: an output manifest holds up to 32 entries (#747) | `rules/subagents/rule-worker-result.mdc`, [shared artifacts](../shared-artifacts.md) | `src/schemas/artifacts.py` (`entries` `max_length=32`), `tests/test_subagent_artifacts.py` | Corrected: the artifact tool table now states the 1-to-32 bound |
| Branch names: `agent/` for new work (#752) | [AGENTS.md](../../AGENTS.md), [session playbook](../session-playbook.md), the flows, `CLAUDE.md` | `git grep` for `codex/` in maintained documents: only dated records name such branches | Verified unchanged; the Repository Memory block was refreshed |
| README split and the goal-grouped map (#686, #707) | [README](../../README.md), [install](../install.md), [configuration](../configuration.md), [development](../development.md), [architecture](../architecture.md), [map](../README.md) | `scripts/check_docs.py`, the link walk above, `src/utils/debug_logger.py`, `.github/workflows/vulnerability-scan.yml` | Verified unchanged. Corrected: `AGENTS.md` still sent readers to the README for installation and configuration |
| Legacy reminders v26 and v27 (#691, #704) | [Legacy templates](../../scripts/templates/legacy/README.md) | `python -m src.engine.protocol_text --check`, `tests/test_protocol_text.py` | Verified unchanged |
| CI selection: the pytest bound, journal tests, inventory tests (#671, #703, #755) | [Test guide](../../tests/README.md), [quality checks](../quality-checks.md), [memory acceptance](../memory-acceptance.md) | `.github/workflows/*.yml` run steps | Corrected and added: see the rows above |
| Plugin directory refusal of `26.11.14.1328` (#672) | [Claude plugin](../claude-plugin.md), [distribution ledger](../distribution.md) | The owner's portal evidence as recorded | Unverified: the owner-only portal cannot be read from this session; the dated record stands |
| Repository Memory block of `CLAUDE.md` | `CLAUDE.md` | `describe_repo(force_refresh=True)` and `write_repo_summary` for this worktree, hash `ab1c6470` | Corrected: the block of 2026-11-19 counted 89 skills, 21 flows, 34 tools in `src/server.py` and four memory tools. The writer names the block after the worktree directory; its heading was set back to `Agents` |
| The work map, [#756](https://github.com/IEZhu/Agents/pull/756) | Its own documentation on `agent/workmap` | `gh pr view 756`: open against `dev`, head `e696517541bff96a911823881691f419a044abc0` on 2026-11-26 | Out of scope at `43a1d121`: not merged yet. It merged later that day; see the [delta](#delta-for-756-and-762) |

## Delta for #756 and #762

`6d6213f6` merged `origin/dev` at `3a8c624a13cd861d7171c3187e927295cb849e32` into this
branch, adding two pull requests to the review above:
[#756](https://github.com/IEZhu/Agents/pull/756), the work map (`workmap_update`,
`workmap_view`, `workmap_submit`, `src/workmap/`, `src/safe_store.py`, the `orchestrate`
flow, [the plan](../workmap-plan.md) and its
[implementation plan](../proposals/workmap-implementation-plan.md)), and
[#762](https://github.com/IEZhu/Agents/pull/762), which moved the documents no source
file names into `docs/proposals/` and `docs/records/`, this record among them. The
merge itself brought the tool index, the architecture tree, the worker contract, the
model-tier guide and the moved links; this pass covered the rest of the reader-facing
references.

| Surface | Change in this pass | Source checked |
|---|---|---|
| [README](../../README.md) | Added: the work map in the delegated-workers row and a task-table row with its tools and the `orchestrate` flow | `src/server.py` (`workmap_update`, `workmap_view`, `workmap_submit`, the `workmap_node` and `workmap_inputs` arguments of `get_subagent_context`) |
| [Features](../features.md) | Added: a Work map row and note 10; partial outside Claude Code, the only client where a worker was seen to reach the server's tools | `_workmap_call` (`require_root`), the [spike](../workmap-plan.md#spike-of-2026-11-25) |
| [Agent guide](../agent-guide.md) | Added: a task row, the bound-package steps and `data/workmap/` in the persistence line | `src/workmap/store.py` (`DIRECTORY`), `src/workmap/binding.py`, `flows/orchestrate.md` |
| [Architecture](../architecture.md) | Corrected: the tree listed two of the three work-map tools. Added: a Work map component | `src/server.py`, `src/workmap/` |
| [Documentation map](../README.md), [shared artifacts](../shared-artifacts.md) | Added: a delegation row for the map; `workmap_inputs` and its refusal with `artifact_manifest` | `src/workmap/binding.py` (`invalid_request`) |
| [Test guide](../../tests/README.md) | Added: a Work map row and the CI steps that run its tests; the single-encoded row names the work-map tools | `tests/test_workmap_*.py`, `.github/workflows/shared-runtime.yml`, `.github/workflows/documentation-quality.yml`, `SINGLE_ENCODED_TOOLS` |
| `CLAUDE.md` | Added: a repository note on the map. Regenerated: the Repository Memory block (hash `3598eef5`), whose previous text counted 34 `@mcp.tool` handlers, 51 tools, 22 flows and 268 test files; the writer heads it with the worktree name, `docs-refresh`, and it is left as written | `describe_repo(force_refresh=True)` and `write_repo_summary` for this worktree |
| [AGENTS.md](../../AGENTS.md), [agent catalog](../../agents/README.md), the six artifact tools of [shared artifacts](../shared-artifacts.md) | Verified unchanged: no tool count or list the merge made stale | `src/artifacts/tools.py`, `src/server.py` |

Tool count at `3a8c624a`: 37 handlers decorated `@mcp.tool` and 3 registered through
`protocol_tool` in `src/server.py`, 6 from `TOOL_NAMES` in `src/artifacts/tools.py`
(the keys of `REQUEST_MODELS` in `src/artifacts/service.py`) and 8 from `TOOL_NAMES` in
`src/local_inference/tools.py`: 54, three more than the review above found. Built-in
flows are 23 with `orchestrate`.

Checks of the delta, run from the worktree root with the installation's Python
environment and `markdown-it-py` on `PYTHONPATH`, which that environment lacks:
`scripts/check_docs.py` reported "Checked 198 Markdown files; 0 navigation/tool-index
errors.", `python -m src.engine.protocol_text --check` listed nothing, and
`tests/test_check_docs.py` gave 9 passed. The full suite and the work-map tests were
not run; this pass changed documentation only.

## Before the promotion

- `VERSION` is `26.11.14.1328`, the version `main` was released at, and
  `python scripts/bump_plugin_version.py --check --base origin/main` answers
  that it must be greater than `main`'s; the promotion needs the bump of
  [step 3](../releases.md#prepare-a-promotion).
- [#756](https://github.com/IEZhu/Agents/pull/756) was open when the review ran;
  it merged first, and the [delta](#delta-for-756-and-762) re-checked the guides
  against the integrated tree.
- Still owed at the frozen promotion head: the release checks, current-head CI,
  the bot reviews and the owner's decision. After the merge: the tag and GitHub
  Release at the exact merge commit, the release run's own scan and the raw
  `main` URLs of `llms.txt`.

## Executed checks

Run from the worktree root with the installation's Python environment and `src`
resolving to the worktree:

- `git diff --check`: no output.
- `scripts/check_docs.py`: "Checked 194 Markdown files; 0 navigation/tool-index
  errors." (193 at the base, with the same result).
- `python -m src.engine.protocol_text --check` and
  `python -m src.engine.subagent_contract_text --check`: nothing listed, exit 0,
  at the base and after the edits.
- `tests/test_check_docs.py`, `tests/test_flows.py`, `tests/test_protocol_text.py`:
  118 passed.
- The tests that read the edited instruction files or catalogs:
  `tests/test_cooperation_instructions.py`, `tests/test_content_checks_workflow.py`,
  `tests/test_instruction_pointers.py`, `tests/test_managed_section.py`,
  `tests/test_data_skills.py`: 508 passed, 37 skipped.
- `scripts/bump_plugin_version.py --check --base origin/main`: fails, as
  expected before the promotion's bump (above).
- Not run: the full regular suite, external URLs and the owner's portal.

## Limits

- External client facts were not fetched again: the Claude Code and Codex memory
  locations and settings in [shared memory](../shared-memory.md), the hook events in
  [session readiness](../session-readiness.md) and [thread status](../thread-status.md).
- Recorded measurements (the restore replay, the verifier pilot and stage 1, the
  cold-ranker margin) are cited as recorded, not reproduced.
- Deterministic checks and fixtures are not live client acceptance.
