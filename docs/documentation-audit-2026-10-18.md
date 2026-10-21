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

## Parallel feature integration

At the owner's request, the initial documentation follow-up at `f6aeb253984d`
included guides from two neighboring implementation PRs. Neither implementation
was present in that revision. The initial pinned snapshot is recorded below;
the integration updates below distinguish those historical snapshots from
current branch behavior. Both feature implementations are now integrated in
`dev`; no `main` publication is claimed.

| Feature | Source inspected | Documentation and release boundaries |
|---|---|---|
| Client-aware emoji status | [PR #452 at 8efc4e0cf39e](https://github.com/IEZhu/Agents/tree/8efc4e0cf39ecca8daad0d7aace75a4f12cf9e2b) | [Thread status](thread-status.md): ten states including confirmed interruption, final human handoff semantics, client support, copied hook installation/removal, Claude SDK metadata and Codex Interrupt adapter. Tests use temporary profiles/fake metadata APIs; persistence does not establish live sidebar refresh, client trust or Windows execution |
| HTTP MCP proxy and per-host inventory | [PR #449 at d067c315caf3](https://github.com/IEZhu/Agents/tree/d067c315caf3c9877896315b96e68d4bb4a25c11) | [HTTP proxy](mcp-proxy.md): connection UI/CLI, cached app-scoped tools, exact-revision local consent, native-only credential storage, shared definitions and informational host inventory. Library metadata is ordinary private-repository JSON; credentials and execution approval remain local. This is the HTTP slice, not all of epic #306 |

The proxy guide incorporates the implementation originally developed in
[PR #445](https://github.com/IEZhu/Agents/pull/445); #449 consolidates it, so #445
is not an additional release prerequisite. At that pinned snapshot, the native
API-reference badge counted 48 registered tools; dynamic upstream aliases and
title hooks are outside that metric. The historical proxy plan links to the
implemented slice while preserving the remaining proposal.

Two prepared read-only documentation audits checked these pinned feature
sources and test assertions; both returned envelopes passed their original
contract validators, followed by the integrator's source assessment. The proxy
guide now explicitly requires **Connect** after confirming a changed revision
to refresh its catalog before reconnecting clients. Neighbor implementation
tests were inspected rather than rerun. The follow-up documentation selection
passed 59 tests; the offline checker passed for 133 Markdown files and the
unchanged 48/48 native-tool index. Diff whitespace validation passed.

Before the planned `dev → main` promotion:

1. Complete review of [this documentation/version PR #451](https://github.com/IEZhu/Agents/pull/451) and include its accepted changes in `dev`. PRs #449 and #452 are integrated as recorded below; the documentation PR remains open for owner approval.
2. Re-fetch the target branch and compare any newer source with the integrated revisions below. Reconcile overlapping README, documentation index, proxy plan, daemon/sync guides and optional dependency metadata. Keep the canonical dynamic `VERSION` source and include any resulting lock-file changes.
3. Verify the final integrated CLI/help, status vocabulary, client support and sync allowlist against source/tests. The proxy guide is now a maintained reference and its pending banner has been removed against integrated source. A copied guide alone does not establish live-client acceptance.
4. Run the integrated documentation, release-version and affected status/proxy/sync checks. Record platform/live-client limitations and any owner acceptance still required. Do not reuse an older feature PR's CI result as a check of the final release SHA.
5. Prepare or validate one canonical version against fetched `origin/main` after integration, then follow the [release procedure](releases.md#prepare-a-promotion), including the owner's decision and verification of the resulting tag and release. This documentation work does not publish or merge a release.

### Integration update after PR #452

PR #452 merged into `dev` at
[`195156ad1dd0`](https://github.com/IEZhu/Agents/commit/195156ad1dd0f95857173e82f3cc6af9aa7e371f)
on 2026-10-18. This branch incorporates that target update and preserves its
final thread-status guide, including native Windows Codex launcher handling,
the precise prompt-start preservation rule and the three-platform test workflow.
Thread status is now a maintained reference rather than a pending feature.
The proxy implementation was still outside this branch at that integration
snapshot; the subsequent #449 update below supersedes its pending notice.
The optional Claude SDK dependency is reconciled with this
PR's dynamic canonical version metadata: regenerating `uv.lock` adds the
`thread-status` extra and its pinned `claude-agent-sdk==0.2.168` package without
changing existing dependency versions or restoring a static root version.

The focused integration selection passed 539 tests with 9 skips. It covers
documentation, flows, registrations and permissions, calendar preparation and
publication, status/installer/metadata adapters, rules and prepared workers.
The offline documentation check passed for 133 Markdown files and the unchanged
48/48 native-tool index. `uv lock --check --offline`, canonical version
`--check --base origin/main` and whitespace validation passed. An additional
prepared independent review of the pinned integration inputs identified the
missing SDK lock metadata and status-guide reconciliation above; its unchanged
result passed the original contract validator. These checks do not establish
live client rendering or release publication.

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


### Integration update after PR #449

The final documentation preparation combines preserved candidate
[`d1d1cefb273d`](https://github.com/IEZhu/Agents/commit/d1d1cefb273d893fc2e8478cf32dd2833ab90a09)
with `origin/dev` at
[`8a789eb79d96`](https://github.com/IEZhu/Agents/commit/8a789eb79d964d1013008e3801907e6a7a7ae480).
That target contains the #449 merge
[`d963b3dbac4d`](https://github.com/IEZhu/Agents/commit/d963b3dbac4d955a3511d4f204fb9cd91d09addd).
The [proxy guide](mcp-proxy.md) now describes the integrated Streamable HTTP and
shared stdio implementation. Discovery links, daemon/sync guidance and the
historical plan no longer present #449 as pending. This update preserves the
calendar-version implementation and both sides' bounded worker/model-tier guidance.

The current source checks are `src/mcp_registry.py`,
`src/daemon/mcp_cli.py`, `src/daemon/mcp_proxy/upstreams.py` and
`src/daemon/mcp_proxy/exposure.py`. They establish absolute stdio executable
paths, nonsecret arguments/environment metadata, exact-revision app consent,
explicit catalog refresh and reused serialized sessions. The daemon owns shared
upstreams; the Plata compatibility entry is a forwarding bridge and must not
launch a second native server. Credentials remain in their existing local store,
and `ENABLE_WRITE_TOOLS=false` remains the documented default. No client
configuration, service or credential migration was executed for this refresh.

Recomputing the AST inventory against this integrated tree still finds **48
native registered tools and 48 indexed references**, with no missing or unknown
rows. Explicit badge regeneration produces the same **48/48** SVG; dynamic
HTTP/stdio aliases remain excluded. The offline navigation checker passes for
**133 Markdown files**, with zero navigation/tool-index errors. These are fresh
static results for the combined tree, not reuse of the earlier counts above.

The full regular suite, affected behavior suites, final-head remote CI and
substantive bot reviews remain the coordinator's subsequent checks. This
preparation does not push, request bot reviews, merge a PR or publish a release.
