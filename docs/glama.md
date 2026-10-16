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

Observed on **2026-10-15**, after publication at **12:08 UTC (14:08 Europe/Madrid)**:

- Glama approved the listing, the public page was opened, and author verification/ownership was confirmed in the admin UI.
- Repository synchronization recovered after previously showing `Repository Not Ready` and no known commit. Glama recognizes published `main` commit [`b23f8ad60f39494b8e69ac6b6c6d656b8ede68cc`](https://github.com/IEZhu/Agents/commit/b23f8ad60f39494b8e69ac6b6c6d656b8ede68cc), version `26.10.14.1807`, GitHub tag [`v26.10.14.1807`](https://github.com/IEZhu/Agents/releases/tag/v26.10.14.1807).
- Provider build/test [`01a13f09-f62f-7903-a294-4ee9f3804c49`](https://glama.ai/mcp/servers/IEZhu/Agents/admin/dockerfile/tests/01a13f09-f62f-7903-a294-4ee9f3804c49) succeeded in **10m 7s**. Its saved Build Spec pins that source and the Debian/Python 3.11 configuration below. The actual provider logs establish Linux/amd64 execution, dependency consistency, completed strict warmup and a running MCP server through Glama's proxy.
- Glama release **`26.10.14+1807`** was created from that successful test and is shown as **Latest** in [Releases admin](https://glama.ai/mcp/servers/IEZhu/Agents/admin/releases). The test detail shows **Release Created** and the same version and publication time. This SemVer uses `1807` as build metadata; the source's calendar version and GitHub tag retain their original four components.
- Discovery score rose to **92%** after publication. Before publication, the score page showed Server Coherence **A**, Tool Definition Quality **A** and Maintenance **B**; the release-specific [TDQS page](https://glama.ai/mcp/servers/IEZhu/Agents/admin/tdqs) now selects **v26.10.14+1807 (latest)** and reports **4.3/5 across 18 evaluated tools**. These ranking and tool-definition results do not establish a clean image security scan. No separate passing security-scan report or search-indexing result was exposed in the inspected admin/public pages.

A later test, [`01a13f43-712f-76d5-be2d-281597d54045`](https://glama.ai/mcp/servers/IEZhu/Agents/admin/dockerfile/tests/01a13f43-712f-76d5-be2d-281597d54045), failed in **1m 11.8s** with a different Build Spec: Node 24, Python 3.15, `uv sync`, no explicit commit pin and `python src/server.py`. Its log reports that locked `onnxruntime==1.24.4` has no compatible CPython 3.15 wheel. This failure does not describe the published Python 3.11 artifact. The Dockerfile editor displayed those different values, and Releases admin reported that Auto-Release cannot publish while the last build is failed. The successful test remains the published release's saved configuration reference. Do not copy the failing defaults or change that artifact's source identity.

At **12:38 UTC (14:38 Europe/Madrid)**, the successful Debian/Node 26/Python 3.11 recipe was restored in the editor and submitted as test [`01a13f92-628a-7561-8a76-f8dcdfa9df8f`](https://glama.ai/mcp/servers/IEZhu/Agents/admin/dockerfile/tests/01a13f92-628a-7561-8a76-f8dcdfa9df8f). Its saved Build Spec has `pinnedCommit: null` for future updates and exactly one generated `mcp-proxy --` wrapper. The test was still pending without logs at 12:54 UTC. At **13:46 UTC**, the same test showed **success in 17m 21.2s**. Its generated build checked out `b23f8ad60f39494b8e69ac6b6c6d656b8ede68cc`, installed CPython **3.11.17**, passed compatibility checks for **75 packages**, completed the chained build steps and reached image export. The instance initialized MCP **2025-11-25** and returned tool, prompt and resource discovery, including **18 tools**, with 71 skills and 57 implants loaded from disk. Releases admin showed **Auto-Release enabled** and no failed-last-build warning. The existing published release `26.10.14+1807` was retained; this manual retry confirms the restored recipe but does not exercise delivery of a GitHub Release event or publish another version. The reference JSON below continues to describe the original published, explicitly pinned artifact.

This is a dated observation, not a live status indicator. Inspect the saved test and release before starting another build or announcing publication. The source pin above does not include PRs #390–#393; their integration and a subsequent GitHub release are separate work.

## Submitted build configuration

[glama-build.json](glama-build.json) is a repository reference copy of the fields used by the successful published test, rather than a claim that the live Dockerfile editor still contains them. It is not an automatic deployment manifest or a replacement for root `glama.json`; saving it in Git does not update Glama. Copy the corresponding fields into the form and inspect Glama's generated Dockerfile before building.

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

Those local results cover the baseline and its warmed derivative. The separate successful provider test now establishes the submitted Debian/uv recipe on **Linux/amd64**, with CPython **3.11.17**, `mcp-proxy` **6.7.16**, 75 compatible installed packages and the shared non-root warmup/runtime cache. Its logs show initialization with MCP protocol `2025-11-25`, followed by tool, prompt and resource discovery. The runtime loaded 71 skills and 57 implants from disk. Informational log lines and a nonfatal Pydantic `lifespan` warning are not failed checks; the authoritative test result is `success`.

The provider log does not exercise functional `tools/call` operations, prove a complete image vulnerability scan, establish tenant/proxy isolation, or supply an immutable output-image digest. Those limits remain distinct from successful build/startup and publication. Glama's [release guide](https://glama.ai/blog/2026-03-15-how-to-make-a-release) describes image security scans as work performed for a release, and its TDQS page queues published releases for evaluation. Record their actual results when exposed; do not represent a quality badge or the absence of errors as a passing scan.

## Troubleshoot the online Inspector

On **2026-10-15 at 13:15 UTC**, the [Glama Inspector](https://glama.ai/mcp/inspector) reproduced `Streamable HTTP error: Unexpected content type: null` for a deployed Agents-Core instance when **Use proxy to bypass CORS restrictions** was checked. At **13:15:58 UTC**, the same endpoint and credentials connected with that option unchecked. Discovery returned **18 tools and 55 prompts**, and a subsequent read-only `list_agents` call returned an agent list with `isError: false`. The public Inspector test server also connected successfully. This comparison establishes a working direct connection and an observed failure through the Inspector proxy; the failing HTTP response's status, headers and body were not captured, so the internal proxy failure remains unexplained.

The instance's [MCP Deployments](https://glama.ai/settings/mcp/deployments) profile showed `started`. Runtime logs showed the Python server completing readiness in **29.795 seconds**, then handling tool, resource and prompt discovery. The virtual machine had also been suspended and resumed. Those observations do not establish that suspension caused the Inspector error, and the connection error alone is not evidence of a Python crash.

For the same symptom:

1. Open the instance from MCP Deployments, check its status and **logs**, and look for completed readiness, discovery requests or a traceback. A successful image build is separate from a live client connection.
2. In the Inspector, choose **Edit server**, uncheck **Use proxy to bypass CORS restrictions**, save, and connect again to the same Glama endpoint with its existing authentication. This changes the Inspector's connection route; keep the container's generated `mcp-proxy --` wrapper in place.
3. Require **Connected**, successful discovery and a safe read-only call such as `list_agents` before recording recovery. This is a diagnostic comparison, not a guarantee that direct mode works for every remote server; other origins may require the proxy for CORS.
4. If direct mode also fails, retain the failing request's method, HTTP status, response headers/body, console error and matching runtime timestamps for further investigation. MCP request responses require JSON or an SSE stream as described in the [transport specification](https://modelcontextprotocol.io/specification/2025-11-25/basic/transports).

The Inspector persists connection settings, including credentials, in its page URL. Never put an instance token or a token-bearing Inspector URL in this repository, PRs, screenshots or support messages. Use redacted evidence when documenting a failure.

## Build, publish and update

Follow Glama's [release procedure](https://glama.ai/blog/2026-03-15-how-to-make-a-release), checking the current admin UI labels and results:

1. Choose a published `main` commit and verify its calendar version and GitHub tag using the [repository release procedure](releases.md). Fetch the remote refs in an isolated worktree; preserve the live installation checkout. Do not silently replace the pin with the current `dev` head.
2. In Repository admin, request synchronization when the selected commit is missing. Wait and reload, then verify the known commit. A Sync button click or notification alone is not proof that ingestion finished.
3. Review the reference configuration against the selected source. Update its pin and any required build/runtime fields in the Dockerfile form. Compare the generated Dockerfile with the intended user, Python path, warmup cache and CMD. Keep the reference JSON in this repository aligned with the submitted fields through a branch and PR against `dev`.
4. Use **Build** to create a test. Record its ID, source SHA, configuration and result. Read the full build and instance logs. Require successful provider build and MCP checks; address actionable failures before release. If the job stays pending with no logs, keep the existing job as evidence instead of submitting duplicate builds. Escalate persistent synchronization/queue failures through [Glama support](https://glama.ai/support).
5. Once the saved test succeeds and the UI offers **Create Release**, verify the source SHA and Build Spec of that test, rather than the current editor's defaults or a later test using another recipe. The successful image build and server startup test is one pre-publication gate shown in the current interface and release guide. For every pre-publication safety or quality check required by Glama's current UI or release documentation, verify a passing result for that exact source SHA and Build Spec. If a required result is missing, pending, failed or cannot be verified, keep publication pending, retain the evidence and use [Glama support](https://glama.ai/support) for the supported resolution. Glama performs image security scans for a release, and release-specific TDQS enters its evaluation queue after publication; record those results separately in step 6. Enter a valid semantic version and an accurate changelog, then use **Create & Publish Release**. The recorded source `26.10.14.1807` maps to Glama version `26.10.14+1807`; retain both identifiers and the exact source SHA.
6. Verify **Release Created**, the published **Latest** entry, version, publication time, linked test and public listing. Record actual scan/check results separately when available. Investigate a failed scan or newly exposed mandatory failure; if a result or remediation path is unavailable, retain its evidence and ask [Glama support](https://glama.ai/support) for the supported path. Do not equate successful publication, a GitHub tag, ownership, a quality score or a local container test with a clean security scan or search indexing.

Changes to this guide or configuration enter `dev` through a normal PR and reach `main` through the repository release process. Publishing a Glama container does not merge those changes or release pending source fixes.

## Automatic updates after a release merge

The maintainer Releases page has **Auto-Release** enabled and describes it as building and publishing on every GitHub release. A Git tag alone is not a GitHub Release: the existing tag `v26.10.14.1807` had no Release entry when checked through GitHub's API on 2026-10-15.

The repository implements the GitHub publication step in [the Release version workflow](../.github/workflows/release-version.yml) and [scripts/release_version.py](../scripts/release_version.py): after an approved same-repository `dev → main` PR is merged with a merge commit, the job verifies its calendar tag and publishes a stable GitHub Release for that exact SHA. It uses the built-in `GITHUB_TOKEN`, preserves existing release content on retries, and changes no repository protection or billing settings. See [calendar releases](releases.md) for version selection and recovery. Implementation in `dev` does not establish deployment to `main` or successful delivery to Glama; verify those outcomes for the actual promotion.

For future Auto-Release builds, use the successful runtime/build fields and leave the editor's optional commit pin empty; keep the historical published recipe in `glama-build.json` intact. The editor calls an empty pin “latest commit,” so check each automatic build's source against its release tag, particularly when `main` advances while a build is queued. A green Actions publication result proves the GitHub tag and Release exist; Glama's build, required checks and public version are separate outcomes.

Before calling this chain verified end to end, observe a real automatic release and record event delivery, source SHA, build result, public version and calendar-to-SemVer mapping, including the same-minute `-prN` case. No public Glama release-write API or automatic loading of this guide's reference JSON has been established. If the provider misses the event or the saved build fails, retain the evidence and use the supported manual build/release procedure after fixing the cause. Reusing an existing GitHub Release on an Actions rerun does not emit a new publication event.
