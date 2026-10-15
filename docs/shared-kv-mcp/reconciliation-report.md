# Shared-KV source reconciliation

Recorded on 2026-10-15 against integrated `dev` revision
`d503b39d01f69ac92e4cc44c3bbf5e1621260b7e`.

## Scope and result

The original `codex/shared-kv-mcp` checkout retained 36 modified tracked files,
45 untracked source/document/test files and inherited handoff commit
`0268879f3a2909137fc3b87982ede198b090f6d5`. An uncommitted file is not necessarily
unintegrated: compare its bytes and behavior with the target before importing it.

The audit covered all 81 dirty/untracked paths and four additional distinct
paths from the inherited handoff commit: 85 paths in total. All exist in the
compared `dev` revision. Of these, 53 match the legacy working files byte for
byte; the remaining 32 are superseded by later integrated changes. No missing
production behavior, test, tooling, policy, documentation or example-config
content was identified that requires restoring a legacy file.

The local-inference implementation and durable artifacts are preserved through
[PR #402](https://github.com/IEZhu/Agents/pull/402), followed by cross-client
cooperation in [PR #407](https://github.com/IEZhu/Agents/pull/407). Restoring the old
files wholesale would remove later fixes and artifact/cooperation support.
The inherited commit remains useful as provenance; its original files do not
need a second feature import.

## Disposition of the differing paths

| Area | Differences inspected | Disposition |
|---|---|---|
| Autodev path policies | `.github/autodev-control-paths`, `.github/autodev-protected-paths` | Keep current review-configuration and artifact-schema coverage. |
| Managed instructions | `CLAUDE.md`, `scripts/templates/routing-protocol-core.md`, `scripts/templates/memory-routing.md` | Keep current pinned-artifact and cooperation rules. |
| Update channel | `env.example`, `src/daemon/control.py`, `src/daemon/overview.py` | Keep current channel controls, reporting and credential stripping. |
| Daemon integration | `src/daemon/app.py`, `src/daemon/autodev_ui.py` | Keep artifact/cooperation lifecycle and the configuration-save snapshot fix. |
| Persona and MCP entry points | `src/engine/persona.py`, `src/server.py` | Keep manifest checks and current artifact/cooperation tools; original inference entry points remain present. |
| Local inference | `src/local_inference/context.py`, `models.py`, `service.py`, `tools.py` | Keep artifact bindings, access rechecks, terminal-publication protection and resident preservation after preflight refusal. |
| Regression coverage | `tests/test_daemon_runtime_override.py`, `test_install_instructions.py`, `test_protocol_migration.py`, `test_local_inference_cache.py`, `test_local_inference_service.py`, `test_local_inference_mcp.py`, `test_local_inference_process.py` | Keep expanded fixtures, added regressions and explicit child-readiness signalling. |
| Maintained references | `README.md`, `docs/README.md`, `docs/shared-mcp-daemon.md`, `tests/README.md`, `docs/autodev-plan.md` | Keep later functionality, channel operations and recorded integration updates. |
| Historical handoff and reports | `docs/shared-kv-mcp/START_HERE.md`, `implementation-report.md`, `resident-deployment-report.md` | Keep original provenance and measurements; distinguish their checkpoints from current execution instructions. |
| Owner configuration example | `docs/shared-kv-mcp/owner-config.example.json` | Keep generic absolute-path placeholders instead of personal legacy paths; the current example validates with inference and Redis disabled. |

## Verification and limits

Before comparison, the original source files and dirty patch were preserved in
the owner's existing private deployment evidence. The immutable inventory
contains each path, legacy hash, size and comparison result; its SHA256 is
`4c46e355a702cc993c49b45c4499d9a5f66f6932efb4fa9dbc547477fd96ca13`.
Two separately prepared read-only workers consumed that pinned MCP artifact
completely and checked their assigned code and documentation differences. Their
unchanged results passed validation against the parent's original contracts;
manifest integrity was checked separately. The code audit independently
reconstructed the full inventory and checked all 85 hashes and comparisons.

This is a source-preservation audit. It does not rerun native generation, Redis
or historical benchmarks, certify a full regression suite on this revision, or
establish the current configuration of an owner's running installation.
Historical raw outputs identified as unavailable in the
[implementation report](implementation-report.md) and
[Redis live report](redis-live-report.md) remain unavailable; source preservation
and later checks do not reconstruct those measurements or change their original
denominators and failures.

## Follow-up work

Use a fresh authorized worktree from current `origin/dev`, read the repository
instructions and compare the requested gap with current source/tests. Use
[daemon operations](../shared-mcp-daemon.md#local-inference),
[shared artifacts](../shared-artifacts.md) and
[cooperation](../agent-cooperation/README.md) for maintained behavior. When
explicitly requested, [the implementation flow](../../flows/shared-kv-inference.md)
uses the historical contract's invariants without replaying completed work.

The original dirty checkout and its ignored evidence were retained. This report
does not authorize deleting them, rewriting its inherited commit, deployment,
publication or a merge without the repository's required approvals.
