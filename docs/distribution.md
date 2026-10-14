# Distribution and directory submissions

Agents-Core has two distribution artifacts: a local MCP server installed from this repository and a Claude plugin in `plugins/agents-core` that launches that installation. The plugin does not bundle Python dependencies or the embedding model. Describe that prerequisite in every listing.

## Own Claude Code marketplace

The root [`.claude-plugin/marketplace.json`](../.claude-plugin/marketplace.json) defines the `agents-core` marketplace with one plugin, also named `agents-core`, from `./plugins/agents-core`. Once published on `main`, Claude Code users on macOS or Linux can run:

```text
/plugin marketplace add IEZhu/Agents
/plugin install agents-core@agents-core
```

For a fresh setup, install the plugin first, then follow its [Setup](../plugins/agents-core/README.md#setup) to install the local server. That order lets setup avoid a second Claude Code MCP registration. Windows users use the server installer directly. The root [llms-install.md](../llms-install.md) covers manual stdio configuration, including Cline.

This is a user-added marketplace, separate from Anthropic's curated directory. See the official [creation guide](https://code.claude.com/docs/en/plugin-marketplaces) and [marketplace reference](https://code.claude.com/docs/en/plugins/marketplace-reference).

Validate both artifacts from the repository root:

```bash
claude plugin validate ./plugins/agents-core
claude plugin validate .
```

Test adding the marketplace and installing `agents-core@agents-core` in an isolated Claude Code profile. Keep the plugin manifest as the sole source of its version; increment it when releasing a changed plugin. See [release instructions](claude-plugin.md#release).

## Submission text

Name: **Agents-Core**

Repository: <https://github.com/IEZhu/Agents>

Plugin: <https://github.com/IEZhu/Agents/tree/main/plugins/agents-core>

Short description:

> Local MCP server for specialist personas, reusable Markdown workflows, task-scoped subagent contracts and per-repository memory.

Claude plugin description:

> Claude Code launcher for Agents-Core specialist personas, workflows and repository memory. Requires a separately installed local Python MCP server on macOS or Linux.

License: MIT. Author: WonderMr. The core server needs no external API key. Data handling is described in the plugin's [Privacy Policy](../plugins/agents-core/README.md#privacy-policy), including optional Langfuse tracing and optional private Git sync.

## Directory routes

Requirements checked on 2026-10-14. Submission means the request was delivered; it does not mean the maintainers accepted or published it. No moderation deadline is promised.

| Directory | Route and requirements | Status |
|---|---|---|
| [ccplugins/awesome-claude-code-plugins](https://github.com/ccplugins/awesome-claude-code-plugins#contributing) | README contribution; the catalog accepts plugins and external marketplaces. List the plugin in Workflow Orchestration with its local-server prerequisite | [PR #707](https://github.com/ccplugins/awesome-claude-code-plugins/pull/707) submitted 2026-10-14; awaiting maintainer review |
| [hekmon8/awesome-claude-code-plugins](https://github.com/hekmon8/awesome-claude-code-plugins/blob/main/CONTRIBUTING.md) | Individual plugin files under `plugins/`, marketplace entry and README entry, with testing evidence | [PR #24](https://github.com/hekmon8/awesome-claude-code-plugins/pull/24) submitted 2026-10-14; awaiting maintainer review |
| [Glama](https://glama.ai/mcp/servers) | Add Server flow for a public GitHub repository; the current UI requires sign-up or sign-in | Awaiting owner login; no new account or terms accepted |
| [Cline MCP Marketplace](https://github.com/cline/mcp-marketplace#how-to-submit-your-mcp-server) | GitHub issue, repository URL, 400×400 PNG and reason for inclusion; explicitly requires observing Cline install successfully from README and/or `llms-install.md` | Installation guide prepared; actual Cline installation and correctly sized logo still required before submission |
| [Official MCP Registry](https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/quickstart.mdx) | Verified namespace and a supported public package/artifact or public remote server; see [package types](https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/package-types.mdx) | Packaging/publication work still required; no registry entry submitted |
| [PulseMCP](https://www.pulsemcp.com/submit) | Submission page reports intake paused, last updated 2026-09-03; it directs publishers to the Official MCP Registry for eventual import | Intake paused; no submission sent |

Do not publish a localhost or loopback URL as a public remote endpoint. Do not describe `install.sh` or this repository's unpublished `pyproject.toml` project as a PyPI package. Do not assert a completed Cline test from JSON validation or a different client's installation.

The existing Anthropic directory submission is tracked separately in [Claude plugin](claude-plugin.md); keep its status tied to portal evidence. Skill catalogs require actual `SKILL.md` artifacts: Agents-Core's runtime `.mdc` skills are not standalone `SKILL.md` packages.

## Submission record

- **2026-10-14, ccplugins [PR #707](https://github.com/ccplugins/awesome-claude-code-plugins/pull/707):** README-only Workflow Orchestration entry, with the separate local installation prerequisite. The plugin and own marketplace passed manifest validation; native registration and installation in an isolated Claude Code profile succeeded.
- **2026-10-14, hekmon8 [PR #24](https://github.com/hekmon8/awesome-claude-code-plugins/pull/24):** plugin 0.1.3 snapshot with MIT license, local launcher, marketplace entry and README entry. Plugin manifest validation passed; adding that marketplace and installing/enabling `agents-core@awesome-claude-code-plugins` in an isolated profile succeeded. Full marketplace validation reports an existing bare HTTPS source on the NotFair entry, plus existing unknown metadata warnings; these unrelated entries were preserved and the limitation is disclosed in the PR.

Only mark a listing published after opening its accepted catalog entry. Keep installation requirements visible when the listing is revised. Update this record when maintainers accept, reject or request changes.
