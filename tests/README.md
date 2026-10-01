# Tests

The suite covers routing, the persona protocol, prompt assembly, memory, daemon
operations, installers and evaluation runners. Test discovery and default marker
selection are configured in [pyproject.toml](../pyproject.toml).

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

### Checks outside pytest

Agent metadata and the Node bridge have their own checks:

```bash
.venv/bin/python scripts/validate_agents.py  # agent frontmatter and metadata
node --test bridge/test.mjs                  # bridge/ changes; Node 22+, no npm install
```

The daemon smoke and soak tests (`scripts/daemon_smoke.py`) need the e5-large
model and start a temporary daemon. Run them alone in a dedicated checkout, as
described in [daemon validation](../docs/shared-mcp-daemon.md#validation).

## Choose tests by change

| Area | Starting points |
|---|---|
| Routing and intent | `test_routing.py`, `test_intent.py` |
| Protocol 2 and fresh bundles | `test_persona_protocol.py`, `test_persona_bundle.py` |
| Skills, implants and rules | `test_skill_freshness.py`, `test_implant_gating.py`, `test_rules.py`, `test_web_search_skill.py` |
| Installers: one-command install, version checks, client profiles, instructions and migration | `test_installer_oneliner.py` (`install.sh` and `init_repo.sh --yes`; Unix only), `test_installer_python.py`, `test_installer_windows.py`, `test_installer_profiles.py`, `test_install_instructions.py`, `test_installer_instructions.py`, `test_codex_instructions.py`, `test_protocol_migration.py`, `test_inject_mcp.py` |
| Agent frontmatter and metadata | `scripts/validate_agents.py` |
| Node bridge (`bridge/`) | `node --test bridge/test.mjs` |
| Repository memory | `test_describer.py`, `test_managed_section.py` (the repository-memory section editor), `test_server_describe.py`, `test_server_sandbox.py`, `test_history.py`, `test_per_repo_memory.py` |
| Installed workflows and caller targeting | `test_flows.py`, `test_server_flows.py`, `test_config_client_root.py`, `test_daemon.py` |
| Cloud issue-agent dispatch, startup acknowledgement and reactions | `test_issue_agent_bridge.py` (template and installed workflow, mocked APIs; no live sessions) |
| Personal and repository flows, flow editor | `test_user_flows.py` |
| Daemon and client configuration | `test_daemon*.py`, `test_config_client_root.py` |
| Updates and startup | `test_self_update.py`, `test_startup.py` |
| Data isolation and storage | `test_data_isolation.py`, `test_vector_store.py`, `test_file_lock.py` |
| Language detection | `test_language.py` |
| Evaluation runners and providers (`evals/runners/`) | `test_persona_dialogue*.py`, `test_bench.py`, `test_local_provider.py`, `test_openrouter_provider.py` |
| Evaluation scripts, statistics and telemetry | `test_ablation_harness.py`, `test_prompt_ab.py`, `test_local_ab.py`, `test_compare_rules.py`, `test_bench_significance.py`, `test_label_with_claude_alloc.py`, `test_telemetry.py` |

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
spaces without modifying the user's client files.
`test_installer_instructions.py` executes the actual template selection, skip guard
and Codex instruction hook from each installer in temporary directories. Unix
uses Bash; Windows uses native `cmd.exe`. These focused checks cover template
selection (a stale `AGENTS_PERSONA_PROTOCOL` value is ignored), repeated updates,
override precedence and errors without running dependency installation or
editing real client settings.

To reproduce the workflow job locally in PowerShell with Python 3.11+ selected:

```powershell
python -m pip install pytest python-dotenv
$env:AGENTS_TEST_PYTHON310 = 'C:\absolute\path\to\Python310\python.exe'
python -m pytest tests/test_installer_windows.py tests/test_installer_instructions.py tests/test_codex_instructions.py tests/test_install_instructions.py tests/test_installer_profiles.py tests/test_protocol_migration.py -v
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
that client-root tests set and aim memory and flows at the live project.

Tests that load retrievers may still initialize an embedding model during
collection. A fresh worktree has no copied `.env` or `data/`, so it may need a
cached model or a download even when slow tests are excluded. Do not copy secrets
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
