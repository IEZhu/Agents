# Glama listing and container releases

This guide records the Agents-Core listing and the configuration submitted to Glama. A directory listing, a GitHub release and a Glama container release are separate publication steps. For local installation and other directories, see [Distribution and submissions](distribution.md).

## Maintainer links

| Purpose | Location |
|---|---|
| Public listing | [Agents-Core on Glama](https://glama.ai/mcp/servers/IEZhu/Agents) |
| Repository synchronization | [Repository admin](https://glama.ai/mcp/servers/IEZhu/Agents/admin/repository) |
| Container configuration and build tests | [Dockerfile admin](https://glama.ai/mcp/servers/IEZhu/Agents/admin/dockerfile) |
| Published container releases | [Releases admin](https://glama.ai/mcp/servers/IEZhu/Agents/admin/releases) |
| Public source | [IEZhu/Agents](https://github.com/IEZhu/Agents) |

Admin pages require a signed-in maintainer with ownership of the listing. The root [glama.json](../glama.json) identifies the individual maintainer `WonderMr`; the repository belongs to the `IEZhu` organization. Follow Glama's [ownership guide](https://glama.ai/blog/2025-07-08-what-is-glamajson) when claiming the listing or changing maintainers.

## Recorded state

Observed on **2026-10-15 at 10:27 UTC**:

- Glama approved the listing, the public page was opened, and author verification/ownership was confirmed in the admin UI.
- Repository synchronization recovered after previously showing `Repository Not Ready` and no known commit. The admin UI now recognizes published `main` commit [`b23f8ad60f39494b8e69ac6b6c6d656b8ede68cc`](https://github.com/IEZhu/Agents/commit/b23f8ad60f39494b8e69ac6b6c6d656b8ede68cc), version `26.10.14.1807`, GitHub tag [`v26.10.14.1807`](https://github.com/IEZhu/Agents/releases/tag/v26.10.14.1807).
- Glama accepted build/test [`01a13f09-f62f-7903-a294-4ee9f3804c49`](https://glama.ai/mcp/servers/IEZhu/Agents/admin/dockerfile/tests/01a13f09-f62f-7903-a294-4ee9f3804c49) at 10:09 UTC. It still shows **pending**, no duration, and empty build and instance logs.
- No successful Glama build, provider MCP checks, security scan or published Glama release has been confirmed. Search indexing and quality grades remain unverified.

This is a dated observation, not a live status indicator. Inspect the existing test before starting another build or announcing publication. The source pin above does not include PRs #390–#393; their integration and a subsequent GitHub release are separate work.

## Submitted build configuration

[glama-build.json](glama-build.json) is a repository reference copy of the fields submitted in the Dockerfile admin form. It is not an automatic deployment manifest or a replacement for root `glama.json`; saving it in Git does not update Glama. Copy the corresponding fields into the form and inspect Glama's generated Dockerfile before building.

| Form field | Reference value |
|---|---|
| Base image | `debian:trixie-slim` |
| Node version | `26` (Glama wrapper tooling) |
| Python version | `3.11` |
| Pinned commit | `sourceCommit` from the reference JSON, currently the published `b23f8ad6…` above |
| Build steps | The four `buildSteps` strings, in order |
| CMD | The `cmd` JSON array |
| Environment variables schema | `environmentVariablesSchema`: an object with no properties and `additionalProperties: false` |
| Placeholder parameters | `placeholderParameters`: `{}` |

The build installs `passwd` and `util-linux`, then places a uv-managed Python interpreter in `/opt/python` and a virtual environment in `/opt/agents-venv`. This makes Python available to the server user without relying on an interpreter installed under root's home. Dependencies come from the pinned source's `requirements.txt`; `uv pip check` checks the resulting virtual environment. Requirements and the base tag are not fully pinned, so the source SHA alone does not guarantee identical image contents on later builds.

The build creates `agents` with UID **10001**, writable `/app/data`, and cache directory `/home/agents/.cache/fastembed`. Both strict readiness warmup and the stdio server run as that user through `runuser`. The final generated wrapper is **`mcp-proxy --`**, followed by the configured CMD. Glama adds the proxy automatically; do not include a second proxy in the CMD form field. The proxy's own user remains controlled by Glama's generated Dockerfile.

Warmup uses:

```sh
/opt/agents-venv/bin/python -c 'from src.engine.readiness import run_blocking; run_blocking()'
```

The complete build step in the reference JSON also supplies the same user, cache path and environment as runtime. Model files and indexes must remain accessible to the runtime user. Warmup requires network access to download the public embedding model; the cold local run took about five minutes. Avoid concurrent warmups or embedding processes.

| Environment setting | Purpose |
|---|---|
| `AGENTS_AUTO_UPDATE=0` | Keep the selected source snapshot during runtime; publish a rebuilt image for updates |
| `AGENTS_STDIO_STANDALONE=1` | Run the stdio server directly rather than attaching to a host daemon |
| `AGENTS_DEBUG=0`, `LANGFUSE_TRACING_ENABLED=false` | Disable debug mode and optional Langfuse tracing |
| `FASTEMBED_CACHE_DIR=/home/agents/.cache/fastembed` | Reuse the model cache warmed by the same user |
| `PYTHONDONTWRITEBYTECODE=1`, `PYTHONUNBUFFERED=1` | Avoid bytecode writes and buffer delays |

The core server requires no external API key. Do not add a host `.env`, MCP client registrations, private flows, repository memory or credentials to this image. The submitted build clones only public source and downloads public dependencies/model files. Optional private integrations are outside this configuration.

## Validation evidence and limits

A local **Linux/arm64** baseline built the same source commit using `python:3.11-slim`, UID 10001 and `python -m src.server`. Dependency consistency, real server import and strict readiness passed. Warmup loaded 71 skills, 57 implants and 6 rules in 313.473 seconds.

A cached derivative created after that warmup passed a real stdio MCP session with network disabled, no host mounts and no published ports, under `--cpus=2 --memory=4g --pids-limit=256`. Initialize completed in 1.660 seconds with MCP protocol `2025-11-25`; discovery returned 18 tools with valid input schemas. `list_agents`, built-in `list_flows`, and protocol 2 `get_agent_context` with semantic enrichment succeeded. Cached readiness completed in 7.006 seconds.

Those results cover the local baseline and its warmed derivative. They do **not** establish Linux/amd64 compatibility, a successful build of the submitted Debian/uv recipe, operation through Glama's proxy, or Glama's quality/security checks. Static review of the submitted configuration found no actionable issue, but its actual provider execution is still pending. Do not reuse the local success as a provider release gate.

## Build, publish and update

Follow Glama's [release procedure](https://glama.ai/blog/2026-03-15-how-to-make-a-release), checking the current admin UI labels and results:

1. Choose a published `main` commit and verify its calendar version and GitHub tag using the [repository release procedure](releases.md). Fetch the remote refs in an isolated worktree; preserve the live installation checkout. Do not silently replace the pin with the current `dev` head.
2. In Repository admin, request synchronization when the selected commit is missing. Wait and reload, then verify the known commit. A Sync button click or notification alone is not proof that ingestion finished.
3. Review the reference configuration against the selected source. Update its pin and any required build/runtime fields in the Dockerfile form. Compare the generated Dockerfile with the intended user, Python path, warmup cache and CMD. Keep the reference JSON in this repository aligned with the submitted fields through a branch and PR against `dev`.
4. Use **Build** to create a test. Record its ID, source SHA, configuration and result. Read the full build and instance logs. Require successful provider build and MCP checks; address actionable failures before release. If the job stays pending with no logs, keep the existing job as evidence instead of submitting duplicate builds. Escalate persistent synchronization/queue failures through [Glama support](https://glama.ai/support).
5. After the test succeeds and the UI offers **Create Release**, set the release version (initially `26.10.14.1807` for the recorded pin) and an accurate changelog, then **Create & Publish Release**. For a later pin, use that source's verified version. Run or wait for the provider's required safety/quality checks; resolve blocking results before declaring completion.
6. Verify the published release entry, source SHA, version, scan/check results and public listing. Record their actual URLs and observed results here in a follow-up PR. A GitHub tag, ownership badge or completed local container test alone does not establish a published Glama release or search indexing.

Changes to this guide or configuration enter `dev` through a normal PR and reach `main` through the repository release process. Publishing a Glama container does not merge those changes or release pending source fixes.
