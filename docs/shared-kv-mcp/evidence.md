# Evidence and source map

Preparation date: 2026-10-14. This source map records code inspection and the historical experiments below. New implementation checks and model evidence are recorded separately in [implementation-report.md](implementation-report.md) and the follow-up [Redis live report](redis-live-report.md).

## Recorded revisions

Implementation base: `34671459c6b0a445f0ad0812d7c1e4c8b3e22827` from origin/dev.

Research reference: `2f4f2810738d890f0d49c177bf0ab3c4780a6c3e` on origin/codex/agent-language-concept. This branch is kept separate. Example read command:

```bash
git show 2f4f2810738d890f0d49c177bf0ab3c4780a6c3e:experiments/accord/shared_kv/cache.py
```

If that object is absent in another clone, fetch the named research branch, then verify the exact commit exists before reading it. Do not substitute an unrelated current branch. The companion [source manifest](source-manifest.json) records file hashes and sizes at the two revisions.

## Research results and reuse limits

| Source at the research revision | Evidence | Consequence |
|---|---|---|
| [Resident cache report](https://github.com/IEZhu/Agents/blob/2f4f2810738d890f0d49c177bf0ab3c4780a6c3e/docs/accord-shared-kv-evaluation.md) | 8 episodes on one defect, 2 per cell; Accord model-call median 87.62 -> 65.38 seconds, Python 94.77 -> 65.78; 48.0% input reused | A shared dense-model prefix can avoid prefill for either frontend |
| Same report, quality/time sections | 0/8 accepted repairs; full orchestration did not accelerate | Do not claim useful task success or end-to-end speed from model-only time |
| [Bounded repair report](https://github.com/IEZhu/Agents/blob/2f4f2810738d890f0d49c177bf0ab3c4780a6c3e/docs/accord-repair-loop-evaluation.md) | 7/8 successful repairs; 29 calls; 189,813 input tokens; 0 reused tokens | A stronger model can complete work while its tested cache profile gives no benefit |
| Same report, controls | Both runtimes accept 7/8 identical captured trajectories; zero stage differences | No IR-specific advantage established; build a normal Python service |
| [Token/snapshot report](https://github.com/IEZhu/Agents/blob/2f4f2810738d890f0d49c177bf0ab3c4780a6c3e/docs/accord-inference-evaluation.md) | Balanced text/ID full-response ratio 1.006x; saved snapshot 553,315,860 bytes | Token-ID transport is not the main gain; snapshots need chunking and bounded I/O |
| `experiments/accord/shared_kv/cache.py` | Object handles, actor grants, one slot/lock, native counter checks | Reuse invariants, not its trusted-host wire/security assumptions |
| `experiments/accord/inference/client.py` | Native rendering/tokenization/completion and internal SSE | Reference protocol details, then test the selected exact engine build |
| `experiments/accord/shared_kv/test_cache.py` and `test_workflow.py` | Invalidation, serialization, scope/candidate rejection controls | Adapt behavioral ideas into production tests; do not import research runtime |
| `experiments/accord/repair_loop/` | Bounded real workflow with checks and model outputs | Future benchmark reference, not an automatic production tool runner |

The local experiment machine was an Apple M4 Max with 36 GiB unified memory. Engine build was llama.cpp b11429, commit `d81235049384534c167caea52b85a694f6103d14`. A 27B run had substantial system swap and variable throughput; do not turn those timings into a universal hardware claim. The current feature must measure its own memory baseline.

## Current implementation anchors

| Area | Verified source / symbol |
|---|---|
| Daemon lifecycle | `src/daemon/app.py`: Service, start_runtime, lifespan, health |
| Stop/drain | `src/daemon/control.py`: Controller._drain; at the preparation base it watched inflight/io_pending/streams; implementation adds local_inference_pending |
| Context boundary | `src/daemon/workspaces.py`: ClientContext, resolve_client_context, WorkspaceRegistry |
| Worktree normalization | `src/engine/config.py`: _main_checkout, _project_root |
| Shared bearer / client label | `src/daemon/app.py` authentication and dispatch; `src/daemon/usage.py` app_name |
| Existing inference lane | `src/daemon/execution.py`: InferenceExecutor; used for retrieval embeddings |
| MCP schema/annotations | `src/server.py`: _WorkerInputMCP, TOOL_HINTS, tool_hints; `src/schemas/tool_args.py` |
| Bridge constraints | `bridge/stdio.mjs`: 120,000 ms upstream timeout; no automatic retry of uncertain requests |
| Fake runtime tests | `tests/test_daemon.py`; `tests/test_tool_annotations.py`; `tests/test_config_client_root.py` |
| Test isolation | `tests/conftest.py` and `tests/README.md` |

These are the anchors to inspect, not instructions to rewrite entire modules. The independent source audit checked these integration boundaries; the parent also checked the timeout, workspace code and lifecycle before fixing the design.

## Primary external references

Checked during preparation; recheck the exact selected backend version before implementation.

- [Pinned llama.cpp server API](https://github.com/ggml-org/llama.cpp/blob/d81235049384534c167caea52b85a694f6103d14/tools/server/README.md): native completion, chat-template rendering, tokenization and slot file operations. The save/restore endpoints use a filename under the configured slot-save directory; the snapshot payload is backend-specific.
- [vLLM prefix caching design](https://docs.vllm.ai/en/latest/design/prefix_caching/): exact token-prefix identity and additional compatibility/isolation inputs.
- [vLLM prefix-cache limitations](https://docs.vllm.ai/en/latest/features/automatic_prefix_caching/): prefill savings do not remove decoding.
- [MCP sampling specification](https://modelcontextprotocol.io/specification/2025-11-25/client/sampling): model execution remains with the client; MCP does not provide arbitrary cloud KV export.
- [LMCache Redis/Valkey adapter](https://docs.lmcache.ai/mp/l2_storage/resp.html): existing L2 option for a compatible serving stack.
- [LMCache installation](https://docs.lmcache.ai/getting_started/installation.html): do not assume that documentation for Linux accelerators proves integration with our Metal backend.
- [Python resource limits](https://docs.python.org/3/library/resource.html#resource.RLIMIT_FSIZE): Unix per-file limits; platform support must be checked before enabling native snapshot writes.
- [Python subprocess guidance](https://docs.python.org/3/library/subprocess.html): avoid preexec_fn in a threaded parent; use an exec wrapper for child resource setup.
- [Redis strings](https://redis.io/docs/latest/develop/data-types/strings/): binary-safe strings and the default 512 MB single-value limit.
- [Redis KV transfer analysis](https://redis.io/blog/throughput-optimizing-redis-for-l2-kv-cache-reuse/): loading bandwidth/copies can determine whether offload beats recomputation; its datacenter results are not our Mac benchmark.

All API names outside the verified existing source map are proposed new interfaces in this handoff. Performance thresholds, defaults and limits are design choices unless explicitly attributed to a recorded experiment.
