# Shared MCP daemon validation

Date: 2026-09-20. Model: `intfloat/multilingual-e5-large`; MCP SDK: 1.27.1.

## Automated checks

- Initial `scripts/run_tests.sh -q` run: 846 passed, 16 deselected in 47.29 seconds.
- Follow-up regression checks on 2026-09-21: all 25 daemon, audit, controller,
  migration, token rotation, and update tests passed. Coverage includes repeated
  token rotation, unreadable Claude configurations, missing model cache
  references, and configuration changes between preparation and migration.
- Opt-in routing tests: 16 passed.
- Node bridge: real loopback HTTP, concurrent request IDs, notifications,
  absence of upstream callbacks, and no replay after HTTP 503 passed.
- Controller and update: writer leases, stdio readers, drain timeout, readiness
  failure, code and index rollback, recovery, and dependency-change rejection
  passed.
- Fast transport: token, Host and Origin validation, workspace UUID isolation,
  absence of MCP sessions, cancellation accounting, and bounded LRU passed.
- `compileall`, `git diff --check`, and `bash -n scripts/init_repo.sh` passed.
- Importing the controller and bootstrap does not load numpy, fastembed, or the
  MCP server.

## Real-model load measurements

Tests used a temporary daemon and temporary workspaces without changing user
project history. Measurements were recorded locally; use the scripts in the
[operations guide](shared-mcp-daemon.md#validation) to repeat them on another host.

| Metric | Observed result |
|---|---:|
| Time to readiness with a cached model | 1.24-1.56 s |
| New connection and routing, sequential p95 | 37-58 ms |
| Initialization and routing, 20 concurrent clients, p95 | 569-615 ms |
| Maximum event-loop lag after readiness | 2.36 ms |
| Physical footprint after warmup | 1.6 GiB |
| Physical footprint after 1,000 connections and 20 workspaces | 1.6 GiB |
| CPU during 300 seconds idle | 0.20% of one core |
| Shutdown after drain and SIGTERM | Completed without forced kill |

Checks also covered separate A/B histories, rejection of missing or invalid
workspace UUIDs, rejection of summaries transferred between identical clones,
rejection of stale source digests, preservation of user text in `CLAUDE.md`,
absence of sampling even when advertised by the client, and exclusion of the
administrative cache reset from the HTTP tool catalog.

Physical footprint comes from `vmmap --summary`, which rounds its output. This
checks the memory budget; it does not prove the absence of small leaks. Cold
startup caused event-loop lag of up to 414 ms, measured separately from request
handling in the ready runtime. The 100 ms loop-lag target was met for the measured
client workload after readiness, but not during model initialization.

## Host rollout verification

The pre-migration baseline found 18 stdio processes. Footprint was measured for
17 of them, totaling 24,161,271,808 bytes (22.5 GiB); one process exited before its
measurement. Swap usage was 30,077.31 MiB out of 30,720 MiB. The baseline script
omits full command arguments and secrets.

Host deployment and client migration have not been performed. These results
therefore do not establish an actual reduction in the host's total memory use.
After migration, verify the effective configuration of each installed client,
the Code tab and bridge namespace, Dock launch, sleep/wake, and reconnection.
Repeat the process and memory baseline to confirm that one process holds the
model. Synthetic HTTP and Node tests do not replace these GUI checks.
