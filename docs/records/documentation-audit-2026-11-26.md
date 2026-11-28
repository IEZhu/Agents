# Documentation audit — 2026-11-26

**Status:** recorded audit, 2026-11-26, taken before the `dev` to `main`
promotion. Its review covered `origin/dev` at `43a1d121`; a
[first delta](#delta-for-756-and-762) covered #756 and #762 at `3a8c624a`, and a
[second](#delta-for-759-760-763-764-765-769-and-770) covers #759, #760, #763, #764,
#765, #769 and #770 at `10e1e6a1`, where this record stops. It is evidence for the
revisions named below; repeat the
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
| PR review: reviewer allowance, shared window, cross-PR agreement, resolved threads (#706, #712, #720, #742) | [PR review flow](../../flows/pr-review.md), [cooperation runtime](../agent-cooperation/runtime.md), `docs/agent-cooperation/schemas.json` | `scripts/dev/pr_threads.py` (options and docstring), `src/cooperation/models.py`, `src/cooperation/quota.py`, `tests/test_pr_threads.py` | Verified unchanged at `43a1d121`; #759 and what #765 changed under it are in the [second delta](#delta-for-759-760-763-764-765-769-and-770) |
| Skills and agents: code generation, data intake, clinical lab units, statistical geography, SEC Form 13F (#678, #735, #736, #741) | [Skills](../../skills/README.md), [agent catalog](../../agents/README.md) | `skills/*.mdc` against the skills README, agent frontmatter, `tests/test_data_skills.py`, `.github/workflows/content-checks.yml` | Verified unchanged: every skill file has a README row and its agents name it. Corrected: the agent catalog's total read 45 against 46 persona folders and 46 table rows |
| Flows: consistency audit, verifier accuracy, open-source tuning from an own defect (#673, #677, #732, #748) | [Workflow catalog](../../flows/README.md), [map](../README.md) | the frontmatter of all 22 built-in flows against the catalog's persona paragraphs, `tests/test_flows.py` | Verified unchanged |
| Verifier accuracy measurement (#729, #732, #748) | [Measurement design](../verifier-accuracy-measurement.md), [pilot](verifier-accuracy-pilot.md), [stage 1](verifier-accuracy-stage1.md), [flow](../../flows/verifier-accuracy.md) | `evals/verifier_accuracy/`, `evals/scripts/verifier_accuracy.py`, `tests/test_verifier_accuracy.py` | Historical: recorded results with status lines, not re-run. Verified unchanged: the flow's commands and the map entries. The harness changes of #763 and #770 are in the [second delta](#delta-for-759-760-763-764-765-769-and-770) |
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
| [Test guide](../../tests/README.md) | Added: a Work map row and the CI steps that run its tests; the single-encoded row says which tools `test_single_encoded_results.py` calls and that `describe_repo` and the work-map tools are covered there only by the listed-schema check | `tests/test_workmap_*.py`, `tests/test_single_encoded_results.py`, `.github/workflows/shared-runtime.yml`, `.github/workflows/documentation-quality.yml`, `SINGLE_ENCODED_TOOLS` |
| `CLAUDE.md` | Added: a repository note on the map. The Repository Memory block this pass regenerated (hash `3598eef5`; the previous text counted 34 `@mcp.tool` handlers, 51 tools, 22 flows and 268 test files) was superseded by #766, which regenerated it on `dev` after #760 and #759 (hash `a91aa780`); the merge keeps #766's block unchanged | `describe_repo(force_refresh=True)` and `write_repo_summary` for this worktree, then a comparison of the merged block with `dev`'s |
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

## Delta for #759, #760, #763, #764, #765, #769 and #770

`origin/dev` is at `10e1e6a1e18c4d957523bcfca8bf1d34460655f5`, the merge of
[#770](https://github.com/IEZhu/Agents/pull/770) (`git ls-remote origin` on 2026-11-28;
`main` is still `19e7e28b`). Nine pull requests merged between `3a8c624a` and it. Two
belong to this record already: [#766](https://github.com/IEZhu/Agents/pull/766)
regenerated the Repository Memory block of `CLAUDE.md`, which the first delta names, and
[#767](https://github.com/IEZhu/Agents/pull/767) carried this record and that delta. This
pass covers the other seven, each read against source and tests at that head. A pull
request description was read as a claim, never copied as evidence.

| Surface (pull requests) | Change in this pass | Source checked |
|---|---|---|
| [PR review flow](../../flows/pr-review.md) (#759; #765 changed what it cites) | Corrected: the overlap step said a `propose` answers `stale_generation` while any participant it binds is `suspect`. Since #765 a `propose` checks liveness only for the proposer and for a cancelled party, so the step now says that a `suspect` peer blocks neither the `propose` nor a consent it gave, must `recover` before it can acknowledge, and has an hour by default, a day at most, to do so. Verified unchanged: the `claim_lookup` and `claim_record` steps for the merge gating and the reviewer allowance key, and the claim states they name | `src/cooperation/service.py` (`apply`, `_check_bindings`, `_reconcile`), `src/cooperation/models.py` (`AgreementTerms`), `tests/test_cooperation_service.py` (`test_turn_based_sessions_commit_an_agreement_minutes_apart_across_a_recover`), `src/server.py` (`claim_record`, `claim_lookup`), [validated claims](../validated-claims.md) |
| [Cooperation runtime guide](../agent-cooperation/runtime.md), [protocol](../agent-cooperation/protocol.md), [`schemas.json`](../agent-cooperation/schemas.json) and the `cooperation_register` description (#765) | Verified unchanged: each rule the pull request documents, namely the acknowledgement window (3600 seconds, at most 86400), consent bound to the generation that gave it, liveness needed to execute only, a commit straight into `suspended` when a consenting party has gone quiet, and `stale_generation` naming the party. Corrected: the runtime guide said a born-suspended agreement's assignments exclude every other session, without the allowance case it describes above. Unverified: the protocol's 44-minute gap between one session's journal actions on 2026-11-26 is a field observation, not source | `src/cooperation/models.py`, `src/cooperation/service.py` (`_acknowledge`, `_acquire`, `_stale_assignment`, `_recover`), `tests/test_cooperation_models.py` (the stored schema, the window bounds), `tests/test_cooperation_service.py` (the six regression tests), `python -m src.engine.protocol_text --check` |
| [Test guide](../../tests/README.md), [quality checks](../quality-checks.md), [user sync guide](../user-sync.md) (#760) | Verified unchanged: the test guide's paragraph on four Windows jobs and two macOS jobs, the User sync row of the quality guide, which states scope and no job count, and the user sync guide's line that the Windows and macOS jobs set `AGENTS_TEST_REAL_SECRET_STORE` | `.github/workflows/user-sync.yml` (the matrix, the `-p tests.ci_shard` step), `tests/ci_shard.py` (`shard_settings`, `shard_for`) |
| [Measurement note](../verifier-accuracy-measurement.md), [flow](../../flows/verifier-accuracy.md), [stage 1 report](verifier-accuracy-stage1.md) (#763, #770) | Verified unchanged: the stage cut, the seed refusal, the `seed` field and the interim look (the note's paragraph on schedule and stages, the flow's steps 5 and 6), and the run lock taken before the load (the note's paragraph alone, which the flow does not mention). Corrected: the stage 1 report called the cut it ran with the current one; it now says what changed and links the note. Left open: nothing describes the new Verifier accuracy workflow, see below | `evals/verifier_accuracy/runner.py` (`stage`, `check_seed`, `first_half`, `load`, `Runner.run`), `evals/verifier_accuracy/control.py` (`RECORD_KEYS`, `exclusive`, `RunLock`), `evals/verifier_accuracy/builder.py` (`build`, `discard`), `evals/scripts/verifier_accuracy.py`, `tests/test_verifier_accuracy.py`, [#733](https://github.com/IEZhu/Agents/issues/733#issuecomment-7193468521) (the owner's decision of 2026-11-26) |
| [Map](../README.md#plans-research-and-recorded-results) and the records it lists (#764) | Verified unchanged: of the 50 targets that section links, 47 open with the title and a `**Status:**` line, 2 are the exempt generated reports and 1 is the maintained [agent cooperation reference](../agent-cooperation/README.md), which has none; none of the 47 tracked Markdown files under `docs/` that the section does not link has one. The statuses' claim that local inference and its Redis backend are disabled by default matches the code | a script over the section's links and `git ls-files docs`, `src/local_inference/config.py` (`OwnerConfig.enabled`, `RedisConfig.enabled`), `env.example` |
| `CLAUDE.md` repository notes and Repository Memory block (#764, #769) | Verified unchanged: the commit format, the branch names, the English-only rule and the status-line convention, which [AGENTS.md](../../AGENTS.md#make-changes), [CONTRIBUTING.md](../../CONTRIBUTING.md) and the map state the same way, and every catalog count of the block (46 personas, 93 skills, 57 implants, 8 always-on and 6 worker rules, 23 flows, 54 tools, 273 test files). Left open: the block counts 14 workflows and the tree has 15, see below | `git ls-files`, `src/server.py`, `TOOL_NAMES` of `src/artifacts/tools.py` and `src/local_inference/tools.py` |
| [Calendar releases](../releases.md#prepare-a-promotion) | Corrected: step 3 linked the pre-promotion section of the 2026-10-25 record, a historical record since 2026-11-26; it links this record's | the [status line of the 2026-10-25 record](documentation-audit-2026-10-25.md) |

Open at this head, in files that other branches change and this pass leaves alone:

- `tests/README.md` describes the Documentation and User sync workflows but not the
  Verifier accuracy one (`.github/workflows/verifier-accuracy.yml`), which runs
  `tests/test_verifier_accuracy.py` on macOS for a change under
  `evals/verifier_accuracy/`, `evals/scripts/verifier_accuracy.py`, that test file or
  the workflow itself.
- The Repository Memory block of `CLAUDE.md` counts 14 workflows; the tree has 15.
  Regenerate it with `describe_repo(force_refresh=True)` and `write_repo_summary`
  once the branches that change `CLAUDE.md` have merged.

This delta stops at `10e1e6a1`. [#773](https://github.com/IEZhu/Agents/pull/773) was an
open draft on 2026-11-28, and whatever else merges into `dev` after this head is outside
it: a final delta covers those pull requests and the two open items above before the
promotion head is frozen.

Checks of the second delta, run from the worktree root with Python 3.13 and `src`
resolving to the worktree: `scripts/check_docs.py` reported "Checked 198 Markdown files;
0 navigation/tool-index errors." at the base and after the edits (no document was added),
`python -m src.engine.protocol_text --check` listed nothing and `git diff --check`
printed nothing. Tests: `tests/test_check_docs.py` and `tests/test_flows.py` 51 passed;
`tests/test_flows_ui_page.py`, `tests/test_schedules_report.py` and
`tests/test_schedules_stages.py`, which also read the PR review flow, 323 passed;
`tests/test_cooperation_models.py`, `tests/test_cooperation_service.py` and
`tests/test_cooperation_instructions.py` 183 passed; `tests/test_verifier_accuracy.py`
115 passed; the nine files of the flow's routing-instructions row 431 passed and 61
skipped. The full suite and the native CI jobs were not run; this pass changed
documentation only.

## Before the promotion

- `VERSION` is `26.11.14.1328`, the version `main` was released at, and
  `python scripts/bump_plugin_version.py --check --base origin/main` answers
  that it must be greater than `main`'s (rechecked at `10e1e6a1`); the promotion
  needs the bump of [step 3](../releases.md#prepare-a-promotion).
- [#756](https://github.com/IEZhu/Agents/pull/756) was open when the review ran;
  it merged first, and the [first delta](#delta-for-756-and-762) re-checked the
  guides against the integrated tree.
- The [second delta](#delta-for-759-760-763-764-765-769-and-770) stops at `10e1e6a1`;
  what merges after it, [#773](https://github.com/IEZhu/Agents/pull/773) among it,
  and the two open items it lists need a final delta before the promotion head is
  frozen.
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
- The second delta cites two facts that are not in source as recorded: the owner's
  decision of 2026-11-26 to stop the verifier measurement after stage 1, read from its
  [issue comment](https://github.com/IEZhu/Agents/issues/733#issuecomment-7193468521), and the 44-minute gap
  between one session's journal actions that the cooperation protocol gives.
- The second delta's tests ran on macOS with Python 3.13. CI pins Python 3.11 in most
  workflows, adds 3.10 and 3.14 in the Windows installer workflow and 3.13 on Windows in
  Workspace bindings, and leaves the release, issue-agent and vulnerability scan
  workflows on the runner's own Python. It runs on Linux, Windows and macOS, among
  others the new macOS workflow and the seven-job User sync matrix (one Linux job, four
  Windows shards, two macOS shards). None of those was executed here.
