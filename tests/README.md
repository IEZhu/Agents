# Tests

The suite covers routing, persona protocols, prompt assembly, memory, daemon
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
LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/test_protocol_migration.py tests/test_managed_section.py -q
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

## Choose tests by change

| Area | Starting points |
|---|---|
| Routing and intent | `test_routing.py`, `test_intent.py` |
| Protocol 2 and fresh bundles | `test_persona_protocol.py`, `test_persona_bundle.py` |
| Skills, implants and rules | `test_skill_freshness.py`, `test_implant_gating.py`, `test_rules.py` |
| Installer version checks, instructions and migration | `test_installer_python.py`, `test_protocol_migration.py`, `test_managed_section.py`, `test_inject_mcp.py` |
| Repository memory | `test_describer.py`, `test_server_describe.py`, `test_history.py`, `test_per_repo_memory.py` |
| Daemon and client configuration | `test_daemon*.py`, `test_config_client_root.py` |
| Updates and startup | `test_self_update.py`, `test_startup.py` |
| Data isolation and storage | `test_data_isolation.py`, `test_vector_store.py`, `test_file_lock.py` |
| Language detection | `test_language.py` |
| Evaluation runners | `test_persona_dialogue*.py`, `test_ablation_harness.py`, `test_prompt_ab.py`, `test_telemetry.py` |

Patterns in this table name groups of files. See the directory for the full list.
Do not treat an old test count or duration as an expected result; pytest reports
the selected, passed, skipped and deselected tests for each run.

## Isolation and resource use

[conftest.py](conftest.py) redirects derived stores and update state into a
temporary directory before test collection. It seeds available skill/implant
stores from this checkout and pins the embedding model from the environment,
or from this checkout's `.env` when the environment has no override. It also
redirects `AGENTS_ROUTER_DATA_DIR` away from live router state.

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
