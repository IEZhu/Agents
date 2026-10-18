# Documentation audit — 2026-10-18

This records a bounded source-based audit and refresh of Agents-Core at
`bea70c576d0fe0f07208049ec8520dd6fc0bc432` (`origin/dev` when fetched for this
run), on branch `codex/documentation-coverage-refresh`. It is evidence for this
revision, not a guarantee that future source or installed clients match it.
Repeat the [documentation refresh](../flows/documentation-refresh.md) for a later
revision. Use the [documentation map](README.md) for current reader entry points.

## Method and scope

The run inventoried tracked Markdown, discovery files, main-server tool
registrations, configuration/CLI entry points and test/CI references. Three
prepared workers inspected disjoint API, operations and autodev areas. Their
unchanged result envelopes passed validation against the original contracts;
the integrator also reviewed source evidence and diffs. Structural validation
alone was not treated as evidence of factual correctness.

CodeGraph was consulted before source exploration. TokenSave reported that `dev`
was untracked and its results fell back to `main`; current worktree files were
used for branch-specific evidence. No index was initialized. The live installation
was clean on `dev` at the initial check; all edits stayed in a linked worktree
created with `--no-track` from fetched `origin/dev`.

Dispositions below mean **corrected** (material drift fixed), **added** (a missing
reference or check supplied), **verified unchanged** (the stated bounded check
found no required edit), or **historical/unverified** (preserved with limits).
They are not percentages or a line-by-line certification of every guide.

## Coverage and corrections

| Reader task / surface | Maintained reference | Source and test evidence inspected | Disposition and result |
|---|---|---|---|
| Find a reference for every main-server tool | [MCP tool index](mcp-tools.md), [agent guide](agent-guide.md) | `src/server.py`, `src/artifacts/tools.py`, `src/artifacts/service.py`, `src/local_inference/tools.py`, `tests/test_tool_annotations.py` | Added a task-oriented index for all 48 registered tools at this revision; static inventory is checked against runtime registrations in a model-free test |
| Execute and repeat a documentation refresh | [Refresh flow](../flows/documentation-refresh.md), [workflow catalog](../flows/README.md) | Actual source layout, `src/flows.py`, `tests/test_flows.py`, contributor branch/worktree rules | Corrected the base example and live-checkout guidance; added bidirectional coverage, freshness checks, disjoint validated delegation and explicit completion evidence |
| Activate, keep, restore and refresh a persona | [Routing](routing_flow.md), [agent guide](agent-guide.md) | `src/server.py`, `src/schemas/protocol.py`, retained protocol 2 instructions | Corrected supporting guidance for artifact-backed work and optional completions; protocol templates and managed instructions were unchanged |
| Prepare a worker and accept its result | [Worker contract](subagent-contract.md), [model tiers](subagent-model-tiers.md), [session playbook](session-playbook.md) | `src/engine/subagent.py`, `src/engine/subagent_validation.py`, `src/engine/model_tiers.py`, `src/schemas/subagent.py`, `tests/test_model_tiers.py`, `tests/test_subagent_artifacts.py` | Corrected the missing `artifact_manifest` argument, preparation I/O claims, conditional worker rule/errors, launch guidance and floor-reason semantics; removed an unmeasured model-quality assertion |
| Supply and verify exact shared input | [Shared artifacts](shared-artifacts.md) | `src/artifacts/service.py`, `src/schemas/artifacts.py`, `tests/test_artifact_mcp.py`, `tests/test_subagent_artifacts.py` | Corrected response extraction (`data`), artifact versus manifest hashes, full required reads and the separate integrity check before acceptance |
| Register and coordinate overlapping work | [Cooperation overview](agent-cooperation/README.md), [runtime](agent-cooperation/runtime.md) | `src/cooperation/models.py`, `src/cooperation/service.py`, exported schemas | Added a closed-schema registration example and top-level response/error guidance; retained distinctions between delivery, processing, consent and validated acceptance |
| Configure installation, optional inference and updates | [Project README](../README.md), [daemon operations](shared-mcp-daemon.md), [release procedure](releases.md) | `src/daemon/bootstrap.py`, `src/local_inference/service.py`, `src/local_inference/config.py`, `src/local_inference/process.py`, `src/update_channel.py`, `src/version.py`, `scripts/release_version.py` | Corrected missing environment discovery and Windows service references; added lazy configuration/error lifetime, restart/disable steps, POSIX native limits and readiness versus capability status. Existing update/release procedure required no further edit in the inspected scope |
| Sync personal content and manage URL rules | [Library sync](user-sync.md), [user rules](user-rules.md) | `src/user_sync/__main__.py`, `src/user_sync/installer.py`, `src/user_sync/schedule.py`, `src/daemon/sync_loop.py`, `src/user_rules.py`, rule lifecycle tests | Corrected command/interpreter context, Windows UI support and standalone-schedule handoff. Existing user-rule lifecycle guidance was verified unchanged; missing focused test references were added |
| Describe a repository and persist its summary | [Cloud guidance](cloud-runs.md), [memory reference](../README.md#-repository-memory) | `src/memory/describer.py`, `src/server.py`, `tests/test_server_describe.py` | Corrected the stale claim that `describe_repo` writes `CLAUDE.md`; explicit persistence uses `write_repo_summary` and refuses installation/runtime source roots |
| Run and inspect autodev | [User guide](autodev.md), [runner](autodev-runner.md), [sequence](autodev-sequence.md), [workers](autodev-workers.md), [Codex](autodev-codex.md), [sandbox](autodev-sandbox.md), [guards](autodev-guards.md), [acceptance](autodev-acceptance.md) | `src/schedules/repos.py`, `preconditions.py`, `broker.py`, `drift.py`, `config.py`, `workers.py`, `worker_launch.py`, `sandbox.py`, `sequence.py`; relevant existing schedule tests | Corrected fixed-main versus update-channel boundaries, explicit delegate profiles versus generic tiers, stage/worker record names, integrated-worker wording, stage schema argv and scanner fixture semantics. Acceptance evidence now goes outside the live checkout; live acceptance remains pending |
| Choose checks and interpret CI | [Test guide](../tests/README.md) | `pyproject.toml`, `.github/workflows/user-sync.yml`, `shared-runtime.yml`, `release-version.yml`; named focused test files | Corrected User sync CI's selected collection versus the regular suite; added model-tier, URL-rule, readiness and release test selection, plus the offline documentation checker |
| Discover/install the server and plugin | [Discovery index](../llms.txt), [installation](../llms-install.md), [plugin](claude-plugin.md) | Public registrations, `plugins/agents-core/scripts/start-server.sh`, plugin manifest, artifact/cooperation/local-inference transport boundaries | Corrected discovery links and standalone versus shared-daemon capabilities; existing plugin launcher guidance was verified unchanged in the inspected scope |
| Use agents, rules and instruction-signing primitives | [Agent catalog](../agents/README.md), [rules](../rules/README.md), [signing core](instruction-signing-core.md), [intent classifier](intent-classifier.md) | `agents/*/system_prompt.mdc`, agent schema/validator, rule collection, current signing package boundaries and maintained-reference status notes | Agent metadata validation passed; bounded reference/status review found no required narrative change. No claim of runtime signing enforcement, full content review of every prompt or new intent evaluation |
| Preserve plans, evaluation records and legacy templates | [Documentation map](README.md), [legacy templates](../scripts/templates/legacy/README.md) | Tracked-document inventory, local navigation, existing status labels and language scan | Historical records and byte-matched migration templates preserved. No benchmark was rerun or old measurement changed; current external/provider claims remain unverified |

## Reproducible checks

Run from the task worktree using its project environment. If it has no `.venv`,
use an existing interpreter by absolute path and first confirm `import src`
resolves to that worktree, as described in the [test guide](../tests/README.md).

| Executed check | Result |
|---|---|
| `python -m pytest tests/test_check_docs.py tests/test_flows.py tests/test_tool_annotations.py tests/test_subagent_artifacts.py tests/test_server_describe.py -q -p no:cacheprovider` with tracing and bytecode writes disabled | Final combined run: 95 passed, including both integration and HTML-anchor regression checks |
| `python -m pytest tests/test_persona_protocol.py tests/test_persona_bundle.py tests/test_model_tiers.py tests/test_subagent_tiers.py -q -p no:cacheprovider` with the same isolation | 196 passed |
| `python scripts/validate_agents.py` | 45/45 agent metadata records valid |
| `python scripts/check_docs.py`, then `python scripts/check_docs.py docs/mcp-tools.md docs/documentation-audit-2026-10-18.md` | 128 tracked Markdown files plus both new Markdown documents: zero local navigation/tool-index errors |
| Worker-local schema/example checks | JSON and closed-schema examples, public argument coverage, tier-floor cases, source assertions and acceptance-shell syntax checked; these do not replace behavior tests |
| Flow source/check path inventory and English-prose scan | Referenced path/glob examples resolve; no long Cyrillic narrative found outside fenced examples. This scan supplements manual review rather than proving translation quality |

The offline checker parses Markdown instead of matching links inside code
examples, checks local files and heading/HTML anchors, and compares tool-index
rows with source registrations. Its regression fixtures exercise missing paths,
missing anchors, repository escapes, encoded names, duplicate headings, references,
images, fenced/inline examples, tool aliases and added/removed registrations.
An independent read-only review found a false positive for commented HTML anchors
and false negatives for valid unquoted/space-separated HTML attributes. The
integrator replaced the regex with `HTMLParser` and added regression fixtures;
the final checks below cover that correction.
It does not fetch URLs or check arbitrary HTML navigation.

## Limits and open behavior

- Autodev still pins and targets `main`, while this repository's ordinary
  contributions target `dev`. This run documented the mismatch; changing the
  runner/broker policy is product work outside the documentation refresh.
- The full regular suite, native Windows/client rendering, live Codex/Claude
  probes, real Redis/native inference, installer/update/restart procedures,
  scheduled runs and paid model evaluations were not executed. Owner acceptance
  remains pending in its original results table.
- External listing acceptance, remote URL availability and current provider/CLI
  behavior were not re-researched. Raw-main discovery destinations were checked
  against files in this development revision, not treated as proof of publication.
- A reference for every tool establishes navigability and inventory coverage;
  it does not certify every parameter, all runtime behavior or model quality.
- No runtime source or managed protocol template changed. No commit, push, PR,
  client configuration change or service operation was performed by this run.

## Final validation

The two final focused runs passed all 291 selected tests. All 48 main-server tool
names match the index and registered runtime, all 45 agent metadata records pass
validation, and local navigation passes for 130 Markdown files (128 tracked plus
the two explicit additions). `git diff --check` passes. The interpreter imports
`src` from this worktree, not the live installation.

The independent review covered every changed and new file and matched material
documentation claims to source. Its one HTML-anchor finding was fixed and
regression-tested by the integrator; no second independent review was claimed.
The live checkout remains clean on `dev`. Changes remain local and uncommitted
in the task worktree, with no PR or merge performed.

## Quality badge follow-up — 2026-10-18

At the owner's request, the README header now contains three GitHub workflow
status badges for `dev` push runs (Shared runtime, Instruction signing and the
new Documentation workflow), plus a source-derived **MCP API docs 48/48** badge.
The [quality guide](quality-checks.md) states each signal's scope and limitations.
The SVG counts references in this revision; the checker rejects stale values and
unknown/missing tool rows. Explicit `--update-badge` refreshes it in a development
worktree. CI has read-only permissions, uses no secrets, and never pushes changes.

Follow-up validation passed all 56 selected tests in `test_check_docs.py`,
`test_flows.py` and `test_workflow_permissions.py`, with offline model settings.
The checker passed for the original 128 tracked Markdown files and all three new
Markdown documents, including the quality guide (131 total). Static checks verified
workflow triggers, read-only permissions and the three GitHub badge targets;
`git diff --check` passed. The new workflow has not run on GitHub because these
changes remain local. These results supplement the earlier audit runs above.

## Combined documentation, quality and version submission

At the owner's request, the prepared documentation/quality changes and the
calendar-version changes were combined on `codex/docs-quality-calendar-versions`,
from the same fetched `origin/dev` baseline `bea70c576d0fe0f07208049ec8520dd6fc0bc432`.
Both patches applied cleanly. The original worktrees were preserved. The sections
above record their original audit and badge runs; their local-only statements
refer to those earlier stages.

The combined change also introduces the root `VERSION` as the shared UTC
`YY.MM.DD.HHMM` number for the runtime, Claude plugin, Python package metadata and
GitHub release tag. Preparation is idempotent against a pinned `main` baseline,
and promotion checks reject reused versions or a mismatched plugin manifest.
Python metadata reads `VERSION` dynamically and `uv.lock` keeps the editable
root package dynamic, without dependency changes. The publisher reads the
version and manifest at the exact merge SHA and refuses canonical tag collisions;
historical checkouts and release retries keep their documented fallback.
See the [release procedure](releases.md) for commands and compatibility details.

Combined focused validation passed **464 tests**, with one Windows-only inherited
stdin-handle test skipped on macOS. The selection includes the earlier audit
files, documentation/flow/workflow checks and the complete focused version,
preparation and publication regressions. The checker passes for **131 Markdown
files** and all **48 main-server tools**. These are local, offline checks; they
do not publish a release or establish provider delivery. Final remote CI and
bot-review results belong to the pull request's current head.

The first remote Documentation run exposed a cached-model dependency in the
existing explicit flow-bundle test. It initialized vector retrievers merely to
format declared components. The test now keeps the real source validation and
formatters without constructing retrievers, and fails if an embedding model is
initialized. The guard reproduced the original failure locally before the fix;
the focused workflow selection passes without a model cache.
