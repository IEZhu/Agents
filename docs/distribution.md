# Distribution and directory submissions

Agents-Core has two distribution artifacts: a local MCP server installed from this repository and a Claude plugin in `plugins/agents-core` that launches that installation. The plugin does not bundle Python dependencies or the embedding model. Describe that prerequisite in every listing.

## Agent discovery entry points

The root [llms.txt](../llms.txt) is a compact Markdown index following the [llms.txt proposal](https://llmstxt.org/). It links directly to public Markdown on `main`. The [agent guide](agent-guide.md) maps user needs to tools, gives first-call examples and explains requirements and limits; [llms-install.md](../llms-install.md) covers client setup. The README links all three so an assistant arriving through GitHub can find them.

When releasing a change to public tool behavior or installation requirements, update the relevant guide and keep the index links current. Before publishing a listing, check that its linked files exist on the published branch; new files added on `dev` become available at these URLs after release to `main`. Keep task descriptions concrete and verify example arguments against the exposed MCP schemas. Avoid duplicating entire references into the index.

These files help an assistant understand and navigate the project when it reads them. They do not register the server in a catalog, guarantee crawling or establish increased tool selection. Evaluate changes with representative discovery tasks: find the installation prerequisite, select the tool for a PR procedure, complete a persona selection example, distinguish `needs_execution` from completed work, and identify the workspace required for repository history. Record actual results and directory evidence separately from traffic or adoption claims.

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

## Submitted destinations

This is the central ledger of actual directory submissions. Submission means the request was delivered, not accepted or publicly listed. A closed implementation issue or a merged preparation PR does not prove directory approval. Submission dates record when requests were sent; status dates record observations, not moderation deadlines.

| Destination | Submitted | Last confirmed status and evidence | Where to follow up |
|---|---|---|---|
| **Anthropic plugin directory** | 2026-10-13, by the owner | The owner's portal page on 2026-11-15 showed the latest check refusing `26.11.14.1328` at `19e7e28`, the release merge, on two findings: *Files outside the allowed types accepted only from Verified organizations* and *Local MCP servers accepted only from Verified organizations*. Both state that this organization is not Verified, that Anthropic chooses who is, and that there is no way to ask to become one yet. That page is the first evidence of this refusal naming the version and the commit it applies to, and it reports that version's scan as passed, so policy alone stops it. The same two findings were recorded on 2026-11-14, and on 2026-11-11 after `26.11.10.2154` reached `main`, so they do not gate on the version. Earlier evidence: on 2026-11-08 the page showed **Needs changes** dated 2026-10-28, where an automatic check stopped `26.10.28.1144` at source `5f59fdb` with `LOCAL_CODE_NOT_CHECKED`; the screenshot of 2026-10-13 showed **In review** at version `0.1.1`, source `2dbbc2f`. No version has ever been published and no public catalog URL has been verified. [Implementation issue #362](https://github.com/IEZhu/Agents/issues/362) records the preparation scope; its closure is not acceptance evidence | [Owner's directory portal](https://claude.ai/directory/manage), then the existing Agents-Core submission: **Check for new commits** asks for another check of the newest commit, and a refused version offers **Appeal this decision** and **Get help / Contact Anthropic**; `directory@anthropic.com` takes the same questions by email, where ticket **#155892203** now tracks this case. [Upstream issue #7024](https://github.com/anthropics/claude-plugins-official/issues/7024) collects the same refusal from other projects; [plugin operations](claude-plugin.md#anthropic-directory). The portal requires the owner's signed-in account |
| **ccplugins/awesome-claude-code-plugins** | 2026-10-14 | [PR #707](https://github.com/ccplugins/awesome-claude-code-plugins/pull/707), **OPEN** when checked 2026-10-15; no maintainer review or merge observed | Watch and answer the existing PR; verify the accepted catalog entry after a merge |
| **hekmon8/awesome-claude-code-plugins** | 2026-10-14 | [PR #24](https://github.com/hekmon8/awesome-claude-code-plugins/pull/24), **OPEN** when checked 2026-10-15; no maintainer review or merge observed. The submitted plugin snapshot is `0.1.3` | Watch and answer the existing PR; keep its copied plugin snapshot and launcher prerequisites accurate |
| **Glama MCP directory** | 2026-10-14 | [Public Agents-Core listing](https://glama.ai/mcp/servers/IEZhu/Agents) approved and ownership verified 2026-10-15. Provider build/test passed; container release `26.10.14+1807` was published 2026-10-15. Separate security-scan results and search indexing remain unverified | [Repository admin](https://glama.ai/mcp/servers/IEZhu/Agents/admin/repository), [build tests](https://glama.ai/mcp/servers/IEZhu/Agents/admin/dockerfile), [releases](https://glama.ai/mcp/servers/IEZhu/Agents/admin/releases), and [Glama operations](glama.md). Admin pages require listing ownership |

The Anthropic row preserves the owner's dated submission evidence; it is not a fresh portal inspection. The two external GitHub PR states were checked through GitHub on 2026-10-15. Keep credentials, private messages and token-bearing instance or Inspector URLs out of this ledger.

The repository-owned Claude Code marketplace described above is also available through `IEZhu/Agents`; it is our own distribution channel, not another third-party application. Its preparation merged in [PR #380](https://github.com/IEZhu/Agents/pull/380) on 2026-10-14. The install commands above do not prove acceptance into any external directory.

## Researched routes without a submission

These are preparation or research items, not additional submitted applications. Route requirements were checked on 2026-10-14 unless stated otherwise; recheck the destination before preparing a new application.

| Destination | Recorded route and remaining work | Submission state |
|---|---|---|
| [Cline MCP Marketplace](https://github.com/cline/mcp-marketplace#how-to-submit-your-mcp-server) | GitHub issue, repository URL, 400×400 PNG and reason for inclusion; observing a successful Cline installation from README and/or `llms-install.md` is required. The installation guide is prepared, but the actual Cline installation and correctly sized logo remain unverified | **Not submitted** |
| [Official MCP Registry](https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/quickstart.mdx) | Verified namespace and a supported public package/artifact or public remote server; see [package types](https://github.com/modelcontextprotocol/registry/blob/main/docs/modelcontextprotocol-io/package-types.mdx). Packaging/publication work remains | **Not submitted** |
| [PulseMCP](https://www.pulsemcp.com/submit) | The intake page checked 2026-10-14 reported intake paused, with its own last-updated date of 2026-09-03, and directed publishers to the Official MCP Registry for eventual import. That is a dated observation, not a current intake guarantee | **Not submitted** |
| ChatGPT app directory or Anthropic remote connector directory | Remote-server publication is separate from the submitted local Claude plugin. No application was sent through those routes in this work; the original [scope decision](https://github.com/IEZhu/Agents/issues/362) leaves them outside the local-plugin submission | **Not submitted** |

Do not publish a localhost or loopback URL as a public remote endpoint. Do not describe `install.sh` or this repository's unpublished `pyproject.toml` project as a PyPI package. Do not assert a completed Cline test from JSON validation or a different client's installation. Skill catalogs require actual `SKILL.md` artifacts: Agents-Core's runtime `.mdc` skills are not standalone `SKILL.md` packages.

## Submission record

- **2026-10-13, Anthropic plugin directory:** the owner confirmed submission and supplied a portal screenshot showing **In review**, version `0.1.1` at source `2dbbc2f`, and an icon **Waiting for review**. This is the local Claude plugin in `plugins/agents-core`, not a remote connector. No accepted public catalog link was recorded. Check the existing entry in the [owner portal](https://claude.ai/directory/manage) for later validation, policy questions or a review decision; do not resubmit merely because the public listing is not yet verified. The [plugin guide](claude-plugin.md) describes its launcher and separate local-server requirement.
- **2026-10-14, ccplugins [PR #707](https://github.com/ccplugins/awesome-claude-code-plugins/pull/707):** README-only Workflow Orchestration entry, with the separate local installation prerequisite. The plugin and own marketplace passed manifest validation; native registration and installation in an isolated Claude Code profile succeeded.
- **2026-10-14, hekmon8 [PR #24](https://github.com/hekmon8/awesome-claude-code-plugins/pull/24):** plugin 0.1.3 snapshot with MIT license, local launcher, marketplace entry and README entry. Plugin manifest validation passed; adding that marketplace and installing/enabling `agents-core@awesome-claude-code-plugins` in an isolated profile succeeded. Full marketplace validation reports an existing bare HTTPS source on the NotFair entry, plus existing unknown metadata warnings; these unrelated entries were preserved and the limitation is disclosed in the PR.
- **2026-10-14, Glama submission:** after the owner completed GitHub sign-in, submitted repository `https://github.com/IEZhu/Agents` as `Agents-Core` through **Add MCP Server > Runs from source > Submit for Review**. The form showed Loading and then closed without a displayed error; no persistent receipt or submission ID was shown. At that time the direct public server page still reported no listing. The form states that approval is followed by email instructions for a Dockerfile, and safety/quality checks must pass before search indexing. Ownership claim was still pending at submission; root `glama.json` names the individual maintainer `WonderMr`.

- **2026-10-15, Glama container release:** approval, the public listing, ownership and recovered synchronization to published `main` commit `b23f8ad60f39494b8e69ac6b6c6d656b8ede68cc` were verified. The saved Debian/Python 3.11 provider test passed build, strict warmup and MCP initialization/discovery on Linux/amd64 in 10m 7s. Release **`26.10.14+1807`** was published from that test at **12:08 UTC (14:08 Europe/Madrid)** and appears as Latest in [Releases admin](https://glama.ai/mcp/servers/IEZhu/Agents/admin/releases); it corresponds to GitHub tag `v26.10.14.1807`. A later Python 3.15/default-configuration test failed. The intended Python 3.11 recipe was restored and submitted again at 12:38 UTC; after initially remaining queued, the same test was verified successful at 13:46 UTC in 17m 21.2s, with the expected source SHA and successful MCP discovery. Auto-Release is enabled without the failed-last-build warning; a real GitHub Release event and the resulting automatic publication still need end-to-end verification. PR #409 proposes publishing GitHub Releases automatically after approved `dev → main` merges to supply the Auto-Release event. Separate security-scan results and search indexing remain unverified. The [operations guide](glama.md) records the successful artifact, version mapping, differing test configurations and post-publication checks.

- **2026-11-08, Anthropic plugin directory:** the owner's portal page showed the submission at **Needs changes**, the second of four stages, dated 2026-10-28. An automatic check stopped version `26.10.28.1144` at source `5f59fdb`: *Directory policy failed* with `LOCAL_CODE_NOT_CHECKED`, "the scan of this version didn't confirm that it read the whole plugin", which leaves the version unpublished. The portal states that another check may be all it needs: a pushed commit is checked when it arrives, and **Check for new commits** re-checks the newest commit even when nothing has changed. The same check asked that a script the plugin runs be shipped inside the plugin; the setup hint of `plugins/agents-core/scripts/start-server.sh` no longer prints an installer command line for that reason. It listed for information that the plugin starts a local stdio MCP server as a program on the user's machine, that such a server runs in Claude Code and Cowork but not on claude.ai, and that `.claude-plugin/icon.png` was let through without being read as code. The icon was still **Waiting for review**, and no public catalog URL was recorded. Starting the separately installed server was not among the findings that stopped this version.

- **2026-11-14, Anthropic plugin directory:** the owner's portal page showed **Directory policy failed** on the latest check, with two findings under **Needs you**. *Local MCP servers accepted only from Verified organizations*: the directory takes a server that runs on the user's computer only from a Verified organization, and asks that every such server be removed. *Files outside the allowed types accepted only from Verified organizations*: it takes scripts, programs and files outside Markdown, text, JSON, YAML and PNG, JPEG, GIF, WebP or ICO images only from a Verified organization, and names a file that starts with `#!` among them. The two files those findings reach are `plugins/agents-core/.mcp.json`, which declares the stdio server, and `plugins/agents-core/scripts/start-server.sh`, which begins with `#!`. They are the plugin's whole function: removing them leaves a manifest, a README, a LICENSE and an icon. Neither finding gates on the version, and the same two were already recorded on 2026-11-11 after `26.11.10.2154` reached `main`, so a bump alone does not clear them. Neither rule appears in the plugin pre-submission checklist, in Publish to the directory or in the Anthropic Software Directory Policy, and the portal states that Anthropic chooses who is a Verified organization and that there is no way to ask to become one yet. [Upstream issue #7024](https://github.com/anthropics/claude-plugins-official/issues/7024), open since 2026-11-06, collects about thirty projects refused the same way since 2026-11-05; Anthropic's support answered one of them on 2026-11-11 that the rule is not covered by the public documentation, that a listing needs no Verified label and has no application for one, and that an own marketplace reaches users meanwhile. The repository marketplace already serves that route. Follow-up belongs in **Appeal this decision** and **Get help / Contact Anthropic** on the plugin's portal page, or in an email to `directory@anthropic.com`.

- **2026-11-14, Anthropic plugin directory, contact:** the owner's account emailed `directory@anthropic.com` with the submission's details, both findings and three questions: what makes an organization Verified for plugins and whether an independent developer can do anything towards it, whether the rule will be documented and whether it reaches updates of plugins that are already listed, and whether the submission should stay open meanwhile. The reply was an automated acknowledgement from the directory's support agent, opening ticket **#155892203** at the first of three stages, *Submitted*, with no date and no answer to any of the three. Quote that number in a later message or in the portal's appeal. The same case was posted to [upstream issue #7024](https://github.com/anthropics/claude-plugins-official/issues/7024). Replies in that ticket are readable only in the owner's mailbox: the Gmail connector counts the message in the thread and matches a search on the ticket number, yet omits the message itself from every result, so the next reply has to be read there and quoted by hand.

- **2026-11-15, Anthropic plugin directory:** the directory picked the release merge `19e7e28` up within the hour after it reached `main` at 16:07:55Z and refused it. The owner's portal page, read the same day, showed the submission still at **Needs changes** and *Directory policy refused this version*, naming `v26.11.14.1328 · 19e7e28` and adding that a new version meeting the rules can go live. The reasons were the two findings of 2026-11-11 and 2026-11-14 unchanged: *Files outside the allowed types accepted only from Verified organizations* and *Local MCP servers accepted only from Verified organizations*. This is the first evidence of this refusal that names the version and the commit it applies to, and the first to separate the two gates: its activity list reads *New version detected*, then *Scan passed, but this version doesn't meet directory policy*, then *Blocked from going live by directory policy*. This version's scan passed, so `LOCAL_CODE_NOT_CHECKED` is not among the reasons it is refused; the check of `26.10.28.1144` keeps its own result, which this page does not revisit. A fresh version at a fresh commit still fails on the organization, as the earlier records inferred. The page also records that new commits are picked up about every six hours on a scheduled check, that no version of this plugin goes live without an Anthropic reviewer, that nothing is published yet, and that the listing surface would be Claude Code Cowork. This is the evidence [issue #605](https://github.com/IEZhu/Agents/issues/605) asked for, a check of a commit newer than `5f59fdb` recorded with its date; that issue closed with the promotion that produced this commit, and this entry carries its outcome.

Only mark a listing published after opening its accepted catalog entry. Keep installation requirements visible when the listing is revised. Update this record when maintainers accept, reject or request changes.

## Glama submission and ownership

Submit the GitHub repository as a local MCP server through [Add Server](https://glama.ai/mcp/servers). Use these prepared fields:

| Field | Value |
|---|---|
| Repository | `https://github.com/IEZhu/Agents` |
| Name | `Agents-Core` |
| Submitted description | Local MCP server for specialist personas, reusable Markdown workflows, task-scoped subagent contracts and per-repository memory. Runs on your own computer with Python 3.11+ and a locally installed embedding model; its Claude Code plugin launches that separate installation. |
| Installation details, when available | Requires a local Python 3.11+ installation and embedding-model setup. The Claude Code plugin launches that separately installed server on macOS or Linux; Windows uses direct server setup. |

The root [glama.json](../glama.json) lists the individual GitHub maintainer `WonderMr`. `IEZhu` is the organization that owns the repository, rather than an individual maintainer. Glama's [ownership guide](https://glama.ai/blog/2025-07-08-what-is-glamajson) requires the file for organization repositories; GitHub sign-in alone does not automatically claim these listings. After the file reaches the default branch and the server is listed, complete **Claim ownership** to make Glama read it. Repeat that flow after changing the file.

The file follows the [current JSON schema](https://glama.ai/mcp/schemas/server.json), which defines only `maintainers`. Name and description belong in the listing UI; publishing `glama.json` alone does not submit the server or prove a successful claim. This submission does not deploy a public remote endpoint.

For the accepted listing, submitted container configuration, synchronization recovery and release checks, see [Glama listing and container releases](glama.md).
