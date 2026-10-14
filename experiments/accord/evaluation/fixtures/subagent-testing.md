### Focused subagent contract checks

For changes to compact worker preparation or result validation, run both the
core and MCP-facing checks from the repository root:

```bash
LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest tests/test_subagent.py tests/test_subagent_mcp.py -q
```

The core checks cover context budgets, revision binding, result shapes and
invalid inputs. The MCP checks exercise registered schemas and strict argument
handling through the tool interface. See the
[subagent contract](../docs/subagent-contract.md) for the supported exchange.

A passing run verifies these implementation checks. A `valid: true` worker
result still means `structural_only`; it does not prove the worker's claims,
rerun its tests or authorize an external action. When shared delegation
instructions change, also run the persona, logging and installer/migration
checks listed in the coverage table below.
