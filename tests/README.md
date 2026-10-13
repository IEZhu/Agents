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

The User sync workflow keeps the complete suite on Linux and macOS and splits
the same collection across four Windows jobs. Its opt-in `tests.ci_shard` plugin
assigns each node ID to exactly one shard; ordinary pytest runs stay unchanged.
To reproduce one part, set `AGENTS_TEST_SHARD_COUNT=4` and
`AGENTS_TEST_SHARD_INDEX=0` (indices 0 through 3), then add `-p tests.ci_shard`
to the workflow's pytest command. Invalid or incomplete settings fail before
collection. `test_ci_shard.py` checks complete, disjoint and stable coverage.

### Checks outside pytest

Agent metadata and the Node bridge have their own checks:

```bash
.venv/bin/python scripts/validate_agents.py  # agent frontmatter and metadata
node --test bridge/test.mjs                  # bridge/ changes; Node 22+, no npm install
```

The daemon smoke and soak tests (`scripts/daemon_smoke.py`) need the default
model's plain-file copy (`microsoft/harrier-oss-v1-270m`) and start a temporary daemon. Run them alone in a dedicated checkout, as
described in [daemon validation](../docs/shared-mcp-daemon.md#validation).

## Choose tests by change

| Area | Starting points |
|---|---|
| Routing and intent | `test_routing.py`, `test_intent.py` |
| Protocol 2, fresh bundles and the footer version | `test_persona_protocol.py`, `test_persona_bundle.py`, `test_version.py` |
| Compact worker profiles, bounded preparation and result validation | `test_subagent.py`, `test_subagent_mcp.py`; include `test_persona_protocol.py`, `test_log_interaction_contract.py` and installer/migration checks when shared instructions change |
| Skills, implants and rules, and their on/off switches | `test_skill_freshness.py`, `test_implant_gating.py`, `test_rules.py`, `test_web_search_skill.py`, `test_component_toggles.py` |
| Embedding model, its prompts, batching and the one-time model switch | `test_embedder.py`, `test_embedding_prompts.py`, `test_model_migration.py` (no model is loaded); the service's switch is in `test_daemon_update.py` |
| Installers: one-command install, version checks, client profiles, instructions, migration and the sync step | `test_installer_oneliner.py` (`install.sh` and `init_repo.sh --yes`; an update from a terminal that runs the real sync step and leaves the library's `.git` byte for byte; the GitHub token reaching `init_repo.sh` and never git; Unix only), `test_setup_cloud_env.py` (`scripts/setup_cloud_env.sh`; Unix only), `test_installer_python.py`, `test_installer_windows.py`, `test_installer_profiles.py`, `test_install_instructions.py`, `test_installer_instructions.py`, `test_codex_instructions.py`, `test_protocol_migration.py`, `test_inject_mcp.py`, `test_installer_sync.py` (both installers' sync section around a stub, the GitHub token only for the step; a pseudo-terminal on Unix, `cmd.exe` on Windows) |
| Agent frontmatter and metadata | `scripts/validate_agents.py` |
| Node bridge (`bridge/`) | `node --test bridge/test.mjs` |
| Repository memory and `log_interaction` | `test_describer.py`, `test_managed_section.py` (the repository-memory section editor), `test_server_describe.py`, `test_server_sandbox.py`, `test_history.py`, `test_read_history_size.py` (`read_history` listings under Claude Code's result limit, an entry read whole by its id), `test_per_repo_memory.py`, `test_log_interaction_async.py`, `test_log_interaction_contract.py` |
| Installed workflows and caller targeting | `test_flows.py`, `test_server_flows.py`, `test_flow_result_size.py` (the result size `run_flow` and `get_flow` declare), `test_config_client_root.py`, `test_daemon.py`, `test_thread_inventory.py` (the `thread-close` helper) |
| Cloud issue-agent dispatch, startup acknowledgement and its deletion, reactions and receipt collapse | `test_issue_agent_bridge.py` (template and installed workflow, mocked APIs; no live sessions) |
| Personal and repository flows, the `/ui` settings page and its sign-in | `test_user_flows.py`, `test_component_toggles.py` (switches and the Agents listing), `test_daemon_peer.py` (loopback owner lookup per OS), `test_flows_ui_page.py` (page script in Node through `flows_ui_page_harness.mjs`; skipped without `node`) |
| Git-backed local flow versions and migration | `test_library_history.py` (durable writes, recovery, legacy aliases and Git isolation), `test_user_flows.py` (MCP/editor read and restore contract), `test_user_sync_versions.py` (complete filtered history across machines); the User sync workflow runs the backend and sync suites on Linux, Windows and macOS |
| Existing library connection choices | `test_user_sync_connection.py` (whole-library comparison, explicit merge/replace, stale confirmation, private adoption recovery and publication boundaries), `test_sync_ui.py` (mode-bound preview/detail API and authentication), `test_user_sync_github_setup.py` and `test_flows_ui_page.py` (terminal and web wizards), `test_user_sync_install.py` (unattended setup requires a choice) |
| Sync of the personal library between machines | `test_user_sync.py` (two libraries as two machines against a local bare repository), `test_user_sync_github.py` and `test_user_sync_github_setup.py` (GitHub sign-in, API and the wizard against a fake GitHub), `test_user_sync_history.py` (each repository's history merged across machines), `test_user_sync_schedule.py` and `test_user_sync_trigger.py` (scheduled runs with a fake OS, the stdio trigger), `test_user_sync_install.py` (the installers' sync step and setup from `AGENTS_USER_SYNC_*`); the User sync workflow runs them on Linux, Windows and macOS. `test_daemon_user_sync.py` covers the daemon's sync loop, its admin endpoints and `user-sync` command with a fake engine (no git); `test_sync_ui.py` the web UI's `/ui/api/sync` routes (access checks, the queue, no secret in any answer) with a fake engine and a fake GitHub, and `test_flows_ui_page.py` the header chip, the Sync page and its wizard |
| The settings page's landing page: answer counts from history files, connected AI apps, `X-Agents-Client` and the overview | `test_usage_stats.py` (fixture histories, a fake clock and a test daemon; no model), `test_daemon_clients.py` (the header per client), `test_landing_overview.py` (repository links, sizes, configured apps), `test_flows_ui_page.py` (the page itself) |
| The settings page's History tab | `test_history_ui.py` (`/ui/api/history…`: access checks, registered workspaces and history files only, portions, search; a test daemon), `test_flows_ui_page.py` (the tab, portions on scroll, Contents by day, Source, files, search and links) |
| Daemon and client configuration | `test_daemon*.py`, `test_config_client_root.py` |
| The autodev runner (`src/schedules/`) | `test_schedules_tick.py` (slots, catch-up, `missed`, continuations and allowed hours on a fake clock), `test_schedules_gate.py` (every gate branch on GitHub responses in `fixtures/autodev_gate.json`, forged state comments ignored, and real responses of IEZhu/Agents recorded read-only in `fixtures/autodev_gate_recorded.json`), `test_schedules_github.py` (the read-only client and the keychain provider, both faked; redirects refused against a local HTTP server), `test_schedules_supervise.py` (no process of a run survives a normal exit, a timeout, a double fork or a stop signal to the supervisor; `caffeinate`; the run lease), `test_schedules_launchagent.py` (the scheduler job with a fake `launchctl`), `test_schedules_drift.py`, `test_schedules_repos.py` (the trusted repository, one-off session repositories, bundle refusals and quarantine, planted hooks and configuration, drift against the trusted `pinned`, environments with a fake builder; throwaway repositories under `tmp_path`, a local path as the remote), `test_schedules_runner.py` (the commands, configuration and run records; a killed run's socket removed after the sweep), `test_schedules_socket.py` (the runner socket on real sockets: placement away from session-writable paths, also by file identity against case-variant and `/System/Volumes/Data` aliases, the fallback for an over-long path, cleanup after a run, an exception, SIGTERM or SIGHUP and after a SIGKILL, the whole-request read timeout against a trickling writer, handler deadlines and one overall close wait, session-bound types, the narrow argument defaults, the request registry and the thin clients on the resolved base interpreter), `test_schedules_heavy.py` (the heavy-process lease: a second grant and a foreign `pytest` refused, also end to end through the real client and probe, refused when `ps` fails, the requester's ancestors excluded only up to the session root, no lease held after the socket closes during a grant, release when a client disconnects or dies, the `heavy` client's command and status); `test_daemon_autoupdate.py` covers the auto-update waiting for the run lease. No test reaches GitHub, the keychain or launchd |
| Updates, startup and readiness | `test_self_update.py`, `test_startup.py`, `test_readiness.py`, `test_startup_handshake.py` (slow; a fake `fastembed`, no model), `test_langfuse_compat.py` |
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
