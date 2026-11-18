# Development reference

Source of truth for behavior: `.github/workflows/vulnerability-scan.yml`, `src/utils/debug_logger.py`, `src/engine/config.py`.

## Vulnerability gate

`Vulnerability scan` audits the complete committed `uv.lock` on every pull request,
merge queue, push to `dev`/`main` and manual run, and daily on the default branch,
`main`, once the workflow is there. Trivy 0.76.1 includes
development dependencies and every severity, including unknown and unfixed issues.
uv 0.11.25 first rejects drift between dependency declarations (including every
optional extra) and the lock, with source builds disabled. Missing or unparsed
lockfiles and scanner/database errors fail the job. The independent `requirements.txt`
installation input is also resolved universally for Python 3.11 and later, without
Python downloads, and its complete pinned dependency list is scanned alongside
`uv.lock`. That resolution takes published wheels only, except for a declared
dependency that publishes no usable wheel, whose source distribution it accepts
so the audit still reaches it and its transitive dependencies. The
report must include every expected package name and version from both inventories,
including conditional versions. This audit resolves the
requirements at scan time; it does not turn independent pip installs into locked installs.
No repository ignore policy is loaded. Release publication scans the exact merged
release commit again and depends on both inventories passing.

A failing scan blocks a merge into `dev` or `main` only when the repository rules
require the `Vulnerability scan` check; add that required check to enforce the
gate before a merge. Release publication waits for its own scan of the merge
commit either way. Fix dependencies rather than adding an automatic exception for
a vulnerability without an available fix. Revert the workflow change to roll back
this policy, updating any required-check rule at the same time.

## Debug Logging

Enable detailed per-call JSON logging when [running the server manually](install.md#generic-stdio):

```bash
AGENTS_DEBUG=1 python src/server.py
```

Standalone debug logs are written under the client project's `logs/`; HTTP
debug logs are written under the daemon's private `debug/` directory. Files use
`{YYYY-MM-DD}/{HH-MM-SS.fff}_{uuid}_{tool}_{direction}.json`. Logging is disabled
unless `AGENTS_DEBUG=1` (or `true`).

`route_and_load`, `get_agent_context`, and `refresh_persona_context` record paired
`req` and `res` snapshots. Their `data.call_id` identifies one invocation, including
concurrent calls and early errors, independently of the protocol's request ID.
The request stores all effective arguments, preserving supplied history and
persona inputs. The response's `data.response` is the exact JSON string returned
to the client, including all four instruction blocks, the descriptor, and footer
on `SUCCESS`. These snapshots show what the server returned; they do not prove
that the client applied the instructions.

Persona snapshots run outside the asyncio event-loop thread, in request/response
order. Diagnostic failures leave the protocol response unchanged.

Debug files can contain complete prompts, instructions, and supplied chat
history. Standalone logs are never pruned; `logs/` is ignored only in this
checkout, so exclude it in the client project or delete the files after
diagnosis. The daemon keeps its debug files for at most seven days and 100 MiB.
