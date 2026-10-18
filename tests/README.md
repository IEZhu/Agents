# Tests

The suite covers routing, the persona protocol, prompt assembly, memory, daemon
operations, installers and evaluation runners. The `slow` marker and its default
exclusion are configured in [pyproject.toml](../pyproject.toml); pytest discovers
`tests/test_*.py` by its defaults.

## Environment

Use Python **3.11 or later**, as required by the package. From the repository root:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt -e '.[evals]'
```

`requirements.txt` includes pytest and pytest-asyncio; the package and `evals`
extra provide the runtime and evaluation dependencies used by the suite. Reuse
an existing environment when it already has those dependencies.

## Run checks

Run the regular suite, excluding tests marked `slow`:

```bash
LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/ -q
```

Run focused files while iterating:

```bash
LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/test_language.py -q
LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/test_install_instructions.py tests/test_installer_instructions.py tests/test_codex_instructions.py tests/test_protocol_migration.py -q
```

The default local-inference selection uses deterministic fake native HTTP/process
and Redis adapters. It never requires a GGUF, running Redis, optional redis-py or
cloud key; the real Redis module skips before optional imports:

```bash
PYTHONDONTWRITEBYTECODE=1 LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/test_local_inference_*.py -q -p no:cacheprovider
```

Process/adapter tests use private temporary Unix sockets, adversarial loopback
listeners and actual POSIX file-size limits. A sandbox must permit those bounded
listeners; a denied socket is an environment failure, not native acceptance. The large snapshot test streams a
sparse payload above 512 MiB with bounded chunks instead of retaining it whole.
Enabled version 1 is POSIX-only: Windows collection omits these native/capture
fixtures, while `test_optional_inference_disabled.py` checks portable disabled
status, transport refusal and platform rejection before native I/O.
For explicit real-server acceptance, provide a dedicated loopback endpoint through
`AGENTS_REDIS_LIVE_TEST_URL` and set `AGENTS_RUN_REDIS_LIVE_TESTS=1`; the optional
Redis extra must be available. Also set `AGENTS_RUN_REDIS_LIVE_LARGE=1` to select
the separately gated 512-MiB-plus-one-byte streaming roundtrip. These tests create
random installation namespaces, delete only their own keys, and never flush the
database. See [the Redis live report](../docs/shared-kv-mcp/redis-live-report.md).
Only one heavy test, embedding process or live model may run at once. For offline
regular-suite checks select an already cached `EMBEDDING_MODEL` and set
`HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`; do not download a replacement to make a
test pass. See [the live validation contract](../docs/shared-kv-mcp/validation.md)
and [actual implementation results](../docs/shared-kv-mcp/implementation-report.md).

Include slow tests, or select only slow tests:

```bash
LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/ -m '' -q
LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/ -m slow -q
```

The optional wrapper runs `tests/` with verbose output and forwards additional
pytest arguments. It selects `.venv`, then `venv`, then a pyenv interpreter:

```bash
LANGFUSE_TRACING_ENABLED=false ./scripts/run_tests.sh --tb=short
LANGFUSE_TRACING_ENABLED=false ./scripts/run_tests.sh -k russian
```

The wrapper runs the whole selected suite, not only language detection tests.
Use the direct Python command when you need to select specific test files.

The [Documentation workflow](../.github/workflows/documentation-quality.yml) runs
the offline documentation checker and `test_check_docs.py`, `test_flows.py` and
`test_workflow_permissions.py` for every pull request and pushes to `main`/`dev`.
It checks the source-derived README badge as well as navigation and public tool
references. It installs the runtime test dependencies for the registration and
flow checks, but starts no model. See [badge scope](../docs/quality-checks.md).

The [User sync workflow](../.github/workflows/user-sync.yml) runs its selected
library-history and user-sync files intact on Linux and macOS and splits that
collection across four Windows jobs. It does not run the full regular suite.
Its opt-in `tests.ci_shard` plugin
assigns each node ID to exactly one shard; ordinary pytest runs stay unchanged.
To reproduce one part, set `AGENTS_TEST_SHARD_COUNT=4` and
`AGENTS_TEST_SHARD_INDEX=0` (indices 0 through 3), then add `-p tests.ci_shard`
to the workflow's pytest command. Invalid or incomplete settings fail before
collection. `test_ci_shard.py` checks complete, disjoint and stable coverage.

### Checks outside pytest

Check local Markdown navigation and the public MCP tool inventory without
importing the server, loading a model or starting a service:

```bash
.venv/bin/python -B scripts/check_docs.py
.venv/bin/python -B scripts/check_docs.py README.md docs/mcp-tools.md
```

The default checks tracked `.md` files; explicit paths also cover new documents
before they are staged. The checker uses `markdown-it-py` from the project
environment, ignores inline/fenced examples, checks local paths and rendered
heading/HTML anchors, and compares `docs/mcp-tools.md` with source registrations
using AST reads. It does not fetch external URLs. `--skip-tool-index` selects
navigation only. `tests/test_check_docs.py` checks the parser, path boundaries,
anchors and tool additions/removals against temporary fixtures.

Agent metadata and the Node bridge have their own checks:

```bash
.venv/bin/python scripts/validate_agents.py  # agent frontmatter and metadata
node --test bridge/test.mjs                  # bridge/ changes; Node 22+, no npm install
```

For the shared calendar version, preparation helper and release gate, run
`python -m pytest tests/test_bump_plugin_version.py tests/test_release_version.py tests/test_version.py tests/test_workflow_permissions.py -q`.
These checks cover the canonical `VERSION`, matching plugin manifest, monotonic
preparation, runtime labels, strict tag collisions and the legacy no-`VERSION`
fallback. They use temporary Git repositories and fake APIs, never a live release.

The daemon smoke and soak tests (`scripts/daemon_smoke.py`) need the default
model's plain-file copy (`microsoft/harrier-oss-v1-270m`) and start a temporary daemon. Run them alone in a dedicated checkout, as
described in [daemon validation](../docs/shared-mcp-daemon.md#validation).

### Persistent artifact checks

The [artifact workflow](../docs/shared-artifacts.md) has focused checks using
temporary filesystem stores and fake boundary services, with no model or Redis
required:

```bash
PYTHONDONTWRITEBYTECODE=1 LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/test_artifact_store.py tests/test_artifact_service.py tests/test_artifact_mcp.py tests/test_subagent_artifacts.py -q -p no:cacheprovider
```

The [Shared runtime workflow](../.github/workflows/shared-runtime.yml) runs
persistent storage, service/MCP, prepared-artifact and portable disabled-inference
checks on Linux, macOS and Windows. Its POSIX jobs also run fake native/Redis,
runtime-identity and update-admission checks. It downloads no model and starts
no real Redis; native Windows inference remains unsupported in version 1.

These files cover durable identity and first-descriptor retention, workspace and
safe-capture boundaries, byte quotas, complete integrity checks, bounded UTF-8
reads, manifest expiry/release and immutable task-contract binding. Also run the
local-inference selection above when changing manifest hydration or the checks
before dispatch, publication and result retrieval. Shared instruction changes
need the focused installer/migration and persona checks listed below. These are
commands and coverage descriptions, not a recorded acceptance run; historical
KV reports do not establish artifact-layer test or deployment results.

## Choose tests by change

The optional bounded GitHub run observer is tested offline with scripted API
responses and a fake clock. Repeated unchanged states produce no intermediate
model output; timeout, identity changes, reruns, skipped/failed jobs and partial
pagination cannot pass. No live GitHub mutation or model is needed:

```bash
PYTHONDONTWRITEBYTECODE=1 LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/test_watch_github_run.py -q -p no:cacheprovider
```

| Area | Starting points |
|---|---|
| OMS/profile, exact verified bytes and protected inventory | `test_instruction_signing.py`, `test_instruction_inventory.py`; `test_instruction_signing_interop.py` uses the optional `signing-tests` extra with pinned model-signing 1.1.1; [API and commands](../docs/instruction-signing-core.md) |
| Routing and intent | `test_routing.py`, `test_intent.py` |
| Protocol 2, fresh bundles and the footer version | `test_persona_protocol.py`, `test_persona_bundle.py`, `test_version.py` |
| Compact worker profiles, bounded preparation and result validation | `test_subagent.py`, `test_subagent_mcp.py`; include `test_persona_protocol.py`, `test_log_interaction_contract.py` and installer/migration checks when shared instructions change |
| Delegated model tiers, catalogs, launch arguments and escalation | `test_model_tiers.py`, `test_subagent_tiers.py`, `test_subagent_tiers_mcp.py`; fake catalogs and prepared contracts cover complexity/floor resolution, overrides, client detection, execution integrity and escalation. These checks do not establish worker quality or token savings; [model-tier contract](../docs/subagent-model-tiers.md) |
| Persistent artifacts, manifests and artifact-backed workers | `test_artifact_store.py`, `test_artifact_service.py`, `test_artifact_mcp.py`, `test_subagent_artifacts.py`; include `test_local_inference_*.py` for exact hydration and manifest rechecks, and installer/migration plus persona checks for shared instruction changes; [workflow](../docs/shared-artifacts.md) |
| Cooperation mailbox, private notifications and request tracking | `test_cooperation_*.py`, including real initialized MCP clients, schema-1/2/3 migration, durable receipt replay, private cursors, revision races, deadline-only wakeups, lost-wakeup boundaries and cancelled/shutdown waits; include `test_tool_annotations.py`, `test_subagent_combined_budget.py` and installer/migration checks when shared guidance changes. The Shared runtime workflow runs mailbox, request, notification, schema, MCP, instruction and combined-budget checks on Linux, macOS and Windows. Tests use isolated stores and ASGI clients, never a live daemon or model; [runtime contract](../docs/agent-cooperation/runtime.md#request-tracking-and-event-waits) |
| Skills, implants and rules, and their on/off switches | `test_skill_freshness.py`, `test_implant_gating.py`, `test_rules.py`, `test_web_search_skill.py`, `test_component_toggles.py` |
| URL-subscribed user rules | `test_rule_sources.py` (HTTPS, DNS/address policy, auth and bounded fetches without external network), `test_user_rules.py` (cached lifecycle, revision conflicts and damaged snapshots), `test_user_rule_tools.py` (MCP lifecycle), `test_user_rule_integration.py` (persona/worker delivery, exact selections and UI conflicts); [operator guide](../docs/user-rules.md) |
| Embedding model, its prompts, batching and the one-time model switch | `test_embedder.py`, `test_embedding_prompts.py`, `test_model_migration.py` (no model is loaded); the service's switch is in `test_daemon_update.py` |
| Installers: one-command install, version checks, client profiles, instructions, migration and the sync step | `test_installer_oneliner.py` (`install.sh` and `init_repo.sh --yes`; an update from a terminal that runs the real sync step and leaves the library's `.git` byte for byte; the GitHub token reaching `init_repo.sh` and never git; `AGENTS_BRANCH` becoming the update branch of a new `.env`; Unix only), `test_setup_cloud_env.py` (`scripts/setup_cloud_env.sh`; Unix only), `test_installer_python.py`, `test_installer_windows.py`, `test_installer_profiles.py`, `test_install_instructions.py`, `test_installer_instructions.py`, `test_codex_instructions.py`, `test_protocol_migration.py`, `test_inject_mcp.py`, `test_installer_sync.py` (both installers' sync section around a stub, the GitHub token only for the step; a pseudo-terminal on Unix, `cmd.exe` on Windows) |
| Agent frontmatter and metadata | `scripts/validate_agents.py` |
| Node bridge (`bridge/`) | `node --test bridge/test.mjs` |
| Repository memory and `log_interaction` | `test_describer.py`, `test_managed_section.py` (the repository-memory section editor), `test_server_describe.py`, `test_server_sandbox.py`, `test_history.py`, `test_read_history_size.py` (`read_history` listings under Claude Code's result limit, an entry read whole by its id), `test_per_repo_memory.py`, `test_log_interaction_async.py`, `test_log_interaction_contract.py` |
| Installed workflows and caller targeting | `test_flows.py`, `test_server_flows.py`, `test_flow_result_size.py` (the result size `run_flow` and `get_flow` declare), `test_config_client_root.py`, `test_daemon.py`, `test_thread_inventory.py` (the `thread-close` helper), `test_pr_threads.py` (the `pr-review` thread helper with a faked `gh`: the default mode and the autodev mode, which refuses `--resolve-mine`, shows the owner's reviews and marks each thread and review as a trusted bot source or not) |
| Cloud issue-agent dispatch, startup acknowledgement and its deletion, reactions and receipt collapse | `test_issue_agent_bridge.py` (template and installed workflow, mocked APIs; no live sessions) |
| Personal and repository flows, the `/ui` settings page and its sign-in | `test_user_flows.py`, `test_component_toggles.py` (switches and the Agents listing), `test_daemon_peer.py` (loopback owner lookup per OS), `test_flows_ui_page.py` (page script in Node through `flows_ui_page_harness.mjs`; skipped without `node`) |
| Git-backed local flow versions and migration | `test_library_history.py` (durable writes, recovery, legacy aliases and Git isolation), `test_user_flows.py` (MCP/editor read and restore contract), `test_user_sync_versions.py` (complete filtered history across machines); the User sync workflow runs the backend and sync suites on Linux, Windows and macOS |
| Existing library connection choices | `test_user_sync_connection.py` (whole-library comparison, explicit merge/replace, stale confirmation, private adoption recovery and publication boundaries), `test_sync_ui.py` (mode-bound preview/detail API and authentication), `test_user_sync_github_setup.py` and `test_flows_ui_page.py` (terminal and web wizards), `test_user_sync_install.py` (unattended setup requires a choice) |
| Sync of the personal library between machines | `test_user_sync.py` (two libraries as two machines against a local bare repository), `test_user_sync_github.py` and `test_user_sync_github_setup.py` (GitHub sign-in, API and the wizard against a fake GitHub), `test_user_sync_history.py` (each repository's history merged across machines), `test_user_sync_schedule.py` and `test_user_sync_trigger.py` (scheduled runs with a fake OS, the stdio trigger), `test_user_sync_install.py` (the installers' sync step and setup from `AGENTS_USER_SYNC_*`); the User sync workflow runs them on Linux, Windows and macOS. `test_daemon_user_sync.py` covers the daemon's sync loop, its admin endpoints and `user-sync` command with a fake engine (no git); `test_sync_ui.py` the web UI's `/ui/api/sync` routes (access checks, the queue, no secret in any answer) with a fake engine and a fake GitHub, and `test_flows_ui_page.py` the header chip, the Sync page and its wizard |
| The settings page's landing page: answer counts from history files, connected AI apps, `X-Agents-Client` and the overview | `test_usage_stats.py` (fixture histories, a fake clock and a test daemon; no model), `test_daemon_clients.py` (the header per client), `test_landing_overview.py` (repository links, sizes, configured apps), `test_updates_ui.py` (the Updates section's data and route), `test_flows_ui_page.py` (the page itself) |
| The settings page's Autodev page (#348) | `test_autodev_ui.py` (`/ui/api/autodev…`: access checks, the defaults, saves through the config module with the file's content, mode and no partial file after a failed write, every refusal leaving the file unchanged, a changed or invalid file, the switch: `run` refused without `accept_shared_account`, without a passing `autodev ready` recorded in the last 15 minutes or after a trip, and turned on with the acceptance once both hold (#350), the turn-off withdrawing it, Codex's stage availability from the stored check, the status with the recorded result and the missing items and without secrets; a test daemon, a temporary state directory, a fake command runner and broker credentials, a GitHub client and a key reader that fail the test when used (the daemon reads no credential), so no CLI, model, scheduler job, keychain read or network call happens), `test_flows_ui_page.py` (the page: the defaults, Codex unavailable with the reason, the recorded `autodev ready`, `run` offered only on a recent passing one and turned on after the acceptance question, a save, a refusal, the switch), `test_schedules_codex.py` (the configured stage availability) |
| The settings page's History tab | `test_history_ui.py` (`/ui/api/history…`: access checks, registered workspaces and history files only, portions, search; a test daemon), `test_flows_ui_page.py` (the tab, portions on scroll, Contents by day, Source, files, search and links) |
| Daemon and client configuration | `test_daemon*.py`, `test_config_client_root.py` |
| The autodev runner (`src/schedules/`) | `test_schedules_tick.py` (slots, catch-up, `missed`, continuations and allowed hours on a fake clock), `test_schedules_gate.py` (every gate branch on GitHub responses in `fixtures/autodev_gate.json`, forged state comments ignored, and real responses of IEZhu/Agents recorded read-only in `fixtures/autodev_gate_recorded.json`), `test_schedules_github.py` (the read-only client and the keychain provider, both faked; redirects refused against a local HTTP server), `test_schedules_supervise.py` (no process of a run survives a normal exit, a timeout, a double fork or a stop signal to the supervisor; `caffeinate`; the run lease), `test_schedules_launchagent.py` (the scheduler job with a fake `launchctl`), `test_schedules_drift.py` (content compared with the trusted repository's tree, unaffected by index flags, ignore rules or a `pinned` the installation lacks), `test_schedules_repos.py` (the trusted repository, one-off session repositories, bundle refusals and quarantine refs and files with their pruning, the commit cap, an oversized symbolic link target refused unread, the import against the session's base after main moved, fast-forward-only promotion, planted hooks and configuration, drift against the trusted `pinned`, environments with a fake builder: only commits of main, no symbolic links, built in a sibling and renamed; throwaway repositories under `tmp_path`, a local path as the remote), `test_schedules_runner.py` (the commands, configuration and run records; a killed run's socket removed after the sweep), `test_schedules_automatic.py` (the automatic mode of #350: `autodev ready` naming each missing condition, among them each protection value, credential, a missing, failed or future probe record per mode and kind (an old passing one still counts), a Codex select's Claude fallback probes and a missing, outdated, withdrawn, future-dated or stale acceptance (a trip by count or date, an unreadable `tripwire.json`); the recorded result for the page; `enable --mode run` with and without `--accept-shared-account`, a trip during its checks, the withdrawal by `disable` and `dry-run`; the tick's refusal of a scheduled run and continuation and the child's check after a trip; the schedule-off notification's fixed phrases; the risk list pinned to the plan; synthetic GitHub answers in `fixtures/autodev_preconditions_synthetic.json` and stored probe records only), `test_schedules_socket.py` (the runner socket on real sockets: placement away from session-writable paths, also by file identity against case-variant and `/System/Volumes/Data` aliases, the fallback for an over-long path, cleanup after a run, an exception, SIGTERM or SIGHUP and after a SIGKILL, the whole-request read timeout against a trickling writer, handler deadlines and one overall close wait, session-bound types, the narrow argument defaults, the request registry and the thin clients on the resolved base interpreter), `test_schedules_workers.py` (the worker launcher with fake `claude` and `codex` workers and a throwaway trusted repository: argv, environment and stdin of both worker types, a wrong `worker_context` length, `valid: false`, `partial`, `failed` and `input_required`, evidence naming another head, session refs that try to inject a git option, `--end-of-options` on every git call, an outside symbolic link and a gitlink in the materials, a decoy read and a decoy marker in the output, the follow-up effort, the restricted last review, the pre-mortem and the verification review, the records read back by the broker, the Codex schema derivation and the fallback to Claude, the thin client end to end), `test_schedules_heavy.py` (the heavy-process lease: a second grant and a foreign `pytest` refused, also end to end through the real client and probe, refused when `ps` fails, the requester's ancestors excluded only up to the session root, no lease held after the socket closes during a grant, release when a client disconnects or dies, the `heavy` client's command and status); `test_daemon_autoupdate.py` covers the auto-update waiting for the run lease. No test reaches GitHub, the keychain or launchd |
| The autodev run sequence (#347) | `test_schedules_sequence.py` (whole runs with the fake `claude`'s stage, probe and worker modes, synthetic GitHub answers in `fixtures/autodev_select_synthetic.json` and `fixtures/autodev_preconditions_synthetic.json`, a fake broker transport and push runner and a throwaway trusted repository: the dry-run printout with an empty broker log, select, implement and submit with the exact broker operations, each missing precondition, the control-path patch and bundle, a changed `.git`, invalid outputs, the checks after implement, the decoy tripwire, a failed probe, the stage timeout, resume with a foreign head, a new review before the push and the merge base, the Codex select stage and the Codex fallbacks; macOS only), `test_schedules_candidates.py` (the GET-only candidate fetch), `test_flow_execution.py` (the effort table from a stored `claude --help` excerpt), `test_schedules_stages.py` (the stage schemas pinned to the flows, the models, prompts, inputs files and the stream parser), `test_schedules_preconditions.py` (each missing item of a real run), `test_schedules_run_state.py` (`state.json` and its read-only copy), `test_schedules_report.py` (the report and the notification's arguments), `test_schedules_broker_sequence.py` (the broker's runner-internal operations), `test_schedules_repos_sequence.py` (the session repository with the branch, the export and the quarantine), `test_schedules_integrity.py` (the post-session `.git` check) and `test_schedules_workers_sequence.py` (one worker service across stages, open security risks and risk candidates). No test starts a model, reaches GitHub or reads the keychain; the owner's live steps are listed in docs/autodev-sequence.md |
| The update channel (#381): the branch an installation follows | `test_update_channel.py` (the `.env` key read without the rest of `.env`, an exported override, `set_branch`'s refusals and records, `channel` and `python -m src.update_channel`), `test_self_update.py`, `test_daemon_update.py` and `test_daemon_autoupdate.py` (switching inside the update transactions: only from the previous setting, never for an exported branch, a release merge commit is no downgrade, a target branch in another worktree, a channel saved during the build, the activation gates and the rollback of the target branch's upstream), `test_env_file.py` (the `.env` bindings both writers change, checked against python-dotenv's parser, and the installers' branch helper; also run by the Windows installer job), `test_updates_ui.py` (`/ui/api/updates`: access checks, the channel without secrets, a save of `dev`, a stale revision, unsafe, unoffered and missing branches, a downgrade only with its confirmation, a busy `control.lock`, `set_branch` off the event loop), `test_flows_ui_page.py` (the overview's Updates section: a save, a confirmed and a declined downgrade, a conflict), `test_installer_oneliner.py` (`AGENTS_BRANCH` through `install.sh` and the real `.env` step of `init_repo.sh`, and `scripts/_helpers/env_branch.py`, which `init_repo.bat` calls too). Temporary git repositories and a local bare remote; nothing is updated |
| Updates, startup and readiness | `test_self_update.py`, `test_startup.py`, `test_readiness.py`, `test_startup_handshake.py` (slow; a fake `fastembed`, no model), `test_langfuse_compat.py` |
| Optional session readiness hooks and compaction reminders | `test_session_readiness.py` covers generated adapters, event/output shapes, copied runtimes, backups, uninstall and a real stdio helper handshake. It calls no model and does not establish interactive client rendering; [installation and limits](../docs/session-readiness.md) |
| Calendar versions and release publication | `test_release_version.py`, `test_version.py`, `test_workflow_permissions.py`; temporary Git histories and fake APIs cover displayed versions, immutable tags, retries and publication permissions, without writing to GitHub; [release procedure](../docs/releases.md) |
| Data isolation and storage | `test_data_isolation.py`, `test_vector_store.py`, `test_file_lock.py` |
| Language detection | `test_language.py` |
| Evaluation runners and providers (`evals/runners/`) | `test_persona_dialogue*.py`, `test_bench.py`, `test_local_provider.py`, `test_openrouter_provider.py`, `test_eval_cache_routing.py`, `test_eval_retrieval.py` |
| Evaluation scripts, statistics and telemetry | `test_ablation_harness.py`, `test_ablation_compare.py`, `test_ablation_hosted.py`, `test_prompt_ab.py`, `test_local_ab.py`, `test_compare_rules.py`, `test_bench_significance.py`, `test_label_with_claude_alloc.py`, `test_telemetry.py` |

Patterns in this table name groups of files. See the directory for the full list.
Do not treat an old test count or duration as an expected result; pytest reports
the selected, passed, skipped and deselected tests for each run.

## Native Windows installer checks

The [Windows installer workflow](../.github/workflows/windows-installer.yml) runs
`test_installer_windows.py` on Windows with real Python 3.10 and 3.11 environments.
It launches the unchanged batch installer through `cmd.exe`, including its version
probes and `set /p` prompt. Explicit `N` and empty Enter must reject an existing
Python 3.10 venv or a broken venv whose base interpreter is missing, preserve the
environment, and exit before activation. A supported venv reaches an activation
sentinel that exits with code 77. This checks the reuse gate; it does not complete
dependency installation, download a model, or change MCP client settings.

The same workflow runs Codex discovery and managed-instruction migration checks.
`test_installer_profiles.py` also runs the actual MCP setup hooks against default
and alternate client profiles, using native `cmd.exe` on Windows and Bash on Unix.
It checks exact configuration targets, preserved inactive profiles, and paths with
spaces without modifying the user's client files. Backup regressions freeze the
clock, preserve pre-existing and repeated snapshots, and reject updates when backup
creation fails. `test_backup_file.py` checks the shared backup helper and partial-copy
cleanup without changing live configuration. Unix-specific cases also repeat
malformed-config failures (including empty files, BOM-only files, malformed JSON
with a UTF-8 BOM, and invalid UTF-8) and require exit code 1 while preserving the
original bytes and every backup. Repeated Claude Code permission updates also
preserve each snapshot byte for byte.
`test_installer_sync.py` runs each installer's token handling, argument parsing,
sync section and summary line around a stub of `python -m src.user_sync`: `--yes`
and `AGENTS_ASSUME_YES` reach the step, which then never asks; stdin from NUL is no
console; the GitHub token reaches no child on Windows and only the step on Unix
(fake `pip`, `git` and `python` report it); and a failing step never fails setup.
On Unix it also runs the real step in a pseudo-terminal, in a copy of the
installation.
`test_installer_instructions.py` executes the actual template selection, skip guard
and Codex instruction hook from each installer in temporary directories. Unix
uses Bash; Windows uses native `cmd.exe`. These focused checks cover template
selection (a stale `AGENTS_PERSONA_PROTOCOL` value is ignored), repeated updates,
override precedence and errors without running dependency installation or
editing real client settings.
`test_daemon_peer.py` (standard library only) checks the flow editor's loopback
owner lookup on a real connection from a child process: `GetExtendedTcpTable` and
the process token on Windows, `/proc/net/tcp` on Linux, `lsof` on macOS. Because
the hosted runner is elevated, a second step runs `scripts/dev/peer_check_probe.py`
as a temporary standard (non-administrator) local user.

To reproduce the workflow job locally in PowerShell with Python 3.11+ selected:

```powershell
python -m pip install pytest python-dotenv
$env:AGENTS_TEST_PYTHON310 = 'C:\absolute\path\to\Python310\python.exe'
python -m pytest tests/test_installer_windows.py tests/test_installer_instructions.py tests/test_codex_instructions.py tests/test_install_instructions.py tests/test_installer_profiles.py tests/test_installer_sync.py tests/test_protocol_migration.py tests/test_daemon_peer.py -v
```

The workflow sets `core.autocrlf false` before checkout so the byte-exact
migration checks see the committed line endings; use a checkout made with the
same setting.

`test_installer_windows.py` skips on macOS/Linux; there the profile and
instruction hook tests run their Bash variants and skip the `cmd.exe` cases. On
Windows, the Python 3.10 cases skip when the variable is absent locally and fail
if it is absent in CI. The workflow supplies the actual interpreter path from
`setup-python`.

## Isolation and resource use

[conftest.py](conftest.py) redirects derived stores and update state into a
temporary directory before test collection. It seeds available skill/implant
stores from this checkout and pins the embedding model from the environment,
or from this checkout's `.env` when the environment has no override. It also
redirects `AGENTS_ROUTER_DATA_DIR` away from live router state and removes an
inherited `CLAUDE_PROJECT_DIR`, which Claude Code exports to the servers and
hooks it starts. Otherwise that variable would outrank the working directory
that client-root tests set and aim memory and flows at the live project. It also
removes `CLAUDE_CONFIG_DIR`, `CODEX_HOME`, `AGENTS_CURSOR_MCP_CONFIG`,
`AGENTS_CLAUDE_DESKTOP_CONFIG` and `AGENTS_ANTIGRAVITY_MCP_CONFIG`, which would
otherwise outrank the temporary home of installer and migration tests and make
them write the user's real client configuration. Tests that need one of these
variables set it themselves. It also points `AGENTS_SERVICE_DIR` to a temporary directory,
and the autouse `service_state_dir` fixture gives every test its own empty one.
Without it, a test that reads or writes the service state (`src.daemon.state.state_dir`):
the daemon's files, the autodev configuration, run records and the run lease, would
use the live installation's directory under `~/Library/Application Support`. A test
that needs the default location removes the variable with `monkeypatch.delenv`.
The autouse `no_real_notifications` fixture fails a test that would let the autodev
runner's notification (`src.schedules.report.notify`) run the real `osascript` on macOS:
a test context passes a fake `notify_runner`.

Tests that build the skill or implant retriever, such as `test_data_isolation.py`,
may still load an embedding model and re-embed the temporary stores, even when slow
tests are excluded. A fresh worktree has no copied `.env` or `data/`, so it may need a
cached model or a download even when slow tests are excluded: the engine default,
`microsoft/harrier-oss-v1-270m`, is about 1.1 GB. To use a model that is already
cached, export it, for example
`EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`. Do not copy secrets
or symlink the live `data/` directory just to make a documentation check run.
Run one heavy test or embedding process at a time; start with focused checks.

When reusing another checkout's interpreter, run from the worktree root and use
its absolute interpreter path. Confirm imports resolve to the worktree:

```bash
/absolute/path/to/environment/bin/python -c 'import src; print(src.__file__)'
```

## Add or update tests

Use `test_*.py`, pytest fixtures and temporary paths for mutable state. Check the
observable contract rather than duplicating implementation details. Run the
relevant files first and broaden to the regular suite when the change warrants it.

For a documentation refresh, follow the focused validation guidance in
[the workflow](../flows/documentation-refresh.md).
