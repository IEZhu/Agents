# Agents-Core

Agents-Core gives Claude a library of specialist personas, reusable Markdown workflows and a
per-repository memory, served by an MCP server that runs entirely on your own computer.

## What it does

The plugin connects Claude Code (and Cowork sessions that run on your computer) to the
Agents-Core MCP server of a local installation. The server's tools let Claude:

- route a task to a specialist persona (software engineer, reviewer, lawyer, analyst and many
  more) and keep that role across turns, with its skills, reasoning implants and rules;
- list, read, save and run Markdown workflows: built-in ones, your personal library and the
  current repository's;
- prepare task-scoped packages for subagents and validate what they return;
- keep a repository memory: a summary section in the project's `CLAUDE.md` and a turn journal
  in the project's `history.md`, which Claude can read back and search.

Every tool declares a title and whether it only reads or may change or delete something
(`readOnlyHint`, `destructiveHint`), so Claude can ask before the ones that write.

Claude in the browser (claude.ai chat) does not start local MCP servers, so there the plugin
adds nothing; use it in Claude Code or in Cowork on your computer.

## Setup

The MCP server is not bundled in the plugin: it needs a local installation with its own Python
environment and embedding model. Install it once (macOS or Linux, Python 3.11 or newer, Git):

If you have not already installed the plugin from a marketplace or Claude's directory, add this
repository's marketplace and install it in Claude Code:

```text
/plugin marketplace add IEZhu/Agents
/plugin install agents-core@agents-core
```

If the plugin is already installed, skip those commands. Install the local server next:

```bash
curl -fsSL https://raw.githubusercontent.com/IEZhu/Agents/main/install.sh | bash
```

This clones the repository into `~/.agents-core`, creates `.venv`, downloads the default
embedding model (about 1.1 GB, from Hugging Face) and builds the local indexes. It also writes
the routing instructions (a managed section of `~/.claude/CLAUDE.md`, and Codex's `AGENTS.md`
when Codex is present) and registers the server for other clients it finds (Codex, Cursor,
Claude Desktop, Antigravity). When the plugin is already installed and enabled, it writes no
`Agents-Core` entry for Claude Code, because the plugin starts the server itself; it says so in
its output. Do not pass `--skip-mcp` for the plugin: that flag also skips the routing
instructions. Set `AGENTS_HOME` before installing to use another directory; the plugin's start script
reads the same variable, so keep the absolute value set in the environment that starts Claude
Code or Cowork (your shell profile, or the login environment for the desktop app), or the
launcher looks in `~/.agents-core` and reports that Agents-Core is not installed.

Start a new session. `/mcp` shows the `Agents-Core` server; ask Claude to call `list_agents` to
check it. To try a local copy instead of a marketplace installation, use
`claude --plugin-dir ~/.agents-core/plugins/agents-core`. The plugin can also be discovered
through Claude's plugin directory when its listing is available.

The plugin is only a launcher: it starts `scripts/start-server.sh`, which runs code outside the
plugin folder, `~/.agents-core/.venv/bin/python ~/.agents-core/src/server.py` of the separately
installed server, the same command the installer registers (on NixOS with the nix-ld
`LD_LIBRARY_PATH` the installer also sets). `AGENTS_HOME`, when set, must be an absolute path.

If you installed Agents-Core before adding the plugin, Claude Code already has a user-level
`Agents-Core` server: remove one of the two (`claude mcp remove Agents-Core --scope user`, or
disable the plugin) so the tools are not listed twice. `~/.agents-core/.venv/bin/python -m
src.daemon registrations status` warns about such a pair when the shared macOS service is
installed. Rerunning the installer after adding the plugin keeps the routing instructions current
without adding the entry again.

On Windows the start script cannot run; set Agents-Core up with `scripts\init_repo.bat` from a
clone instead, which registers the server for Claude Code directly. The project README has the
details: <https://github.com/IEZhu/Agents#readme>.

## Uninstall

Remove the plugin from Claude Code, then remove the local installation and everything it
created (services, client registrations, managed instruction sections, state, caches and logs):

```bash
~/.agents-core/scripts/uninstall.sh --dry-run   # list what would be removed
~/.agents-core/scripts/uninstall.sh             # remove it, after one confirmation
```

`--keep-data` keeps your personal workflows and histories. On Windows run
`scripts\uninstall.ps1` from your clone.

## Privacy Policy

Agents-Core is open-source software that runs on your computer. The plugin's author operates no
server and receives no data from it.

**Data collection.** The server processes what Claude sends to its tools: the task text used to
choose a persona, workflow text you ask it to save, and the turn summaries Claude logs. It
collects no analytics or telemetry, and it does not read your conversations beyond what Claude
passes to a tool.

**Usage and storage.** Everything stays on your computer:

- repository memory in each project you use it in: a managed section of the project's
  `CLAUDE.md`, the turn journal `history.md` and `history/`, and lock files next to them;
- your personal workflows in the installation's `flows/.user`;
- search indexes, settings and logs in the installation's `data/` and in a private state
  directory (`~/Library/Application Support/Agents-Core` on macOS, `~/.local/state/agents-core`
  on Linux, `%USERPROFILE%\.agents-core` on Windows);
- the embedding model in `~/.cache/fastembed`.

**Third-party sharing.** Agents-Core sends nothing to third parties by default. It connects to
the network only for these, each of which you can see in its source:

- downloading the embedding model from Hugging Face at installation, and Git fetches from
  GitHub when automatic updates are on (`AGENTS_AUTO_UPDATE=1` by default in `.env`; set it to
  `0` to turn them off);
- Langfuse tracing, only if you put Langfuse keys into `.env`: tool calls and their inputs and
  outputs then go to the Langfuse host you configured;
- syncing your personal library between your machines, only if you set it up: flows and history
  shared under your chosen policy go to the private Git repository you choose. In
  `history share approved` mode, approving a repository's preview covers its listed past entries
  and future entries. Explicitly choosing `history share all` shares eligible past and future
  entries without individual review. Revocations recorded while share-all mode is active remain
  excluded until reapproved; revoking in approved mode does not exclude a later switch to share-all;
- fetching a user-selected HTTPS rule source when you explicitly subscribe or refresh;
  configured authorization is sent to that source when enabled.

Prompts and answers themselves are exchanged between you and Claude as usual; the plugin adds no
other recipient.

**Retention.** Data stays until you delete it. `history.md` rotates into `history/` by month and
is never uploaded unless you set up sync. The uninstall script above removes the installation,
its state, caches and logs; files it wrote into your projects (`history.md`, `history/`, the
repository-memory section in `CLAUDE.md`) are listed in its report for you to delete.

**Contact.** Questions and requests: open an issue at
<https://github.com/IEZhu/Agents/issues>.

## License

MIT; see [LICENSE](LICENSE).
