# Claude plugin

`plugins/agents-core` is the Claude plugin listed in Anthropic's plugin directory (#362). The
Connectors Directory takes only remote `https://` MCP servers and no longer takes MCPB
extensions, so a local server like Agents-Core can be listed only inside a plugin.

## What it contains

| File | Purpose |
|---|---|
| `.claude-plugin/plugin.json` | Manifest: `name` (`agents-core`, permanent), `displayName`, `version`, `description`, `author`, `homepage`, `repository`, `license` |
| `.claude-plugin/icon.png` | Listing icon, 1024×1024 PNG (an agent ring around a core). The portal takes it once, when the plugin is first saved or submitted; replacing the file later does not change the listing |
| `.mcp.json` | The `Agents-Core` stdio server: `${CLAUDE_PLUGIN_ROOT}/scripts/start-server.sh` |
| `scripts/start-server.sh` | Starts `~/.agents-core/.venv/bin/python ~/.agents-core/src/server.py` (or under an absolute `AGENTS_HOME`), the command `scripts/init_repo.sh` registers for Claude Code, with the same NixOS `LD_LIBRARY_PATH` (`/run/current-system/sw/share/nix-ld/lib`) for its own process; exits with setup instructions when the installation is missing |
| `README.md` | Listing text: what it does, setup, uninstall, Privacy Policy |
| `LICENSE` | MIT, a copy of the repository's |

The server itself is not in the plugin: the plugin is a launcher for a separately installed
local server (`install.sh`), so **the launcher runs code outside the plugin folder**. The
pre-submission checklist says that people who install a plugin get only its folder and that
everything it runs has to be inside it; expect a policy review of this plugin, and possibly a
rejection on that ground, not only the hold below. Every tool in `src/server.py` declares a
`title` and `readOnlyHint`, plus `destructiveHint` (and `idempotentHint` where it is clear) for
tools that change something, and `openWorldHint`: true for the tools whose changes user sync can
push to the private Git remote the user set up (`save_flow`, `delete_flow`, `set_flow_persona`,
`log_interaction`), false for the rest (`TOOL_HINTS`, checked by `tests/test_tool_annotations.py`,
which `.github/workflows/uninstall.yml` runs).

## Alongside the installer

The plugin and the installer would both register `Agents-Core` for Claude Code, and Claude Code
would then list every tool twice and start two servers (#367). So while the plugin is installed
and enabled, `scripts/init_repo.sh`, `scripts/init_repo.bat`, `python -m src.daemon migrate` and
the service's registration upkeep (`registrations run`, and the run at every start) write no
`Agents-Core` entry for Claude Code, neither the user-scope one in `~/.claude.json` nor a
project's, and say why. Everything else still runs: the managed routing section in Claude
Code's `CLAUDE.md`, the routing memory, Codex's `AGENTS.md`, and the registrations of Codex,
Cursor, Claude Desktop and Antigravity. `--skip-mcp` keeps its meaning and skips all of that
too, so the plugin's README no longer asks for it.

To register Claude Code's own entry anyway, pass `--force-claude-code-mcp` to either installer,
`migrate` or `registrations run`, or set `AGENTS_FORCE_CLAUDE_CODE_MCP=1`.

`python -m src.daemon registrations status` adds a `warnings` entry
(`duplicate_claude_code_server`) when the plugin is enabled and Claude Code also has an
`Agents-Core` entry at user scope, in a project's local scope in `~/.claude.json`, or in a
registered workspace's `.mcp.json`. It names the fix: `claude mcp remove Agents-Core --scope
user` (`--scope local` or `--scope project` for the others), or disabling the plugin.

The detection (`src/claude_plugin.py`) only reads Claude Code's own files, as the Claude Code
documentation describes them (checked 2026-10-13):

- the plugins root is `~/.claude/plugins`, or `CLAUDE_CODE_PLUGIN_CACHE_DIR` when set
  ([plugin loading reference](https://code.claude.com/docs/en/plugins/loading#find-plugins-on-disk),
  [environment variables](https://code.claude.com/docs/en/env-vars)); with `CLAUDE_CONFIG_DIR`
  set, Claude Code keeps settings and plugins there
  ([settings](https://code.claude.com/docs/en/settings));
- `installed_plugins.json` in that root records each install, under the plugin's id, with its
  `scope`, `installPath` and `version`; the plugin counts when it has a `user`-scope record
  under an id `agents-core@<marketplace>`. The docs name these keys but do not print the whole
  file; the `{"version": 2, "plugins": {"<id>": [records]}}` layout read here is the one Claude
  Code writes today (not confirmed by the docs);
- `enabledPlugins` in the profile's `settings.json` turns an id on or off; an id the user has not
  set starts enabled, because the manifest leaves `defaultEnabled` at its default, `true`
  ([manifest reference](https://code.claude.com/docs/en/plugins/manifest-reference#defaultenabled)).

Anything it cannot read, or a setup it does not cover, counts as "no plugin", and the entry is
written as before: project- or local-scope installs of the plugin, plugins synced from claude.ai
(`agents-core@synced`, which have no install record), session-only `--plugin-dir` copies, and
`enabledPlugins` set in project, local or managed settings. On Windows the plugin's start script
does not run, so the batch installer's check rarely finds an enabled plugin there.

## Validate

```bash
claude plugin validate ./plugins/agents-core     # from the repository root; prints "Validation passed"
LANGFUSE_TRACING_ENABLED=false .venv/bin/python -m pytest -q tests/test_tool_annotations.py
```

`claude plugin validate` checks only that the files are well-formed. The developer portal's
**Validate** runs the directory's checks ([pre-submission
checklist](https://claude.com/docs/plugins/pre-submission-checklist)). Expected results for this
plugin:

- **Blocking checks**: none expected. The folder has `.claude-plugin/plugin.json`, a README of
  more than 40 words, a `LICENSE`, a lowercase ASCII name, regular files only (no symlinks,
  `.DS_Store` or binaries other than the icon), and every path in its manifest and `.mcp.json` stays inside the
  plugin folder, written from `${CLAUDE_PLUGIN_ROOT}`.
- **Policy hold, expected**: *Scripts the validator couldn't follow*. The plugin folder is a
  subfolder of the repository, and `start-server.sh` reads `AGENTS_HOME` and `HOME` and runs a
  file outside the plugin (the installed server). That is inherent to a local server that is
  not bundled; a reviewer reads the script. It downloads nothing and has no package launcher.
  Say so in the submission notes: the plugin is a launcher for a separately installed local
  server, and the README's Setup section installs it. A reviewer may still decline it under the
  "everything inside the plugin folder" rule; then the server stays available through
  `install.sh` and `scripts/init_repo.sh`, which register it with Claude Code directly.
- **Not met, by design**: claude.ai chat does not start local MCP servers, so the plugin adds
  nothing there; on Windows the shell start script does not run (the README sends Windows users
  to `scripts\init_repo.bat`).

## Release

The directory follows a tracked branch or tag of `IEZhu/Agents` and scans each new commit it
picks up, so a release is a push:

1. Change the plugin, and raise `version` in `.claude-plugin/plugin.json` for every release.
2. Run the checks above.
3. Merge to the tracked branch (or push the tracked tag).

## What the owner submits

At [claude.ai/directory/manage](https://claude.ai/directory/manage) (a paid Claude plan;
the agent prepares everything up to the portal and never submits):

1. **Submit new**, then **Plugin bundle**.
2. Repository `IEZhu/Agents`, plugin path `plugins/agents-core`, the branch or tag to follow.
3. **Validate**; fix anything **Blocking**, push, **Re-validate**.
4. The data-handling answers (they match the README's Privacy Policy: no data collected by the
   author, everything local, optional third parties only when the user configures them) and the
   four acknowledgements.
5. GitHub connected on claude.ai with push access to the repository.
